import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class VideoCompressionError(Exception):
    pass


@dataclass(slots=True)
class CompressedVideoResult:
    output_path: Path
    width: int
    height: int
    duration: int


class VideoCompressorService:
    # تنظیمات هر سطح کیفیت: مقدار CRF، سقف رزولوشن ضلع کوچک و بیت‌ریت صدا
    QUALITY_PRESETS = {
        "low": {"crf": "25", "max_short_side": 720, "audio_bitrate": "96k"},
        "medium": {"crf": "28", "max_short_side": 480, "audio_bitrate": "96k"},
        "high": {"crf": "32", "max_short_side": 240, "audio_bitrate": "64k"},
    }

    def __init__(
        self,
        max_concurrent_tasks: int = 1,
        temp_dir: Path | str = "/tmp/delta_video_conversions",
        process_timeout: int = 900,
        ffmpeg_threads: int = 2,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self._temp_dir = Path(temp_dir)
        self._process_timeout = process_timeout
        self._ffmpeg_threads = str(max(1, ffmpeg_threads))
        self._ensure_temp_dir()

    def _ensure_temp_dir(self) -> None:
        try:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error("خطا در ایجاد پوشه موقت ویدیو %s: %s", self._temp_dir, e)
            raise VideoCompressionError(f"دسترسی به پوشه موقت ویدیو مقدور نیست: {e}") from e

    def get_temp_dir(self) -> Path:
        self._ensure_temp_dir()
        return self._temp_dir

    @staticmethod
    def _extract_rotation(video_stream: dict) -> int:
        # چک کردن تگ rotate قدیمی
        tags = video_stream.get("tags", {})
        rotate_tag = str(tags.get("rotate", "0")).strip()
        if rotate_tag.lstrip("-").isdigit():
            val = int(rotate_tag)
            if val != 0:
                return val

        # چک کردن Display Matrix در ویدیوهای موبایل جدید
        for side_data in video_stream.get("side_data_list", []):
            if "rotation" in side_data:
                rot_val = str(side_data["rotation"]).strip()
                if rot_val.lstrip("-").isdigit():
                    return int(rot_val)

        return 0

    async def _probe_video_metadata(self, video_path: Path) -> tuple[int, int, int]:
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

            rotation = self._extract_rotation(video_stream)
            if abs(rotation) in (90, 270):
                width, height = height, width

            duration_raw = data.get("format", {}).get("duration") or video_stream.get("duration") or 0
            try:
                duration = int(round(float(duration_raw)))
            except (ValueError, TypeError):
                duration = 0

            return max(0, width), max(0, height), max(0, duration)

        except (asyncio.TimeoutError, asyncio.CancelledError):
            if process:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            if isinstance(process, asyncio.CancelledError):
                raise
            logger.warning("تایم‌اوت در خواندن متادیتای ویدیو %s", video_path.name)
            return 0, 0, 0
        except FileNotFoundError:
            logger.warning("ابزار ffprobe روی سرور پیدا نشد.")
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
        in_file = Path(input_path)
        if not in_file.exists() or in_file.stat().st_size == 0:
            raise VideoCompressionError("فایل ویدیویی ورودی یافت نشد یا حجم آن صفر است.")

        preset_config = self.QUALITY_PRESETS.get(quality_level.lower().strip())
        if not preset_config:
            raise VideoCompressionError(f"سطح فشرده‌سازی نامعتبر است: {quality_level}")

        crf_value = preset_config["crf"]
        max_side = preset_config["max_short_side"]
        audio_bitrate = preset_config["audio_bitrate"]

        self._ensure_temp_dir()
        unique_id = uuid.uuid4().hex[:8]
        output_path = self._temp_dir / f"compressed_{unique_id}.mp4"

        # تغییر ابعاد متناسب با ضلع کوچک تصویر و زوج‌سازی (-2) برای سازگاری با H.264
        scale_filter = (
            f"scale='if(gt(min(iw,ih),{max_side}),if(gt(iw,ih),-2,{max_side}),trunc(iw/2)*2)':"
            f"'if(gt(min(iw,ih),{max_side}),if(gt(iw,ih),{max_side},-2),trunc(ih/2)*2)'"
        )

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(in_file),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-vf", scale_filter,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", crf_value,
            "-threads", self._ffmpeg_threads,
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", audio_bitrate,
            "-ar", "44100",
            "-max_muxing_queue_size", "1024",
            "-movflags", "+faststart",
            "-metadata", f"title={bot_title}",
            str(output_path),
        ]

        logger.info(
            "ویدیو %s با کیفیت %s (CRF=%s) وارد صف پردازش شد.",
            in_file.name,
            quality_level,
            crf_value,
        )

        process = None
        try:
            async with self._semaphore:
                logger.info("شروع رندر ویدیو %s", in_file.name)
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
                    logger.error("خطای ffmpeg (کد %s): %s", process.returncode, short_err)
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
            logger.error("تایم‌اوت در پردازش ویدیو %s", in_file.name)
            raise VideoCompressionError("زمان پردازش این ویدیو بیش از حد مجاز طول کشید و متوقف شد.")

        except asyncio.CancelledError:
            if process:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            self.safe_remove(output_path)
            logger.warning("پردازش ویدیو %s لغو شد.", in_file.name)
            raise

        except FileNotFoundError:
            self.safe_remove(output_path)
            logger.critical("ابزار ffmpeg روی سرور نصب نیست.")
            raise VideoCompressionError("سرویس پردازش ویدیو در سرور در دسترس نیست.")

        except VideoCompressionError:
            self.safe_remove(output_path)
            raise

        except Exception as e:
            self.safe_remove(output_path)
            logger.exception("خطای پیش‌بینی‌نشده در پردازش ویدیو: %s", e)
            raise VideoCompressionError("خطای ناشناخته در پردازش ویدیو رخ داد.") from e

        if not output_path.exists() or output_path.stat().st_size == 0:
            self.safe_remove(output_path)
            raise VideoCompressionError("فایل خروجی ویدیو ایجاد نشد یا حجم آن صفر است.")

        width, height, duration = await self._probe_video_metadata(output_path)

        logger.info(
            "ویدیو آماده شد: %s (%s بایت | %sx%s | %s ثانیه)",
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
        if file_path is None:
            return
        try:
            target = Path(file_path)
            if target.exists():
                target.unlink()
        except OSError as e:
            logger.warning("خطا در حذف فایل موقت %s: %s", file_path, e)

            