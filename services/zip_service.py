import asyncio
import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

ArchiveFormat = Literal["zip", "7z", "tar.gz"]

ARCHIVE_FORMAT_LABELS: dict[ArchiveFormat, str] = {
    "zip": "📦 فرمت ZIP",
    "7z": "🗜 فرمت 7Z",
    "tar.gz": "🐧 فرمت TAR.GZ",
}


class ZipServiceError(Exception):
    """خطای پایه سرویس فشرده‌سازی فایل‌ها"""


class ZipUnsupportedEncryptionError(ZipServiceError):
    """زمانی که کاربر برای فرمتی که رمزگذاری ندارد (مثل tar.gz) رمز انتخاب کرده باشد"""


class ZipInsufficientDiskSpaceError(ZipServiceError):
    """زمانی که فضای خالی دیسک سرور برای انجام عملیات کافی نباشد"""


class ZipExecutionError(ZipServiceError):
    """زمانی که فرآیند فشرده‌سازی در سطح سیستم‌عامل با خطا یا Timeout مواجه شود"""


@dataclass(slots=True)
class ArchiveResult:
    output_path: Path
    filename: str
    size_bytes: int
    archive_format: ArchiveFormat
    encrypted: bool


class ZipService:
    MAX_FILES = 50
    MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # دقیقاً ۱ گیگابایت
    DEFAULT_ARCHIVE_NAME = "DeltaBot_Archive"
    MAX_ARCHIVE_NAME_LENGTH = 60
    MAX_MEMBER_STEM_LENGTH = 80  # جلوگیری از خطای File name too long در لینوکس (سقف 255 بایت)
    MAX_PASSWORD_LENGTH = 128
    MIN_FREE_DISK_BUFFER_BYTES = 500 * 1024 * 1024  # ۵۰۰ مگابایت حاشیه امن برای سلامت سیستم و SQLite
    COMPRESSION_TIMEOUT_SECONDS = 900  # حداکثر ۱۵ دقیقه

    def __init__(self, base_temp_dir: str | Path = "temp_zip") -> None:
        self.base_temp_dir = Path(base_temp_dir).resolve()
        self.base_temp_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def format_bytes(size_bytes: int) -> str:
        """تبدیل حجم از بایت به فرمت خوانا (KB / MB / GB)"""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        kb = size_bytes / 1024
        if kb < 1024:
            return f"{kb:.1f} KB"
        mb = kb / 1024
        if mb < 1024:
            return f"{mb:.2f} MB"
        gb = mb / 1024
        return f"{gb:.2f} GB"

    def check_disk_space(self, expected_input_bytes: int) -> None:
        """
        بررسی وجود فضای خالی کافی روی SSD سرور پیش از دانلود و فشرده‌سازی
        (نیاز به حدود ۲ برابر حجم فایل‌های ورودی + ۵۰۰ مگابایت حاشیه امن)
        """
        required_bytes = (expected_input_bytes * 2) + self.MIN_FREE_DISK_BUFFER_BYTES
        free_bytes = shutil.disk_usage(self.base_temp_dir).free
        if free_bytes < required_bytes:
            logger.error(
                "فضای دیسک کافی نیست! فضای خالی: %s | فضای مورد نیاز: %s",
                self.format_bytes(free_bytes),
                self.format_bytes(required_bytes),
            )
            raise ZipInsufficientDiskSpaceError(
                "فضای موقت سرور در حال حاضر برای پردازش این حجم کافی نیست. لطفاً چند دقیقه دیگر تلاش کنید."
            )

    @classmethod
    def validate_password(cls, raw_password: str | None) -> str | None:
        """اعتبارسنجی ایمن رمز عبور برای جلوگیری از کرش subprocess"""
        if not raw_password:
            return None

        password = raw_password.strip()
        if not password:
            return None

        if len(password) > cls.MAX_PASSWORD_LENGTH:
            raise ZipServiceError(
                f"طول رمز عبور نباید بیشتر از {cls.MAX_PASSWORD_LENGTH} کاراکتر باشد."
            )

        if any(ord(ch) < 32 or ord(ch) == 127 for ch in password):
            raise ZipServiceError("رمز عبور نباید شامل کاراکترهای خط جدید یا کنترلی باشد.")

        return password

    @classmethod
    def sanitize_archive_name(cls, raw_name: str | None) -> str:
        """
        پاک‌سازی نام فایل خروجی از کاراکترهای خطرناک (جلوگیری از Path Traversal)
        و حذف پسوندهای اضافی در صورت وارد کردن توسط کاربر
        """
        if not raw_name:
            return cls.DEFAULT_ARCHIVE_NAME

        name = raw_name.strip()
        for suffix in (".tar.gz", ".zip", ".7z", ".rar", ".tar"):
            if name.lower().endswith(suffix):
                name = name[: -len(suffix)].strip()

        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
        name = name.lstrip(".- ")
        name = re.sub(r"[\s_]+", "_", name).strip("_")

        if not name:
            return cls.DEFAULT_ARCHIVE_NAME

        return name[: cls.MAX_ARCHIVE_NAME_LENGTH]

    @classmethod
    def _sanitize_member_filename(cls, original_name: str) -> tuple[str, str]:
        """پاک‌سازی و محدودسازی طول نام هر فایل ورودی با حفظ پسوند"""
        base_name = Path(original_name).name.strip()
        clean_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base_name)
        clean_name = clean_name.lstrip(".")
        if not clean_name:
            return "file", ""

        path_obj = Path(clean_name)
        stem = path_obj.stem[: cls.MAX_MEMBER_STEM_LENGTH].strip(" .") or "file"
        suffix = path_obj.suffix[:16]  # محدودیت ایمن برای پسوند
        return stem, suffix

    def create_job_dirs(self, user_id: int) -> tuple[Path, Path, Path]:
        """ایجاد پوشه‌های ایزوله و اختصاصی برای هر عملیات فشرده‌سازی"""
        job_id = f"{user_id}_{uuid.uuid4().hex[:10]}"
        job_root = self.base_temp_dir / job_id
        input_dir = job_root / "files"
        output_dir = job_root / "output"

        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        return job_root, input_dir, output_dir

    def resolve_unique_filepath(self, input_dir: Path, original_name: str) -> Path:
        """
        جلوگیری از بازنویسی فایل‌های هم‌نام (مثلاً photo.jpg -> photo_1.jpg -> photo_2.jpg)
        """
        stem, suffix = self._sanitize_member_filename(original_name)
        candidate = input_dir / f"{stem}{suffix}"
        if not candidate.exists():
            return candidate

        counter = 1
        while True:
            new_candidate = input_dir / f"{stem}_{counter}{suffix}"
            if not new_candidate.exists():
                return new_candidate
            counter += 1

    async def compress_directory(
        self,
        input_dir: Path,
        output_dir: Path,
        archive_name: str | None,
        archive_format: ArchiveFormat,
        password: str | None = None,
    ) -> ArchiveResult:
        """
        فشرده‌سازی غیرمسدودکننده تمام فایل‌های داخل input_dir با مدیریت کامل Timeout و Cancellation
        """
        if not input_dir.exists() or not any(input_dir.iterdir()):
            raise ZipServiceError("هیچ فایلی برای فشرده‌سازی در پوشه موقت یافت نشد.")

        clean_password = self.validate_password(password)
        if clean_password and archive_format == "tar.gz":
            raise ZipUnsupportedEncryptionError(
                "فرمت TAR.GZ به صورت استاندارد از رمزگذاری پشتیبانی نمی‌کند. "
                "لطفاً فرمت ZIP یا 7Z را انتخاب کنید."
            )

        safe_base_name = self.sanitize_archive_name(archive_name)
        final_filename = f"{safe_base_name}.{archive_format}"
        output_path = (output_dir / final_filename).resolve()

        if archive_format == "zip":
            cmd = [
                "7z",
                "a",
                "-tzip",
                "-mx=5",
                "-mmt=2",
                "-y",
            ]
            if clean_password:
                cmd.extend(["-mem=AES256", f"-p{clean_password}"])
            cmd.extend(["--", str(output_path), "."])

        elif archive_format == "7z":
            cmd = [
                "7z",
                "a",
                "-t7z",
                "-mx=5",
                "-mmt=2",
                "-y",
            ]
            if clean_password:
                cmd.extend(["-mhe=on", f"-p{clean_password}"])
            cmd.extend(["--", str(output_path), "."])

        elif archive_format == "tar.gz":
            cmd = [
                "tar",
                "-czf",
                str(output_path),
                ".",
            ]
        else:
            raise ZipServiceError("فرمت فشرده‌سازی انتخاب‌شده معتبر نیست.")

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(input_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            _, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.COMPRESSION_TIMEOUT_SECONDS,
            )

            if process.returncode != 0:
                err_text = stderr.decode("utf-8", errors="replace").strip()
                logger.error(
                    "خطا در اجرای فرآیند فشرده‌سازی (format=%s, code=%s): %s",
                    archive_format,
                    process.returncode,
                    err_text[:500],
                )
                raise ZipExecutionError("عملیات فشرده‌سازی با خطای سیستمی مواجه شد.")

            if not output_path.exists() or output_path.stat().st_size == 0:
                raise ZipExecutionError("فایل فشرده خروجی ایجاد نشد یا خالی است.")

            return ArchiveResult(
                output_path=output_path,
                filename=final_filename,
                size_bytes=output_path.stat().st_size,
                archive_format=archive_format,
                encrypted=bool(clean_password),
            )

        except asyncio.TimeoutError as err:
            logger.error("Timeout در عملیات فشرده‌سازی (format=%s)", archive_format)
            raise ZipExecutionError(
                "زمان عملیات فشرده‌سازی بیش از حد مجاز طول کشید و متوقف شد."
            ) from err

        except FileNotFoundError as err:
            logger.critical("ابزار سیستمی فشرده‌سازی (7z یا tar) روی سرور یافت نشد: %s", err)
            raise ZipExecutionError("ابزار فشرده‌سازی روی سرور در دسترس نیست.") from err

        finally:
            # جلوگیری از باقی ماندن پردازش یتیم (Orphan Process) در صورت Timeout یا Cancel/Shutdown
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass

    async def cleanup_path(self, path: Path) -> None:
        """حذف ایمن و غیرمسدودکننده پوشه موقت عملیات"""
        if not path.exists():
            return

        resolved_path = path.resolve()
        if self.base_temp_dir not in resolved_path.parents:
            logger.warning("تلاش برای حذف مسیر خارج از پوشه موقت مسدود شد: %s", resolved_path)
            return

        await asyncio.to_thread(shutil.rmtree, resolved_path, True)

    async def cleanup_stale_jobs(self, max_age_seconds: int = 3600) -> int:
        """پاک‌سازی پوشه‌های موقت رهاشده (مثلاً در اثر ریستارت ناگهانی سرور)"""
        def _sync_cleanup() -> int:
            removed = 0
            now = time.time()
            if not self.base_temp_dir.exists():
                return 0
            for item in self.base_temp_dir.iterdir():
                try:
                    if item.is_dir() and (now - item.stat().st_mtime) > max_age_seconds:
                        shutil.rmtree(item, ignore_errors=True)
                        removed += 1
                except OSError:
                    continue
            return removed

        return await asyncio.to_thread(_sync_cleanup)

    