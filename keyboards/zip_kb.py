from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.zip_service import ARCHIVE_FORMAT_LABELS, ArchiveFormat

ZipActionType = Literal[
    "cancel",
    "start_compress",
    "default_name",
    "skip_password",
]


class ZipActionCallback(CallbackData, prefix="zip_act"):
    """کال‌بک استاندارد برای تمام دکمه‌های کنترلی مراحل زیپ کردن"""
    action: ZipActionType


class ZipFormatCallback(CallbackData, prefix="zip_fmt"):
    """کال‌بک استاندارد برای انتخاب فرمت نهایی فشرده‌سازی"""
    fmt: ArchiveFormat


def _cancel_button() -> InlineKeyboardButton:
    """دکمه استاندارد کنسل جهت استفاده یکسان در تمام مراحل"""
    return InlineKeyboardButton(
        text="❌ کنسل",
        callback_data=ZipActionCallback(action="cancel").pack(),
    )


def get_zip_cancel_keyboard() -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای فقط شامل دکمه کنسل (برای پیام شروع فرآیند)"""
    builder = InlineKeyboardBuilder()
    builder.row(_cancel_button())
    return builder.as_markup()


def get_zip_collecting_keyboard(file_count: int) -> InlineKeyboardMarkup:
    """
    کیبورد شیشه‌ای مرحله دریافت فایل‌ها:
    شامل دکمه «فشرده سازی» (با نمایش تعداد فایل‌ها) و دکمه «کنسل»
    """
    if file_count <= 0:
        return get_zip_cancel_keyboard()

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"🗜 فشرده سازی ({file_count} فایل)",
            callback_data=ZipActionCallback(action="start_compress").pack(),
        ),
        _cancel_button(),
    )
    return builder.as_markup()


def get_zip_name_keyboard() -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای مرحله تعیین نام فایل خروجی"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📄 نام پیش‌فرض",
            callback_data=ZipActionCallback(action="default_name").pack(),
        ),
        _cancel_button(),
    )
    return builder.as_markup()


def get_zip_password_keyboard() -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای مرحله رمزگذاری روی فایل"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⏭ رد شدن (بدون رمز)",
            callback_data=ZipActionCallback(action="skip_password").pack(),
        ),
        _cancel_button(),
    )
    return builder.as_markup()


def get_zip_format_keyboard() -> InlineKeyboardMarkup:
    """
    کیبورد شیشه‌ای انتخاب نوع فرمت فشرده‌سازی (ZIP / 7Z / TAR.GZ) به همراه دکمه کنسل
    """
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=ARCHIVE_FORMAT_LABELS["zip"],
            callback_data=ZipFormatCallback(fmt="zip").pack(),
        ),
        InlineKeyboardButton(
            text=ARCHIVE_FORMAT_LABELS["7z"],
            callback_data=ZipFormatCallback(fmt="7z").pack(),
        ),
    )

    builder.row(
        InlineKeyboardButton(
            text=ARCHIVE_FORMAT_LABELS["tar.gz"],
            callback_data=ZipFormatCallback(fmt="tar.gz").pack(),
        ),
        _cancel_button(),
    )

    return builder.as_markup()

