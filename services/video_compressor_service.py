import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class VideoCompressionError(Exception):
    """استثنای اختصاصی برای خطاهای حین فشرده‌سازی فایل ویدیویی"""
    pass


@dataclass(slots=True)
class CompressedVideoResult:
    """مدل خروجی شامل مسیر فایل فشرده‌شده و مشخصات فنی برای نمایش استاندارد در تلگرام"""
    output_path: Path
    width: int
    height: int
    duration: int


class VideoCompressorService:
    """
    سرویس فشرده‌سازی ویدیو با استفاده از ابزارهای سیستمی ffmpeg و ffprobe.
    مجهز به سمافور تک‌ظرفیتی (Semaphore=1)، کنترل تردهای CPU و مدیریت جامع خطاها.
    """

    # تنظیمات سطوح کیفیت: (مقدار CRF، حداکثر ضلع کوچک/ارتفاع مجاز)
    QUALITY_PRESETS = {
        "low": {"crf": "24", "max_short_side": 1080},     # فشرده‌سازی کم (حفظ حداکثری کیفیت)
        "medium": {"crf": "28", "max_short_side": 720},   # فشرده‌سازی متعادل (پیشنهادی)
        "high": {"crf": "32", "max_short_side": 480},     # فشرده‌سازی بالا (حداکثر کاهش حجم)
    }

    def __init__(
        self,
        max_concurrent_tasks: int = 1,
        temp_dir: Path | str = "/tmp/delta_video_conversions",
        process_timeout: int = 900,  # ۱۵ دقیقه مهلت برای فایل‌های سنگین تا ۶۰۰ مگابایت
        ffmpeg_threads: int = 2,     # جلوگیری از قفل شدن کل هسته‌های CPU سرور حین رندر
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self._temp_dir = Path(temp_dir)
        self._process_timeout = process_timeout
        self._ffmpeg_threads = str(max(1, ffmpeg_threads))
        self._ensure_temp_dir()

    def _ensure_temp_dir(self) -> None:
        """اطمینان از وجود پوشه موقت و دسترسی مجاز روی سرور"""
        try:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error("خطا در ایجاد پوشه موقت ویدیو %s: %s", self._temp_dir, e)
            raise VideoCompressionError(f"دسترسی به پوشه موقت ویدیو مقدور نیست: {e}") from e

    def get_temp_dir(self) -> Path:
        """برگرداندن مسیر دایرکتوری موقت برای ذخیره دانلودها"""
        self._ensure_temp_dir()
        return self._temp_dir

    @staticmethod
    def _extract_rotation(video_stream: dict) -> int:
        """استخراج زاویه چرخش تصویر از تگ‌های قدیمی و Display Matrix مدرن"""
        # ۱. بررسی تگ قدیمی rotate
        tags = video_stream.get("tags", {})
        rotate_tag = str(tags.get("rotate", "0")).strip()
        if rotate_tag.lstrip("-").isdigit():
            val = int(rotate_tag)
            if val != 0:
                return val

        # ۲. بررسی side_data_list در نسخه‌های جدید ffprobe و گوشی‌های مدرن
        for side_data in video_stream.get("side_data_list", []):
            if "rotation" in side_data:
                rot_val = str(side_data["rotation"]).strip()
                if rot_val.lstrip("-").isdigit():
                    return int(rot_val)

        return 0

    async def _probe_video_metadata(self, video_path: Path) -> tuple[int, int, int]:
        """
        استخراج عرض، ارتفاع و مدت زمان ویدیو با ffprobe (با پشتیبانی کامل از ویدیوهای عمودی موبایل)
        """
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            str(video_path),
        ]

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)

            if process.returncode != 0:
                return 0, 0, 0

            data = json.loads(stdout.decode(errors="ignore"))
            streams = data.get("streams", [])
            video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)

            if not video_stream:
                return 0, 0, 0

            width = int(video_stream.get("width", 0))
            height = int(video_stream.get("height", 0))

            # اصلاح جابه‌جایی عرض و ارتفاع در صورت وجود متادیتای چرخش
            rotation = self._extract_rotation(video_stream)
            if abs(rotation) in (90, 270):
                width, height = height, width

            duration_raw = data.get("format", {}).get("duration") or video_stream.get("duration") or 0
            try:
                duration = int(round(float(duration_raw)))
            except (ValueError, TypeError):
                duration = 0

            return max(0, width), max(0, height), max(0, duration)

        except asyncio.TimeoutError:
            if process:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            logger.warning("تایم‌اوت در استخراج متادیتای ویدیو %s", video_path.name)
            return 0, 0, 0
        except FileNotFoundError:
            logger.warning("ابزار ffprobe روی سرور یافت نشد؛ ارسال بدون ابعاد دقیق انجام می‌شود.")
            return 0, 0, 0
        except Exception as e:
            logger.warning("خطا در خواندن متادیتای ویدیو %s: %s", video_path.name, e)
            return 0, 0, 0

    async def compress_video(
        self,
        input_path: Path | str,
        quality_level: str,
        bot_title: str = "دلتا ربات",
    ) -> CompressedVideoResult:
        """
        فشرده‌سازی فایل ویدیویی به فرمت استاندارد MP4 همراه با متادیتای عنوان اختصاصی.

        :param input_path: مسیر فایل ویدیویی دانلودشده روی سرور
        :param quality_level: سطح فشرده‌سازی ('low', 'medium', 'high')
        :param bot_title: عنوان ثبت‌شده در متادیتای ویدیو
        :return: شیء CompressedVideoResult شامل مسیر خروجی و ابعاد تصویر
        :raises VideoCompressionError: در صورت خرابی فایل، اتمام زمان یا خطای انکودینگ
        """
        in_file = Path(input_path)
        if not in_file.exists() or in_file.stat().st_size == 0:
            raise VideoCompressionError("فایل ویدیویی ورودی یافت نشد یا حجم آن صفر است.")

        preset_config = self.QUALITY_PRESETS.get(quality_level.lower().strip())
        if not preset_config:
            raise VideoCompressionError(
                f"سطح فشرده‌سازی نامعتبر است: {quality_level}. مقادیر مجاز: {list(self.QUALITY_PRESETS.keys())}"
            )

        crf_value = preset_config["crf"]
        max_side = preset_config["max_short_side"]

        self._ensure_temp_dir()
        unique_id = uuid.uuid4().hex[:8]
        output_path = self._temp_dir / f"compressed_{unique_id}.mp4"

        # فرمول هوشمند مقیاس‌دهی برای ویدیوهای افقی و عمودی:
        # ضلع کوچک‌تر تصویر را حداکثر تا max_side پایین می‌آورد، نسبت تصویر را حفظ می‌کند،
        # هرگز ویدیوی کوچک را بزرگ‌تر (Upscale) نمی‌کند و ابعاد را زوج (-2) نگه می‌دارد.
        scale_filter = (
            f"scale='if(gt(min(iw,ih),{max_side}),if(gt(iw,ih),-2,{max_side}),trunc(iw/2)*2)':"
            f"'if(gt(min(iw,ih),{max_side}),if(gt(iw,ih),{max_side},-2),trunc(ih/2)*2)'"
        )

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",             # ثبت دقیق خطاها بدون اشغال بی‌مورد بافر حافظه
            "-y",                             # رونویسی خودکار فایل هم‌نام
            "-i", str(in_file),               # فایل ورودی
            "-map", "0:v:0",                  # انتخاب اولین ترک ویدیو
            "-map", "0:a?",                   # انتخاب ترک صدا در صورت وجود (عدم خطا روی ویدیوی بی‌صدا)
            "-vf", scale_filter,              # اعمال فیلتر هوشمند رزولوشن و زوج‌سازی ابعاد
            "-c:v", "libx264",                # انکودر استاندارد H.264 سازگار با تمام گوشی‌ها و تلگرام
            "-preset", "veryfast",            # تعادل عالی بین سرعت پردازش و فشرده‌سازی
            "-crf", crf_value,                # میزان فشرده‌سازی هدف
            "-threads", self._ffmpeg_threads, # کنترل مصرف هسته‌های CPU برای پاسخ‌گویی روان ربات
            "-pix_fmt", "yuv420p",            # فرمت رنگی استاندارد برای پخش صحیح در آیفون و اندروید
            "-c:a", "aac",                    # انکودر استاندارد صدا
            "-b:a", "96k",                    # بیت‌ریت صوتی سبک و شفاف
            "-ar", "44100",                   # استانداردسازی نرخ نمونه‌برداری صدا
            "-movflags", "+faststart",        # بهینه‌سازی هدر MP4 برای استریم و پخش فوری در تلگرام
            "-metadata", f"title={bot_title}",
            str(output_path),
        ]

        logger.info(
            "درخواست فشرده‌سازی ویدیو %s با سطح '%s' (CRF=%s) وارد صف سمافور شد.",
            in_file.name,
            quality_level,
            crf_value,
        )

        process = None
        try:
            async with self._semaphore:
                logger.info("پردازش رندر ویدیو %s با ffmpeg آغاز شد.", in_file.name)
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
                    err_msg = stderr.decode(errors="ignore").strip()
                    short_err = err_msg[-500:] if len(err_msg) > 500 else err_msg
                    logger.error("خطای اجرای ffmpeg در ویدیو (کد %s): %s", process.returncode, short_err)
                    raise VideoCompressionError(
                        "فایل ویدیویی آسیب دیده است، فاقد تصویر معتبر است یا فرمت آن پشتیبانی نمی‌شود."
                    )

        except asyncio.TimeoutError:
            if process:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            self.safe_remove(output_path)
            logger.error(
                "پردازش ویدیو %s به دلیل اتمام زمان مجاز (%s ثانیه) متوقف شد.",
                in_file.name,
                self._process_timeout,
            )
            raise VideoCompressionError("زمان پردازش این ویدیو بیش از حد مجاز طول کشید و متوقف شد.")

        except asyncio.CancelledError:
            if process:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            self.safe_remove(output_path)
            logger.warning("تسک فشرده‌سازی ویدیو %s لغو شد.", in_file.name)
            raise

        except FileNotFoundError:
            self.safe_remove(output_path)
            logger.critical("ابزار ffmpeg روی سرور شناسایی نشد.")
            raise VideoCompressionError("سرویس پردازش ویدیو در سرور در دسترس نیست.")

        except VideoCompressionError:
            self.safe_remove(output_path)
            raise

        except Exception as e:
            self.safe_remove(output_path)
            logger.exception("خطای غیرمنتظره حین فشرده‌سازی ویدیو: %s", e)
            raise VideoCompressionError("خطای ناشناخته در پردازش ویدیو رخ داد.") from e

        if not output_path.exists() or output_path.stat().st_size == 0:
            self.safe_remove(output_path)
            raise VideoCompressionError("فایل خروجی ویدیو ایجاد نشد یا حجم آن صفر است.")

        width, height, duration = await self._probe_video_metadata(output_path)

        logger.info(
            "ویدیو با موفقیت فشرده شد: %s (حجم: %s بایت | ابعاد: %sx%s | مدت: %s ثانیه)",
            output_path.name,
            output_path.stat().st_size,
            width,
            height,
            duration,
        )

        return CompressedVideoResult(
            output_path=output_path,
            width=width,
            height=height,
            duration=duration,
        )

    @staticmethod
    def safe_remove(file_path: Path | str | None) -> None:
        """حذف ایمن فایل‌های موقت از روی دیسک بدون پرتاب استثنا"""
        if file_path is None:
            return
        try:
            target = Path(file_path)
            if target.exists():
                target.unlink()
                logger.debug("فایل موقت ویدیو پاک‌سازی شد: %s", target)
        except OSError as e:
            logger.warning("خطا در حذف فایل موقت ویدیو %s: %s", file_path, e)

            