import io
import logging
from aiogram import Router, F, Bot, html
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest

from config import load_config
from keyboards.inline import get_cancel_ocr_keyboard
from services.ocr import OCRService, OCRError
from services.limiter import DailyLimiter

logger = logging.getLogger(__name__)

ocr_router = Router()

# مقداردهی سرویس‌ها


config = load_config()
ocr_service = OCRService(api_keys=config.ai.api_keys)
daily_limiter = DailyLimiter(max_requests=15, window_seconds=86400)


class OCRState(StatesGroup):
    waiting_for_photo = State()


def split_text_into_chunks(text: str, max_chars: int = 3800) -> list[str]:


    """شکستن متون طولانی به چند پارت کمتر از محدودیت تلگرام بر اساس پاراگراف و خطوط"""


    if len(text) <= max_chars:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_chunk = []
    current_length = 0

    for line in lines:
        line_len = len(line) + 1
        if current_length + line_len > max_chars:
            if current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_length = 0
            
            # اگر طول یک سطر به تنهایی از سقف مجاز بیشتر بود


            while len(line) > max_chars:
                chunks.append(line[:max_chars])
                line = line[max_chars:]

        current_chunk.append(line)
        current_length += len(line) + 1

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


# ۱. کلیک روی دکمه منوی اصلی


@ocr_router.message(F.text == "🖼 تبدیل عکس به متن")
async def start_ocr_process(message: Message, state: FSMContext) -> None:


    user_id = message.from_user.id
    remaining = daily_limiter.get_remaining_quota(user_id=user_id)

    if remaining <= 0:
        await message.answer(
            "⚠️ سهمیه روزانه شما (۱۵ درخواست در ۲۴ ساعت) به پایان رسیده است.\n"
            "لطفاً در بازه ۲۴ ساعت آینده دوباره امتحان کنید."
        )
        return

    await state.set_state(OCRState.waiting_for_photo)
    await message.answer(
        f"📸 لطفاً تصویر مورد نظر خود را ارسال کنید.\n\n"
        f"📊 سهمیه باقی‌مانده شما امروز: {html.bold(str(remaining))} عکس\n"
        f"⚠️ لطفاً عکس را به صورت تکی ارسال کنید.",
        reply_markup=get_cancel_ocr_keyboard()
    )


# ۲. دکمه شیشه‌ای انصراف


@ocr_router.callback_query(F.data == "cancel_ocr")
async def cancel_ocr(callback: CallbackQuery, state: FSMContext) -> None:


    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()
        try:
            await callback.message.edit_text("❌ عملیات تبدیل عکس به متن لغو شد.")
        except TelegramBadRequest:
            pass  # اگر کاربر پیام را پاک کرده بود، ارور نده
    
    # همواره جواب کالبک داده شود تا ربات در حالت لودینگ نماند

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


# ۳. دریافت عکس فشرده و پردازش آن


@ocr_router.message(OCRState.waiting_for_photo, F.photo)
async def process_photo(message: Message, state: FSMContext, bot: Bot) -> None:


    user_id = message.from_user.id

    # پاک کردن وضعیت FSM جهت جلوگیری از پردازش سایر عکس‌های آلبومی


    await state.clear()

    # بررسی مجدد سهمیه و کسر یک واحد


    if not daily_limiter.check_and_consume(user_id=user_id):
        await message.answer(
            "⚠️ سهمیه روزانه شما به اتمام رسیده است و این درخواست پردازش نشد."
        )
        return

    status_msg = await message.answer("⏳ در حال استخراج متن از تصویر... لطفاً شکیبا باشید.")

    try:
        # انتخاب بالاترین کیفیت تصویر ارسال‌شده


        photo = message.photo[-1]
        
        # دانلود فایل در حافظه موقت (RAM)


        image_stream = io.BytesIO()
        await bot.download(file=photo.file_id, destination=image_stream)
        image_bytes = image_stream.getvalue()

# فراخوانی سرویس OCR ناهمگام


        extracted_text = await ocr_service.extract_text(image_bytes=image_bytes, mime_type="image/jpeg")

        # تلاش برای حذف پیام وضعیت (اگر توسط کاربر پاک نشده باشد)


        try:
            await status_msg.delete()
        except TelegramBadRequest:
            pass

        # پارت‌بندی متن‌های طولانی


        chunks = split_text_into_chunks(extracted_text)
        total_parts = len(chunks)

        # ارسال خام متن با parse_mode=None برای جلوگیری از خطای فرمت‌بندی تگ‌ها


        if total_parts == 1:
            await message.reply(chunks[0], parse_mode=None)
        else:
            for idx, part in enumerate(chunks, 1):
                header = f"📄 پارت {idx} از {total_parts}:\n\n"
                await message.reply(header + part, parse_mode=None)

    except OCRError as err:
        try:
            await status_msg.edit_text(f"❌ {err}")
        except TelegramBadRequest:
            await message.reply(f"❌ {err}")
            
    except Exception as err:
        logger.exception(f"خطای هندلر در پردازش تصویر: {err}")
        try:
            await status_msg.edit_text("❌ متأسفانه در حین پردازش تصویر خطایی رخ داد.")
        except TelegramBadRequest:
            await message.reply("❌ متأسفانه در حین پردازش تصویر خطایی رخ داد.")


# ۴. هندلر ورودی‌های اشتباه (متن، استیکر، صدا و ...)


@ocr_router.message(OCRState.waiting_for_photo)
async def invalid_input_handler(message: Message) -> None:


    await message.reply(
        "⚠️ ورودی نامعتبر است! لطفاً فقط یک تصویر فشرده (Photo) ارسال کنید یا دکمه انصراف را لمس کنید.",
        reply_markup=get_cancel_ocr_keyboard()
    )

