import asyncio
import logging
import re
import uuid
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from keyboards.audio_compressor_kb import AudioCompressCallback, get_audio_quality_keyboard
from services.audio_compressor_service import AudioCompressionError, AudioCompressorService

logger = logging.getLogger(__name__)

router_audio_compressor = Router()
compressor_service = AudioCompressorService()

ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac", ".opus", ".wma"}
MAX_AUDIO_FILE_SIZE = 100 * 1024 * 1024  # سقف ۱۰۰ مگابایت (سازگار با Local Bot API)
TELEGRAM_MAX_CAPTION_LENGTH = 1024

# جلوگیری از پردازش یا دانلود هم‌زمان چند فایل توسط یک کاربر
_active_compress_users: set[int] = set()

# دکمه‌های شناخته‌شده منوی اصلی برای جلوگیری از گیر افتادن کاربر در وضعیت FSM
MAIN_MENU_BUTTONS: set[str] = {
    "🎧 تبدیل ویس به متن",
    "🗣 تبدیل متن به ویس",
    "🎙 کم کردن حجم فایل صوتی",
    "📄 اسکن فایل pdf",
    "🖼 تبدیل عکس به متن",
    "🔍 توصیف تصویر",
    "📁 تبدیل متن به فایل",
    "🔙 بازگشت به منوی اصلی",
}


class AudioCompressorStates(StatesGroup):
    waiting_for_audio = State()
    waiting_for_quality = State()


def format_file_size(size_bytes: int) -> str:
    """تبدیل بایت به فرمت خوانا (KB یا MB)"""
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def _sanitize_output_filename(raw_name: str) -> str:
    """ساخت نام ایمن و تمیز با پسوند mp3 برای جلوگیری از بازنویسی فایل‌ها در گوشی کاربر"""
    stem = Path(raw_name).stem
    cleaned = re.sub(r'[\\/*?:"<>|,\x00-\x1f]', "", stem).strip()
    cleaned = re.sub(r"\s+", "_", cleaned).strip(". ")
    safe_stem = cleaned[:45] if cleaned else "Audio"
    return f"Delta_{safe_stem}.mp3"


def build_safe_caption(user_caption: str, report_text: str) -> str:
    """
    ترکیب کپشن اصلی فایل با گزارش بهینه‌سازی بدون عبور از سقف ۱۰۲۴ کاراکتر تلگرام
    """
    clean_user_caption = user_caption.strip()
    if not clean_user_caption:
        return report_text[:TELEGRAM_MAX_CAPTION_LENGTH]

    separator = "\n\n"
    available_for_user = TELEGRAM_MAX_CAPTION_LENGTH - len(report_text) - len(separator)

    if available_for_user <= 10:
        return report_text[:TELEGRAM_MAX_CAPTION_LENGTH]

    if len(clean_user_caption) > available_for_user:
        trimmed_caption = clean_user_caption[: available_for_user - 3].rstrip() + "..."
    else:
        trimmed_caption = clean_user_caption

    return f"{trimmed_caption}{separator}{report_text}"


async def safe_edit_text(message: Message, text: str, reply_markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        err_lower = str(e).lower()
        if "message is not modified" in err_lower:
            pass
        elif "message to edit not found" in err_lower:
            await message.answer(text, reply_markup=reply_markup)
        else:
            logger.warning("خطای TelegramBadRequest در ویرایش پیام: %s", e)
    except TelegramForbiddenError:
        pass
    except Exception as e:
        logger.error("خطای غیرمنتظره در ویرایش پیام: %s", e)


# ۱. شروع فرآیند با کلیک روی دکمه متنی
@router_audio_compressor.message(F.text == "🎙 کم کردن حجم فایل صوتی")
async def start_audio_compressor(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return

    user_id = message.from_user.id
    if user_id in _active_compress_users:
        await message.answer("⏳ فایل صوتی قبلی شما هنوز در حال دریافت یا فشرده‌سازی است. لطفاً کمی صبر کنید.")
        return

    old_data = await state.get_data()
    old_file = old_data.get("input_file_path")
    if old_file:
        await compressor_service.async_safe_remove(old_file)

    await state.clear()
    await state.set_state(AudioCompressorStates.waiting_for_audio)

    await message.answer(
        "🎙 لطفاً فایل صوتی، ویس یا موزیک مدنظر خود را ارسال فرمایید:\n"
        "📌 حداکثر حجم مجاز: ۱۰۰ مگابایت"
    )


# ۲. دریافت فایل صوتی
@router_audio_compressor.message(
    AudioCompressorStates.waiting_for_audio,
    F.voice | F.audio | F.document | F.video_note,
)
async def handle_incoming_audio(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return

    user_id = message.from_user.id
    if user_id in _active_compress_users:
        await message.answer("⏳ فایل قبلی شما هنوز در حال دریافت یا پردازش است. لطفاً کمی صبر کنید.")
        return

    file_id: str | None = None
    file_size: int | None = None
    original_file_name = "audio.mp3"

    if message.voice:
        file_id = message.voice.file_id
        file_size = message.voice.file_size
        original_file_name = "voice.ogg"

    elif message.audio:
        file_id = message.audio.file_id
        file_size = message.audio.file_size
        original_file_name = message.audio.file_name or "track.mp3"

    elif message.video_note:
        file_id = message.video_note.file_id
        file_size = message.video_note.file_size
        original_file_name = "video_note.mp4"

    elif message.document:
        doc = message.document
        ext = Path(doc.file_name or "").suffix.lower()
        is_audio_mime = bool(doc.mime_type and doc.mime_type.startswith("audio/"))
        is_audio_ext = ext in ALLOWED_AUDIO_EXTENSIONS

        if not (is_audio_mime or is_audio_ext):
            await message.answer(
                "⚠️ فایل ارسالی معتبر نیست. لطفاً یک فایل با فرمت معتبر صوتی (مانند MP3, WAV, M4A, OGG) یا ویس ارسال کنید."
            )
            return

        file_id = doc.file_id
        file_size = doc.file_size
        original_file_name = doc.file_name or "document_audio.mp3"

    if not file_id:
        await message.answer("❌ دریافت مشخصات فایل ناموفق بود. لطفاً مجدداً ارسال کنید.")
        return

    if file_size and file_size > MAX_AUDIO_FILE_SIZE:
        await message.answer(
            "❌ حجم فایل ارسالی بیش از سقف مجاز (۱۰۰ مگابایت) است.\n"
            "لطفاً فایل کم‌حجم‌تری ارسال نمایید."
        )
        return

    # پاک‌سازی فایل موقت قبلی در صورت وجود
    old_data = await state.get_data()
    old_file = old_data.get("input_file_path")
    if old_file:
        await compressor_service.async_safe_remove(old_file)

    _active_compress_users.add(user_id)
    status_msg = await message.answer("⏳ در حال دریافت و آماده‌سازی فایل بر روی سرور...")
    input_file_path: Path | None = None

    try:
        bot = message.bot
        file_info = await bot.get_file(file_id)
        file_extension = Path(original_file_name).suffix.lower() or ".mp3"

        # پشتیبانی مستقیم از مسیر محلی Local Bot API و فال‌بک به دانلود استاندارد
        local_exists = bool(
            file_info.file_path
            and await asyncio.to_thread(lambda: Path(file_info.file_path).exists())
        )

        if local_exists and file_info.file_path:
            input_file_path = await compressor_service.prepare_local_file(
                local_source_path=file_info.file_path,
                original_ext=file_extension,
            )
        else:
            temp_dir = compressor_service.get_temp_dir()
            unique_name = f"input_{uuid.uuid4().hex[:8]}{file_extension}"
            input_file_path = temp_dir / unique_name
            await bot.download_file(file_info.file_path, destination=input_file_path)

            downloaded_size = await asyncio.to_thread(
                lambda: input_file_path.stat().st_size if input_file_path and input_file_path.exists() else 0
            )
            if downloaded_size <= 0:
                await compressor_service.async_safe_remove(input_file_path)
                await state.clear()
                await safe_edit_text(status_msg, "❌ فایل دریافت‌شده خالی یا خراب است.")
                return

            if downloaded_size > MAX_AUDIO_FILE_SIZE:
                await compressor_service.async_safe_remove(input_file_path)
                await state.clear()
                await safe_edit_text(status_msg, "❌ حجم فایل بیش از سقف مجاز (۱۰۰ مگابایت) است.")
                return

        await state.update_data(
            input_file_path=str(input_file_path),
            original_file_name=original_file_name,
            user_caption=message.caption or "",
            status_message_id=status_msg.message_id,
        )

        await state.set_state(AudioCompressorStates.waiting_for_quality)

        await safe_edit_text(
            status_msg,
            "✅ فایل دریافت شد.\n\nکیفیت و میزان فشرده‌سازی مورد نظر را انتخاب نمایید:",
            reply_markup=get_audio_quality_keyboard(),
        )

    except AudioCompressionError as e:
        if input_file_path:
            await compressor_service.async_safe_remove(input_file_path)
        await state.clear()
        await safe_edit_text(status_msg, f"❌ {e}")
    except TelegramBadRequest as e:
        logger.error("خطای تلگرام حین دریافت فایل: %s", e)
        if input_file_path:
            await compressor_service.async_safe_remove(input_file_path)
        await state.clear()
        await safe_edit_text(status_msg, "❌ خطا در بارگیری فایل تلگرام. لطفاً مجدداً امتحان کنید.")
    except TelegramForbiddenError:
        if input_file_path:
            await compressor_service.async_safe_remove(input_file_path)
        await state.clear()
    except Exception as e:
        logger.exception("خطای غیرمنتظره حین ذخیره فایل موقت: %s", e)
        if input_file_path:
            await compressor_service.async_safe_remove(input_file_path)
        await state.clear()
        await safe_edit_text(status_msg, "❌ خطایی در ذخیره فایل رخ داد. فرآیند لغو شد.")
    finally:
        _active_compress_users.discard(user_id)


# رد پیام‌های غیرصوتی در استیت انتظار (و خروج ایمن در صورت زدن دستورات یا دکمه‌های منو)
@router_audio_compressor.message(AudioCompressorStates.waiting_for_audio)
async def handle_invalid_audio_input(message: Message, state: FSMContext) -> None:
    if message.text:
        clean_text = message.text.strip()
        if clean_text.startswith("/") or clean_text in MAIN_MENU_BUTTONS:
            await state.clear()
            await message.answer("ℹ️ عملیات فشرده‌سازی صوت لغو شد. لطفاً مجدداً گزینه خود را انتخاب کنید.")
            return

    await message.answer("⚠️ لطفاً فقط فایل صوتی، ویس یا آهنگ ارسال نمایید.")


# ۳. مدیریت دکمه انصراف داخل کیبورد شیشه‌ای
@router_audio_compressor.callback_query(
    AudioCompressorStates.waiting_for_quality,
    AudioCompressCallback.filter(F.action == "cancel"),
)
async def process_cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    input_path = data.get("input_file_path")

    if input_path:
        await compressor_service.async_safe_remove(input_path)

    await state.clear()

    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.edit_text("❌ عملیات فشرده‌سازی لغو شد.")
        except TelegramBadRequest:
            pass

    try:
        await callback.answer("عملیات لغو شد.")
    except TelegramBadRequest:
        pass


# ۴. پردازش و فشرده‌سازی با کیفیت انتخابی
@router_audio_compressor.callback_query(
    AudioCompressorStates.waiting_for_quality,
    AudioCompressCallback.filter(F.action == "quality"),
)
async def process_audio_compression(
    callback: CallbackQuery,
    callback_data: AudioCompressCallback,
    state: FSMContext,
) -> None:
    if not callback.from_user or not callback.message:
        return

    user_id = callback.from_user.id
    if user_id in _active_compress_users:
        try:
            await callback.answer("⏳ درخواست قبلی شما در حال پردازش است...", show_alert=True)
        except TelegramBadRequest:
            pass
        return

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    data = await state.get_data()
    input_path = data.get("input_file_path")
    original_file_name = data.get("original_file_name", "audio.mp3")
    user_caption = data.get("user_caption", "")
    bitrate = callback_data.bitrate

    # پاک کردن فوری state برای جلوگیری از کلیک‌های تکراری
    await state.clear()

    file_exists = bool(
        input_path and await asyncio.to_thread(lambda: Path(input_path).exists())
    )
    if not file_exists or not input_path:
        await safe_edit_text(callback.message, "❌ فایل یافت نشد یا منقضی شده است. لطفاً دوباره ارسال کنید.")
        return

    _active_compress_users.add(user_id)

    await safe_edit_text(
        callback.message,
        "⚙️ فایل در صف فشرده‌سازی قرار گرفت...\n"
        "در حال پردازش صدا توسط دلتا ربات، لطفاً شکیبا باشید.",
        reply_markup=None,
    )

    compressed_file_path = None

    try:
        try:
            await callback.message.bot.send_chat_action(
                chat_id=callback.message.chat.id,
                action=ChatAction.UPLOAD_VOICE,
            )
        except TelegramBadRequest:
            pass

        original_size = await asyncio.to_thread(lambda: Path(input_path).stat().st_size)

        compressed_file_path = await compressor_service.compress_audio(
            input_path=input_path,
            bitrate=bitrate,
            bot_title="دلتا ربات",
        )

        compressed_size = await asyncio.to_thread(lambda: Path(compressed_file_path).stat().st_size)

        reduction_percent = 0
        if original_size > compressed_size and original_size > 0:
            reduction_percent = round(((original_size - compressed_size) / original_size) * 100)

        report_text = (
            f"📊 گزارش بهینه‌سازی فایل صوتی:\n"
            f"▫️ کیفیت خروجی: {bitrate}\n"
            f"▫️ حجم اولیه: {format_file_size(original_size)}\n"
            f"▫️ حجم فشرده‌شده: {format_file_size(compressed_size)}\n"
            f"▫️ کاهش حجم: {reduction_percent}٪"
        )

        final_caption = build_safe_caption(user_caption=user_caption, report_text=report_text)
        output_filename = _sanitize_output_filename(original_file_name)

        audio_to_send = FSInputFile(
            path=str(compressed_file_path),
            filename=output_filename,
        )

        await callback.message.answer_audio(
            audio=audio_to_send,
            title="دلتا ربات",
            performer="دلتا ربات",
            caption=final_caption,
        )

        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass

    except AudioCompressionError as e:
        logger.error("خطای تخصصی فشرده‌سازی: %s", e)
        await safe_edit_text(callback.message, f"❌ خطای پردازش: {e}")
    except TelegramRetryAfter as retry_err:
        logger.warning("محدودیت نرخ تلگرام (FloodWait) برای کاربر %s: %ss", user_id, retry_err.retry_after)
        await safe_edit_text(callback.message, "⏳ سرور تلگرام موقتاً شلوغ است. لطفاً چند لحظه دیگر مجدداً امتحان کنید.")
    except TelegramBadRequest as e:
        logger.error("خطای تلگرام حین ارسال صوت خروجی: %s", e)
        await safe_edit_text(callback.message, "❌ ارسال فایل به تلگرام با خطا مواجه شد.")
    except TelegramForbiddenError:
        logger.info("کاربر %s ربات را هنگام فشرده‌سازی بلاک کرد.", user_id)
    except Exception as e:
        logger.exception("خطای غیرمنتظره در ارسال صوت: %s", e)
        await safe_edit_text(callback.message, "❌ متأسفانه در فرآیند تبدیل و ارسال خطای ناشناخته رخ داد.")
    finally:
        _active_compress_users.discard(user_id)
        await compressor_service.async_safe_remove(input_path)
        if compressed_file_path:
            await compressor_service.async_safe_remove(compressed_file_path)

            