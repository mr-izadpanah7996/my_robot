import io
import logging
from typing import cast

from aiogram import Bot, F, Router, html
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from keyboards.qr_kb import (
    QRColorCallback,
    get_qr_cancel_keyboard,
    get_qr_color_keyboard,
)
from services.qr_service import (
    QR_COLOR_LABELS,
    QRColorStyle,
    QRNotFoundError,
    QRService,
    QRServiceError,
    QRTextTooLongError,
)

logger = logging.getLogger(__name__)

qr_router = Router()
qr_service = QRService()

MAX_QR_IMAGE_BYTES = 10 * 1024 * 1024  # حداکثر ۱۰ مگابایت برای تصویر ورودی
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/bmp"}
ALLOWED_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")

# لیست دکمه‌های منوی اصلی برای پاک‌سازی خودکار FSM هنگام تغییر بخش
MAIN_MENU_TEXTS = {
    "🖼 تبدیل عکس به متن",
    "📄 اسکن فایل pdf",
    "🎬 کم کردن حجم ویدیو",
    "🎵 تبدیل mp4 به mp3",
    "📁 تبدیل متن به فایل",
    "🎙 کم کردن حجم فایل صوتی",
    "🎧 تبدیل ویس به متن",
    "🗣 تبدیل متن به ویس",
    "📱 اسکن و ساخت QR",
    "🗜 زیپ کردن فایل‌ها",
}

# جلوگیری از پردازش هم‌زمان و کلیک تکراری (Double-Click) توسط یک کاربر
_active_qr_users: set[int] = set()


# ==========================================
# ۱. کلاس وضعیت‌های FSM برای بخش QR Code
# ==========================================
class QRState(StatesGroup):
    waiting_for_input = State()
    waiting_for_color = State()


# ==========================================
# ۲. پاک‌سازی خودکار وضعیت در صورت زدن منوی اصلی یا دستورات (/start و ...)
# ==========================================
@qr_router.message(
    QRState.waiting_for_input,
    F.text.in_(MAIN_MENU_TEXTS) | F.text.startswith("/"),
)
@qr_router.message(
    QRState.waiting_for_color,
    F.text.in_(MAIN_MENU_TEXTS) | F.text.startswith("/"),
)
async def clear_qr_state_on_navigation(message: Message, state: FSMContext) -> None:
    await state.clear()
    raise SkipHandler


# ==========================================
# ۳. هندلر ورود به بخش اسکن و ساخت QR
# ==========================================
@qr_router.message(F.text == "📱 اسکن و ساخت QR")
async def start_qr_process(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return

    user_id = message.from_user.id
    if user_id in _active_qr_users:
        await message.answer("⏳ درخواست قبلی شما در حال پردازش است. لطفاً چند لحظه صبر کنید.")
        return

    await state.clear()
    await state.set_state(QRState.waiting_for_input)

    await message.answer(
        "📱 <b>اسکنر و سازنده هوشمند QR Code</b>\n\n"
        "• <b>برای اسکن:</b> عکس یا اسکرین‌شات حاوی QR Code را بفرستید.\n"
        "• <b>برای ساخت:</b> متن، لینک اینترنتی یا شماره مورد نظر را بنویسید.\n\n"
        "✨ <i>این بخش رایگان و بدون محدودیت سهمیه است.</i>",
        reply_markup=get_qr_cancel_keyboard(),
    )


# ==========================================
# ۴. دکمه شیشه‌ای انصراف
# ==========================================
@qr_router.callback_query(F.data == "qr_cancel")
async def cancel_qr_process(callback: CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()

    if callback.message:
        try:
            await callback.message.edit_text("❌ عملیات اسکن و ساخت QR Code لغو شد.")
        except TelegramBadRequest:
            pass

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


# ==========================================
# ۵. هندلر اسکن QR Code از روی عکس (Photo)
# ==========================================
@qr_router.message(QRState.waiting_for_input, F.photo)
async def handle_qr_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or not message.photo:
        return

    largest_photo = message.photo[-1]
    if largest_photo.file_size and largest_photo.file_size > MAX_QR_IMAGE_BYTES:
        await message.reply(
            "❌ حجم تصویر ارسالی بیش از حد مجاز (۱۰ مگابایت) است.",
            reply_markup=get_qr_cancel_keyboard(),
        )
        return

    await _process_qr_image_file(
        message=message,
        state=state,
        bot=bot,
        file_id=largest_photo.file_id,
    )


# ==========================================
# ۶. هندلر اسکن QR Code از روی فایل تصویری (Document)
# ==========================================
@qr_router.message(QRState.waiting_for_input, F.document)
async def handle_qr_image_document(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or not message.document:
        return

    mime = (message.document.mime_type or "").lower()
    file_name = (message.document.file_name or "").lower()

    is_valid_mime = mime in ALLOWED_IMAGE_MIMES
    is_valid_ext = file_name.endswith(ALLOWED_IMAGE_EXTS)

    if not (is_valid_mime or is_valid_ext):
        await message.reply(
            "⚠️ فایل ارسالی تصویر نیست!\n"
            "لطفاً یک <b>عکس</b> حاوی QR Code بفرستید یا <b>متن/لینک</b> خود را تایپ کنید.",
            reply_markup=get_qr_cancel_keyboard(),
        )
        return

    if message.document.file_size and message.document.file_size > MAX_QR_IMAGE_BYTES:
        await message.reply(
            "❌ حجم فایل تصویری بیش از حد مجاز (۱۰ مگابایت) است.",
            reply_markup=get_qr_cancel_keyboard(),
        )
        return

    await _process_qr_image_file(
        message=message,
        state=state,
        bot=bot,
        file_id=message.document.file_id,
    )


async def _process_qr_image_file(
    message: Message,
    state: FSMContext,
    bot: Bot,
    file_id: str,
) -> None:
    """تابع کمکی مشترک برای دانلود و اسکن ایمن تصویر QR Code"""
    assert message.from_user is not None
    user_id = message.from_user.id

    if user_id in _active_qr_users:
        await message.reply("⏳ تصویر قبلی شما هنوز در حال اسکن است. لطفاً کمی صبر کنید.")
        return

    _active_qr_users.add(user_id)
    status_msg = await message.answer("🔍 در حال بررسی و اسکن QR Code...")
    image_stream = io.BytesIO()

    try:
        await bot.download(file=file_id, destination=image_stream)
        image_bytes = image_stream.getvalue()

        scanned_items = await qr_service.scan_qr_from_bytes(image_bytes)
        await state.clear()

        if len(scanned_items) == 1:
            response_text = (
                f"✅ <b>QR Code با موفقیت اسکن شد:</b>\n\n"
                f"{scanned_items[0].formatted_html}"
            )
        else:
            blocks = [f"✅ <b>{len(scanned_items)} کد QR در این تصویر شناسایی شد:</b>"]
            for idx, item in enumerate(scanned_items, start=1):
                blocks.append(f"<b>━━━ کد شماره {idx} ━━━</b>\n{item.formatted_html}")
            response_text = "\n\n".join(blocks)

        try:
            await status_msg.delete()
        except TelegramBadRequest:
            pass

        if len(response_text) > 3900:
            raw_combined = "\n\n---\n\n".join(item.raw_data for item in scanned_items)
            txt_file = BufferedInputFile(
                file=raw_combined.encode("utf-8"),
                filename="QR_Scanned_Data.txt",
            )
            await message.reply_document(
                document=txt_file,
                caption=f"✅ {len(scanned_items)} کد QR اسکن شد (به دلیل طولانی بودن متن به صورت فایل ارسال شد).",
            )
        else:
            await message.reply(response_text, disable_web_page_preview=True)

    except QRNotFoundError as err:
        try:
            await status_msg.edit_text(
                f"❌ {html.quote(str(err))}\n\n"
                "می‌توانید تصویر واضح‌تری بفرستید یا دکمه انصراف را بزنید:",
                reply_markup=get_qr_cancel_keyboard(),
            )
        except TelegramBadRequest:
            pass

    except QRServiceError as err:
        try:
            await status_msg.edit_text(
                f"❌ {html.quote(str(err))}",
                reply_markup=get_qr_cancel_keyboard(),
            )
        except TelegramBadRequest:
            pass

    except (TimeoutError, ConnectionError) as err:
        logger.error("خطای شبکه هنگام دانلود تصویر QR: %s", err)
        try:
            await status_msg.edit_text(
                "❌ خطا در ارتباط شبکه هنگام دریافت تصویر. لطفاً مجدداً تلاش کنید.",
                reply_markup=get_qr_cancel_keyboard(),
            )
        except TelegramBadRequest:
            pass

    except Exception as err:
        logger.exception("خطای پیش‌بینی‌نشده در اسکن QR Code: %s", err)
        await state.clear()
        try:
            await status_msg.edit_text("❌ متأسفانه هنگام اسکن تصویر خطایی رخ داد.")
        except TelegramBadRequest:
            pass

    finally:
        _active_qr_users.discard(user_id)
        image_stream.close()


# ==========================================
# ۷. هندلر دریافت متن یا لینک برای ساخت QR Code
# ==========================================
@qr_router.message(QRState.waiting_for_input, F.text)
async def handle_qr_text_input(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.text:
        return

    clean_text = message.text.strip()
    if not clean_text:
        await message.reply(
            "⚠️ متن ارسالی نمی‌تواند خالی باشد.",
            reply_markup=get_qr_cancel_keyboard(),
        )
        return

    byte_len = len(clean_text.encode("utf-8"))
    if byte_len > qr_service.MAX_UTF8_BYTES:
        await message.reply(
            f"❌ طول متن بیش از ظرفیت استاندارد QR Code است ({len(clean_text)} کاراکتر).\n"
            "لطفاً متن کوتاه‌تری بفرستید (حداکثر حدود ۱۰۰۰ کاراکتر فارسی یا ۲۲۰۰ کاراکتر انگلیسی):",
            reply_markup=get_qr_cancel_keyboard(),
        )
        return

    await state.update_data(qr_text=clean_text)
    await state.set_state(QRState.waiting_for_color)

    single_line = " ".join(clean_text.split())
    preview = single_line if len(single_line) <= 80 else f"{single_line[:77]}..."
    await message.reply(
        f"🎨 <b>رنگ QR Code خود را انتخاب کنید:</b>\n\n"
        f"📝 متن: <code>{html.quote(preview)}</code>",
        reply_markup=get_qr_color_keyboard(),
    )


# ==========================================
# ۸. هندلر انتخاب رنگ و ارسال نهایی تصویر QR Code
# ==========================================
@qr_router.callback_query(QRState.waiting_for_color, QRColorCallback.filter())
async def handle_qr_color_selection(
    callback: CallbackQuery,
    callback_data: QRColorCallback,
    state: FSMContext,
) -> None:
    user_id = callback.from_user.id
    if user_id in _active_qr_users:
        try:
            await callback.answer("⏳ در حال ساخت تصویر، لطفاً صبر کنید...")
        except TelegramBadRequest:
            pass
        return

    data = await state.get_data()
    qr_text: str | None = data.get("qr_text")

    if not qr_text:
        await state.clear()
        if callback.message:
            try:
                await callback.message.edit_text(
                    "⚠️ اطلاعات قبلی منقضی شده است. لطفاً دوباره از منو وارد بخش QR شوید."
                )
            except TelegramBadRequest:
                pass
        try:
            await callback.answer()
        except TelegramBadRequest:
            pass
        return

    style = cast(QRColorStyle, callback_data.style)
    style_label = QR_COLOR_LABELS.get(style, "⬛ مشکی کلاسیک")

    _active_qr_users.add(user_id)
    try:
        try:
            await callback.answer("⏳ در حال ساخت QR Code...")
        except TelegramBadRequest:
            pass

        png_bytes = await qr_service.generate_qr_bytes(text=qr_text, style=style)
        await state.clear()

        qr_file = BufferedInputFile(file=png_bytes, filename="DeltaBot_QRCode.png")
        single_line = " ".join(qr_text.split())
        caption_preview = single_line if len(single_line) <= 120 else f"{single_line[:117]}..."

        if callback.message:
            await callback.message.answer_photo(
                photo=qr_file,
                caption=(
                    f"✅ <b>QR Code شما با موفقیت ساخته شد!</b>\n"
                    f"🎨 استایل: {style_label}\n"
                    f"📝 محتوا: <code>{html.quote(caption_preview)}</code>"
                ),
            )
            try:
                await callback.message.delete()
            except TelegramBadRequest:
                pass

    except (QRTextTooLongError, QRServiceError) as err:
        await state.clear()
        if callback.message:
            try:
                await callback.message.edit_text(f"❌ {html.quote(str(err))}")
            except TelegramBadRequest:
                pass

    except Exception as err:
        logger.exception("خطای پیش‌بینی‌نشده در تولید QR Code: %s", err)
        await state.clear()
        if callback.message:
            try:
                await callback.message.edit_text("❌ متأسفانه هنگام ساخت تصویر QR Code خطایی رخ داد.")
            except TelegramBadRequest:
                pass

    finally:
        _active_qr_users.discard(user_id)


# ==========================================
# ۹. هندلر کلیک روی دکمه رنگ پس از پایان وضعیت
# ==========================================
@qr_router.callback_query(QRColorCallback.filter())
async def handle_expired_qr_color_callback(callback: CallbackQuery) -> None:
    try:
        await callback.answer("⚠️ این درخواست منقضی شده است. دوباره متن خود را بفرستید.", show_alert=True)
    except TelegramBadRequest:
        pass


# ==========================================
# ۱۰. هندلر ورودی‌های نامعتبر در وضعیت QR
# ==========================================
@qr_router.message(QRState.waiting_for_input)
async def handle_invalid_qr_input(message: Message) -> None:
    await message.reply(
        "⚠️ ورودی نامعتبر است!\n"
        "لطفاً فقط یک <b>عکس</b> برای اسکن بفرستید یا یک <b>متن / لینک</b> برای ساخت QR Code وارد کنید.",
        reply_markup=get_qr_cancel_keyboard(),
    )


@qr_router.message(QRState.waiting_for_color)
async def handle_waiting_color_text(message: Message) -> None:
    await message.reply(
        "👆 لطفاً یکی از <b>دکمه‌های رنگ</b> بالا را انتخاب کنید یا دکمه <b>انصراف</b> را لمس کنید.",
        reply_markup=get_qr_color_keyboard(),
    )