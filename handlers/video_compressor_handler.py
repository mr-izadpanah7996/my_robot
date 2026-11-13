import asyncio
import logging
import uuid
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import load_config
from keyboards.vid_compress_inline import (
    VideoCompressCallback,
    get_video_download_keyboard,
    get_video_quality_guide_text,
    get_video_quality_keyboard,
)
from services.video_compressor_service import VideoCompressionError, VideoCompressorService

logger = logging.getLogger(__name__)

_config = load_config()
ARCHIVE_CHANNEL_ID = _config.bot.video_archive_channel_id

router_video_compressor = Router()
video_compressor_service = VideoCompressorService()

MAX_VIDEO_SIZE_MB = 600
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".3gp", ".m4v"}


# ==========================================
# وضعیت‌های FSM
# ==========================================
class VideoCompressorStates(StatesGroup):
    waiting_for_video = State()
    waiting_for_quality = State()
    waiting_for_download = State()


# ==========================================
# توابع کمکی
# ==========================================
def format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


async def safe_edit_text(message: Message, text: str, reply_markup=None, parse_mode: str | None = "Markdown") -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        err_str = str(e).lower()
        if "message is not modified" in err_str:
            pass
        elif "can't parse entities" in err_str and parse_mode is not None:
            # تلاش مجدد بدون فرمت‌دهی در صورت وجود کاراکتر ناسازگار
            try:
                await message.edit_text(text, reply_markup=reply_markup, parse_mode=None)
            except TelegramBadRequest:
                pass
        elif "message to edit not found" in err_str:
            await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            logger.warning("خطای TelegramBadRequest در ویرایش پیام ویدیو: %s", e)
    except Exception as e:
        logger.error("خطای غیرمنتظره در ویرایش پیام ویدیو: %s", e)


async def _delete_single_message_later(bot: Bot, chat_id: int, message_id: int, delay_seconds: int) -> None:
    try:
        await asyncio.sleep(delay_seconds)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except (TelegramBadRequest, TelegramForbiddenError, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.warning("خطا در پاک‌سازی زمان‌بندی‌شده پیام آرشیو: %s", e)


async def _auto_cleanup_delivered_video(
    bot: Bot,
    user_chat_id: int,
    user_message_ids: list[int],
    archive_chat_id: int | None,
    archive_message_id: int | None,
    delay_seconds: int = 25,
) -> None:
    try:
        await asyncio.sleep(delay_seconds)
        for msg_id in user_message_ids:
            try:
                await bot.delete_message(chat_id=user_chat_id, message_id=msg_id)
            except (TelegramBadRequest, TelegramForbiddenError):
                pass

        if archive_chat_id and archive_message_id:
            try:
                await bot.delete_message(chat_id=archive_chat_id, message_id=archive_message_id)
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning("خطا در پاک‌سازی خودکار پیام‌های ویدیو: %s", e)


# ==========================================
# ۱. شروع فرآیند با کلیک روی دکمه منوی اصلی
# ==========================================
@router_video_compressor.message(F.text == "🎬 کم کردن حجم ویدیو")
async def start_video_compressor(message: Message, state: FSMContext) -> None:
    old_data = await state.get_data()

    # پاک‌سازی فایل ناتمام روی دیسک در صورت وجود
    old_file = old_data.get("input_file_path")
    if old_file:
        video_compressor_service.safe_remove(old_file)

    # پاک‌سازی پیام قبلی در کانال آرشیو اگر کاربر بدون دریافت ویدیوی قبلی مجدد شروع کرد
    old_archive_msg_id = old_data.get("archive_message_id")
    if old_archive_msg_id and ARCHIVE_CHANNEL_ID:
        try:
            await message.bot.delete_message(chat_id=ARCHIVE_CHANNEL_ID, message_id=old_archive_msg_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass

    await state.clear()
    await state.set_state(VideoCompressorStates.waiting_for_video)

    await message.answer(
        "🎬 **بخش کاهش حجم و بهینه‌سازی ویدیو**\n\n"
        "لطفاً ویدیوی خود را به صورت مستقیم یا به صورت فایل (Document) ارسال کنید.\n\n"
        "📌 **فرمت‌های پشتیبانی‌شده:**\n"
        "`MP4, MKV, MOV, AVI, WEBM, FLV, WMV, 3GP`\n"
        f"⚠️ **حداکثر حجم مجاز:** {MAX_VIDEO_SIZE_MB} مگابایت",
        parse_mode="Markdown",
    )


# ==========================================
# ۲. دریافت فایل ویدیویی (Video / Document)
# ==========================================
@router_video_compressor.message(
    VideoCompressorStates.waiting_for_video,
    F.video | F.document,
)
async def handle_incoming_video(message: Message, state: FSMContext) -> None:
    file_id = None
    file_size = 0
    original_file_name = "video.mp4"

    if message.video:
        file_id = message.video.file_id
        file_size = message.video.file_size or 0
        original_file_name = message.video.file_name or "video.mp4"

    elif message.document:
        doc = message.document
        ext = Path(doc.file_name or "").suffix.lower()
        is_video_mime = doc.mime_type and doc.mime_type.startswith("video/")
        is_video_ext = ext in ALLOWED_VIDEO_EXTENSIONS

        if not (is_video_mime or is_video_ext):
            await message.answer(
                "⚠️ فایل ارسالی معتبر نیست. لطفاً یک فایل ویدیویی با فرمت مجاز "
                "(`MP4, MKV, MOV, AVI, WEBM, 3GP`) ارسال نمایید.",
                parse_mode="Markdown",
            )
            return

        file_id = doc.file_id
        file_size = doc.file_size or 0
        original_file_name = doc.file_name or "document_video.mp4"

    if file_size > MAX_VIDEO_SIZE_BYTES:
        await message.answer(
            f"❌ حجم فایل ارسالی شما ({format_file_size(file_size)}) بیشتر از سقف مجاز "
            f"**{MAX_VIDEO_SIZE_MB} مگابایت** است.\n"
            "لطفاً ویدیویی با حجم کمتر از ۶۰۰ مگابایت ارسال کنید.",
            parse_mode="Markdown",
        )
        return

    status_msg = await message.answer("⏳ در حال دریافت و انتقال فایل ویدیویی به سرور... لطفاً منتظر بمانید.")
    input_file_path = None

    try:
        bot = message.bot
        file_info = await bot.get_file(file_id)

        temp_dir = video_compressor_service.get_temp_dir()
        file_extension = Path(original_file_name).suffix.lower() or ".mp4"
        unique_name = f"vid_in_{uuid.uuid4().hex[:8]}{file_extension}"
        input_file_path = temp_dir / unique_name

        await bot.download_file(file_info.file_path, destination=input_file_path)

        actual_disk_size = input_file_path.stat().st_size
        if actual_disk_size > MAX_VIDEO_SIZE_BYTES:
            video_compressor_service.safe_remove(input_file_path)
            await state.clear()
            await safe_edit_text(
                status_msg,
                f"❌ حجم فایل بیش از {MAX_VIDEO_SIZE_MB} مگابایت است و عملیات لغو شد.",
                parse_mode=None,
            )
            return

        await state.update_data(
            input_file_path=str(input_file_path),
            user_caption=message.caption or "",
            original_size=actual_disk_size,
        )

        await state.set_state(VideoCompressorStates.waiting_for_quality)

        guide_text = f"✅ ویدیو با حجم **{format_file_size(actual_disk_size)}** دریافت شد.\n\n{get_video_quality_guide_text()}"
        await safe_edit_text(
            status_msg,
            guide_text,
            reply_markup=get_video_quality_keyboard(),
            parse_mode="Markdown",
        )

    except TelegramBadRequest as e:
        video_compressor_service.safe_remove(input_file_path)
        logger.error("خطای تلگرام حین دریافت فایل ویدیویی: %s", e)
        await state.clear()
        await safe_edit_text(
            status_msg,
            "❌ خطا در دریافت فایل از تلگرام. لطفاً دوباره امتحان کنید.",
            parse_mode=None,
        )
    except Exception as e:
        video_compressor_service.safe_remove(input_file_path)
        logger.exception("خطای غیرمنتظره حین ذخیره موقت ویدیو: %s", e)
        await state.clear()
        await safe_edit_text(
            status_msg,
            "❌ خطایی در ذخیره‌سازی فایل رخ داد. عملیات لغو شد.",
            parse_mode=None,
        )


# ==========================================
# رد پیام‌های غیرمرتبط در وضعیت انتظار برای ویدیو
# ==========================================
@router_video_compressor.message(VideoCompressorStates.waiting_for_video)
async def handle_invalid_video_input(message: Message) -> None:
    await message.answer("⚠️ لطفاً فقط یک **فایل ویدیویی** ارسال نمایید.", parse_mode="Markdown")


# ==========================================
# ۳. مدیریت دکمه انصراف داخل کیبورد شیشه‌ای
# ==========================================
@router_video_compressor.callback_query(
    VideoCompressorStates.waiting_for_quality,
    VideoCompressCallback.filter(F.action == "cancel"),
)
async def process_video_cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    input_path = data.get("input_file_path")

    if input_path:
        video_compressor_service.safe_remove(input_path)

    await state.clear()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.edit_text("❌ عملیات کاهش حجم ویدیو لغو شد.")
    except TelegramBadRequest:
        pass

    try:
        await callback.answer("عملیات لغو شد.")
    except TelegramBadRequest:
        pass


# ==========================================
# ۴. پردازش ویدیو و انتقال موقت به کانال آرشیو
# ==========================================
@router_video_compressor.callback_query(
    VideoCompressorStates.waiting_for_quality,
    VideoCompressCallback.filter(F.action == "quality"),
)
async def process_video_compression(
    callback: CallbackQuery,
    callback_data: VideoCompressCallback,
    state: FSMContext,
) -> None:
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    data = await state.get_data()
    input_path = data.get("input_file_path")
    user_caption = data.get("user_caption", "")
    quality_level = callback_data.level

    if not input_path or not Path(input_path).exists():
        await state.clear()
        await safe_edit_text(
            callback.message,
            "❌ فایل ویدیویی یافت نشد یا منقضی شده است. لطفاً دوباره ارسال کنید.",
            parse_mode=None,
        )
        return

    await safe_edit_text(
        callback.message,
        "⚙️ ویدیوی شما در صف پردازش قرار گرفت...\n"
        "⏳ رندر و فشرده‌سازی ویدیو بسته به حجم فایل ممکن است کمی زمان ببرد. لطفاً صبور باشید.",
        reply_markup=None,
        parse_mode=None,
    )

    compressed_result = None
    compression_succeeded = False

    try:
        try:
            await callback.message.bot.send_chat_action(
                chat_id=callback.message.chat.id,
                action=ChatAction.UPLOAD_VIDEO,
            )
        except TelegramBadRequest:
            pass

        original_size = data.get("original_size") or Path(input_path).stat().st_size

        compressed_result = await video_compressor_service.compress_video(
            input_path=input_path,
            quality_level=quality_level,
            bot_title="دلتا ربات",
        )

        compressed_size = compressed_result.output_path.stat().st_size

        reduction_percent = 0
        if original_size > compressed_size:
            reduction_percent = round(((original_size - compressed_size) / original_size) * 100)

        report_text = (
            f"🎬 عنوان: دلتا ربات\n"
            f"📊 گزارش کاهش حجم ویدیو:\n"
            f"▫️ حجم اولیه: {format_file_size(original_size)}\n"
            f"▫️ حجم فعلی: {format_file_size(compressed_size)}\n"
            f"▫️ میزان کاهش: {reduction_percent}٪"
        )

        clean_user_caption = user_caption.strip()
        if clean_user_caption:
            max_allowed_user_len = 1024 - len(report_text) - 5
            if len(clean_user_caption) > max_allowed_user_len:
                clean_user_caption = clean_user_caption[:max_allowed_user_len] + "..."
            final_caption = f"{clean_user_caption}\n\n{report_text}"
        else:
            final_caption = report_text

        video_to_upload = FSInputFile(
            path=str(compressed_result.output_path),
            filename="Delta_Video.mp4",
        )

        # آپلود موقت در کانال خصوصی آرشیو برای دریافت file_id و آزادسازی فوری هارد سرور
        archive_msg = await callback.message.bot.send_video(
            chat_id=ARCHIVE_CHANNEL_ID,
            video=video_to_upload,
            width=compressed_result.width if compressed_result.width > 0 else None,
            height=compressed_result.height if compressed_result.height > 0 else None,
            duration=compressed_result.duration if compressed_result.duration > 0 else None,
            caption=f"Archive | User: {callback.from_user.id}",
            parse_mode=None,
            supports_streaming=True,
        )

        # استخراج file_id چه تلگرام آن را video ثبت کند و چه برای ویدیوهای بی‌صدا animation ثبت کند
        uploaded_media = archive_msg.video or archive_msg.animation
        if not uploaded_media:
            raise VideoCompressionError("خطا در ثبت فایل ویدیویی در فضای ابری تلگرام.")

        await state.update_data(
            archive_file_id=uploaded_media.file_id,
            archive_message_id=archive_msg.message_id,
            final_caption=final_caption,
            input_file_path=None,
        )
        await state.set_state(VideoCompressorStates.waiting_for_download)
        compression_succeeded = True

        # تایمر پشتیبان ۱ ساعته برای حذف از کانال آرشیو در صورت عدم دریافت توسط کاربر
        asyncio.create_task(
            _delete_single_message_later(
                bot=callback.message.bot,
                chat_id=ARCHIVE_CHANNEL_ID,
                message_id=archive_msg.message_id,
                delay_seconds=3600,
            )
        )

        ready_text = (
            "✅ **ویدیوی شما با موفقیت فشرده و آماده تحویل شد!**\n\n"
            f"📦 حجم اولیه: `{format_file_size(original_size)}`\n"
            f"📉 حجم جدید: `{format_file_size(compressed_size)}` (**{reduction_percent}٪ کاهش حجم**)\n\n"
            "👇 برای دریافت فایل ویدیویی، دکمه زیر را لمس کنید:"
        )

        await safe_edit_text(
            callback.message,
            ready_text,
            reply_markup=get_video_download_keyboard(),
            parse_mode="Markdown",
        )

    except VideoCompressionError as e:
        logger.error("خطای تخصصی فشرده‌سازی ویدیو برای کاربر %s: %s", callback.from_user.id, e)
        await safe_edit_text(callback.message, f"❌ خطای پردازش: {e}", parse_mode=None)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.error("خطای تلگرام حین آپلود ویدیو به کانال آرشیو (%s): %s", ARCHIVE_CHANNEL_ID, e)
        await safe_edit_text(
            callback.message,
            "❌ خطا در آماده‌سازی فایل خروجی در تلگرام. لطفاً از ادمین بودن ربات در کانال آرشیو اطمینان حاصل کنید.",
            parse_mode=None,
        )
    except Exception as e:
        logger.exception("خطای غیرمنتظره حین پردازش ویدیو: %s", e)
        await safe_edit_text(
            callback.message,
            "❌ متأسفانه در فرآیند تبدیل ویدیو خطایی رخ داد.",
            parse_mode=None,
        )
    finally:
        # پاک‌سازی قطعی فایل‌های موقت از روی هارد دیسک سرور
        video_compressor_service.safe_remove(input_path)
        if compressed_result:
            video_compressor_service.safe_remove(compressed_result.output_path)
        if not compression_succeeded:
            await state.clear()


# ==========================================
# ۵. تحویل آنی ویدیو با کلیک کاربر + تایمر حذف ۲۵ ثانیه‌ای
# ==========================================
@router_video_compressor.callback_query(
    VideoCompressorStates.waiting_for_download,
    VideoCompressCallback.filter(F.action == "download"),
)
async def process_video_download_callback(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        await callback.answer("در حال ارسال ویدیو...")
    except TelegramBadRequest:
        pass

    data = await state.get_data()
    archive_file_id = data.get("archive_file_id")
    archive_message_id = data.get("archive_message_id")
    final_caption = data.get("final_caption", "🎬 عنوان: دلتا ربات")

    await state.clear()

    if not archive_file_id:
        await safe_edit_text(
            callback.message,
            "❌ لینک دریافت این ویدیو منقضی شده است. لطفاً دوباره ویدیوی خود را ارسال کنید.",
            reply_markup=None,
            parse_mode=None,
        )
        return

    try:
        sent_video_msg = await callback.message.answer_video(
            video=archive_file_id,
            caption=final_caption,
            parse_mode=None,
            supports_streaming=True,
        )

        warning_msg = await callback.message.answer(
            "⚠️ **هشدار امنیتی و حریم خصوصی:**\n"
            "به دلایل امنیتی، این ویدیو تا **۲۵ ثانیه دیگر** به صورت خودکار از ربات حذف خواهد شد.\n"
            "لطفاً همین الان آن را ذخیره یا به پیام‌های ذخیره شده (Saved Messages) خود فوروارد کنید!",
            parse_mode="Markdown",
        )

        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass

        asyncio.create_task(
            _auto_cleanup_delivered_video(
                bot=callback.message.bot,
                user_chat_id=callback.message.chat.id,
                user_message_ids=[sent_video_msg.message_id, warning_msg.message_id],
                archive_chat_id=ARCHIVE_CHANNEL_ID,
                archive_message_id=archive_message_id,
                delay_seconds=25,
            )
        )

    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.error("خطای تلگرام حین تحویل ویدیو از آرشیو: %s", e)
        await safe_edit_text(
            callback.message,
            "❌ متأسفانه فایل منقضی شده یا در دسترس نیست. لطفاً مجدداً ویدیو را ارسال کنید.",
            reply_markup=None,
            parse_mode=None,
        )
    except Exception as e:
        logger.exception("خطای غیرمنتظره حین تحویل ویدیو به کاربر: %s", e)
        await safe_edit_text(
            callback.message,
            "❌ خطایی در ارسال ویدیو رخ داد.",
            reply_markup=None,
            parse_mode=None,
        )

        