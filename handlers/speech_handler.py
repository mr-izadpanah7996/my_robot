import io
import logging
import re
from pathlib import Path
from aiogram import Router, F, Bot, html
from aiogram.types import Message, BufferedInputFile, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from config import load_config
from keyboards.inline import get_cancel_ocr_keyboard
from services.limiter import DailyLimiter
from services.speech_service import SpeechService, SpeechError

logger = logging.getLogger(__name__)

speech_router = Router()

# مقداردهی سرویس‌ها مطابق قراردادهای پروژه
config = load_config()
speech_service = SpeechService(api_keys=config.ai.api_keys)
daily_limiter = DailyLimiter(max_requests=15, window_seconds=86400)

SPEECH_QUOTA_COST = 5
MAX_AUDIO_FILE_SIZE = 20 * 1024 * 1024  # سقف ۲۰ مگابایت

ALL_AUDIO_EXTENSIONS: set[str] = (
    set(SpeechService.NATIVE_GEMINI_EXTENSIONS.keys())
    | SpeechService.LEGACY_EXTENSIONS_NEEDING_CONVERSION
)

# جلوگیری از ارسال هم‌زمان چند فایل صوتی توسط یک کاربر برای دور زدن سهمیه
_active_speech_users: set[int] = set()


def _sanitize_transcript_filename(raw_stem: str) -> str:
    """پاک‌سازی و استانداردسازی نام فایل خروجی متنی"""
    cleaned = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", raw_stem).strip()
    cleaned = re.sub(r"\s+", "_", cleaned).strip(". ")
    return cleaned[:50] if cleaned else "Voice_Text"


# ==========================================
# ۱. کلاس وضعیت‌های FSM برای بخش تبدیل صدا به متن
# ==========================================
class SpeechState(StatesGroup):
    waiting_for_audio = State()


# ==========================================
# ۲. هندلر ورود به بخش تبدیل ویس به متن
# ==========================================
@speech_router.message(F.text == "🎧 تبدیل ویس به متن")
async def start_speech_process(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return

    user_id = message.from_user.id

    if user_id in _active_speech_users:
        await message.answer("⏳ فایل صوتی قبلی شما هنوز در حال پردازش است. لطفاً تا پایان آن صبر کنید.")
        return

    remaining = daily_limiter.get_remaining_quota(user_id=user_id)

    if remaining < SPEECH_QUOTA_COST:
        await message.answer(
            f"⚠️ سهمیه روزانه شما برای تبدیل ویس به متن کافی نیست.\n"
            f"برای پردازش هر فایل صوتی به {html.bold(str(SPEECH_QUOTA_COST))} سهمیه نیاز است، "
            f"اما اعتبار فعلی شما {html.bold(str(remaining))} می‌باشد."
        )
        return

    possible_uses = remaining // SPEECH_QUOTA_COST

    await state.set_state(SpeechState.waiting_for_audio)
    await message.answer(
        f"🎧 لطفاً ویس یا فایل صوتی خود را ارسال کنید تا متن آن پیاده‌سازی شود.\n\n"
        f"📊 اعتبار فعلی شما: {html.bold(str(remaining))} سهمیه "
        f"(مناسب برای تبدیل {html.bold(str(possible_uses))} فایل صوتی)\n\n"
        f"📌 راهنما:\n"
        f"• پشتیبانی از تمام فرمت‌های صوتی (ویس تلگرام، MP3، M4A، OGG، WAV، AAC، AMR و...)\n"
        f"• حداکثر حجم مجاز هر فایل: ۲۰ مگابایت\n"
        f"• میزان مصرف اعتبار: {SPEECH_QUOTA_COST} سهمیه به ازای هر فایل صوتی",
        reply_markup=get_cancel_ocr_keyboard(),
    )


# ==========================================
# ۳. دکمه شیشه‌ای انصراف (محدود به وضعیت انتظار صوت)
# ==========================================
@speech_router.callback_query(SpeechState.waiting_for_audio, F.data == "cancel_ocr")
async def cancel_speech_process(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message:
        try:
            await callback.message.edit_text("❌ عملیات تبدیل ویس به متن لغو شد.")
        except TelegramBadRequest:
            pass

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


# ==========================================
# ۴. هندلر دریافت و پردازش ویس / فایل صوتی / ویدیو مسیج
# ==========================================
@speech_router.message(
    SpeechState.waiting_for_audio,
    F.voice | F.audio | F.document | F.video_note,
)
async def process_audio_message(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user:
        return

    user_id = message.from_user.id

    if user_id in _active_speech_users:
        await message.reply("⏳ فایل صوتی قبلی شما هنوز در حال پردازش است. لطفاً کمی صبر کنید.")
        return

    file_id: str | None = None
    file_size: int | None = None
    file_name: str | None = None
    mime_type: str | None = None

    if message.voice:
        file_id = message.voice.file_id
        file_size = message.voice.file_size
        mime_type = message.voice.mime_type or "audio/ogg"
        file_name = "voice_message.ogg"

    elif message.audio:
        file_id = message.audio.file_id
        file_size = message.audio.file_size
        mime_type = message.audio.mime_type
        file_name = message.audio.file_name or "audio_file.mp3"

    elif message.video_note:
        file_id = message.video_note.file_id
        file_size = message.video_note.file_size
        mime_type = "video/mp4"
        file_name = "video_note.mp4"

    elif message.document:
        doc_name = message.document.file_name or ""
        doc_mime = (message.document.mime_type or "").lower()
        ext = Path(doc_name).suffix.lower()

        is_audio_doc = (ext in ALL_AUDIO_EXTENSIONS) or doc_mime.startswith("audio/")
        if not is_audio_doc:
            await message.answer(
                "❌ فرمت این فایل صوتی نیست! لطفاً یک ویس یا فایل صوتی معتبر ارسال کنید.",
                reply_markup=get_cancel_ocr_keyboard(),
            )
            return

        file_id = message.document.file_id
        file_size = message.document.file_size
        mime_type = doc_mime or None
        file_name = doc_name or "audio_document.ogg"

    if not file_id:
        await message.answer(
            "❌ دریافت مشخصات فایل صوتی ناموفق بود. لطفاً مجدداً ارسال کنید.",
            reply_markup=get_cancel_ocr_keyboard(),
        )
        return

    # بررسی سقف حجم ۲۰ مگابایت
    if file_size and file_size > MAX_AUDIO_FILE_SIZE:
        await message.answer(
            "❌ حجم فایل صوتی بیش از سقف مجاز (۲۰ مگابایت) است.\n"
            "لطفاً فایل کم‌حجم‌تری ارسال کنید یا دکمه انصراف را بزنید.",
            reply_markup=get_cancel_ocr_keyboard(),
        )
        return

    # بررسی مجدد سهمیه پیش از شروع عملیات
    remaining = daily_limiter.get_remaining_quota(user_id=user_id)
    if remaining < SPEECH_QUOTA_COST:
        await state.clear()
        await message.answer(
            f"⚠️ سهمیه روزانه شما برای پردازش این فایل صوتی کافی نیست (نیاز به {SPEECH_QUOTA_COST} سهمیه)."
        )
        return

    await state.clear()
    _active_speech_users.add(user_id)

    status_msg = await message.answer("⏳ در حال دانلود فایل صوتی...")
    file_stream = io.BytesIO()
    audio_bytes = b""

    try:
        await bot.download(file=file_id, destination=file_stream)
        audio_bytes = file_stream.getvalue()

        if not audio_bytes:
            await status_msg.edit_text("❌ فایل صوتی دریافت‌شده خالی یا خراب است.")
            return

        if len(audio_bytes) > MAX_AUDIO_FILE_SIZE:
            await status_msg.edit_text("❌ حجم فایل صوتی بیش از سقف مجاز (۲۰ مگابایت) است.")
            return

        try:
            await status_msg.edit_text(
                "🎧 در حال گوش دادن و تبدیل صدا به متن با هوش مصنوعی (لطفاً شکیبا باشید)..."
            )
        except TelegramBadRequest:
            pass

        transcribed_text = await speech_service.transcribe_audio(
            audio_bytes=audio_bytes,
            file_name=file_name,
            mime_type=mime_type,
        )

        # کسر ۵ سهمیه فقط پس از دریافت موفقیت‌آمیز متن
        for _ in range(SPEECH_QUOTA_COST):
            daily_limiter.check_and_consume(user_id=user_id)

        usage_notice = f"💡 تبدیل صدا به متن با موفقیت انجام شد و {SPEECH_QUOTA_COST} سهمیه روزانه کسر گردید."

        if len(transcribed_text) < 4000:
            try:
                await status_msg.delete()
            except TelegramBadRequest:
                pass
            await message.reply(f"{transcribed_text}\n\n{usage_notice}", parse_mode=None)
        else:
            try:
                await status_msg.edit_text("⏳ متن طولانی است؛ در حال آماده‌سازی فایل متنی...")
            except TelegramBadRequest:
                pass

            raw_stem = Path(file_name).stem if file_name else "Voice_Text"
            safe_stem = _sanitize_transcript_filename(raw_stem)
            output_file = BufferedInputFile(
                file=transcribed_text.encode("utf-8"),
                filename=f"Transcript_{safe_stem}.txt",
            )
            await message.reply_document(
                document=output_file,
                caption=f"✅ متن پیاده‌سازی‌شده از فایل صوتی\n\n{usage_notice}",
            )
            try:
                await status_msg.delete()
            except TelegramBadRequest:
                pass

    except SpeechError as err:
        logger.warning(f"خطای سرویس صوتی برای کاربر {user_id}: {err}")
        try:
            await status_msg.edit_text(f"❌ {err}")
        except TelegramBadRequest:
            await message.reply(f"❌ {err}")

    except TelegramBadRequest as tg_err:
        logger.warning(f"خطای TelegramBadRequest در دانلود/ارسال فایل صوتی کاربر {user_id}: {tg_err}")
        try:
            await status_msg.edit_text("❌ فایل صوتی در سرورهای تلگرام در دسترس نیست یا حجم آن بیش از حد مجاز است.")
        except TelegramBadRequest:
            pass

    except TelegramForbiddenError:
        logger.info(f"کاربر {user_id} ربات را هنگام پردازش صوت بلاک کرد.")

    except TelegramRetryAfter as retry_err:
        logger.warning(f"محدودیت نرخ تلگرام (FloodWait) برای کاربر {user_id}: {retry_err.retry_after}s")

    except (TimeoutError, ConnectionError) as err:
        logger.error(f"خطای شبکه هنگام دانلود یا پردازش فایل صوتی کاربر {user_id}: {err}")
        try:
            await status_msg.edit_text("❌ خطا در ارتباط شبکه هنگام دریافت فایل. لطفاً مجدداً تلاش کنید.")
        except TelegramBadRequest:
            await message.reply("❌ خطا در ارتباط شبکه هنگام دریافت فایل. لطفاً مجدداً تلاش کنید.")

    except Exception as err:
        logger.exception(f"خطای پیش‌بینی‌نشده در پردازش فایل صوتی کاربر {user_id}: {err}")
        try:
            await status_msg.edit_text("❌ متأسفانه در فرآیند تبدیل صدا به متن خطایی رخ داد.")
        except TelegramBadRequest:
            await message.reply("❌ متأسفانه در فرآیند تبدیل صدا به متن خطایی رخ داد.")

    finally:
        _active_speech_users.discard(user_id)
        audio_bytes = b""
        file_stream.close()


# ==========================================
# ۵. هندلر ورودی‌های نامعتبر در وضعیت انتظار صوت
# ==========================================
@speech_router.message(SpeechState.waiting_for_audio)
async def invalid_audio_input(message: Message) -> None:
    await message.reply(
        "⚠️ لطفاً فقط یک ویس یا فایل صوتی ارسال کنید یا دکمه انصراف را لمس کنید.",
        reply_markup=get_cancel_ocr_keyboard(),
    )

    