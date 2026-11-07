from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_skip_filename_keyboard() -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای مرحله دریافت نام فایل همراه با گزینه رد شدن و لغو"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏭ رد شدن (نام پیش‌فرض: دلتا)",
                    callback_data="t2f_skip_name",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ لغو عملیات",
                    callback_data="t2f_cancel",
                )
            ],
        ]
    )


def get_format_selection_keyboard() -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای انتخاب ۶ فرمت خروجی با چیدمان دو ستونه و دکمه لغو"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📄 متنی (TXT)",
                    callback_data="t2f_fmt_txt",
                ),
                InlineKeyboardButton(
                    text="📊 اکسل (CSV)",
                    callback_data="t2f_fmt_csv",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌐 وب (HTML)",
                    callback_data="t2f_fmt_html",
                ),
                InlineKeyboardButton(
                    text="📑 پی‌دی‌اف (PDF)",
                    callback_data="t2f_fmt_pdf",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🐍 پایتون (PY)",
                    callback_data="t2f_fmt_py",
                ),
                InlineKeyboardButton(
                    text="📦 جیسون (JSON)",
                    callback_data="t2f_fmt_json",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ لغو عملیات",
                    callback_data="t2f_cancel",
                )
            ],
        ]
    )
