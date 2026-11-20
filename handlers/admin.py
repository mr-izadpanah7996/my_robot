import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Config
from database.db import DatabaseManager

logger = logging.getLogger(__name__)

admin_router = Router()

TEHRAN_TZ = ZoneInfo("Asia/Tehran")

# نگهداری رفرنس تسک‌های پس‌زمینه برای جلوگیری از حذف توسط Garbage Collector پایتون
_BACKGROUND_TASKS: set[asyncio.Task] = set()
# قفل برای جلوگیری از اجرای هم‌زمان چند برودکست و جریمه FloodWait تلگرام
_BROADCAST_LOCK = asyncio.Lock()


class BroadcastStates(StatesGroup):
    waiting_for_message = State()


def _get_cancel_keyboard() -> InlineKeyboardMarkup:
    """دکمه شیشه‌ای انصراف از ارسال پیام همگانی"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ انصراف از ارسال",
                    callback_data="cancel_broadcast",
                )
            ]
        ]
    )


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


def _get_all_user_ids(db_path: str) -> list[int]:
    """استخراج لیست آیدی عددی تمام کاربران در اتصال ایزوله"""
    with DatabaseManager(db_path=db_path) as local_db:
        users = local_db.get_all_users()
    return [int(user["user_id"]) for user in users if user.get("user_id") is not None]


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

    file_bytes = "\n".join(lines).encode("utf-8-sig")
    file_name = f"users_list_{now_tehran.strftime('%Y%m%d_%H%M%S')}.txt"
    return total_users, file_bytes, file_name


async def _run_broadcast_task(
    bot: Bot,
    source_message: Message,
    user_ids: list[int],
    admin_chat_id: int,
) -> None:
    """ارسال ایمن پیام همگانی با رعایت محدودیت نرخ ارسال تلگرام و قفل انحصاری"""
    async with _BROADCAST_LOCK:
        success_count = 0
        blocked_or_failed_count = 0

        for user_id in user_ids:
            retries = 0
            while retries < 3:
                try:
                    await source_message.send_copy(chat_id=user_id)
                    success_count += 1
                    # مکث استاندارد برای حفظ سرعت ~۱۸ پیام در ثانیه و جلوگیری از FloodWait
                    await asyncio.sleep(0.055)
                    break
                except TelegramRetryAfter as retry_err:
                    retries += 1
                    logger.warning(
                        f"محدودیت سرعت تلگرام در برودکست؛ توقف به مدت {retry_err.retry_after} ثانیه..."
                    )
                    await asyncio.sleep(retry_err.retry_after + 0.5)
                except (TelegramForbiddenError, TelegramBadRequest):
                    # کاربر ربات را بلاک کرده یا حساب کاربری او حذف شده است
                    blocked_or_failed_count += 1
                    break
                except Exception as e:
                    logger.error(f"خطای غیرمنتظره در ارسال پیام همگانی به کاربر {user_id}: {e}")
                    blocked_or_failed_count += 1
                    break

        report_text = (
            f"✅ <b>عملیات ارسال پیام همگانی به پایان رسید.</b>\n\n"
            f"📤 پیام شما با موفقیت برای <b>{success_count}</b> کاربر فرستاده شد.\n"
            f"🚫 ارسال ناموفق (بلاک یا اکانت حذف‌شده): <b>{blocked_or_failed_count}</b> کاربر\n"
            f"👥 مجموع کل گیرندگان: <b>{len(user_ids)}</b> کاربر"
        )

        try:
            await bot.send_message(chat_id=admin_chat_id, text=report_text)
        except Exception as e:
            logger.exception(f"خطا در ارسال گزارش نهایی برودکست به ادمین {admin_chat_id}: {e}")


# ==========================================================
# هندلر دریافت فایل لیست کاربران (/users)
# ==========================================================


@admin_router.message(Command("users"), F.chat.type == ChatType.PRIVATE)
async def cmd_export_users(message: Message, config: Config) -> None:
    """ارسال لیست کامل کاربران ربات به صورت فایل متنی تمیز (فقط در چت خصوصی ادمین)"""
    if not message.from_user or message.from_user.id not in config.bot.admin_ids:
        return

    try:
        if message.bot:
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


# ==========================================================
# هندلرهای سیستم ارسال پیام همگانی (/message)
# ==========================================================


@admin_router.message(Command("message"), F.chat.type == ChatType.PRIVATE)
async def cmd_start_broadcast(message: Message, config: Config, state: FSMContext) -> None:
    """ورود ادمین به وضعیت انتظار برای دریافت پیام همگانی"""
    if not message.from_user or message.from_user.id not in config.bot.admin_ids:
        return

    if _BROADCAST_LOCK.locked():
        await message.answer("⏳ یک عملیات ارسال همگانی در حال حاضر در حال اجراست. لطفاً تا پایان آن صبر کنید.")
        return

    await state.set_state(BroadcastStates.waiting_for_message)
    await message.answer(
        "📢 <b>ارسال پیام همگانی (Broadcast)</b>\n\n"
        "ادمین گرامی، لطفاً پیام خودتان را ارسال کنید.\n"
        "<i>(می‌توانید متن، عکس، ویدیو، فایل، ویس یا پیام فورواردشده بفرستید؛ "
        "برای لغو عملیات، دستور /cancel را بزنید یا روی دکمه زیر کلیک کنید.)</i>",
        reply_markup=_get_cancel_keyboard(),
    )


@admin_router.message(
    BroadcastStates.waiting_for_message,
    Command("cancel"),
    F.chat.type == ChatType.PRIVATE,
)
async def cmd_cancel_broadcast(message: Message, config: Config, state: FSMContext) -> None:
    """لغو ارسال پیام همگانی از طریق دستور /cancel"""
    if not message.from_user or message.from_user.id not in config.bot.admin_ids:
        return

    await state.clear()
    await message.answer("❌ عملیات ارسال پیام همگانی لغو شد.")


@admin_router.callback_query(
    BroadcastStates.waiting_for_message,
    F.data == "cancel_broadcast",
)
async def cb_cancel_broadcast(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    """لغو ارسال پیام همگانی از طریق دکمه شیشه‌ای"""
    if not callback.from_user or callback.from_user.id not in config.bot.admin_ids:
        await callback.answer()
        return

    await state.clear()
    await callback.answer("عملیات لغو شد.")
    if callback.message:
        await callback.message.edit_text("❌ عملیات ارسال پیام همگانی توسط ادمین لغو شد.")


@admin_router.message(BroadcastStates.waiting_for_message, F.chat.type == ChatType.PRIVATE)
async def process_broadcast_message(
    message: Message,
    config: Config,
    state: FSMContext,
) -> None:
    """دریافت پیام از ادمین، خروج از FSM و آغاز ارسال همگانی در پس‌زمینه"""
    if not message.from_user or message.from_user.id not in config.bot.admin_ids:
        return

    # جلوگیری از ارسال تصادفی دستورات ربات (مثل /start یا /users) به عنوان پیام همگانی
    if message.text and message.text.strip().startswith("/"):
        await message.answer(
            "⚠️ در حال حاضر در وضعیت ارسال پیام همگانی هستید و نمی‌توانید دستور دیگری اجرا کنید.\n"
            "لطفاً متن یا رسانه مورد نظر خود را بفرستید یا برای خروج دستور /cancel را بزنید."
        )
        return

    if _BROADCAST_LOCK.locked():
        await state.clear()
        await message.answer("⏳ یک برودکست دیگر در حال اجراست. لطفاً پس از اتمام آن دوباره تلاش کنید.")
        return

    if not message.bot:
        await state.clear()
        await message.answer("❌ خطای داخلی: نمونه ربات در دسترس نیست.")
        return

    # خروج فوری از وضعیت انتظار تا پیام‌های بعدی ادمین اشتباهاً برودکست نشوند
    await state.clear()

    try:
        all_user_ids = await asyncio.to_thread(_get_all_user_ids, config.db.path)
        # حذف آیدی ادمین فرستنده از لیست دریافت‌کنندگان
        target_user_ids = [uid for uid in all_user_ids if uid != message.from_user.id]

        if not target_user_ids:
            await message.answer("📭 کاربر دیگری در دیتابیس وجود ندارد که پیام برای او ارسال شود.")
            return

        await message.answer(
            f"⏳ فرآیند ارسال پیام همگانی برای <b>{len(target_user_ids)}</b> کاربر آغاز شد.\n"
            f"پس از اتمام، گزارش دقیق ارسال برایتان فرستاده می‌شود..."
        )

        # ایجاد تسک پس‌زمینه و ثبت رفرنس آن در مجموعه سراسری برای جلوگیری از حذف توسط GC
        task = asyncio.create_task(
            _run_broadcast_task(
                bot=message.bot,
                source_message=message,
                user_ids=target_user_ids,
                admin_chat_id=message.chat.id,
            )
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    except Exception as e:
        logger.exception(f"خطا در شروع فرآیند برودکست توسط ادمین {message.from_user.id}: {e}")
        await message.answer("❌ خطایی هنگام خواندن لیست کاربران برای ارسال همگانی رخ داد.")

