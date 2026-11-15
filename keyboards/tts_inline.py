from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def get_cancel_tts_keyboard() -> InlineKeyboardMarkup:
    """
    کیبورد شیشه‌ای انصراف برای مرحله دریافت متن در بخش تبدیل متن به ویس
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ انصراف",
                    callback_data="cancel_tts",
                )
            ]
        ]
    )


def get_tts_voice_selection_keyboard() -> InlineKeyboardMarkup:
    """
    کیبورد شیشه‌ای انتخاب صدای گوینده (مرد / زن) همراه با دکمه انصراف
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👨 صدای مرد",
                    callback_data="tts_voice:male",
                ),
                InlineKeyboardButton(
                    text="👩 صدای زن",
                    callback_data="tts_voice:female",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ انصراف",
                    callback_data="cancel_tts",
                )
            ],
        ]
    )

