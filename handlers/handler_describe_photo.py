import io
import asyncio
import logging
from aiogram import Router, F, Bot
from aiogram.filters import StateFilter
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

# نگهداری امن تسک‌های تایمر کاربران در حافظه (جلوگیری از خطای سریال‌سازی FSM)
_active_timers: dict[int, asyncio.Task] = {}

FOLLOWUP_TIMEOUT_SECONDS = 180  # ۳ دقیقه


# کلاس وضعیت‌های FSM برای توصیف تصویر (فقط مرحله سوال تکمیلی)
class DescribePhotoState(StatesGroup):
    waiting_for_followup_question = State()


def get_cancel_describe_keyboard() -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای دکمه انصراف"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_describe")]
        ]
    )


def _cancel_user_timer(user_id: int) -> None:
    """لغو و حذف تسک تایمر فعال کاربر در صورت وجود"""
    task = _active_timers.pop(user_id, None)
    if task and not task.done():
        task.cancel()


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


async def _timeout_watcher(
    bot: Bot,
    chat_id: int,
    user_id: int,
    state: FSMContext,
    wait_seconds: int = FOLLOWUP_TIMEOUT_SECONDS,
) -> None:
    """تسک پس‌زمینه برای منقضی کردن حالت FSM پس از ۳ دقیقه"""
    try:
        await asyncio.sleep(wait_seconds)
        current_state = await state.get_state()
        if current_state == DescribePhotoState.waiting_for_followup_question.state:
            await state.clear()
            await bot.send_message(
                chat_id=chat_id,
                text="⌛️ مهلت ۳ دقیقه‌ای شما برای پرسیدن سوال تکمیلی درباره این عکس به پایان رسید."
            )
    except asyncio.CancelledError:
        pass
    finally:
        _active_timers.pop(user_id, None)


# ۱. دریافت مستقیم یا فوروارد عکس زمانی که کاربر در هیچ وضعیت (State) فعالی نیست

@describe_router.message(StateFilter(None), F.photo)
async def process_photo_description(message: Message, state: FSMContext, bot: Bot) -> None:
    user_id = message.from_user.id

    if not daily_limiter.check_and_consume(user_id=user_id):
        await message.answer(
            "⚠️ سهمیه روزانه شما (۱۵ تصویر در ۲۴ ساعت) به پایان رسیده است.\n"
            "لطفاً در بازه ۲۴ ساعت آینده دوباره امتحان کنید."
        )
        return

    remaining = daily_limiter.get_remaining_quota(user_id=user_id)
    status_msg = await message.answer("⏳ در حال تحلیل و توصیف دقیق تصویر... لطفاً شکیبا باشید.")

    try:
        photo = message.photo[-1]
        image_stream = io.BytesIO()
        await bot.download(file=photo.file_id, destination=image_stream)
        image_bytes = image_stream.getvalue()

        # فراخوانی سرویس هوش مصنوعی جهت توصیف کامل عکس
        description = await describe_service.describe_image(
            image_bytes=image_bytes,
            mime_type="image/jpeg",
        )

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

        # لغو تایمر احتمالی قبلی و شروع تایمر ۳ دقیقه‌ای جدید
        _cancel_user_timer(user_id)
        _active_timers[user_id] = asyncio.create_task(
            _timeout_watcher(
                bot=bot,
                chat_id=message.chat.id,
                user_id=user_id,
                state=state,
                wait_seconds=FOLLOWUP_TIMEOUT_SECONDS,
            )
        )

        # ذخیره تصویر و توصیف قبلی در استیت FSM
        await state.update_data(
            image_bytes=image_bytes,
            initial_description=description,
        )

        await message.answer(
            f"💬 شما به مدت ۳ دقیقه زمان دارید تا فقط یک سوال تکمیلی "
            f"جهت شرح جزئیات دقیق‌تر برای همین عکس بپرسید، یا در غیر این‌صورت دکمه انصراف را لمس کنید.\n\n"
            f"📊 سهمیه باقی‌مانده امروز: {remaining} تصویر",
            reply_markup=get_cancel_describe_keyboard(),
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


# ۲. دکمه شیشه‌ای انصراف از پرسش سوال تکمیلی
@describe_router.callback_query(F.data == "cancel_describe")
async def cancel_describe(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    _cancel_user_timer(user_id)

    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()
        try:
            await callback.message.edit_text("❌ عملیات سوال تکمیلی لغو شد.")
        except TelegramBadRequest:
            pass

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


# ۳. دریافت سوال تکمیلی کاربر درباره تصویر
@describe_router.message(DescribePhotoState.waiting_for_followup_question, F.text)
async def process_followup_question(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    _cancel_user_timer(user_id)

    data = await state.get_data()
    image_bytes = data.get("image_bytes")
    initial_description = data.get("initial_description")

    # پاک کردن وضعیت FSM طبق سناریو (فقط یک سوال مجاز است)
    await state.clear()

    if not image_bytes or not initial_description:
        await message.reply("⚠️ اطلاعات تصویر منقضی شده است. لطفاً تصویر را مجدداً ارسال کنید.")
        return

    status_msg = await message.answer("🔍 در حال بررسی سوال و بررسی مجدد تصویر...")

    try:
        user_question = message.text.strip()
        answer = await describe_service.answer_followup(
            image_bytes=image_bytes,
            initial_description=initial_description,
            user_question=user_question,
            mime_type="image/jpeg",
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


# ۴. هندلر ورودی‌های اشتباه در فاز سوال تکمیلی (ارسال فایل یا عکس جدید به جای متن سوال)
@describe_router.message(DescribePhotoState.waiting_for_followup_question)
async def invalid_followup_input_handler(message: Message) -> None:
    await message.reply(
        "⚠️ لطفاً سوال تکمیلی خود را صرفاً به صورت متن تایپ و ارسال کنید، یا دکمه انصراف را لمس کنید.",
        reply_markup=get_cancel_describe_keyboard(),
    )

    