import logging
import uuid
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from services.audio_service import AudioConversionError, AudioConverterService

logger = logging.getLogger(__name__)

# ==========================================
# ۱. کلاس وضعیت‌ها (FSM States)
# ==========================================
class VideoToAudioStates(StatesGroup):
    """مدیریت وضعیت‌های چرخه دریافت و پردازش ویدیو به صوت"""
    waiting_for_video = State()


# ==========================================
# ۲. پیکربندی و راه‌اندازی سرویس
# ==========================================
router_audio = Router()

# سرویس تبدیل صوت با ۲ پردازش هم‌زمان و تایم‌اوت ۱۰ دقیقه
audio_service = AudioConverterService(max_concurrent=2, bitrate="192k", timeout=600.0)

# پوشه تمپ موقت برای فایل‌های خروجی MP3
TMP_DIR = Path("/tmp/delta_audio_conversions")
TMP_DIR.mkdir(parents=True, exist_ok=True)

# سقف مجاز تلگرام (۲ گیگابایت)
MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024 * 1024


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای لغو عملیات"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ لغو عملیات",
                    callback_data="cancel_conversion",
                )
            ]
        ]
    )


async def safe_edit_status(msg: Message, text: str) -> None:
    """ویرایش امن پیام وضعیت بدون کرش در صورت تکراری بودن متن یا خطای کلاینت"""
    try:
        await msg.edit_text(text)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("خطای ویرایش پیام وضعیت: %s", e)
    except TelegramAPIError as e:
        logger.warning("خطای ارتباطی تلگرام در ویرایش وضعیت: %s", e)


# ==========================================
# ۳. هندلرهای ورود و لغو فرآیند
# ==========================================
@router_audio.message(Command("mp3"))
@router_audio.message(F.text == "🎵 تبدیل mp4 به mp3")
async def cmd_start_video_to_audio(message: Message, state: FSMContext) -> None:
    """ورود کاربر به مرحله ارسال ویدیو"""
    await state.set_state(VideoToAudioStates.waiting_for_video)
    await message.answer(
        "🎙 لطفاً ویدیوی خود را ارسال کنید (حداکثر ۲ گیگابایت):\n\n"
        "💡 می‌توانید فایل را به‌صورت ویدیو یا فایل سندی (Document) ارسال نمایید.",
        reply_markup=get_cancel_keyboard(),
    )


@router_audio.callback_query(F.data == "cancel_conversion")
async def cb_cancel_conversion(callback: CallbackQuery, state: FSMContext) -> None:
    """لغو عملیات با دکمه شیشه‌ای"""
    await state.clear()
    try:
        await callback.message.edit_text("❌ عملیات تبدیل ویدیو لغو شد.")
    except TelegramAPIError:
        pass
    await callback.answer()


@router_audio.message(Command("cancel"))
async def cmd_cancel_conversion(message: Message, state: FSMContext) -> None:
    """لغو عملیات با دستور متنی"""
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("هیچ عملیاتی در جریان نیست.")
        return
    await state.clear()
    await message.answer("❌ عملیات جاری لغو گردید.")


# ==========================================
# ۴. هندلر اصلی پردازش و استخراج صوت
# ==========================================
@router_audio.message(
    VideoToAudioStates.waiting_for_video,
    F.video | F.document,
)
async def process_video_message(message: Message, bot: Bot, state: FSMContext) -> None:
    """دریافت، اعتبارسنجی حجم و فرمت، مدیریت فایل لوکال، تبدیل و ارسال صوت"""
    file_id: str | None = None
    file_size: int = 0
    raw_name: str = "audio"
    duration: int = 0

    # بررسی هوشمند نوع فایل ارسالی
    if message.video:
        file_id = message.video.file_id
        file_size = message.video.file_size or 0
        duration = message.video.duration or 0
        if message.video.file_name:
            raw_name = Path(message.video.file_name).stem

    elif message.document:
        mime = (message.document.mime_type or "").lower()
        doc_name = (message.document.file_name or "").lower()
        valid_extensions = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".ts", ".m4v")

        if not (mime.startswith("video/") or doc_name.endswith(valid_extensions)):
            await message.answer(
                "⚠️ فایل ارسالی یک ویدیوی معتبر نیست. لطفاً یک فایل ویدیویی بفرستید.",
                reply_markup=get_cancel_keyboard(),
            )
            return

        file_id = message.document.file_id
        file_size = message.document.file_size or 0
        if message.document.file_name:
            raw_name = Path(message.document.file_name).stem

    # ۱. اعتبارسنجی سقف حجم فایل (۲ گیگابایت)
    if file_size > MAX_FILE_SIZE_BYTES:
        await message.answer(
            "❌ حجم فایل ارسالی فراتر از سقف مجاز (۲ گیگابایت) است.",
            reply_markup=get_cancel_keyboard(),
        )
        return

    # پیام اولیه شروع پردازش
    status_msg = await message.reply("⏳ در حال پردازش درخواست و دریافت اطلاعات از سرور...")
    await state.clear()

    unique_id = uuid.uuid4().hex
    output_audio_path = TMP_DIR / f"{unique_id}.mp3"
    local_downloaded_input: Path | None = None
    final_input_path: Path | None = None

    try:
        # ۲. دریافت اطلاعات فایل
        await safe_edit_status(status_msg, "📥 در حال آماده‌سازی فایل ویدیویی...")
        tg_file = await bot.get_file(file_id)

        if not tg_file.file_path:
            raise RuntimeError("مسیر فایل توسط سرور تلگرام بازگردانده نشد.")

        # سازگاری دوگانه با Local Bot API و Remote Server:
        # در سرور محلی (is_local=True)، فایل مستقیماً روی دیسک سرور در tg_file.file_path قرار دارد
        disk_path = Path(tg_file.file_path)
        if disk_path.is_file():
            # بدون نیاز به دانلود مجدد؛ استفاده مستقیم از فایل روی دیسک (Zero-Copy)
            final_input_path = disk_path
        else:
            # در صورتی که به هر دلیلی فایل محلی نبود، دانلود در پوشه تمپ
            local_downloaded_input = TMP_DIR / f"{unique_id}_input"
            await bot.download_file(tg_file.file_path, destination=local_downloaded_input)
            final_input_path = local_downloaded_input

        # ۳. پردازش و استخراج صوت توسط FFmpeg
        await safe_edit_status(status_msg, "⚙️ در حال استخراج و انکود صدا با کیفیت 192kbps...")
        await audio_service.convert_to_mp3(
            input_path=final_input_path,
            output_path=output_audio_path,
        )

        # ۴. ارسال صوت نهایی به کاربر
        await safe_edit_status(status_msg, "📤 در حال ارسال فایل صوتی...")
        audio_file = FSInputFile(
            path=output_audio_path,
            filename=f"{raw_name}.mp3",
        )

        await message.reply_audio(
            audio=audio_file,
            title=raw_name[:64],
            performer="Delta Bot",
            duration=duration,
            caption="✅ صوت ویدیو با موفقیت استخراج شد.",
        )

        # حذف پیام وضعیت پس از موفقیت کامل
        try:
            await status_msg.delete()
        except TelegramAPIError:
            pass

    except AudioConversionError as e:
        logger.error("خطای تبدیل برای کاربر %s: %s", message.from_user.id, e)
        await safe_edit_status(status_msg, f"❌ خطا در تبدیل فایل: {e}")

    except Exception as e:
        logger.exception("خطای سیستمی در پردازش فایل کاربر %s: %s", message.from_user.id, e)
        await safe_edit_status(status_msg, "❌ متأسفانه در فرآیند تبدیل خطای پیش‌بینی‌نشده‌ای رخ داد.")

    finally:
        # پاک‌سازی فایل خروجی تولیدشده
        AudioConverterService.safe_remove(output_audio_path)
        
        # پاک‌سازی فایل ورودی فقط در صورتی که خودمان دانلود کرده باشیم
        # (تا به فایل‌های کش اصلی داکر کانتینر آسیبی نرسد)
        if local_downloaded_input:
            AudioConverterService.safe_remove(local_downloaded_input)


# ==========================================
# ۵. پاسخ به محتوای متفرقه در زمان انتظار
# ==========================================
@router_audio.message(VideoToAudioStates.waiting_for_video)
async def process_invalid_content(message: Message) -> None:
    """هندل کردن پیام‌های متنی، عکس، استیکر و غیره در حالت انتظار ویدیو"""
    await message.answer(
        "⚠️ لطفاً فقط یک فایل ویدیویی ارسال کنید.\n"
        "در صورت انصراف، گزینه «❌ لغو عملیات» را بزنید.",
        reply_markup=get_cancel_keyboard(),
    )
