from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def get_main_menu() -> ReplyKeyboardMarkup:

    
    keyboard = [
        
        [
            KeyboardButton(text="🖼 تبدیل عکس به متن"),
            KeyboardButton(text="📄 اسکن فایل pdf"),
        ],
        
        [
            KeyboardButton(text="🎙 تبدیل ویس به متن"),
            KeyboardButton(text="🔊 تبدیل متن به ویس"),
        ],
    
        [
            KeyboardButton(text="🎵 تبدیل mp4 به mp3"),
            KeyboardButton(text="🎬 کم کردن حجم ویدیو"),
        ],
    ]

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=True,   # مخفی/بسته شدن کیبورد بلافاصله پس از کلیک کاربر
        input_field_placeholder="یک گزینه را انتخاب کنید..."
    )

