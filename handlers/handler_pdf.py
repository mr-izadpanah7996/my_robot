import io
import math
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

# مقداردهی سرویس‌ها (سقف ۳۰۰ صفحه - پردازش یکپارچه با Gemini Batch OCR)
config = load_config()
ocr_service = OCRService(api_keys=config.ai.api_keys)
daily_limiter = DailyLimiter(max_requests=15, window_seconds=86400)
pdf_service = PDFService(
    ocr_service=ocr_service,
    max_pages=300,
    batch_size=10,
)


def calculate_affordable_pages(remaining_quota: int) -> int:
    """محاسبه حداکثر تعداد صفحاتی که کاربر با سهمیه فعلی خود می‌تواند پردازش کند (هر ۱۰۰ صفحه = ۶ سهمیه)"""
    if remaining_quota <= 0:
        return 0
    return (remaining_quota * 100) // 6


def calculate_required_quota(pages_count: int) -> int:
    """محاسبه دقیق سهمیه مصرف‌شده بر اساس تعداد صفحات پردازش‌شده (گرد کردن به بالا)"""
    if pages_count <= 0:
        return 0
    return math.ceil((pages_count * 6) / 100)


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

    if remaining < 1:
        await message.answer(
            "⚠️ سهمیه روزانه شما به پایان رسیده است و در حال حاضر امکان پردازش فایل PDF را ندارید.\n"
            "لطفاً پس از بازنشانی سهمیه روزانه مجدداً تلاش کنید."
        )
        return

    affordable_pages = min(calculate_affordable_pages(remaining), 300)

    await state.set_state(PDFState.waiting_for_pdf)
    await message.answer(
        f"📄 لطفاً فایل PDF مورد نظر خود را ارسال کنید.\n\n"
        f"📊 سهمیه باقی‌مانده شما: {html.bold(str(remaining))} درخواست "
        f"(قابل استفاده برای حداکثر {html.bold(str(affordable_pages))} صفحه)\n\n"
        f"⚠️ قوانین پردازش:\n"
        f"۱. حداکثر سقف مجاز هر فایل: ۳۰۰ صفحه (حداکثر ۲۰ مگابایت)\n"
        f"۲. تعرفه مصرف سهمیه: به ازای هر ۱۰۰ صفحه، ۶ سهمیه روزانه (محاسبه دقیق بر اساس تعداد صفحات)\n"
        f"۳. در صورتی که تعداد صفحات فایل بیشتر از سهمیه فعلی شما باشد، فقط به اندازه سهمیه شما از ابتدای فایل پردازش می‌شود.",
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

    file_name = message.document.file_name or "document.pdf"
    if not file_name.lower().endswith(".pdf"):
        await message.answer(
            "❌ فرمت فایل نامعتبر است! لطفاً فقط فایل با پسوند PDF ارسال کنید.",
            reply_markup=get_cancel_ocr_keyboard()
        )
        return

    if message.document.file_size and message.document.file_size > 20 * 1024 * 1024:
        await message.answer(
            "❌ حجم فایل بیش از سقف مجاز تلگرام (۲۰ مگابایت) است.\n"
            "لطفاً فایل کم‌حجم‌تری ارسال کنید یا دکمه انصراف را بزنید.",
            reply_markup=get_cancel_ocr_keyboard()
        )
        return

    await state.clear()

    remaining = daily_limiter.get_remaining_quota(user_id=user_id)
    if remaining < 1:
        await message.answer("⚠️ سهمیه روزانه شما برای پردازش این فایل کافی نیست.")
        return

    max_allowed_pages = calculate_affordable_pages(remaining)

    status_msg = await message.answer("⏳ در حال دانلود و آماده‌سازی فایل PDF...")
    file_stream = io.BytesIO()

    try:
        await bot.download(file=message.document.file_id, destination=file_stream)
        file_bytes = file_stream.getvalue()

        try:
            await status_msg.edit_text(
                "⏳ در حال استخراج هوشمند محتوا با هوش مصنوعی (در فایل‌های حجیم ممکن است کمی زمان‌بر باشد، لطفاً شکیبا باشید)..."
            )
        except TelegramBadRequest:
            pass

        full_text, processed_pages, total_pages = await pdf_service.process_pdf(
            file_bytes=file_bytes,
            max_allowed_pages=max_allowed_pages,
        )

        if not full_text.strip():
            await status_msg.edit_text("⚠️ هیچ متن قابل تشخیصی در این سند یافت نشد.")
            return

        # محاسبه و کسر دقیق سهمیه بر اساس تعداد صفحات واقعی پردازش‌شده
        used_quota = min(calculate_required_quota(processed_pages), remaining)
        for _ in range(used_quota):
            daily_limiter.check_and_consume(user_id=user_id)

        if processed_pages < total_pages:
            skipped_pages = total_pages - processed_pages
            usage_notice = (
                f"💡 به دلیل محدودیت سهمیه روزانه، {processed_pages} صفحه اول از کل {total_pages} صفحه پردازش شد "
                f"({skipped_pages} صفحه پایانی پردازش نشد) و {used_quota} سهمیه روزانه کسر گردید."
            )
        else:
            usage_notice = (
                f"💡 کل {processed_pages} صفحه فایل با موفقیت پردازش شد و {used_quota} سهمیه روزانه کسر گردید."
            )

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

    