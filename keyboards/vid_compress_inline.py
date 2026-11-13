from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class VideoCompressCallback(CallbackData, prefix="vid_cmp"):
    # action: 'quality' | 'cancel' | 'download'
    action: str
    level: str = "none"


def get_video_quality_guide_text() -> str:
    return (
        "🎬 **سطح فشرده‌سازی و کیفیت ویدیو را انتخاب کنید:**\n\n"
        "💎 **کیفیت خوب (تا 720p):**\n"
        "مناسب ویدیوهای آموزشی و محتوایی که وضوح تصویر در آن‌ها اهمیت دارد.\n\n"
        "⚖️ **کیفیت متعادل (تا 480p - پیشنهادی):**\n"
        "بهترین تعادل بین وضوح تصویر و کاهش حجم؛ مناسب اشتراک‌گذاری روزمره در تلگرام.\n\n"
        "🗜 **حداکثر کاهش حجم (تا 240p):**\n"
        "مناسب ارسال سریع با اینترنت ضعیف و کاهش چشمگیر حجم فایل."
    )


def get_video_quality_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.button(
        text="💎 کیفیت خوب (تا 720p)",
        callback_data=VideoCompressCallback(action="quality", level="low"),
    )

    builder.button(
        text="⚖️ کیفیت متعادل (تا 480p - پیشنهادی)",
        callback_data=VideoCompressCallback(action="quality", level="medium"),
    )

    builder.button(
        text="🗜 حداکثر کاهش حجم (تا 240p)",
        callback_data=VideoCompressCallback(action="quality", level="high"),
    )

    builder.button(
        text="❌ انصراف و لغو عملیات",
        callback_data=VideoCompressCallback(action="cancel", level="none"),
    )

    builder.adjust(1, 1, 1, 1)
    return builder.as_markup()


def get_video_download_keyboard() -> InlineKeyboardMarkup:
    # کیبورد مرحله آماده تحویل برای دریافت آنی فایل از کانال آرشیو
    builder = InlineKeyboardBuilder()

    builder.button(
        text="📥 دریافت ویدیو",
        callback_data=VideoCompressCallback(action="download", level="none"),
    )

    builder.adjust(1)
    return builder.as_markup()

