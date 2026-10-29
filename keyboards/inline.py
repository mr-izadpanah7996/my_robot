from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_join_channels_keyboard(channels: list[str]) -> InlineKeyboardMarkup:
    buttons = []

    # ساخت دکمه لینک برای هر کانال

    
    for index, channel in enumerate(channels, start=1):
        clean_username = channel.lstrip("@")
        buttons.append([
            InlineKeyboardButton(
                text=f"📢 عضویت در کانال {index}",
                url=f"https://t.me/{clean_username}",
            )
        ])

    # دکمه شیشه‌ای بررسی مجدد عضویت

    buttons.append([
        InlineKeyboardButton(
            text="✅ عضو شدم، بررسی عضویت",
            callback_data="check_membership",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)



def get_cancel_ocr_keyboard() -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای دکمه انصراف از ارسال عکس"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_ocr")
            ]
        ]
    )

