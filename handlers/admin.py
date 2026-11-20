import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from config import Config
from database.db import DatabaseManager

logger = logging.getLogger(__name__)

admin_router = Router()

TEHRAN_TZ = ZoneInfo("Asia/Tehran")


def _format_to_tehran(utc_str: str | None) -> str:
    """تبدیل رشته زمان UTC ذخیره‌شده در SQLite به فرمت خوانا به وقت تهران"""
    if not utc_str:
        return "نامشخص"
    try:
        clean_str = str(utc_str).split(".")[0].strip()
        dt_utc = datetime.strptime(clean_str, "%Y-%m-%d %H:%M:%S")
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        dt_tehran = dt_utc.astimezone(TEHRAN_TZ)
        return dt_tehran.strftime("%Y/%m/%d - %H:%M:%S")
    except Exception:
        return str(utc_str)


def _sanitize_text(text: str | None, fallback: str = "بدون نام") -> str:
    """حذف کاراکترهای خط جدید و فاصله‌های اضافی برای جلوگیری از بهم‌ریختگی فایل متنی"""
    if not text:
        return fallback
    cleaned = " ".join(str(text).split())
    return cleaned if cleaned else fallback


def _fetch_users_and_build_report(db_path: str) -> tuple[int, bytes, str]:
    """
    اجرای همگام خواندن کاربران در یک اتصال ایزوله و ساخت محتوای فایل متنی
    (طراحی‌شده برای اجرا در ترد جداگانه جهت جلوگیری از قفل شدن Event Loop)
    """
    with DatabaseManager(db_path=db_path) as local_db:
        users = local_db.get_all_users()

    total_users = len(users)
    if total_users == 0:
        return 0, b"", ""

    now_tehran = datetime.now(TEHRAN_TZ)
    now_tehran_str = now_tehran.strftime("%Y/%m/%d - %H:%M:%S")

    lines: list[str] = [
        "==================================================",
        "        گزارش جامع کاربران ثبت‌شده در ربات        ",
        "==================================================",
        f"تعداد کل کاربران : {total_users} نفر",
        f"زمان تهیه گزارش  : {now_tehran_str} (به وقت تهران)",
        "==================================================",
        "",
    ]

    for idx, user in enumerate(users, start=1):
        full_name = _sanitize_text(user.get("full_name"), fallback="بدون نام")
        raw_username = _sanitize_text(user.get("username"), fallback="")
        username = f"@{raw_username.lstrip('@')}" if raw_username else "ندارد"
        user_id = user.get("user_id", "نامشخص")
        joined_tehran = _format_to_tehran(user.get("joined_at"))

        lines.extend(
            [
                f"{idx}. نام و نام خانوادگی : {full_name}",
                f"   یوزرنیم            : {username}",
                f"   آیدی عددی          : {user_id}",
                f"   اولین استارت       : {joined_tehran}",
                "--------------------------------------------------",
            ]
        )

    # افزودن BOM برای نمایش بدون مشکل حروف فارسی در تمام ویرایشگرهای متنی ویندوز و موبایل
    file_bytes = "\n".join(lines).encode("utf-8-sig")
    file_name = f"users_list_{now_tehran.strftime('%Y%m%d_%H%M%S')}.txt"
    return total_users, file_bytes, file_name


@admin_router.message(Command("users"), F.chat.type == ChatType.PRIVATE)
async def cmd_export_users(message: Message, config: Config) -> None:
    """ارسال لیست کامل کاربران ربات به صورت فایل متنی تمیز (فقط در چت خصوصی ادمین)"""
    if not message.from_user or message.from_user.id not in config.bot.admin_ids:
        return

    try:
        # نمایش وضعیت در حال ارسال سند در بالای صفحه تلگرام
        await message.bot.send_chat_action(
            chat_id=message.chat.id,
            action=ChatAction.UPLOAD_DOCUMENT,
        )

        total_users, file_bytes, file_name = await asyncio.to_thread(
            _fetch_users_and_build_report,
            config.db.path,
        )

        if total_users == 0:
            await message.answer("📭 هنوز هیچ کاربری در دیتابیس ثبت نشده است.")
            return

        now_caption = datetime.now(TEHRAN_TZ).strftime("%Y/%m/%d - %H:%M:%S")
        document = BufferedInputFile(file=file_bytes, filename=file_name)

        await message.answer_document(
            document=document,
            caption=(
                f"📊 <b>لیست کامل کاربران ربات</b>\n\n"
                f"👥 تعداد کل: <b>{total_users}</b> کاربر\n"
                f"🕒 زمان گزارش: <code>{now_caption}</code>"
            ),
        )

    except Exception as e:
        logger.exception(f"خطا در استخراج لیست کاربران توسط ادمین {message.from_user.id}: {e}")
        await message.answer("❌ خطایی هنگام تهیه فایل لیست کاربران رخ داد. لاگ سرور را بررسی کنید.")

