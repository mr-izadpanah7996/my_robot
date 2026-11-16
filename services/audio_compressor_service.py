import asyncio
import logging
import shutil
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioCompressionError(Exception):
    """استثنای اختصاصی برای خطاهای حین فشرده‌سازی فایل صوتی"""
    pass


class AudioCompressorService:
    """
    سرویس فشرده‌سازی و تغییر بیت‌ریت فایل‌های صوتی تا سقف ۱۰۰ مگابایت با ابزار ffmpeg.
    - پشتیبانی از بیت‌ریت‌های 16k، 32k، 48k و 64k با تنظیم هوشمند کانال و نرخ نمونه‌برداری
    - سازگار با Local Bot API و حالت استاندارد
    - مجهز به کنترل همزمانی (Semaphore)، بررسی فضای دیسک، تایم‌اوت و پاک‌سازی ضدزامبی
    """

    MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # سقف ۱۰۰ مگابایت

    ALLOWED_BITRATES = {
        "16k": "16k",
        "32k": "32k",
        "48k": "48k",
        "64k": "64k",
    }

    def __init__(
        self,
        max_concurrent_tasks: int = 2,
        temp_dir: Path | str = "/tmp/delta_audio_conversions",
        process_timeout: int = 360,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self._temp_dir = Path(temp_dir)
        self._process_timeout = process_timeout
        self._ensure_temp_dir()
        self.cleanup_stale_files(max_age_seconds=7200)

    def _ensure_temp_dir(self) -> None:
        """اطمینان از وجود پوشه موقت و دسترسی مجاز روی سرور"""
        try:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error("خطا در ایجاد پوشه موقت صوت %s: %s", self._temp_dir, e)
            raise AudioCompressionError(f"دسترسی به پوشه موقت صوت مقدور نیست: {e}") from e

    def get_temp_dir(self) -> Path:
        """برگرداندن مسیر دایرکتوری موقت برای ذخیره فایل‌های ورودی"""
        self._ensure_temp_dir()
        return self._temp_dir

    def cleanup_stale_files(self, max_age_seconds: int = 7200) -> None:
        """پاک‌سازی فایل‌های موقت قدیمی باقی‌مانده از نشست‌ها یا ری‌استارت‌های قبلی"""
        if not self._temp_dir.exists():
            return
        now = time.time()
        try:
            for item in self._temp_dir.iterdir():
                if item.is_file():
                    age = now - item.stat().st_mtime
                    if age > max_age_seconds:
                        item.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("خطا در پاک‌سازی فایل‌های موقت قدیمی صوت: %s", e)

    def _check_disk_space(self, required_bytes: int) -> None:
        """بررسی وجود فضای کافی در دیسک پیش از پردازش فایل‌های حجیم"""
        try:
            usage = shutil.disk_usage(self._temp_dir)
            # نیاز به حداقل ۳ برابر حجم فایل ورودی (ورودی + خروجی + حاشیه امن سیستم)
            if usage.free < required_bytes * 3:
                logger.critical(
                    "کمبود فضای دیسک در %s: فضای آزاد %s بایت، مورد نیاز %s بایت",
                    self._temp_dir,
                    usage.free,
                    required_bytes * 3,
                )
                raise AudioCompressionError("فضای موقت سرور موقتاً پر است. لطفاً دقایقی دیگر تلاش کنید.")
        except AudioCompressionError:
            raise
        except OSError as e:
            logger.warning("امکان بررسی فضای دیسک وجود نداشت: %s", e)

    @staticmethod
    def _stat_file_size(path: Path) -> int:
        if not path.exists() or not path.is_file():
            return -1
        return path.stat().st_size

    async def prepare_local_file(self, local_source_path: Path | str, original_ext: str = ".mp3") -> Path:
        """
        کپی ایمن و غیرمسدودکننده فایل از مسیر Local Bot API به پوشه موقت با بررسی سقف ۱۰۰ مگابایت
        """
        src = Path(local_source_path)
        file_size = await asyncio.to_thread(self._stat_file_size, src)

        if file_size < 0:
            raise AudioCompressionError("فایل صوتی روی سرور یافت نشد.")
        if file_size == 0:
            raise AudioCompressionError("فایل صوتی ارسالی خالی است.")
        if file_size > self.MAX_FILE_SIZE_BYTES:
            raise AudioCompressionError("حجم فایل صوتی بیشتر از سقف مجاز (۱۰۰ مگابایت) است.")

        self._ensure_temp_dir()
        await asyncio.to_thread(self._check_disk_space, file_size)

        clean_ext = original_ext.strip().lower()
        if not clean_ext or not clean_ext.startswith(".") or len(clean_ext) > 10:
            clean_ext = ".mp3"

        dest_path = self._temp_dir / f"input_{uuid.uuid4().hex[:8]}{clean_ext}"

        try:
            await asyncio.to_thread(shutil.copy2, src, dest_path)
            return dest_path
        except OSError as e:
            await self.async_safe_remove(dest_path)
            logger.error("خطا در انتقال فایل محلی %s به پوشه موقت: %s", src, e)
            raise AudioCompressionError("خطا در آماده‌سازی فایل صوتی روی سرور رخ داد.") from e

    async def compress_audio(
        self,
        input_path: Path | str,
        bitrate: str,
        bot_title: str = "دلتا ربات",
    ) -> Path:
        """
        فشرده‌سازی فایل صوتی ورودی به فرمت MP3 با بیت‌ریت انتخابی و اعمال متادیتای اختصاصی عنوان.

        :param input_path: مسیر فایل صوتی روی دیسک
        :param bitrate: کیفیت مورد نظر ('16k', '32k', '48k', '64k')
        :param bot_title: عنوان ترک داخل متادیتا (ID3 Title)
        :return: مسیر شیء Path فایل فشرده‌شده MP3
        :raises AudioCompressionError: در صورت نامعتبر بودن ورودی یا شکست فرآیند ffmpeg
        """
        in_file = Path(input_path)
        file_size = await asyncio.to_thread(self._stat_file_size, in_file)

        if file_size < 0:
            raise AudioCompressionError("فایل ورودی یافت نشد یا منقضی شده است.")
        if file_size == 0:
            raise AudioCompressionError("فایل ورودی فاقد محتوا (حجم صفر) است.")
        if file_size > self.MAX_FILE_SIZE_BYTES:
            raise AudioCompressionError("حجم فایل ورودی بیش از سقف مجاز (۱۰۰ مگابایت) است.")

        clean_bitrate = self.ALLOWED_BITRATES.get(bitrate.lower().strip())
        if not clean_bitrate:
            raise AudioCompressionError(
                f"بیت‌ریت نامعتبر است: {bitrate}. مقادیر مجاز: {list(self.ALLOWED_BITRATES.keys())}"
            )

        self._ensure_temp_dir()
        await asyncio.to_thread(self._check_disk_space, file_size)

        unique_id = uuid.uuid4().hex[:8]
        output_path = self._temp_dir / f"compressed_{unique_id}.mp3"

        # تنظیم بهینه کانال و نرخ نمونه‌برداری متناسب با بیت‌ریت برای حداکثر شفافیت صدا
        if clean_bitrate == "16k":
            audio_channels = "1"
            sample_rate = "22050"
        else:
            audio_channels = "2"
            sample_rate = "44100"

        resolved_input = str(in_file.resolve())
        resolved_output = str(output_path.resolve())

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-i", resolved_input,
            "-vn",                        # حذف استریم ویدیو یا کاور آلبوم
            "-map", "0:a:0",              # انتخاب قطعی اولین استریم صوتی فایل
            "-map_metadata", "-1",        # پاک‌سازی متادیتاهای سنگین فایل اصلی
            "-c:a", "libmp3lame",
            "-b:a", clean_bitrate,
            "-ac", audio_channels,
            "-ar", sample_rate,
            "-metadata", f"title={bot_title}",
            resolved_output,
        ]

        logger.info(
            "درخواست فشرده‌سازی برای فایل %s با بیت‌ریت %s در صف سمافور قرار گرفت.",
            in_file.name,
            clean_bitrate,
        )

        process = None
        try:
            async with self._semaphore:
                logger.info("پردازش تبدیل فایل %s با ابزار ffmpeg آغاز شد.", in_file.name)
                try:
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )

                    _, stderr = await asyncio.wait_for(
                        process.communicate(),
                        timeout=self._process_timeout,
                    )

                    if process.returncode != 0:
                        err_msg = stderr.decode(errors="ignore").strip()[-400:]
                        logger.error("خطای اجرای ffmpeg (کد خروج %s): %s", process.returncode, err_msg)
                        if "matches no streams" in err_msg.lower() or "stream map" in err_msg.lower():
                            raise AudioCompressionError("فایل ارسالی فاقد هرگونه لایه صوتی قابل استخراج است.")
                        raise AudioCompressionError("ساختار فایل صوتی معتبر نیست یا توسط انکودر پشتیبانی نمی‌شود.")

                except asyncio.TimeoutError:
                    if process:
                        try:
                            process.kill()
                            await process.wait()
                        except ProcessLookupError:
                            pass
                    logger.error(
                        "پردازش فایل %s به دلیل اتمام زمان مجاز (%s ثانیه) متوقف شد.",
                        in_file.name,
                        self._process_timeout,
                    )
                    raise AudioCompressionError("زمان پردازش فایل بیش از حد مجاز طول کشید.")

                except asyncio.CancelledError:
                    if process:
                        try:
                            process.kill()
                            await process.wait()
                        except ProcessLookupError:
                            pass
                    logger.warning("تسک فشرده‌سازی فایل %s لغو شد.", in_file.name)
                    raise

                except FileNotFoundError as fnf_err:
                    logger.critical("ابزار ffmpeg روی سیستم شناسایی نشد.")
                    raise AudioCompressionError("سرویس فشرده‌سازی سرور در دسترس نیست (عدم نصب ffmpeg).") from fnf_err

            final_size = await asyncio.to_thread(self._stat_file_size, output_path)
            if final_size <= 0:
                raise AudioCompressionError("فایل خروجی ساخته نشد یا خالی است.")

            logger.info(
                "فایل با موفقیت فشرده شد: %s (حجم نهایی: %s بایت)",
                output_path.name,
                final_size,
            )
            return output_path

        except AudioCompressionError:
            await self.async_safe_remove(output_path)
            raise
        except asyncio.CancelledError:
            await self.async_safe_remove(output_path)
            raise
        except Exception as e:
            await self.async_safe_remove(output_path)
            logger.exception("خطای غیرمنتظره حین اجرای پردازش ffmpeg: %s", e)
            raise AudioCompressionError("خطای ناشناخته در تبدیل فایل رخ داد.") from e

    @staticmethod
    def safe_remove(file_path: Path | str | None) -> None:
        """حذف ایمن و همگام فایل‌های موقت بدون توقف یا پرتاب استثنای دیسک"""
        if file_path is None:
            return
        try:
            target = Path(file_path)
            if target.exists():
                target.unlink()
                logger.debug("فایل موقت پاک‌سازی شد: %s", target)
        except OSError as e:
            logger.warning("خطا در حذف فایل موقت %s: %s", file_path, e)

    @classmethod
    async def async_safe_remove(cls, file_path: Path | str | None) -> None:
        """نسخه ناهمگام حذف فایل‌های موقت برای جلوگیری از مسدود شدن Event Loop"""
        if file_path is None:
            return
        await asyncio.to_thread(cls.safe_remove, file_path)

        