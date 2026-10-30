import io
from aiogram import Router, F, Bot
from aiogram.types import Message, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from services.pdf_service import (
    PDFService,
    PDFPageLimitExceeded,
    PDFEncryptedError,
    PDFEmptyError
)
from services.limiter import DailyLimiter


# ==========================================
# ۱. کلاس وضعیت‌های (FSM) ویژه PDF
# ==========================================
class PDFStates(StatesGroup):
    waiting_for_pdf = State()


# ==========================================
# ۲. روتر و هندلرهای تلگرام
# ==========================================


router_pdf = Router()

@router_pdf.message(F.text == "📄 اسکن فایل pdf")
async def enter_pdf_mode(message: Message, state: FSMContext, limiter: DailyLimiter):
    """
    هندلر ورود به بخش PDF: بررسی موجودی ۳ سهمیه از همان ابتدا.
    """
    user_id = message.from_user.id
    remaining = limiter.get_remaining_quota(user_id)
    
    if remaining < 3:
        await message.answer(
            f"❌ سهمیه شما کافی نیست.\n"
            f"برای بخش PDF به حداقل ۳ سهمیه نیاز دارید، اما موجودی شما {remaining} درخواست است."
        )
        return

    await state.set_state(PDFStates.waiting_for_pdf)
    await message.answer(
        "📄 لطفاً فایل PDF خود را ارسال کنید.\n\n"
        "⚠️ قوانین:\n"
        "۱. حداکثر ۷۰ صفحه مجاز است.\n"
        "۲. فایل دیجیتالی متنی: رایگان (۰ سهمیه).\n"
        "۳. فایل اسکن‌شده/عکس: کسر ۳ سهمیه.\n\n"
        "(برای انصراف، دکمه بازگشت را بزنید یا کلمه /cancel را بفرستید)"
    )


@router_pdf.message(PDFStates.waiting_for_pdf, F.document)
async def handle_pdf_document(
    message: Message, 
    state: FSMContext, 
    bot: Bot, 
    pdf_service: PDFService, 
    limiter: DailyLimiter
):
    """
    هندلر دریافت و پردازش فایل PDF کاربر
    """
    user_id = message.from_user.id

    # بررسی مجدد سهمیه محکم‌کاری

    if limiter.get_remaining_quota(user_id) < 3:
        await message.answer("❌ سهمیه روزانه شما برای پردازش این فایل کافی نیست.")
        await state.clear()
        return

    # اعتبارسنجی پسوند فایل

    file_name = message.document.file_name or "document"
    if not file_name.lower().endswith('.pdf'):
        await message.answer("❌ فرمت فایل نامعتبر است. لطفاً فقط فایل PDF ارسال کنید.")
        return

    status_msg = await message.answer("⏳ در حال دانلود فایل از سرور تلگرام...")

    try:
        # دانلود فایل در رم (BytesIO)
        file_io = io.BytesIO()
        await bot.download(message.document.file_id, destination=file_io)
        file_bytes = file_io.getvalue()
        
        await status_msg.edit_text("⏳ در حال بررسی لایه‌ها و استخراج متن (لطفاً صبور باشید)...")
        
        # پردازش هیبریدی (فراخوانی متد کلاس PDFService)
        full_text, is_scanned = await pdf_service.process_pdf(file_bytes)
        
        # کسر ۳ سهمیه در صورت تصویری بودن با حلقه ساده
        if is_scanned:
            for _ in range(3):
                limiter.check_and_consume(user_id)
            usage_msg = "💡 این فایل اسکن‌شده تشخیص داده شد و ۳ سهمیه کسر گردید."


        else:

            usage_msg = "💡 این فایل دیجیتالی بود و هیچ سهمیه‌ای از شما کسر نشد (رایگان)."

        if not full_text.strip():
            await status_msg.edit_text("⚠️ هیچ متنی در این فایل یافت نشد!")
            await state.clear()
            return

        # ارسال خروجی هوشمند (متن در چت یا آپلود به صورت فایل txt)
        if len(full_text) < 4000:
            await status_msg.delete()
            await message.answer(f"{full_text}\n\n{usage_msg}")
        else:
            await status_msg.edit_text("⏳ متن بسیار طولانی است؛ در حال ساخت فایل خروجی...")
            text_file = BufferedInputFile(
                full_text.encode('utf-8'),
                filename=f"Extracted_{file_name}.txt"


            )
            
            await message.answer_document(
                document=text_file, 
                caption=f"✅ عملیات موفق.\nمتن به دلیل طولانی بودن در این فایل قرار گرفت.\n\n{usage_msg}"
            )
            await status_msg.delete()
            
    except PDFPageLimitExceeded as e:
        await status_msg.edit_text(f"❌ خطا: {str(e)}")
    except PDFEncryptedError as e:
        await status_msg.edit_text(f"🔒 خطا: {str(e)}")
    except PDFEmptyError as e:
        await status_msg.edit_text(f"❌ خطا: {str(e)}")
    except Exception as e:
        await status_msg.edit_text("❌ خطای ناشناخته‌ای هنگام پردازش رخ داد. لطفاً به پشتیبانی اطلاع دهید.")
        
    finally:
        await state.clear()

