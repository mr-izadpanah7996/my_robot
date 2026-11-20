import io
import logging
import math

from aiogram import Bot, F, Router, html
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from config import load_config
from keyboards.inline import get_cancel_ocr_keyboard
from services.limiter import DailyLimiter
from services.ocr import OCRError, OCRService
from services.pdf_service import (
    PDFEmptyError,
    PDFEncryptedError,
    PDFPageLimitExceeded,
    PDFService,
)

logger = logging.getLogger(__name__)

pdf_router = Router()

# مقداردهی سرویس‌ها (سقف ۳۰۰ صفحه - دسته‌های ۴ صفحه‌ای با Fallback تک‌صفحه‌ای)
config = load_config()
ocr_service = OCRService(api_keys=config.ai.api_keys)
daily_limiter = DailyLimiter(max_requests=15, window_seconds=86400)

PAGES_PER_QUOTA = 4
MAX_PDF_PAGES = 300
MAX_PDF_FILE_SIZE = 20 * 1024 * 1024  # ۲۰ مگابایت

pdf_service = PDFService(
    ocr_service=ocr_service,
    max_pages=MAX_PDF_PAGES,
    batch_size=PAGES_PER_QUOTA,
)

# جلوگیری از پردازش هم‌زمان چند فایل PDF توسط یک کاربر
_active_pdf_users: set[int] = set()


def calculate_affordable_pages(remaining_quota: int) -> int:
    """محاسبه حداکثر تعداد صفحاتی که کاربر با سهمیه فعلی خود می‌تواند پردازش کند (هر ۱ سهمیه = ۴ صفحه)"""
    if remaining_quota <= 0:
        return 0
    return remaining_quota * PAGES_PER_QUOTA


def calculate_required_quota(pages_count: int) -> int:
    """محاسبه دقیق سهمیه مصرف‌شده بر اساس تعداد صفحات پردازش‌شده (گرد کردن به بالا)"""
    if pages_count <= 0:
        return 0
    return math.ceil(pages_count / PAGES_PER_QUOTA)


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
    if not message.from_user:
        return

    user_id = message.from_user.id
    if user_id in _active_pdf_users:
        await message.answer("⏳ فایل PDF قبلی شما هنوز در حال اسکن است. لطفاً تا پایان پردازش صبر کنید.")
        return

    remaining = daily_limiter.get_remaining_quota(user_id=user_id)

    if remaining < 1:
        await message.answer(
            "⚠️ سهمیه روزانه شما به پایان رسیده است و در حال حاضر امکان پردازش فایل PDF را ندارید.\n"
            "لطفاً پس از بازنشانی سهمیه روزانه مجدداً تلاش کنید."
        )
        return

    affordable_pages = min(calculate_affordable_pages(remaining), MAX_PDF_PAGES)

    await state.set_state(PDFState.waiting_for_pdf)
    await message.answer(
        f"📄 لطفاً فایل PDF خود را ارسال کنید تا متن آن با دقت بالا استخراج شود.\n\n"
        f"📊 اعتبار فعلی شما: {html.bold(str(remaining))} سهمیه "
        f"(مناسب برای اسکن تا {html.bold(str(affordable_pages))} صفحه)\n\n"
        f"📌 راهنما:\n"
        f"• حداکثر تعداد صفحات مجاز هر فایل: {MAX_PDF_PAGES} صفحه (حداکثر حجم: ۲۰ مگابایت)\n"
        f"• میزان مصرف اعتبار: به ازای هر {PAGES_PER_QUOTA} صفحه، ۱ سهمیه کسر می‌شود (متناسب با تعداد صفحات پردازش‌شده).\n"
        f"• اگر تعداد صفحات فایل شما بیشتر از اعتبار فعلی‌تان باشد، ربات به اندازه اعتبار شما از ابتدای فایل را اسکن می‌کند.",
        reply_markup=get_cancel_ocr_keyboard(),
    )


# ==========================================
# ۳. دکمه شیشه‌ای انصراف
# ==========================================
@pdf_router.callback_query(F.data == "cancel_ocr")
async def cancel_pdf_process(callback: CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()
        if callback.message:
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
    if not message.from_user or not message.document:
        return

    user_id = message.from_user.id
    if user_id in _active_pdf_users:
        await message.answer("⏳ فایل قبلی شما هنوز در حال پردازش است. لطفاً کمی شکیبا باشید.")
        return

    file_name = message.document.file_name or "document.pdf"
    if not file_name.lower().endswith(".pdf"):
        await message.answer(
            "❌ فرمت فایل نامعتبر است! لطفاً فقط فایل با پسوند PDF ارسال کنید.",
            reply_markup=get_cancel_ocr_keyboard(),
        )
        return

    if message.document.file_size and message.document.file_size > MAX_PDF_FILE_SIZE:
        await message.answer(
            "❌ حجم این فایل بیشتر از سقف مجاز (۲۰ مگابایت) است.\n"
            "لطفاً فایل کم‌حجم‌تری ارسال کنید یا دکمه انصراف را بزنید.",
            reply_markup=get_cancel_ocr_keyboard(),
        )
        return

    await state.clear()

    remaining = daily_limiter.get_remaining_quota(user_id=user_id)
    if remaining < 1:
        await message.answer("⚠️ سهمیه روزانه شما برای پردازش این فایل کافی نیست.")
        return

    max_allowed_pages = calculate_affordable_pages(remaining)

    _active_pdf_users.add(user_id)
    status_msg = await message.answer("⏳ در حال دانلود و آماده‌سازی فایل PDF...")
    file_stream = io.BytesIO()

    try:
        await bot.download(file=message.document.file_id, destination=file_stream)
        file_bytes = file_stream.getvalue()

        try:
            await status_msg.edit_text(
                "⏳ در حال اسکن و استخراج هوشمند صفحات...\n"
                "(در فایل‌های چندصفحه‌ای برای حفظ دقت متن ممکن است کمی زمان‌بر باشد، لطفاً شکیبا باشید)"
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
                f"💡 به دلیل محدودیت سهمیه روزانه، {processed_pages} صفحه اول از کل {total_pages} صفحه اسکن شد "
                f"({skipped_pages} صفحه پایانی پردازش نشد) و {used_quota} سهمیه روزانه کسر گردید."
            )
        else:
            usage_notice = (
                f"💡 کل {processed_pages} صفحه فایل با موفقیت اسکن شد و {used_quota} سهمیه روزانه کسر گردید."
            )

        if len(full_text) < 3800:
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
            caption_text = f"✅ متن استخراج‌شده فایل پی‌دی‌اف\n\n{usage_notice}"[:1024]
            await message.reply_document(
                document=output_file,
                caption=caption_text,
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
        _active_pdf_users.discard(user_id)
        file_stream.close()


# ==========================================
# ۵. هندلر ورودی‌های اشتباه
# ==========================================
@pdf_router.message(PDFState.waiting_for_pdf)
async def invalid_pdf_input(message: Message) -> None:
    await message.reply(
        "⚠️ لطفاً فقط یک فایل PDF ارسال کنید یا دکمه انصراف را لمس کنید.",
        reply_markup=get_cancel_ocr_keyboard(),
    )

    