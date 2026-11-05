import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioConversionError(Exception):

    """استثنای اختصاصی برای خطاهای حین تبدیل ویدیو به صوت"""

    pass


class AudioConverterService:

    """
    سرویس مدیریت پردازش و استخراج صوت از فایل‌های ویدیویی با استفاده از FFmpeg

    همراه با مدیریت صف و هم‌زمانی از طریق Semaphore.
    """

    def __init__(
        self,
        max_concurrent: int = 2,
        bitrate: str = "192k",
        timeout: float = 600.0,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._bitrate = bitrate
        self._timeout = timeout

    async def convert_to_mp3(
        self,
        input_path: Path | str,
        output_path: Path | str,
    ) -> Path:
        """
        تبدیل غیرهمگام ویدیو به فرمت MP3 با کیفیت تعیین‌شده.
        
        :param input_path: مسیر فایل ویدیوی ورودی
        :param output_path: مسیر فایل MP3 خروجی
        :return: شیء Path فایل صوتی خروجی
        :raises FileNotFoundError: در صورت نبود فایل ورودی
        :raises AudioConversionError: در صورت خطای FFmpeg یا اتمام مهلت پردازش
        """
        input_file = Path(input_path)
        output_file = Path(output_path)

        if not input_file.exists():
            raise FileNotFoundError(f"فایل ورودی یافت نشد: {input_file}")

        # ساخت پوشه‌های والد در صورت نیاز

        output_file.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "ffmpeg",
            "-y",                    # بازنویسی خودکار خروجی در صورت وجود
            "-i", str(input_file),   # فایل ورودی
            "-vn",                   # حذف تصویر ویدیو
            "-c:a", "libmp3lame",    # انکودر صوتی MP3
            "-b:a", self._bitrate,   # بیت‌ریت خروجی
            str(output_file),        # مسیر ذخیره فایل نهایی
        ]

        async with self._semaphore:
            logger.info("شروع پردازش تبدیل: %s -> %s", input_file.name, output_file.name)
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                # مدیریت مهلت زمانی پردازش برای جلوگیری از قفل شدن سرور روی فایل‌های خراب
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self._timeout,
                )

                if process.returncode != 0:
                    err_output = stderr.decode(errors="replace").strip()
                    logger.error("خطای FFmpeg روی فایل %s:\n%s", input_file.name, err_output)
                    
                    # بررسی ویدیوی بدون ترک صوتی
                    
                    if "does not contain any stream" in err_output or "Output file is empty" in err_output:
                        raise AudioConversionError("ویدیو فاقد هرگونه استریم یا ترک صوتی جهت استخراج است.")
                    
                    raise AudioConversionError(f"پردازش FFmpeg با کد خطای {process.returncode} متوقف شد.")

                logger.info("فایل با موفقیت تبدیل شد: %s", output_file.name)
                return output_file

            except asyncio.TimeoutError:
                if process:
                    process.kill()
                    await process.wait()
                logger.error("مهلت زمان پردازش فایل %s به پایان رسید (Timeout).", input_file.name)
                raise AudioConversionError("مدت زمان پردازش فایل طولانی شد و متوقف گردید.")

            except asyncio.CancelledError:
                if process:
                    process.kill()
                    await process.wait()
                logger.warning("تسک پردازش فایل %s کنسل شد.", input_file.name)
                raise

    @staticmethod
    def safe_remove(file_path: Path | str | None) -> None:
        """
        حذف ایمن فایل‌های موقت بدون ایجاد خطا در سطح سیستم‌عامل
        """
        if file_path is None:
            return
        try:
            target = Path(file_path)
            if target.exists():
                target.unlink()
                logger.debug("فایل موقت حذف شد: %s", target)
        except OSError as e:
            logger.warning("خطا در حذف فایل موقت %s: %s", file_path, e)
