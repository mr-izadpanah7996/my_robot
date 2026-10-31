import io
import logging
from aiogram import Router, F, Bot, html
from aiogram.types import Message, BufferedInputFile, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest

from config import load_config
from keyboards.inline import get_cancel_ocr_keyboard
from services.ocr import OCRService, OCRError
from services.limiter import DailyLimiter
from services.pdf_service import (
    PDFService,
    PDFPageLimitExceeded,
    PDFEncryptedError,
    PDFEmptyError,
)

logger = logging.getLogger(__name__)

pdf_router = Router()

# مقداردهی سرویس‌ها (هماهنگ با سقف جدید ۳۰۰ صفحه)
config = load_config()
ocr_service = OCRService(api_keys=config.ai.api_keys)
daily_limiter = DailyLimiter(max_requests=15, window_seconds=86400)
pdf_service = PDFService(ocr_service=ocr_service, max_pages=300)


# ==========================================
# ۱. کلاس وضعیت‌های FSM برای بخش PDF
# ==========================================
class PDFState(StatesGroup):
    waiting_for_pdf = State()


# ==========================================
# ۲. هندلر ورود به بخش PDF
# ==========================================
@pdf_router.message(F.text == "📄 اسکن فایل pdf")
async def start_pdf_process(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    remaining = daily_limiter.get_remaining_quota(user_id=user_id)

    # بررسی حداقل سهمیه لازم برای شروع فرآیند پردازش
    if remaining < 3:
        await message.answer(
            f"⚠️ سهمیه روزانه شما برای این بخش کافی نیست.\n"
            f"برای پردازش PDF حداقل به ۳ سهمیه نیاز است، اما سهمیه فعلی شما: {html.bold(str(remaining))} است."
        )
        return

    await state.set_state(PDFState.waiting_for_pdf)
    await message.answer(
        f"📄 لطفاً فایل PDF مورد نظر خود را ارسال کنید.\n\n"
        f"📊 سهمیه باقی‌مانده شما: {html.bold(str(remaining))} درخواست\n\n"
        f"⚠️ قوانین پردازش:\n"
        f"۱. حداکثر سقف مجاز: ۳۰۰ صفحه (حداکثر ۲۰ مگابایت)\n"
        f"۲. فایل‌های متنی دیجیتال: کاملاً رایگان (بدون کسر سهمیه)\n"
        f"۳. فایل‌های اسکن‌شده تصویری: کسر ۳ سهمیه",
        reply_markup=get_cancel_ocr_keyboard()
    )


# ==========================================
# ۳. دکمه شیشه‌ای انصراف
# ==========================================
@pdf_router.callback_query(F.data == "cancel_ocr")
async def cancel_pdf_process(callback: CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()
        try:
            await callback.message.edit_text("❌ عملیات اسکن فایل PDF لغو شد.")
        except TelegramBadRequest:
            pass
            
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


# ==========================================
# ۴. هندلر دریافت و پردازش فایل PDF
# ==========================================
@pdf_router.message(PDFState.waiting_for_pdf, F.document)
async def process_pdf_document(message: Message, state: FSMContext, bot: Bot) -> None:
    user_id = message.from_user.id

    # ۱. بررسی پسوند فایل (قبل از خارج کردن کاربر از استیت)
    file_name = message.document.file_name or "document.pdf"
    if not file_name.lower().endswith(".pdf"):
        await message.answer(
            "❌ فرمت فایل نامعتبر است! لطفاً فقط فایل با پسوند PDF ارسال کنید.",
            reply_markup=get_cancel_ocr_keyboard()
        )
        return

    # ۲. بررسی محدودیت حجم دانلود بات تلگرام (۲۰ مگابایت)
    if message.document.file_size and message.document.file_size > 20 * 1024 * 1024:
        await message.answer(
            "❌ حجم فایل بیش از سقف مجاز تلگرام (۲۰ مگابایت) است.\n"
            "لطفاً فایل کم‌حجم‌تری ارسال کنید یا دکمه انصراف را بزنید.",
            reply_markup=get_cancel_ocr_keyboard()
        )
        return

    # خروج از استیت برای جلوگیری از ارسال فایل‌های مکرر در صف پردازش
    await state.clear()

    # ۳. بررسی مجدد سهمیه
    if daily_limiter.get_remaining_quota(user_id=user_id) < 3:
        await message.answer("⚠️ سهمیه روزانه شما برای پردازش این فایل کافی نیست.")
        return

    status_msg = await message.answer("⏳ در حال دانلود و ارزیابی لایه‌های فایل PDF...")
    file_stream = io.BytesIO()

    try:
        # دانلود فایل به شکل جریانی در حافظه RAM
        await bot.download(file=message.document.file_id, destination=file_stream)
        file_bytes = file_stream.getvalue()

        try:
            await status_msg.edit_text(
                "⏳ در حال استخراج محتوا (در فایل‌های اسکن‌شده یا حجیم ممکن است کمی زمان‌بر باشد، لطفاً شکیبا باشید)..."
            )
        except TelegramBadRequest:
            pass

        # پردازش هیبریدی و ناهمگام با متد کلاس سرویس
        full_text, is_scanned = await pdf_service.process_pdf(file_bytes=file_bytes)

        # بررسی خالی نبودن خروجی قبل از کسر سهمیه
        if not full_text.strip():
            await status_msg.edit_text("⚠️ هیچ متن قابل تشخیصی در این سند یافت نشد.")
            return

        # مدیریت کسر سهمیه
        if is_scanned:
            for _ in range(3):
                daily_limiter.check_and_consume(user_id=user_id)
            usage_notice = "💡 این سند اسکن‌شده بود و ۳ سهمیه روزانه کسر گردید."
        else:
            usage_notice = "💡 این سند دارای لایه متنی بود و رایگان (بدون کسر سهمیه) پردازش شد."

        # ارسال بر اساس حجم متن (پیام چت یا فایل .txt)
        if len(full_text) < 4000:
            try:
                await status_msg.delete()
            except TelegramBadRequest:
                pass
            await message.reply(f"{full_text}\n\n{usage_notice}", parse_mode=None)
        else:
            try:
                await status_msg.edit_text("⏳ متن طولانی است؛ در حال آماده‌سازی فایل متنی...")
            except TelegramBadRequest:
                pass

            clean_name = file_name[:-4] if file_name.lower().endswith(".pdf") else file_name
            output_file = BufferedInputFile(
                file=full_text.encode("utf-8"),
                filename=f"Extracted_{clean_name}.txt",
            )
            await message.reply_document(
                document=output_file,
                caption=f"✅ متن استخراج‌شده فایل پی‌دی‌اف\n\n{usage_notice}",
            )
            try:
                await status_msg.delete()
            except TelegramBadRequest:
                pass

    except PDFPageLimitExceeded as err:
        try:
            await status_msg.edit_text(f"❌ {err}")
        except TelegramBadRequest:
            await message.reply(f"❌ {err}")

    except (PDFEncryptedError, PDFEmptyError) as err:
        try:
            await status_msg.edit_text(f"❌ {err}")
        except TelegramBadRequest:
            await message.reply(f"❌ {err}")

    except OCRError as err:
        logger.warning(f"خطای سرویس OCR در پردازش PDF کاربر {user_id}: {err}")
        try:
            await status_msg.edit_text(f"❌ خطا در ارتباط با هوش مصنوعی: {err}")
        except TelegramBadRequest:
            await message.reply(f"❌ خطا در ارتباط با هوش مصنوعی: {err}")

    except (TimeoutError, ConnectionError) as err:
        logger.error(f"خطای شبکه هنگام دانلود یا ارسال PDF: {err}")
        try:
            await status_msg.edit_text("❌ خطا در ارتباط شبکه هنگام دریافت فایل. لطفاً مجدداً تلاش کنید.")
        except TelegramBadRequest:
            await message.reply("❌ خطا در ارتباط شبکه هنگام دریافت فایل. لطفاً مجدداً تلاش کنید.")

    except Exception as err:
        logger.exception(f"خطای پیش‌بینی‌نشده در پردازش فایل PDF: {err}")
        try:
            await status_msg.edit_text("❌ متأسفانه در فرآیند استخراج متن PDF خطایی رخ داد.")
        except TelegramBadRequest:
            await message.reply("❌ متأسفانه در فرآیند استخراج متن PDF خطایی رخ داد.")

    finally:
        # آزادسازی بافر دانلود از حافظه رم سرور
        file_stream.close()


# ==========================================
# ۵. هندلر ورودی‌های اشتباه
# ==========================================
@pdf_router.message(PDFState.waiting_for_pdf)
async def invalid_pdf_input(message: Message) -> None:
    await message.reply(
        "⚠️ لطفاً فقط یک فایل PDF ارسال کنید یا دکمه انصراف را لمس کنید.",
        reply_markup=get_cancel_ocr_keyboard()
    )

    