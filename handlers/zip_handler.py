import asyncio
import logging
from pathlib import Path
from typing import Any, cast

from aiogram import Bot, F, Router, html
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import Config
from keyboards.zip_kb import (
    ZipActionCallback,
    ZipFormatCallback,
    get_zip_cancel_keyboard,
    get_zip_collecting_keyboard,
    get_zip_format_keyboard,
    get_zip_name_keyboard,
    get_zip_password_keyboard,
)
from services.zip_service import (
    ARCHIVE_FORMAT_LABELS,
    ArchiveFormat,
    ZipExecutionError,
    ZipInsufficientDiskSpaceError,
    ZipService,
    ZipServiceError,
    ZipUnsupportedEncryptionError,
)
from services.limiter import DailyLimiter

logger = logging.getLogger(__name__)

zip_router = Router()
zip_service = ZipService()

ZIP_QUOTA_COST = 5

# سمافور تک‌ظرفیتی: در هر لحظه حداکثر ۱ عملیات فشرده‌سازی روی سرور اجرا می‌شود
ZIP_SEMAPHORE = asyncio.Semaphore(1)

# جلوگیری از کلیک تکراری یا اجرای هم‌زمان توسط یک کاربر
_active_zip_users: set[int] = set()

# قفل اختصاصی هر کاربر برای جلوگیری از Race Condition هنگام دریافت سریع چند فایل
_user_fsm_locks: dict[int, asyncio.Lock] = {}

# لیست دکمه‌های منوی اصلی برای خروج ایمن از وضعیت FSM
MAIN_MENU_TEXTS = {
    "🖼 تبدیل عکس به متن",
    "📄 اسکن فایل pdf",
    "🎬 کم کردن حجم ویدیو",
    "🎵 تبدیل mp4 به mp3",
    "📁 تبدیل متن به فایل",
    "🎙 کم کردن حجم فایل صوتی",
    "🎧 تبدیل ویس به متن",
    "🗣 تبدیل متن به ویس",
    "📱 اسکن و ساخت QR",
    "🗜 زیپ کردن فایل‌ها",
}


# ==========================================
# ۱. کلاس وضعیت‌های FSM برای بخش زیپ کردن فایل‌ها
# ==========================================
class ZipState(StatesGroup):
    collecting_files = State()
    waiting_for_name = State()
    waiting_for_password = State()
    waiting_for_format = State()


# ==========================================
# پاک‌سازی خودکار پوشه‌های رهاشده هنگام روشن شدن ربات
# ==========================================
@zip_router.startup()
async def on_zip_router_startup() -> None:
    removed = await zip_service.cleanup_stale_jobs(max_age_seconds=1800)
    if removed > 0:
        logger.info("تعداد %d پوشه موقت رهاشده در بخش زیپ پاک‌سازی شد.", removed)


# ==========================================
# توابع کمکی
# ==========================================
def _get_user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_fsm_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_fsm_locks[user_id] = lock
    return lock


def _get_limiter(config: Config) -> DailyLimiter:
    return DailyLimiter(db_path=config.db.path)


def _format_reset_time(seconds: int) -> str:
    if seconds <= 0:
        return "چند لحظه دیگر"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours > 0:
        return f"{hours} ساعت و {minutes} دقیقه دیگر"
    return f"{max(1, minutes)} دقیقه دیگر"


def _extract_file_metadata(message: Message) -> tuple[str, str, int] | None:
    """استخراج (file_id, file_name, file_size) از هر نوع فایل ارسالی کاربر"""
    if message.document:
        doc = message.document
        name = doc.file_name or f"document_{doc.file_unique_id}"
        return doc.file_id, name, int(doc.file_size or 0)

    if message.photo:
        photo = message.photo[-1]
        name = f"photo_{photo.file_unique_id}.jpg"
        return photo.file_id, name, int(photo.file_size or 0)

    if message.video:
        vid = message.video
        name = vid.file_name or f"video_{vid.file_unique_id}.mp4"
        return vid.file_id, name, int(vid.file_size or 0)

    if message.audio:
        aud = message.audio
        name = aud.file_name or f"audio_{aud.file_unique_id}.mp3"
        return aud.file_id, name, int(aud.file_size or 0)

    if message.voice:
        vc = message.voice
        name = f"voice_{vc.file_unique_id}.ogg"
        return vc.file_id, name, int(vc.file_size or 0)

    if message.animation:
        anim = message.animation
        name = anim.file_name or f"animation_{anim.file_unique_id}.mp4"
        return anim.file_id, name, int(anim.file_size or 0)

    if message.video_note:
        vn = message.video_note
        name = f"video_note_{vn.file_unique_id}.mp4"
        return vn.file_id, name, int(vn.file_size or 0)

    if message.sticker:
        st = message.sticker
        ext = ".tgs" if st.is_animated else (".webm" if st.is_video else ".webp")
        name = f"sticker_{st.file_unique_id}{ext}"
        return st.file_id, name, int(st.file_size or 0)

    return None


def _build_summary_text(data: dict[str, Any]) -> str:
    """ساخت متن خلاصه نهایی قبل از انتخاب فرمت فشرده‌سازی"""
    files: list[dict[str, Any]] = data.get("files", [])
    total_size: int = int(data.get("total_size", 0))
    archive_name: str = data.get("archive_name") or zip_service.DEFAULT_ARCHIVE_NAME
    has_password = bool(data.get("password"))

    password_status = "🔐 بله (رمزگذاری قوی AES-256)" if has_password else "🔓 خیر (بدون رمز)"

    return (
        "📋 <b>خلاصه نهایی پیش از فشرده‌سازی:</b>\n\n"
        f"• <b>تعداد فایل‌ها:</b> {len(files)} فایل\n"
        f"• <b>مجموع حجم:</b> {zip_service.format_bytes(total_size)}\n"
        f"• <b>نام فایل خروجی:</b> <code>{html.quote(archive_name)}</code>\n"
        f"• <b>رمزگذاری:</b> {password_status}\n"
        f"• <b>هزینه سهمیه:</b> {ZIP_QUOTA_COST} سهمیه روزانه\n\n"
        "👇 <b>لطفاً نوع فرمت فشرده‌سازی را انتخاب کنید:</b>"
    )


async def _safe_edit_text(
    msg: Message | None,
    text: str,
    reply_markup: Any = None,
) -> None:
    if msg is None:
        return
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        pass


# ==========================================
# ۲. پاک‌سازی خودکار FSM هنگام زدن منوی اصلی یا دستورات (/start و ...)
# ==========================================
@zip_router.message(
    ZipState.collecting_files,
    F.text.in_(MAIN_MENU_TEXTS) | F.text.startswith("/"),
)
@zip_router.message(
    ZipState.waiting_for_name,
    F.text.in_(MAIN_MENU_TEXTS) | F.text.startswith("/"),
)
@zip_router.message(
    ZipState.waiting_for_password,
    F.text.in_(MAIN_MENU_TEXTS) | F.text.startswith("/"),
)
@zip_router.message(
    ZipState.waiting_for_format,
    F.text.in_(MAIN_MENU_TEXTS) | F.text.startswith("/"),
)
async def clear_zip_state_on_navigation(message: Message, state: FSMContext) -> None:
    await state.clear()
    raise SkipHandler


# ==========================================
# ۳. ورود به بخش زیپ کردن فایل‌ها
# ==========================================
@zip_router.message(F.text == "🗜 زیپ کردن فایل‌ها")
async def start_zip_process(message: Message, state: FSMContext, config: Config) -> None:
    if not message.from_user:
        return

    user_id = message.from_user.id
    if user_id in _active_zip_users:
        await message.answer("⏳ درخواست فشرده‌سازی قبلی شما هنوز در حال انجام است. لطفاً صبر کنید.")
        return

    limiter = _get_limiter(config)
    remaining_quota = await asyncio.to_thread(limiter.get_remaining_quota, user_id)
    if remaining_quota < ZIP_QUOTA_COST:
        reset_seconds = await asyncio.to_thread(limiter.get_seconds_until_reset, user_id)
        reset_str = _format_reset_time(reset_seconds)
        await message.answer(
            f"⛔️ <b>سهمیه روزانه شما کافی نیست!</b>\n\n"
            f"• سهمیه مورد نیاز برای فشرده‌سازی: <b>{ZIP_QUOTA_COST} سهمیه</b>\n"
            f"• سهمیه باقی‌مانده شما: <b>{remaining_quota} سهمیه</b>\n"
            f"• زمان شارژ مجدد سهمیه: <b>{reset_str}</b>"
        )
        return

    await state.clear()
    await state.set_state(ZipState.collecting_files)
    await state.update_data(files=[], total_size=0)

    await message.answer(
        "🗜 <b>بخش فشرده‌سازی و زیپ کردن فایل‌ها</b>\n\n"
        "لطفاً فایل‌های خود را ارسال کنید (عکس، ویدیو، صدا، ویس، PDF، سند و هر فرمت دیگری پذیرفته می‌شود).\n\n"
        "⚠️ <b>هشدار مهم:</b> لطفاً فایل‌ها را به صورت <b>تک‌به‌تک و با کمی فاصله</b> ارسال کنید. "
        "از ارسال رگباری یا دسته‌جمعی (آلبومی) خودداری کنید؛ در غیر این صورت سیستم ضداسپم ربات شما را موقتاً مسدود می‌کند.\n\n"
        f"📌 <b>قوانین این بخش:</b>\n"
        f"• حداکثر تعداد فایل: <b>{zip_service.MAX_FILES} فایل</b>\n"
        f"• حداکثر مجموع حجم: <b>۱ گیگابایت</b>\n"
        f"• هزینه هر فشرده‌سازی نهایی: <b>{ZIP_QUOTA_COST} سهمیه روزانه</b> (بدون وابستگی به حجم یا تعداد فایل‌ها)",
        reply_markup=get_zip_cancel_keyboard(),
    )


# ==========================================
# ۴. دکمه شیشه‌ای کنسل (در همه مراحل)
# ==========================================
@zip_router.callback_query(ZipActionCallback.filter(F.action == "cancel"))
async def cancel_zip_process(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    msg = callback.message if isinstance(callback.message, Message) else None
    await _safe_edit_text(msg, "❌ عملیات فشرده‌سازی فایل‌ها لغو شد.")
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


# ==========================================
# ۵. دریافت فایل‌ها در مرحله collecting_files
# ==========================================
@zip_router.message(
    ZipState.collecting_files,
    F.document | F.photo | F.video | F.audio | F.voice | F.animation | F.video_note | F.sticker,
)
async def handle_zip_file_upload(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return

    meta = _extract_file_metadata(message)
    if meta is None:
        return

    file_id, file_name, file_size = meta
    user_lock = _get_user_lock(message.from_user.id)

    async with user_lock:
        data = await state.get_data()
        files: list[dict[str, Any]] = list(data.get("files", []))
        total_size: int = int(data.get("total_size", 0))

        # بررسی سقف تعداد فایل (۵۰ فایل)
        if len(files) >= zip_service.MAX_FILES:
            await message.reply(
                f"⛔️ شما به سقف مجاز <b>{zip_service.MAX_FILES} فایل</b> رسیده‌اید!\n"
                "لطفاً برای ادامه دکمه <b>«🗜 فشرده سازی»</b> را بزنید:",
                reply_markup=get_zip_collecting_keyboard(len(files)),
            )
            return

        # بررسی سقف مجموع حجم (۱ گیگابایت) قبل از افزودن به لیست
        new_total_size = total_size + file_size
        if new_total_size > zip_service.MAX_TOTAL_BYTES:
            remaining_bytes = max(0, zip_service.MAX_TOTAL_BYTES - total_size)
            await message.reply(
                "⛔️ <b>این فایل باعث عبور از سقف مجاز ۱ گیگابایت می‌شود!</b>\n\n"
                f"• حجم این فایل: <b>{zip_service.format_bytes(file_size)}</b>\n"
                f"• مجموع حجم فعلی: <b>{zip_service.format_bytes(total_size)}</b>\n"
                f"• فضای باقی‌مانده مجاز: <b>{zip_service.format_bytes(remaining_bytes)}</b>\n\n"
                "می‌توانید فایل کوچک‌تری بفرستید یا فایل‌های فعلی را فشرده کنید:",
                reply_markup=get_zip_collecting_keyboard(len(files)),
            )
            return

        files.append(
            {
                "file_id": file_id,
                "file_name": file_name,
                "file_size": file_size,
            }
        )
        await state.update_data(files=files, total_size=new_total_size)

    count = len(files)
    remaining_files = zip_service.MAX_FILES - count
    remaining_bytes = max(0, zip_service.MAX_TOTAL_BYTES - new_total_size)

    await message.reply(
        f"✅ <b>فایل شماره {count} دریافت شد!</b>\n\n"
        f"• حجم این فایل: <b>{zip_service.format_bytes(file_size)}</b>\n"
        f"• مجموع حجم کل: <b>{zip_service.format_bytes(new_total_size)}</b>\n"
        f"• سقف باقی‌مانده: <b>{remaining_files} فایل</b> دیگر | "
        f"<b>{zip_service.format_bytes(remaining_bytes)}</b> حجم مجاز\n\n"
        "فایل بعدی را بفرستید یا روی دکمه فشرده‌سازی کلیک کنید:",
        reply_markup=get_zip_collecting_keyboard(count),
    )


@zip_router.message(ZipState.collecting_files)
async def handle_invalid_zip_collecting_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    count = len(data.get("files", []))
    await message.reply(
        "⚠️ لطفاً یک <b>فایل</b> (عکس، ویدیو، سند، صدا و...) ارسال کنید یا از دکمه‌های زیر استفاده نمایید:",
        reply_markup=get_zip_collecting_keyboard(count),
    )


# ==========================================
# ۶. کلیک روی دکمه «🗜 فشرده سازی» -> ورود به مرحله نام فایل
# ==========================================
@zip_router.callback_query(
    ZipState.collecting_files,
    ZipActionCallback.filter(F.action == "start_compress"),
)
async def ask_archive_name(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    files: list[dict[str, Any]] = data.get("files", [])
    if not files:
        try:
            await callback.answer("⚠️ هنوز هیچ فایلی ارسال نکرده‌اید!", show_alert=True)
        except TelegramBadRequest:
            pass
        return

    await state.set_state(ZipState.waiting_for_name)
    msg = callback.message if isinstance(callback.message, Message) else None
    await _safe_edit_text(
        msg,
        "📝 <b>مرحله ۱ از ۳: انتخاب نام فایل فشرده</b>\n\n"
        "لطفاً نام دلخواه خود را برای فایل خروجی تایپ و ارسال کنید "
        f"(یا روی دکمه <b>«📄 نام پیش‌فرض»</b> بزنید تا با نام <code>{zip_service.DEFAULT_ARCHIVE_NAME}</code> ذخیره شود):",
        reply_markup=get_zip_name_keyboard(),
    )
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


# ==========================================
# ۷. دریافت نام فایل (متنی یا پیش‌فرض) -> ورود به مرحله رمزگذاری
# ==========================================
@zip_router.message(ZipState.waiting_for_name, F.text)
async def handle_custom_archive_name(message: Message, state: FSMContext) -> None:
    safe_name = zip_service.sanitize_archive_name(message.text)
    await state.update_data(archive_name=safe_name)
    await state.set_state(ZipState.waiting_for_password)

    await message.reply(
        f"✅ نام فایل ثبت شد: <code>{html.quote(safe_name)}</code>\n\n"
        "🔐 <b>مرحله ۲ از ۳: رمزگذاری روی فایل (اختیاری)</b>\n\n"
        "آیا می‌خواهید روی فایل فشرده رمز بگذارید؟\n"
        "رمز دلخواه خود را تایپ و ارسال کنید، یا روی دکمه <b>«⏭ رد شدن (بدون رمز)»</b> بزنید:",
        reply_markup=get_zip_password_keyboard(),
    )


@zip_router.callback_query(
    ZipState.waiting_for_name,
    ZipActionCallback.filter(F.action == "default_name"),
)
async def handle_default_archive_name(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(archive_name=zip_service.DEFAULT_ARCHIVE_NAME)
    await state.set_state(ZipState.waiting_for_password)

    msg = callback.message if isinstance(callback.message, Message) else None
    await _safe_edit_text(
        msg,
        f"✅ نام پیش‌فرض انتخاب شد: <code>{zip_service.DEFAULT_ARCHIVE_NAME}</code>\n\n"
        "🔐 <b>مرحله ۲ از ۳: رمزگذاری روی فایل (اختیاری)</b>\n\n"
        "آیا می‌خواهید روی فایل فشرده رمز بگذارید؟\n"
        "رمز دلخواه خود را تایپ و ارسال کنید، یا روی دکمه <b>«⏭ رد شدن (بدون رمز)»</b> بزنید:",
        reply_markup=get_zip_password_keyboard(),
    )
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@zip_router.message(ZipState.waiting_for_name)
async def handle_invalid_name_input(message: Message) -> None:
    await message.reply(
        "⚠️ لطفاً نام فایل را به صورت <b>متن</b> ارسال کنید یا دکمه «📄 نام پیش‌فرض» را بزنید:",
        reply_markup=get_zip_name_keyboard(),
    )


# ==========================================
# ۸. دریافت رمز عبور (متنی یا رد شدن) -> نمایش خلاصه و انتخاب فرمت
# ==========================================
@zip_router.message(ZipState.waiting_for_password, F.text)
async def handle_custom_password(message: Message, state: FSMContext) -> None:
    try:
        valid_password = zip_service.validate_password(message.text)
    except ZipServiceError as err:
        await message.reply(
            f"❌ {html.quote(str(err))}\nلطفاً رمز دیگری وارد کنید یا دکمه رد شدن را بزنید:",
            reply_markup=get_zip_password_keyboard(),
        )
        return

    await state.update_data(password=valid_password)
    await state.set_state(ZipState.waiting_for_format)
    data = await state.get_data()

    await message.reply(
        _build_summary_text(data),
        reply_markup=get_zip_format_keyboard(),
    )


@zip_router.callback_query(
    ZipState.waiting_for_password,
    ZipActionCallback.filter(F.action == "skip_password"),
)
async def handle_skip_password(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(password=None)
    await state.set_state(ZipState.waiting_for_format)
    data = await state.get_data()

    msg = callback.message if isinstance(callback.message, Message) else None
    await _safe_edit_text(
        msg,
        _build_summary_text(data),
        reply_markup=get_zip_format_keyboard(),
    )
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@zip_router.message(ZipState.waiting_for_password)
async def handle_invalid_password_input(message: Message) -> None:
    await message.reply(
        "⚠️ لطفاً رمز عبور را به صورت <b>متن</b> وارد کنید یا روی دکمه «⏭ رد شدن» بزنید:",
        reply_markup=get_zip_password_keyboard(),
    )


# ==========================================
# ۹. انتخاب فرمت و اجرای نهایی فشرده‌سازی با Semaphore(1)
# ==========================================
@zip_router.callback_query(ZipState.waiting_for_format, ZipFormatCallback.filter())
async def handle_format_and_compress(
    callback: CallbackQuery,
    callback_data: ZipFormatCallback,
    state: FSMContext,
    bot: Bot,
    config: Config,
) -> None:
    user_id = callback.from_user.id
    if user_id in _active_zip_users:
        try:
            await callback.answer("⏳ درخواست شما در حال انجام است، لطفاً صبر کنید...")
        except TelegramBadRequest:
            pass
        return

    data = await state.get_data()
    files: list[dict[str, Any]] = data.get("files", [])
    total_size: int = int(data.get("total_size", 0))
    archive_name: str = data.get("archive_name") or zip_service.DEFAULT_ARCHIVE_NAME
    password: str | None = data.get("password")
    archive_format = cast(ArchiveFormat, callback_data.fmt)

    if not files:
        await state.clear()
        try:
            await callback.answer("⚠️ لیست فایل‌ها منقضی شده است.", show_alert=True)
        except TelegramBadRequest:
            pass
        return

    # بررسی سازگاری رمز عبور با فرمت TAR.GZ قبل از کسر سهمیه
    if password and archive_format == "tar.gz":
        try:
            await callback.answer(
                "⚠️ فرمت TAR.GZ از رمزگذاری پشتیبانی نمی‌کند! لطفاً ZIP یا 7Z را انتخاب کنید.",
                show_alert=True,
            )
        except TelegramBadRequest:
            pass
        return

    # بررسی اولیه فضای خالی دیسک پیش از کسر سهمیه
    try:
        zip_service.check_disk_space(total_size)
    except ZipInsufficientDiskSpaceError as err:
        try:
            await callback.answer(f"❌ {err}", show_alert=True)
        except TelegramBadRequest:
            pass
        return

    # بررسی و کسر اتمیک ۵ سهمیه روزانه (بدون بلاک کردن Event Loop)
    limiter = _get_limiter(config)
    consumed = await asyncio.to_thread(limiter.check_and_consume, user_id, ZIP_QUOTA_COST)
    status_msg = callback.message if isinstance(callback.message, Message) else None
    chat_id = status_msg.chat.id if status_msg else user_id

    if not consumed:
        remaining_quota = await asyncio.to_thread(limiter.get_remaining_quota, user_id)
        reset_seconds = await asyncio.to_thread(limiter.get_seconds_until_reset, user_id)
        reset_str = _format_reset_time(reset_seconds)
        await state.clear()
        await _safe_edit_text(
            status_msg,
            f"⛔️ <b>سهمیه روزانه شما کافی نیست!</b>\n"
            f"• سهمیه باقی‌مانده: <b>{remaining_quota}</b> | مورد نیاز: <b>{ZIP_QUOTA_COST}</b>\n"
            f"• شارژ مجدد: <b>{reset_str}</b>",
        )
        try:
            await callback.answer()
        except TelegramBadRequest:
            pass
        return

    # پاک کردن وضعیت FSM و قفل کردن کاربر
    await state.clear()
    _active_zip_users.add(user_id)

    try:
        await callback.answer("🚀 درخواست فشرده‌سازی ثبت شد...")
    except TelegramBadRequest:
        pass

    job_root: Path | None = None

    try:
        if ZIP_SEMAPHORE.locked():
            await _safe_edit_text(
                status_msg,
                "⏳ <b>در صف پردازش...</b>\n"
                "سرور در حال فشرده‌سازی فایل کاربر دیگری است. به محض آزاد شدن، کار شما آغاز می‌شود.",
            )

        async with ZIP_SEMAPHORE:
            # بررسی مجدد فضای دیسک در لحظه شروع کار واقعی
            zip_service.check_disk_space(total_size)
            job_root, input_dir, output_dir = zip_service.create_job_dirs(user_id)

            await _safe_edit_text(
                status_msg,
                f"📥 <b>در حال دانلود {len(files)} فایل روی سرور...</b>\n"
                f"حجم کل: {zip_service.format_bytes(total_size)}",
            )

            # دانلود فایل‌ها همراه با راستی‌آزمایی حجم واقعی روی دیسک
            actual_downloaded_bytes = 0
            for item in files:
                dest_path = zip_service.resolve_unique_filepath(input_dir, item["file_name"])
                await bot.download(file=item["file_id"], destination=dest_path)

                if dest_path.exists():
                    actual_downloaded_bytes += dest_path.stat().st_size
                    if actual_downloaded_bytes > zip_service.MAX_TOTAL_BYTES:
                        raise ZipServiceError(
                            "حجم واقعی فایل‌های دانلودشده از سقف مجاز ۱ گیگابایت بیشتر شد."
                        )

            await _safe_edit_text(
                status_msg,
                f"⚙️ <b>در حال فشرده‌سازی با فرمت {archive_format.upper()}...</b>\n"
                "لطفاً تا پایان عملیات صبر کنید.",
            )

            result = await zip_service.compress_directory(
                input_dir=input_dir,
                output_dir=output_dir,
                archive_name=archive_name,
                archive_format=archive_format,
                password=password,
            )

            await _safe_edit_text(
                status_msg,
                f"📤 <b>فشرده‌سازی کامل شد! در حال ارسال فایل...</b>\n"
                f"حجم فایل خروجی: {zip_service.format_bytes(result.size_bytes)}",
            )

            archive_input = FSInputFile(path=result.output_path, filename=result.filename)
            enc_line = "🔐 <b>وضعیت رمز:</b> رمزگذاری شده (AES-256)\n" if result.encrypted else ""
            fmt_label = ARCHIVE_FORMAT_LABELS.get(result.archive_format, result.archive_format)

            await bot.send_document(
                chat_id=chat_id,
                document=archive_input,
                caption=(
                    f"✅ <b>فایل فشرده شما آماده است!</b>\n\n"
                    f"• <b>نام فایل:</b> <code>{html.quote(result.filename)}</code>\n"
                    f"• <b>تعداد فایل‌ها:</b> {len(files)} فایل\n"
                    f"• <b>فرمت:</b> {fmt_label}\n"
                    f"• <b>حجم اولیه:</b> {zip_service.format_bytes(actual_downloaded_bytes)}\n"
                    f"• <b>حجم نهایی:</b> {zip_service.format_bytes(result.size_bytes)}\n"
                    f"{enc_line}"
                    f"• <b>سهمیه کسر شده:</b> {ZIP_QUOTA_COST} سهمیه"
                ),
            )

            if status_msg:
                try:
                    await status_msg.delete()
                except TelegramBadRequest:
                    pass

    except (
        ZipUnsupportedEncryptionError,
        ZipInsufficientDiskSpaceError,
        ZipExecutionError,
        ZipServiceError,
    ) as err:
        await _safe_edit_text(status_msg, f"❌ {html.quote(str(err))}")

    except Exception as err:
        logger.exception("خطای پیش‌بینی‌نشده در عملیات فشرده‌سازی کاربر %s: %s", user_id, err)
        await _safe_edit_text(
            status_msg,
            "❌ متأسفانه هنگام دانلود یا فشرده‌سازی فایل‌ها خطایی رخ داد.",
        )

    finally:
        _active_zip_users.discard(user_id)
        if job_root is not None:
            await zip_service.cleanup_path(job_root)


# ==========================================
# ۱۰. هندلرهای ورودی نامعتبر و دکمه‌های منقضی‌شده
# ==========================================
@zip_router.message(ZipState.waiting_for_format)
async def handle_waiting_format_text(message: Message) -> None:
    await message.reply(
        "👆 لطفاً یکی از <b>دکمه‌های فرمت فشرده‌سازی</b> بالا را انتخاب کنید یا دکمه کنسل را بزنید:",
        reply_markup=get_zip_format_keyboard(),
    )


@zip_router.callback_query(ZipActionCallback.filter())
@zip_router.callback_query(ZipFormatCallback.filter())
async def handle_expired_zip_callbacks(callback: CallbackQuery) -> None:
    try:
        await callback.answer(
            "⚠️ این درخواست منقضی شده است. لطفاً دوباره از منو وارد بخش زیپ شوید.",
            show_alert=True,
        )
    except TelegramBadRequest:
        pass

