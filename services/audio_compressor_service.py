import asyncio
import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioCompressionError(Exception):
    """استثنای اختصاصی برای خطاهای حین فشرده‌سازی فایل صوتی"""
    pass


class AudioCompressorService:
    """
    سرویس فشرده‌سازی و تغییر بیت‌ریت فایل‌های صوتی با استفاده از ابزار سیستمی ffmpeg.
    مجهز به صف کنترل همزمانی (Semaphore)، تایم‌اوت، مدیریت خطاهای فرآیند و پاک‌سازی خودکار.
    """

    ALLOWED_BITRATES = {
        "32k": "32k",
        "48k": "48k",
        "64k": "64k",
    }

    def __init__(
        self,
        max_concurrent_tasks: int = 2,
        temp_dir: Path | str = "/tmp/delta_audio_conversions",
        process_timeout: int = 300,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self._temp_dir = Path(temp_dir)
        self._process_timeout = process_timeout
        self._ensure_temp_dir()

    def _ensure_temp_dir(self) -> None:
        """اطمینان از وجود پوشه موقت و دسترسی مجاز روی سرور"""
        try:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error("خطا در ایجاد پوشه موقت صوت %s: %s", self._temp_dir, e)
            raise AudioCompressionError(f"دسترسی به پوشه موقت صوت مقدور نیست: {e}") from e

    def get_temp_dir(self) -> Path:
        """برگرداندن مسیر دایرکتوری موقت برای ذخیره دانلودها"""
        self._ensure_temp_dir()
        return self._temp_dir

    async def compress_audio(
        self,
        input_path: Path | str,
        bitrate: str,
        bot_title: str = "دلتا ربات",
    ) -> Path:
        """
        فشرده‌سازی فایل صوتی ورودی به فرمت MP3 با بیت‌ریت انتخابی و اعمال متادیتای اختصاصی عنوان.

        :param input_path: مسیر فایل صوتی دانلود شده روی دیسک
        :param bitrate: کیفیت مورد نظر ('32k', '48k', '64k')
        :param bot_title: عنوان ترک داخل متادیتا (ID3 Title)
        :return: مسیر شیء Path فایل فشرده‌شده MP3
        :raises AudioCompressionError: در صورت نامعتبر بودن ورودی یا شکست فرآیند ffmpeg
        """
        in_file = Path(input_path)
        if not in_file.exists() or in_file.stat().st_size == 0:
            raise AudioCompressionError("فایل ورودی یافت نشد یا فاقد محتوا (حجم صفر) است.")

        clean_bitrate = self.ALLOWED_BITRATES.get(bitrate.lower().strip())
        if not clean_bitrate:
            raise AudioCompressionError(
                f"بیت‌ریت نامعتبر است: {bitrate}. مقادیر مجاز: {list(self.ALLOWED_BITRATES.keys())}"
            )

        self._ensure_temp_dir()
        unique_id = uuid.uuid4().hex[:8]
        output_path = self._temp_dir / f"compressed_{unique_id}.mp3"

        # دستور بهینه و همه‌منظوره ffmpeg برای تبدیل انواع ورودی‌های صوتی (Voice، آهنگ، ویدیو)
        cmd = [
            "ffmpeg",
            "-y",                     # رونویسی خودکار در صورت وجود فایل هم‌نام
            "-i", str(in_file),       # فایل ورودی
            "-vn",                    # حذف استریم تصویر/ویدیو/کاور آلبوم جهت کاهش حجم
            "-c:a", "libmp3lame",     # انکودر استاندارد MP3
            "-b:a", clean_bitrate,    # اعمال بیت‌ریت هدف
            "-ac", "2",               # تنظیم دو کاناله استاندارد برای سازگاری حداکثری
            "-metadata", f"title={bot_title}",
            str(output_path),
        ]

        logger.info(
            "درخواست فشرده‌سازی برای فایل %s با بیت‌ریت %s در صف سمافور قرار گرفت.",
            in_file.name,
            clean_bitrate,
        )

        process = None
        async with self._semaphore:
            logger.info("پردازش تبدیل فایل %s با ابزار ffmpeg آغاز شد.", in_file.name)
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                # مدیریت تایم‌اوت برای جلوگیری از قفل شدن ابدی فرآیند
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self._process_timeout,
                )

                if process.returncode != 0:
                    err_msg = stderr.decode(errors="ignore").strip()
                    logger.error("خطای اجرای ffmpeg (کد خروج %s): %s", process.returncode, err_msg)
                    raise AudioCompressionError("ساختار فایل صوتی معتبر نیست یا توسط انکودر پشتیبانی نمی‌شود.")

            except asyncio.TimeoutError:
                if process:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                self.safe_remove(output_path)
                logger.error("پردازش فایل %s به دلیل اتمام زمان مجاز (%s ثانیه) متوقف شد.", in_file.name, self._process_timeout)
                raise AudioCompressionError("زمان پردازش فایل بیش از حد مجاز طول کشید.")

            except asyncio.CancelledError:
                # جلوگیری از به جا ماندن فرآیند معلق (Zombie Process) در سرور هنگام لغو تسک
                if process:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                self.safe_remove(output_path)
                logger.warning("تسک فشرده‌سازی فایل %s توسط سیستم یا کاربر لغو شد.", in_file.name)
                raise

            except FileNotFoundError:
                self.safe_remove(output_path)
                logger.critical("ابزار ffmpeg روی سیستم یا مسیر اجرایی شناسایی نشد.")
                raise AudioCompressionError("سرویس فشرده‌سازی سرور در دسترس نیست (عدم نصب ابزار پردازش).")

            except AudioCompressionError:
                self.safe_remove(output_path)
                raise

            except Exception as e:
                self.safe_remove(output_path)
                logger.exception("خطای غیرمنتظره حین اجرای پردازش ffmpeg: %s", e)
                raise AudioCompressionError("خطای ناشناخته در تبدیل فایل رخ داد.") from e

        # بررسی نهایی سلامت فایل خروجی
        if not output_path.exists() or output_path.stat().st_size == 0:
            self.safe_remove(output_path)
            raise AudioCompressionError("فایل خروجی ساخته نشد یا خالی است.")

        logger.info(
            "فایل با موفقیت فشرده شد: %s (حجم نهایی: %s بایت)",
            output_path.name,
            output_path.stat().st_size,
        )
        return output_path

    @staticmethod
    def safe_remove(file_path: Path | str | None) -> None:
        """
        حذف ایمن فایل‌های موقت بدون توقف یا پرتاب استثنای دیسک
        """
        if file_path is None:
            return
        try:
            target = Path(file_path)
            if target.exists():
                target.unlink()
                logger.debug("فایل موقت پاک‌سازی شد: %s", target)
        except OSError as e:
            logger.warning("خطا در حذف فایل موقت %s: %s", file_path, e)

            