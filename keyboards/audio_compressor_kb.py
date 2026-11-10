from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class AudioCompressCallback(CallbackData, prefix="audio_cmp"):
    """
    مدل ساختاریافته داده‌های کلیک روی کیبورد فشرده‌سازی صدا.
    action: 'quality' یا 'cancel'
    bitrate: '64k' یا '48k' یا '32k' (در صورت انصراف مقدار none می‌گیرد)
    """
    action: str
    bitrate: str = "none"


def get_audio_quality_keyboard() -> InlineKeyboardMarkup:
    """
    کیبورد شیشه‌ای انتخاب کیفیت فشرده‌سازی فایل صوتی همراه با دکمه لغو عملیات.
    """
    builder = InlineKeyboardBuilder()

    # ردیف اول: ۶۴ کیلوبیت (کیفیت استاندارد/پادکست و موزیک سبک)
    builder.button(
        text="🎧 کیفیت استاندارد (64 kbps)",
        callback_data=AudioCompressCallback(action="quality", bitrate="64k"),
    )

    # ردیف دوم: ۴۸ کیلوبیت (فشرده و بهینه)
    builder.button(
        text="⚡ کیفیت فشرده (48 kbps)",
        callback_data=AudioCompressCallback(action="quality", bitrate="48k"),
    )

    # ردیف سوم: ۳۲ کیلوبیت (فوق فشرده - مناسب ویس طولانی)
    builder.button(
        text="🗜 فوق فشرده (32 kbps)",
        callback_data=AudioCompressCallback(action="quality", bitrate="32k"),
    )

    # ردیف چهارم: دکمه انصراف و لغو فرآیند
    builder.button(
        text="❌ انصراف و لغو",
        callback_data=AudioCompressCallback(action="cancel", bitrate="none"),
    )

    # چینش دکمه‌ها: هر گزینه در یک ردیف مجزا برای لمس راحت‌تر در موبایل
    builder.adjust(1, 1, 1, 1)

    return builder.as_markup()

