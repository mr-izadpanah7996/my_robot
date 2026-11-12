from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class VideoCompressCallback(CallbackData, prefix="vid_cmp"):
    """
    مدل ساختاریافته داده‌های کلیک روی کیبورد فشرده‌سازی ویدیو.
    action: 'quality' یا 'cancel'
    level: 'low' یا 'medium' یا 'high' (در صورت انصراف مقدار none می‌گیرد)
    """
    action: str
    level: str = "none"


def get_video_quality_guide_text() -> str:
    """
    متن راهنمای کوتاه برای توضیح کاربرد هر سطح فشرده‌سازی و رزولوشن به کاربر.
    """
    return (
        "🎬 **سطح فشرده‌سازی و کیفیت ویدیو را انتخاب کنید:**\n\n"
        "💎 **کیفیت بالا (تا 1080p - فشرده‌سازی کم):**\n"
        "مناسب ویدیوهای آموزشی، متنی و جزئیات‌دار که وضوح تصویر در آن‌ها حیاتی است.\n\n"
        "⚖️ **کیفیت متعادل (تا 720p - پیشنهادی):**\n"
        "بهترین تعادل بین وضوح تصویر و کاهش حجم؛ مناسب فیلم‌های روزمره و انتشار در شبکه‌های اجتماعی.\n\n"
        "🗜 **حداکثر کاهش حجم (تا 480p - فشرده‌سازی بالا):**\n"
        "مناسب ارسال سریع در چت، اشتراک‌گذاری با اینترنت ضعیف و کاهش چشمگیر حجم فایل."
    )


def get_video_quality_keyboard() -> InlineKeyboardMarkup:
    """
    کیبورد شیشه‌ای انتخاب سطح فشرده‌سازی ویدیو همراه با دکمه انصراف در ردیف پایانی.
    """
    builder = InlineKeyboardBuilder()

    # ردیف اول: کیفیت بالا / فشرده‌سازی کم
    builder.button(
        text="💎 کیفیت بالا (تا 1080p)",
        callback_data=VideoCompressCallback(action="quality", level="low"),
    )

    # ردیف دوم: کیفیت متعادل (پیشنهادی)
    builder.button(
        text="⚖️ کیفیت متعادل (تا 720p - پیشنهادی)",
        callback_data=VideoCompressCallback(action="quality", level="medium"),
    )

    # ردیف سوم: حداکثر کاهش حجم / فشرده‌سازی بالا
    builder.button(
        text="🗜 حداکثر کاهش حجم (تا 480p)",
        callback_data=VideoCompressCallback(action="quality", level="high"),
    )

    # ردیف چهارم: دکمه انصراف و لغو عملیات
    builder.button(
        text="❌ انصراف و لغو عملیات",
        callback_data=VideoCompressCallback(action="cancel", level="none"),
    )

    # چیدمان تک‌ستونه (هر دکمه در یک ردیف مجزا برای خوانایی کامل در موبایل)
    builder.adjust(1, 1, 1, 1)

    return builder.as_markup()
