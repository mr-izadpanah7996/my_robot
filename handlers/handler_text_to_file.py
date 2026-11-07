import asyncio
import html
import logging
from typing import Dict

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from keyboards.text_to_file_kb import (
    get_format_selection_keyboard,
    get_skip_filename_keyboard,
)
from services.text_converter_service import TextConversionError, TextToFileService

logger = logging.getLogger(__name__)

# ==========================================
# ۱. کلاس وضعیت‌ها (FSM States)
# ==========================================
class TextToFileStates(StatesGroup):
    """مدیریت مراحل تبدیل متن به فایل"""
    waiting_for_text = State()
    waiting_for_filename = State()
    choosing_format = State()
    processing_file = State()  # وضعیت محافظ برای جلوگیری از کلیک‌های تکراری و اسپم


# ==========================================
# ۲. روتر و ابزارهای کمکی
# ==========================================
router_text = Router()
text_service = TextToFileService()

# دیکشنری نگهداری تسک‌های فعال تایمر برای هر کاربر
_user_debounce_tasks: Dict[int, asyncio.Task] = {}


def cancel_user_timer(user_id: int) -> None:
    """لغو و پاک‌سازی تسک زمان‌بندی کاربر از حافظه"""
    task = _user_debounce_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()


async def safe_edit_or_reply(message: Message, text: str, reply_markup=None, parse_mode: str = "HTML") -> Message:
    """ویرایش یا ارسال امن پیام بدون ایجاد کرش برای ارورهای متداول تلگرام"""
    try:
        return await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        if "message is not modified" in err_msg:
            return message
        return await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramAPIError:
        return await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


# ==========================================
# ۳. هندلرهای شروع و لغو عملیات
# ==========================================

@router_text.message(Command("text2file"))
@router_text.message(F.text == "📁 تبدیل متن به فایل")
async def cmd_start_text_to_file(message: Message, state: FSMContext) -> None:
    """ورود کاربر به مرحله دریافت متن و مقداردهی اولیه امن"""
    cancel_user_timer(message.from_user.id)
    await state.clear()
    
    await state.set_state(TextToFileStates.waiting_for_text)
    await state.update_data(text_parts=[])

    await message.answer(
        "📝 لطفاً متن مورد نظرتان را ارسال کنید:\n\n"
        "💡 اگر متن شما طولانی است و به چند پیام تقسیم می‌شود، به صورت پشت‌سرهم ارسال نمایید؛ "
        "ربات تمام بخش‌ها را به ترتیب دریافت و تجمیع خواهد کرد.\n\n"
        "⚠️ حداقل طول متن باید ۲۰ کاراکتر باشد."
    )


@router_text.callback_query(F.data == "t2f_cancel")
async def cb_cancel_process(callback: CallbackQuery, state: FSMContext) -> None:
    """لغو فرآیند توسط دکمه شیشه‌ای و پاک‌سازی وضعیت و تایمرها"""
    cancel_user_timer(callback.from_user.id)
    await state.clear()
    try:
        await callback.message.edit_text("❌ عملیات تبدیل متن به فایل لغو شد.")
    except TelegramAPIError:
        pass
    await callback.answer()


@router_text.message(Command("cancel"))
async def cmd_cancel_process(message: Message, state: FSMContext) -> None:
    """لغو فرآیند توسط دستور متنی"""
    cancel_user_timer(message.from_user.id)
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("هیچ عملیاتی در جریان نیست.")
        return
    await state.clear()
    await message.answer("❌ عملیات جاری لغو گردید.")


# ==========================================
# ۴. مدیریت دریافت متن با تایمر سکوت ۲ ثانیه (Debounce)
# ==========================================
async def _debounce_worker(user_id: int, chat_id: int, bot: Bot, state: FSMContext) -> None:
    """تسک پس‌زمینه برای شمارش معکوس ۲ ثانیه‌ای و تجمیع قطعی متن"""
    try:
        await asyncio.sleep(2.0)
    except asyncio.CancelledError:
        return
    finally:
        # حذف ایمن تسک از دیکشنری پس از اتمام یا لغو
        _user_debounce_tasks.pop(user_id, None)

    # بررسی اینکه کاربر در این فاصله وضعیت را لغو نکرده باشد
    current_state = await state.get_state()
    if current_state != TextToFileStates.waiting_for_text.state:
        return

    data = await state.get_data()
    parts = data.get("text_parts", [])
    full_text = "\n".join(parts).strip()

    # اعتبارسنجی حداقل ۲۰ کاراکتر
    if len(full_text) < 20:
        await state.update_data(text_parts=[])
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ مجموع متن ارسالی کمتر از ۲۰ کاراکتر است. لطفاً متنی با بیش از ۲۰ کاراکتر ارسال کنید."
        )
        return

    # ذخیره متن تجمیع‌شده و رفتن به وضعیت انتظار نام فایل
    await state.update_data(accumulated_text=full_text, text_parts=[])
    await state.set_state(TextToFileStates.waiting_for_filename)

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ متن شما با موفقیت دریافت شد ({len(full_text)} کاراکتر).\n\n"
            "🏷 اکنون یک نام برای فایل خود بفرستید:\n"
            "یا در صورت تمایل، دکمه «رد شدن» را بزنید تا نام پیش‌فرض انتخاب شود."
        ),
        reply_markup=get_skip_filename_keyboard(),
    )


@router_text.message(TextToFileStates.waiting_for_text, F.text)
async def process_incoming_text_chunk(message: Message, bot: Bot, state: FSMContext) -> None:
    """دریافت پارت‌های متن و تمدید مجدد تایمر سکوت"""
    user_id = message.from_user.id
    
    # ریست کردن تسک قبلی برای جلوگیری از اجرای هم‌زمان
    cancel_user_timer(user_id)

    data = await state.get_data()
    parts = data.get("text_parts", [])
    parts.append(message.text)
    await state.update_data(text_parts=parts)

    # ثبت تایمر جدید ۲ ثانیه‌ای
    task = asyncio.create_task(
        _debounce_worker(
            user_id=user_id,
            chat_id=message.chat.id,
            bot=bot,
            state=state,
        )
    )
    _user_debounce_tasks[user_id] = task


@router_text.message(TextToFileStates.waiting_for_text)
async def process_invalid_text_input(message: Message) -> None:
    """مدیریت ارسال استیکر، عکس یا فایل به جای متن در این مرحله"""
    await message.answer("⚠️ لطفاً فقط محتوای متنی ارسال کنید.")


# ==========================================
# ۵. مدیریت نام فایل (دریافت یا رد شدن)
# ==========================================
@router_text.callback_query(TextToFileStates.waiting_for_filename, F.data == "t2f_skip_name")
async def cb_skip_filename(callback: CallbackQuery, state: FSMContext) -> None:
    """اعمال نام پیش‌فرض و انتقال به صفحه انتخاب فرمت"""
    await state.update_data(filename="Delta_Bot")
    await state.set_state(TextToFileStates.choosing_format)
    await safe_edit_or_reply(
        callback.message,
        "🏷 نام فایل: <b>Delta_Bot</b> (پیش‌فرض)\n\n"
        "📂 لطفاً فرمت فایل خروجی را انتخاب کنید:",
        reply_markup=get_format_selection_keyboard(),
    )
    await callback.answer()


@router_text.message(TextToFileStates.waiting_for_filename, F.text)
async def process_custom_filename(message: Message, state: FSMContext) -> None:

    """تنظیم نام دلخواه فایل همراه با اسکیپ امن کاراکترهای HTML"""
    raw_name = message.text.strip()
    clean_name = TextToFileService.sanitize_filename(raw_name, default="Delta_Bot")
    await state.update_data(filename=clean_name)
    await state.set_state(TextToFileStates.choosing_format)

    safe_display_name = html.escape(clean_name)
    await message.answer(
        f"🏷 نام فایل: <b>{safe_display_name}</b>\n\n"
        "📂 لطفاً فرمت فایل خروجی را انتخاب کنید:",
        reply_markup=get_format_selection_keyboard(),
        parse_mode="HTML",
    )


@router_text.message(TextToFileStates.waiting_for_filename)
async def process_invalid_filename_input(message: Message) -> None:
    """مدیریت ورودی‌های نامعتبر در مرحله تعیین نام"""
    await message.answer(
        "⚠️ لطفاً نام فایل را به صورت متنی بفرستید، یا دکمه «رد شدن» را انتخاب کنید.",
        reply_markup=get_skip_filename_keyboard(),
    )


# ==========================================
# ۶. ساخت و ارسال فایل بر اساس فرمت انتخابی
# ==========================================
@router_text.callback_query(TextToFileStates.choosing_format, F.data.startswith("t2f_fmt_"))
async def cb_select_format_and_generate(callback: CallbackQuery, state: FSMContext) -> None:
    """تبدیل متن به فرمت هدف، ارسال فایل و پاک‌سازی کامل وضعیت"""
    # تغییر فوری وضعیت به processing_file برای جلوگیری از کلیک‌های تکراری و تداخل پردازش
    await state.set_state(TextToFileStates.processing_file)
    
    format_type = callback.data.replace("t2f_fmt_", "").strip().lower()
    data = await state.get_data()
    text = data.get("accumulated_text", "")
    filename = data.get("filename", "Delta_Bot")

    if not text:
        await state.clear()
        await safe_edit_or_reply(
            callback.message,
            "❌ متن مورد نظر یافت نشد یا منقضی شده است. لطفاً فرآیند را مجدداً آغاز کنید."
        )
        await callback.answer()
        return

    status_msg = await safe_edit_or_reply(callback.message, "⏳ در حال ساخت و آماده‌سازی فایل شما...")
    await callback.answer()

    created_path = None
    try:
        # اجرای ساخت فایل در ترد مجزا در صورت نیاز یا به شکل مستقیم از طریق سرویس
        created_path = text_service.create_file(
            text=text,
            filename=filename,
            format_type=format_type,
        )

        input_doc = FSInputFile(path=created_path, filename=created_path.name)
        
        safe_caption_name = html.escape(created_path.name[:60])
        await callback.message.answer_document(
            document=input_doc,
            caption=(
                f"✅ فایل شما با موفقیت ساخته شد.\n"
                f"📂 فرمت: <code>{format_type.upper()}</code>\n"
                f"🏷 نام: <code>{safe_caption_name}</code>"
            ),
            parse_mode="HTML",
        )

        # حذف پیام وضعیت پس از موفقیت
        try:
            await status_msg.delete()
        except TelegramAPIError:
            pass

        # پاک‌سازی قطعی وضعیت کاربر
        await state.clear()

    except TextConversionError as e:
        logger.error("خطای منطقی در تبدیل فایل کاربر %s: %s", callback.from_user.id, e)
        await safe_edit_or_reply(status_msg, f"❌ خطا در ساخت فایل: {html.escape(str(e))}")
        await state.clear()
    except Exception as e:
        logger.exception("خطای سیستمی ناشناخته در تبدیل فایل کاربر %s: %s", callback.from_user.id, e)
        await safe_edit_or_reply(status_msg, "❌ متأسفانه حین ساخت فایل خطای پیش‌بینی‌نشده‌ای رخ داد.")
        await state.clear()
    finally:
        # پاک‌سازی قطعی فایل موقت از هارد دیسک سرور
        TextToFileService.safe_remove(created_path)

        