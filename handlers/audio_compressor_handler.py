import logging
import uuid
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from keyboards.audio_compressor_kb import AudioCompressCallback, get_audio_quality_keyboard
from services.audio_compressor_service import AudioCompressionError, AudioCompressorService

logger = logging.getLogger(__name__)

router_audio_compressor = Router()
compressor_service = AudioCompressorService()

ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac", ".opus", ".wma"}


class AudioCompressorStates(StatesGroup):
    waiting_for_audio = State()
    waiting_for_quality = State()


def format_file_size(size_bytes: int) -> str:
    """تبدیل بایت به فرمت خوانا (KB یا MB)"""
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


async def safe_edit_text(message: Message, text: str, reply_markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            pass
        elif "message to edit not found" in str(e).lower():
            await message.answer(text, reply_markup=reply_markup)
        else:
            logger.warning("خطای TelegramBadRequest در ویرایش پیام: %s", e)
    except Exception as e:
        logger.error("خطای غیرمنتظره در ویرایش پیام: %s", e)


# ۱. شروع فرآیند با کلیک روی دکمه متنی
@router_audio_compressor.message(F.text == "🎙 کم کردن حجم فایل صوتی")
async def start_audio_compressor(message: Message, state: FSMContext) -> None:
    old_data = await state.get_data()
    old_file = old_data.get("input_file_path")
    if old_file:
        compressor_service.safe_remove(old_file)

    await state.clear()
    await state.set_state(AudioCompressorStates.waiting_for_audio)

    await message.answer("🎙 لطفاً فایل صوتی، ویس یا موزیک مدنظر خود را ارسال فرمایید:")


# ۲. دریافت فایل صوتی
@router_audio_compressor.message(
    AudioCompressorStates.waiting_for_audio,
    F.voice | F.audio | F.document,
)
async def handle_incoming_audio(message: Message, state: FSMContext) -> None:
    file_id = None
    original_file_name = "audio.mp3"

    if message.voice:
        file_id = message.voice.file_id
        original_file_name = "voice.ogg"

    elif message.audio:
        file_id = message.audio.file_id
        original_file_name = message.audio.file_name or "track.mp3"

    elif message.document:
        doc = message.document
        ext = Path(doc.file_name or "").suffix.lower()
        is_audio_mime = doc.mime_type and doc.mime_type.startswith("audio/")
        is_audio_ext = ext in ALLOWED_AUDIO_EXTENSIONS

        if not (is_audio_mime or is_audio_ext):
            await message.answer(
                "⚠️ فایل ارسالی معتبر نیست. لطفاً یک فایل با فرمت معتبر صوتی (مانند MP3, WAV, M4A, OGG) یا ویس ارسال کنید."
            )
            return

        file_id = doc.file_id
        original_file_name = doc.file_name or "document_audio.mp3"

    status_msg = await message.answer("⏳ در حال دریافت و بررسی فایل بر روی سرور...")

    try:
        bot = message.bot
        file_info = await bot.get_file(file_id)

        temp_dir = compressor_service.get_temp_dir()
        file_extension = Path(original_file_name).suffix.lower() or ".mp3"
        unique_name = f"input_{uuid.uuid4().hex[:8]}{file_extension}"
        input_file_path = temp_dir / unique_name

        await bot.download_file(file_info.file_path, destination=input_file_path)

        await state.update_data(
            input_file_path=str(input_file_path),
            user_caption=message.caption or "",
            status_message_id=status_msg.message_id,
        )

        await state.set_state(AudioCompressorStates.waiting_for_quality)

        await safe_edit_text(
            status_msg,
            "✅ فایل دریافت شد.\n\nکیفیت و میزان فشرده‌سازی مورد نظر را انتخاب نمایید:",
            reply_markup=get_audio_quality_keyboard(),
        )

    except TelegramBadRequest as e:
        logger.error("خطای تلگرام حین دریافت فایل: %s", e)
        await state.clear()
        await safe_edit_text(status_msg, "❌ خطا در بارگیری فایل تلگرام. لطفاً مجدداً امتحان کنید.")
    except Exception as e:
        logger.exception("خطای غیرمنتظره حین ذخیره فایل موقت: %s", e)
        await state.clear()
        await safe_edit_text(status_msg, "❌ خطایی در ذخیره فایل رخ داد. فرآیند لغو شد.")


# رد پیام‌های غیرصوتی در استیت انتظار
@router_audio_compressor.message(AudioCompressorStates.waiting_for_audio)
async def handle_invalid_audio_input(message: Message) -> None:
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
        compressor_service.safe_remove(input_path)

    await state.clear()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.edit_text("❌ عملیات فشرده‌سازی لغو شد.")
    except TelegramBadRequest:
        pass

    await callback.answer("عملیات لغو شد.")


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
    await callback.answer()

    data = await state.get_data()
    input_path = data.get("input_file_path")
    user_caption = data.get("user_caption", "")
    bitrate = callback_data.bitrate

    if not input_path or not Path(input_path).exists():
        await state.clear()
        await safe_edit_text(callback.message, "❌ فایل یافت نشد یا منقضی شده است. لطفاً دوباره ارسال کنید.")
        return

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

        # محاسبه حجم فایل اصلی قبل از تبدیل
        original_size = Path(input_path).stat().st_size

        compressed_file_path = await compressor_service.compress_audio(
            input_path=input_path,
            bitrate=bitrate,
            bot_title="دلتا ربات",
        )

        # محاسبه حجم فایل فشرده‌شده
        compressed_size = Path(compressed_file_path).stat().st_size

        # محاسبه درصد کاهش حجم
        reduction_percent = 0
        if original_size > compressed_size:
            reduction_percent = round(((original_size - compressed_size) / original_size) * 100)

        # ساخت گزارش مقایسه‌ای حجم
        report_text = (
            f"📊 گزارش بهینه‌سازی فایل صوتی:\n"
            f"▫️ حجم اولیه: {format_file_size(original_size)}\n"
            f"▫️ حجم فشرده‌شده: {format_file_size(compressed_size)}\n"
            f"▫️ کاهش حجم: {reduction_percent}٪"
        )

        # ترکیب کپشن کاربر با گزارش فشرده‌سازی
        if user_caption.strip():
            final_caption = f"{user_caption}\n\n{report_text}"
        else:
            final_caption = report_text

        audio_to_send = FSInputFile(
            path=str(compressed_file_path),
            filename="Delta_Audio.mp3",
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
    except TelegramBadRequest as e:
        logger.error("خطای تلگرام حین ارسال صوت خروجی: %s", e)
        await safe_edit_text(callback.message, "❌ ارسال فایل به تلگرام با خطا مواجه شد.")
    except Exception as e:
        logger.exception("خطای غیرمنتظره در ارسال صوت: %s", e)
        await safe_edit_text(callback.message, "❌ متأسفانه در فرآیند تبدیل و ارسال خطای ناشناخته رخ داد.")
    finally:
        compressor_service.safe_remove(input_path)
        if compressed_file_path:
            compressor_service.safe_remove(compressed_file_path)
        await state.clear()
