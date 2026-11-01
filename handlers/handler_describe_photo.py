import io
import asyncio
import logging
from aiogram import Router, F, Bot, html
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest

from config import load_config
from services.describe_the_photo import ImageDescriptionService, ImageDescriptionError
from services.limiter import DailyLimiter

logger = logging.getLogger(__name__)

describe_router = Router()

# مقداردهی سرویس‌ها

config = load_config()
describe_service = ImageDescriptionService(api_keys=config.ai.api_keys)
daily_limiter = DailyLimiter(max_requests=15, window_seconds=86400)


# کلاس وضعیت‌های FSM برای توصیف تصویر

class DescribePhotoState(StatesGroup):
    waiting_for_photo = State()
    waiting_for_followup_question = State()


def get_cancel_describe_keyboard() -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای دکمه انصراف"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_describe")]
        ]
    )


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

            while len(line) > max_chars:
                chunks.append(line[:max_chars])
                line = line[max_chars:]

        current_chunk.append(line)
        current_length += len(line) + 1

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


async def _timeout_watcher(bot: Bot, chat_id: int, state: FSMContext, wait_seconds: int = 120) -> None:
    """تسک پس‌زمینه برای منقضی کردن حالت FSM پس از دو دقیقه"""
    try:
        await asyncio.sleep(wait_seconds)
        current_state = await state.get_state()
        if current_state == DescribePhotoState.waiting_for_followup_question.state:
            await state.clear()
            await bot.send_message(
                chat_id=chat_id,
                text="⌛️ مهلت ۲ دقیقه‌ای شما برای پرسیدن سوال تکمیلی درباره این عکس به پایان رسید."
            )
    except asyncio.CancelledError:
        # وقتی کاربر سوال می‌پرسد یا انصراف می‌دهد، تسک لغو می‌شود
        pass


# ۱. کلیک روی دکمه منوی اصلی
@describe_router.message(F.text == "🖼 توصیف عکس")
async def start_describe_process(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    remaining = daily_limiter.get_remaining_quota(user_id=user_id)

    if remaining <= 0:
        await message.answer(
            "⚠️ سهمیه روزانه شما (۱۵ درخواست در ۲۴ ساعت) به پایان رسیده است.\n"
            "لطفاً در بازه ۲۴ ساعت آینده دوباره امتحان کنید."
        )
        return

    await state.set_state(DescribePhotoState.waiting_for_photo)
    await message.answer(
        f"📸 لطفاً تصویر مورد نظر خود را برای توصیف با تمام جزئیات ارسال کنید.\n\n"
        f"📊 سهمیه باقی‌مانده شما امروز: {html.bold(str(remaining))} عکس\n"
        f"⚠️ لطفاً تصویر را به صورت فشرده (Photo) و تکی ارسال کنید.",
        reply_markup=get_cancel_describe_keyboard()
    )


# ۲. دکمه شیشه‌ای انصراف
@describe_router.callback_query(F.data == "cancel_describe")
async def cancel_describe(callback: CallbackQuery, state: FSMContext) -> None:
    # لغو تسک تایمر فعال (در صورت وجود)
    data = await state.get_data()
    timer_task: asyncio.Task | None = data.get("timer_task")
    if timer_task and not timer_task.done():
        timer_task.cancel()

    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()
        try:
            await callback.message.edit_text("❌ عملیات توصیف تصویر لغو شد.")
        except TelegramBadRequest:
            pass

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


# ۳. دریافت عکس و توصیف اولیه آن با هوش مصنوعی
@describe_router.message(DescribePhotoState.waiting_for_photo, F.photo)
async def process_photo_description(message: Message, state: FSMContext, bot: Bot) -> None:
    user_id = message.from_user.id

    if not daily_limiter.check_and_consume(user_id=user_id):
        await state.clear()
        await message.answer(
            "⚠️ سهمیه روزانه شما به اتمام رسیده است و این درخواست پردازش نشد."
        )
        return

    status_msg = await message.answer("⏳ در حال تحلیل و توصیف دقیق تصویر... لطفاً شکیبا باشید.")

    try:
        photo = message.photo[-1]
        image_stream = io.BytesIO()
        await bot.download(file=photo.file_id, destination=image_stream)
        image_bytes = image_stream.getvalue()

        # فراخوانی سرویس هوش مصنوعی جهت توصیف کامل عکس
        description = await describe_service.describe_image(image_bytes=image_bytes, mime_type="image/jpeg")

        try:
            await status_msg.delete()
        except TelegramBadRequest:
            pass

        # ارسال متن توصیف (پارت‌بندی در صورت طولانی بودن)
        chunks = split_text_into_chunks(description)
        total_parts = len(chunks)

        if total_parts == 1:
            await message.reply(chunks[0], parse_mode=None)
        else:
            for idx, part in enumerate(chunks, 1):
                header = f"📄 پارت {idx} از {total_parts}:\n\n"
                await message.reply(header + part, parse_mode=None)

        # تغییر وضعیت به انتظار برای سوال تکمیلی
        await state.set_state(DescribePhotoState.waiting_for_followup_question)

        # شروع تایمر ۲ دقیقه‌ای در پس‌زمینه
        timer_task = asyncio.create_task(
            _timeout_watcher(bot=bot, chat_id=message.chat.id, state=state, wait_seconds=120)
        )

        # ذخیره تصویر، توصیف قبلی و تسک تایمر در استیت FSM
        await state.update_data(
            image_bytes=image_bytes,
            initial_description=description,
            timer_task=timer_task
        )

        await message.answer(
            "💬 کاربر گرامی، شما به مدت ۲ دقیقه زمان دارید تا فقط یک سوال تکمیلی "
            "جهت شرح جزئیات دقیق‌تر برای همین عکس بپرسید، یا در غیر این‌صورت دکمه انصراف را لمس کنید.",
            reply_markup=get_cancel_describe_keyboard()
        )

    except ImageDescriptionError as err:
        await state.clear()
        try:
            await status_msg.edit_text(f"❌ {err}")
        except TelegramBadRequest:
            await message.reply(f"❌ {err}")

    except Exception as err:
        await state.clear()
        logger.exception(f"خطای هندلر در توصیف تصویر: {err}")
        try:
            await status_msg.edit_text("❌ متأسفانه در حین تحلیل تصویر خطایی رخ داد.")
        except TelegramBadRequest:
            await message.reply("❌ متأسفانه در حین تحلیل تصویر خطایی رخ داد.")


# ۴. دریافت سوال تکمیلی کاربر درباره تصویر
@describe_router.message(DescribePhotoState.waiting_for_followup_question, F.text)
async def process_followup_question(message: Message, state: FSMContext) -> None:
    data = await state.get_data()

    # لغو فوری تایمر ۲ دقیقه‌ای
    timer_task: asyncio.Task | None = data.get("timer_task")
    if timer_task and not timer_task.done():
        timer_task.cancel()

    image_bytes = data.get("image_bytes")
    initial_description = data.get("initial_description")

    # پاک کردن وضعیت FSM طبق سناریو (فقط یک سوال مجاز است)
    await state.clear()

    if not image_bytes or not initial_description:
        await message.reply("⚠️ اطلاعات تصویر منقضی شده است. لطفاً فرآیند را مجدداً شروع کنید.")
        return

    status_msg = await message.answer("🔍 در حال بررسی سوال و بررسی مجدد تصویر...")

    try:
        user_question = message.text.strip()
        answer = await describe_service.answer_followup(
            image_bytes=image_bytes,
            initial_description=initial_description,
            user_question=user_question,
            mime_type="image/jpeg"
        )

        try:
            await status_msg.delete()
        except TelegramBadRequest:
            pass

        chunks = split_text_into_chunks(answer)
        total_parts = len(chunks)

        if total_parts == 1:
            await message.reply(chunks[0], parse_mode=None)
        else:
            for idx, part in enumerate(chunks, 1):
                header = f"📄 پارت {idx} از {total_parts}:\n\n"
                await message.reply(header + part, parse_mode=None)

    except ImageDescriptionError as err:
        try:
            await status_msg.edit_text(f"❌ {err}")
        except TelegramBadRequest:
            await message.reply(f"❌ {err}")

    except Exception as err:
        logger.exception(f"خطای هندلر در پاسخ به سوال تکمیلی: {err}")
        try:
            await status_msg.edit_text("❌ در پاسخ به سوال شما خطایی رخ داد.")
        except TelegramBadRequest:
            await message.reply("❌ در پاسخ به سوال شما خطایی رخ داد.")


# ۵. هندلر ورودی‌های اشتباه در فاز انتظار برای عکس
@describe_router.message(DescribePhotoState.waiting_for_photo)
async def invalid_photo_input_handler(message: Message) -> None:
    await message.reply(
        "⚠️ ورودی نامعتبر است! لطفاً فقط یک تصویر فشرده (Photo) ارسال کنید یا دکمه انصراف را لمس کنید.",
        reply_markup=get_cancel_describe_keyboard()
    )


# ۶. هندلر ورودی‌های اشتباه در فاز سوال تکمیلی (ارسال فایل به جای متن سوال)
@describe_router.message(DescribePhotoState.waiting_for_followup_question)
async def invalid_followup_input_handler(message: Message) -> None:
    await message.reply(
        "⚠️ لطفاً سوال تکمیلی خود را صرفاً به صورت متن تایپ و ارسال کنید، یا دکمه انصراف را لمس کنید.",
        reply_markup=get_cancel_describe_keyboard()
    )

