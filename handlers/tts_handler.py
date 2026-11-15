import asyncio
import logging
from aiogram import Router, F, Bot, html
from aiogram.types import Message, BufferedInputFile, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from keyboards.tts_inline import get_cancel_tts_keyboard, get_tts_voice_selection_keyboard
from services.limiter import DailyLimiter
from services.tts_service import TTSService, TTSError, VoiceGender

logger = logging.getLogger(__name__)

tts_router = Router()

# مقداردهی سرویس تولید صدا و مدیریت سهمیه مطابق قراردادهای پروژه
tts_service = TTSService(max_text_length=4000)
daily_limiter = DailyLimiter(max_requests=15, window_seconds=86400)

TTS_QUOTA_COST = 3
MAX_TEXT_LENGTH = 4000

# جلوگیری از پردازش هم‌زمان چند درخواست صوتی برای یک کاربر
_active_tts_users: set[int] = set()

# دکمه‌های شناخته‌شده منوی اصلی برای جلوگیری از تبدیل اشتباه نام دکمه منو به ویس
MAIN_MENU_BUTTONS: set[str] = {
    "🎧 تبدیل ویس به متن",
    "🗣 تبدیل متن به ویس",
    "📄 اسکن فایل pdf",
    "🖼 تبدیل عکس به متن",
    "🔍 توصیف تصویر",
    "📁 تبدیل متن به فایل",
    "🗜 فشرده‌سازی فایل",
    "🔙 بازگشت به منوی اصلی",
}


# ==========================================
# ۱. کلاس وضعیت‌های FSM برای بخش تبدیل متن به ویس
# ==========================================
class TTSState(StatesGroup):
    waiting_for_text = State()
    waiting_for_voice = State()


# ==========================================
# ۲. هندلر ورود به بخش تبدیل متن به ویس
# ==========================================
@tts_router.message(F.text == "🗣 تبدیل متن به ویس")
async def start_tts_process(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return

    user_id = message.from_user.id

    if user_id in _active_tts_users:
        await message.answer("⏳ درخواست قبلی شما هنوز در حال تبدیل به صدا است. لطفاً تا پایان آن صبر کنید.")
        return

    remaining = daily_limiter.get_remaining_quota(user_id=user_id)

    if remaining < TTS_QUOTA_COST:
        await message.answer(
            f"⚠️ سهمیه روزانه شما برای تبدیل متن به ویس کافی نیست.\n"
            f"برای هر بار تبدیل متن به ویس به {html.bold(str(TTS_QUOTA_COST))} سهمیه نیاز است، "
            f"اما اعتبار فعلی شما {html.bold(str(remaining))} می‌باشد."
        )
        return

    possible_uses = remaining // TTS_QUOTA_COST

    await state.clear()
    await state.set_state(TTSState.waiting_for_text)
    await message.answer(
        f"🗣 لطفاً متن مورد نظر خود را ارسال کنید تا به ویس تبدیل شود.\n\n"
        f"📊 اعتبار فعلی شما: {html.bold(str(remaining))} سهمیه "
        f"(قابل استفاده برای {html.bold(str(possible_uses))} تبدیل)\n\n"
        f"📌 راهنما:\n"
        f"• حداکثر طول مجاز متن: {MAX_TEXT_LENGTH} کاراکتر\n"
        f"• پشتیبانی هوشمند از متن‌های فارسی و انگلیسی\n"
        f"• میزان مصرف اعتبار: {TTS_QUOTA_COST} سهمیه به ازای هر تبدیل",
        reply_markup=get_cancel_tts_keyboard(),
    )


# ==========================================
# ۳. دکمه شیشه‌ای انصراف (در هر دو مرحله انتظار متن و انتخاب گوینده)
# ==========================================
@tts_router.callback_query(TTSState.waiting_for_text, F.data == "cancel_tts")
@tts_router.callback_query(TTSState.waiting_for_voice, F.data == "cancel_tts")
async def cancel_tts_process(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message:
        try:
            await callback.message.edit_text("❌ عملیات تبدیل متن به ویس لغو شد.")
        except TelegramBadRequest:
            pass

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


# ==========================================
# ۴. هندلر دریافت متن و باز کردن کیبورد انتخاب گوینده
# ==========================================
@tts_router.message(TTSState.waiting_for_text, F.text)
async def receive_tts_text(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or not message.text:
        return

    user_id = message.from_user.id
    user_text = message.text.strip()

    # جلوگیری از گیر افتادن کاربر در وضعیت FSM اگر دکمه بخش دیگری از ربات را زده باشد
    if user_text in MAIN_MENU_BUTTONS or user_text.startswith("/"):
        await state.clear()
        await message.answer(
            "ℹ️ عملیات تبدیل متن به ویس لغو شد. لطفاً مجدداً گزینه مورد نظر خود را از منو انتخاب کنید."
        )
        return

    if not user_text:
        await message.reply(
            "⚠️ متن ارسالی خالی است! لطفاً متن معتبری ارسال کنید یا دکمه انصراف را بزنید.",
            reply_markup=get_cancel_tts_keyboard(),
        )
        return

    text_length = len(user_text)
    if text_length > MAX_TEXT_LENGTH:
        await message.reply(
            f"❌ طول متن ارسالی شما ({html.bold(str(text_length))} کاراکتر) بیشتر از سقف مجاز ({MAX_TEXT_LENGTH} کاراکتر) است.\n"
            f"لطفاً متن کوتاه‌تری ارسال کنید یا دکمه انصراف را بزنید.",
            reply_markup=get_cancel_tts_keyboard(),
        )
        return

    remaining = daily_limiter.get_remaining_quota(user_id=user_id)
    if remaining < TTS_QUOTA_COST:
        await state.clear()
        await message.answer(
            f"⚠️ سهمیه روزانه شما برای تبدیل متن به ویس کافی نیست (نیاز به {TTS_QUOTA_COST} سهمیه)."
        )
        return

    # غیرفعال کردن کیبورد شیشه‌ای پیام قبلی در صورت وجود
    old_data = await state.get_data()
    old_prompt_id = old_data.get("prompt_msg_id")
    if old_prompt_id and message.chat:
        try:
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=old_prompt_id,
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass

    prompt_msg = await message.reply(
        f"✅ متن شما دریافت شد.\n"
        f"📏 طول متن ارسالی: {html.bold(str(text_length))} کاراکتر\n\n"
        f"🎙 لطفاً صدای گوینده مورد نظر خود را انتخاب کنید:",
        reply_markup=get_tts_voice_selection_keyboard(),
    )

    await state.update_data(tts_text=user_text, prompt_msg_id=prompt_msg.message_id)
    await state.set_state(TTSState.waiting_for_voice)


# ==========================================
# ۵. هندلر انتخاب گوینده، تولید صدا و ارسال فایل صوتی
# ==========================================
@tts_router.callback_query(TTSState.waiting_for_voice, F.data.startswith("tts_voice:"))
async def process_tts_voice_selection(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.data or not callback.message:
        return

    user_id = callback.from_user.id

    if user_id in _active_tts_users:
        try:
            await callback.answer("⏳ درخواست قبلی شما در حال پردازش است...", show_alert=True)
        except TelegramBadRequest:
            pass
        return

    selected_gender = callback.data.split(":", maxsplit=1)[1]
    if selected_gender not in ("male", "female"):
        try:
            await callback.answer("❌ گزینه انتخاب‌شده نامعتبر است.", show_alert=True)
        except TelegramBadRequest:
            pass
        return

    gender: VoiceGender = selected_gender  # type: ignore[assignment]
    gender_label = "مرد" if gender == "male" else "زن"

    data = await state.get_data()
    text_to_speak: str = data.get("tts_text", "").strip()

    if not text_to_speak:
        await state.clear()
        try:
            await callback.message.edit_text("⚠️ اطلاعات متن منقضی شده است. لطفاً مجدداً متن خود را ارسال کنید.")
        except TelegramBadRequest:
            pass
        try:
            await callback.answer()
        except TelegramBadRequest:
            pass
        return

    remaining = daily_limiter.get_remaining_quota(user_id=user_id)
    if remaining < TTS_QUOTA_COST:
        await state.clear()
        try:
            await callback.message.edit_text(
                f"⚠️ سهمیه روزانه شما برای تبدیل متن به ویس کافی نیست (نیاز به {TTS_QUOTA_COST} سهمیه)."
            )
        except TelegramBadRequest:
            pass
        try:
            await callback.answer()
        except TelegramBadRequest:
            pass
        return

    await state.clear()
    _active_tts_users.add(user_id)

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    try:
        await callback.message.edit_text(
            f"⏳ در حال تبدیل متن به ویس با صدای {html.bold(gender_label)} (لطفاً شکیبا باشید)..."
        )
    except TelegramBadRequest:
        pass

    audio_bytes = b""

    try:
        audio_bytes = await tts_service.synthesize_speech(
            text=text_to_speak,
            gender=gender,
        )

        audio_file = BufferedInputFile(
            file=audio_bytes,
            filename="Delta_Bot_Voice.mp3",
        )

        # ارسال فایل صوتی با عنوان «ربات دلتا»
        await callback.message.reply_audio(
            audio=audio_file,
            title="ربات دلتا",
            performer=f"گوینده {gender_label} - ربات دلتا",
            caption=(
                f"✅ تبدیل متن به ویس با موفقیت انجام شد.\n"
                f"🎙 گوینده: صدای {gender_label}\n"
                f"💡 {TTS_QUOTA_COST} سهمیه روزانه کسر گردید."
            ),
        )

        # کسر ۳ سهمیه فقط پس از تحویل موفقیت‌آمیز فایل صوتی به کاربر
        for _ in range(TTS_QUOTA_COST):
            daily_limiter.check_and_consume(user_id=user_id)

        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass

    except TTSError as err:
        logger.warning(f"خطای سرویس TTS برای کاربر {user_id}: {err}")
        try:
            await callback.message.edit_text(f"❌ {err}")
        except TelegramBadRequest:
            await callback.message.reply(f"❌ {err}")

    except TelegramForbiddenError:
        logger.info(f"کاربر {user_id} ربات را هنگام تولید ویس بلاک کرد.")

    except TelegramRetryAfter as retry_err:
        logger.warning(f"محدودیت نرخ تلگرام (FloodWait) برای کاربر {user_id}: {retry_err.retry_after}s")
        await asyncio.sleep(min(retry_err.retry_after, 5))

    except TelegramBadRequest as tg_err:
        logger.warning(f"خطای TelegramBadRequest در ارسال فایل صوتی TTS کاربر {user_id}: {tg_err}")
        try:
            await callback.message.edit_text("❌ خطا در ارسال فایل صوتی به تلگرام. لطفاً مجدداً تلاش کنید.")
        except TelegramBadRequest:
            pass

    except Exception as err:
        logger.exception(f"خطای پیش‌بینی‌نشده در هندلر تبدیل متن به ویس کاربر {user_id}: {err}")
        try:
            await callback.message.edit_text("❌ متأسفانه در فرآیند تبدیل متن به ویس خطایی رخ داد.")
        except TelegramBadRequest:
            await callback.message.reply("❌ متأسفانه در فرآیند تبدیل متن به ویس خطایی رخ داد.")

    finally:
        _active_tts_users.discard(user_id)
        audio_bytes = b""


# ==========================================
# ۶. هندلر ورودی‌های نامعتبر در وضعیت انتظار متن یا انتخاب صدا
# ==========================================
@tts_router.message(TTSState.waiting_for_text)
async def invalid_tts_text_input(message: Message) -> None:
    await message.reply(
        "⚠️ لطفاً فقط متن مورد نظر خود را ارسال کنید یا دکمه انصراف را لمس کنید.",
        reply_markup=get_cancel_tts_keyboard(),
    )


@tts_router.message(TTSState.waiting_for_voice)
async def invalid_tts_voice_input(message: Message) -> None:
    await message.reply(
        "⚠️ لطفاً از طریق دکمه‌های شیشه‌ای بالا، صدای گوینده (مرد یا زن) را انتخاب کنید یا دکمه انصراف را بزنید.",
        reply_markup=get_tts_voice_selection_keyboard(),
    )

    