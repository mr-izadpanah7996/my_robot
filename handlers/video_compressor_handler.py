import asyncio
import logging
import uuid
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from keyboards.vid_compress_inline import (
    VideoCompressCallback,
    get_video_quality_guide_text,
    get_video_quality_keyboard,
)
from services.video_compressor_service import VideoCompressionError, VideoCompressorService

logger = logging.getLogger(__name__)

# نام مستقل روتر برای جلوگیری از هرگونه تداخل با روتر مادر
router_video_compressor = Router()

video_compressor_service = VideoCompressorService()

# محدودیت حداکثر حجم ورودی (۶۰۰ مگابایت)
MAX_VIDEO_SIZE_MB = 600
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024

# پسوندهای معتبر فایل‌های اسنادی ویدیویی
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".3gp", ".m4v"}


# ==========================================
# وضعیت‌های FSM (State Machine)
# ==========================================
class VideoCompressorStates(StatesGroup):
    waiting_for_video = State()
    waiting_for_quality = State()


# ==========================================
# توابع کمکی
# ==========================================
def format_file_size(size_bytes: int) -> str:
    """تبدیل بایت به فرمت خوانا (KB یا MB)"""
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


async def safe_edit_text(message: Message, text: str, reply_markup=None, parse_mode: str | None = "Markdown") -> None:
    """ویرایش ایمن پیام با مدیریت کامل خطای TelegramBadRequest"""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        err_str = str(e).lower()
        if "message is not modified" in err_str:
            pass
        elif "message to edit not found" in err_str:
            await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            logger.warning("خطای TelegramBadRequest در ویرایش پیام ویدیو: %s", e)
    except Exception as e:
        logger.error("خطای غیرمنتظره در ویرایش پیام ویدیو: %s", e)


async def _auto_delete_video_messages(bot: Bot, chat_id: int, message_ids: list[int], delay_seconds: int = 25) -> None:
    """
    تسک پس‌زمینه برای حذف خودکار ویدیوی ارسال‌شده و پیام هشدار پس از ۲۵ ثانیه
    جهت حفظ امنیت ربات و جلوگیری از باقی ماندن محتوای حساس در تاریخچه چت.
    """
    try:
        await asyncio.sleep(delay_seconds)
        for msg_id in message_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except TelegramBadRequest:
                pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning("خطا در حذف خودکار پیام‌های ویدیو: %s", e)


# ==========================================
# ۱. شروع فرآیند با کلیک روی دکمه منوی اصلی
# ==========================================
@router_video_compressor.message(F.text == "🎬 کم کردن حجم ویدیو")
async def start_video_compressor(message: Message, state: FSMContext) -> None:
    """ورود کاربر به بخش کاهش حجم ویدیو و پاک‌سازی وضعیت‌های ناتمام قبلی"""
    old_data = await state.get_data()
    old_file = old_data.get("input_file_path")
    if old_file:
        video_compressor_service.safe_remove(old_file)

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
    """بررسی حجم و فرمت ویدیو، دانلود روی سرور و نمایش کیبورد انتخاب کیفیت"""
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

    # بررسی سقف حجم مجاز ۶۰۰ مگابایت پیش از دانلود
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

        # دانلود محلی فایل (سازگار با لوکال بات سرور)
        await bot.download_file(file_info.file_path, destination=input_file_path)

        # بررسی مجدد حجم واقعی پس از دانلود روی دیسک
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
    """راهنمایی کاربر در صورت ارسال متن، عکس یا صدا به جای ویدیو"""
    await message.answer("⚠️ لطفاً فقط یک **فایل ویدیویی** ارسال نمایید.", parse_mode="Markdown")


# ==========================================
# ۳. مدیریت دکمه انصراف داخل کیبورد شیشه‌ای
# ==========================================
@router_video_compressor.callback_query(
    VideoCompressorStates.waiting_for_quality,
    VideoCompressCallback.filter(F.action == "cancel"),
)
async def process_video_cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """لغو عملیات فشرده‌سازی ویدیو و پاک‌سازی فایل موقت از دیسک"""
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
# ۴. پردازش و فشرده‌سازی ویدیو با کیفیت انتخابی
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
    """اجرای فشرده‌سازی ویدیو در صف سمافور و ارسال خروجی با تایمر حذف ۲۵ ثانیه‌ای"""
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

    # بستن دکمه‌های شیشه‌ای بلافاصله پس از انتخاب جهت جلوگیری از کلیک تکراری
    await safe_edit_text(
        callback.message,
        "⚙️ ویدیوی شما در صف پردازش قرار گرفت...\n"
        "⏳ رندر و فشرده‌سازی ویدیو بسته به حجم فایل ممکن است کمی زمان ببرد. لطفاً صبور باشید.",
        reply_markup=None,
        parse_mode=None,
    )

    compressed_result = None

    try:
        # نمایش وضعیت در حال آپلود ویدیو در تلگرام
        try:
            await callback.message.bot.send_chat_action(
                chat_id=callback.message.chat.id,
                action=ChatAction.UPLOAD_VIDEO,
            )
        except TelegramBadRequest:
            pass

        original_size = data.get("original_size") or Path(input_path).stat().st_size

        # پردازش ویدیو از طریق سرویس (با سمافور ۱، ترد کنترل‌شده و متادیتای 'دلتا ربات')
        compressed_result = await video_compressor_service.compress_video(
            input_path=input_path,
            quality_level=quality_level,
            bot_title="دلتا ربات",
        )

        compressed_size = compressed_result.output_path.stat().st_size

        # محاسبه درصد کاهش حجم
        reduction_percent = 0
        if original_size > compressed_size:
            reduction_percent = round(((original_size - compressed_size) / original_size) * 100)

        # ساخت گزارش بهینه‌سازی برای بخش توضیحات ویدیو
        report_text = (
            f"🎬 عنوان: دلتا ربات\n"
            f"📊 گزارش کاهش حجم ویدیو:\n"
            f"▫️ حجم اولیه: {format_file_size(original_size)}\n"
            f"▫️ حجم فعلی: {format_file_size(compressed_size)}\n"
            f"▫️ میزان کاهش: {reduction_percent}٪"
        )

        # جلوگیری از عبور کپشن ترکیبی از سقف ۱۰۲۴ کاراکتر تلگرام
        clean_user_caption = user_caption.strip()
        if clean_user_caption:
            max_allowed_user_len = 1024 - len(report_text) - 5
            if len(clean_user_caption) > max_allowed_user_len:
                clean_user_caption = clean_user_caption[:max_allowed_user_len] + "..."
            final_caption = f"{clean_user_caption}\n\n{report_text}"
        else:
            final_caption = report_text

        video_to_send = FSInputFile(
            path=str(compressed_result.output_path),
            filename="Delta_Video.mp4",
        )

        # ارسال ویدیوی فشرده‌شده با مشخصات فنی دقیق و parse_mode=None برای امنیت کپشن کاربر
        sent_video_msg = await callback.message.answer_video(
            video=video_to_send,
            width=compressed_result.width if compressed_result.width > 0 else None,
            height=compressed_result.height if compressed_result.height > 0 else None,
            duration=compressed_result.duration if compressed_result.duration > 0 else None,
            caption=final_caption,
            parse_mode=None,
            supports_streaming=True,
        )

        # ارسال پیام هشدار امنیتی برای حذف خودکار ۲۵ ثانیه‌ای
        warning_msg = await callback.message.answer(
            "⚠️ **هشدار امنیتی و حریم خصوصی:**\n"
            "به دلایل امنیتی، این ویدیو تا **۲۵ ثانیه دیگر** به صورت خودکار از ربات حذف خواهد شد.\n"
            "لطفاً همین الان آن را ذخیره یا به پیام‌های ذخیره شده (Saved Messages) خود فوروارد کنید!",
            parse_mode="Markdown",
        )

        # حذف پیام وضعیت «در حال پردازش...»
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass

        # اجرای تسک پس‌زمینه غیرمسدودکننده برای حذف خودکار ویدیو و پیام هشدار پس از ۲۵ ثانیه
        asyncio.create_task(
            _auto_delete_video_messages(
                bot=callback.message.bot,
                chat_id=callback.message.chat.id,
                message_ids=[sent_video_msg.message_id, warning_msg.message_id],
                delay_seconds=25,
            )
        )

    except VideoCompressionError as e:
        logger.error("خطای تخصصی فشرده‌سازی ویدیو برای کاربر %s: %s", callback.from_user.id, e)
        await safe_edit_text(callback.message, f"❌ خطای پردازش: {e}", parse_mode=None)
    except TelegramBadRequest as e:
        logger.error("خطای تلگرام حین ارسال ویدیوی خروجی: %s", e)
        await safe_edit_text(
            callback.message,
            "❌ ارسال ویدیوی فشرده‌شده به تلگرام با خطا مواجه شد.",
            parse_mode=None,
        )
    except Exception as e:
        logger.exception("خطای غیرمنتظره حین تبدیل یا ارسال ویدیو: %s", e)
        await safe_edit_text(
            callback.message,
            "❌ متأسفانه در فرآیند تبدیل و ارسال ویدیو خطایی رخ داد.",
            parse_mode=None,
        )
    finally:
        # پاک‌سازی قطعی فایل‌های موقت از روی هارد دیسک سرور در هر شرایطی
        video_compressor_service.safe_remove(input_path)
        if compressed_result:
            video_compressor_service.safe_remove(compressed_result.output_path)
        await state.clear()

