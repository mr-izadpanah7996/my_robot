from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.qr_service import QR_COLOR_LABELS, QRColorStyle


class QRColorCallback(CallbackData, prefix="qr_gen"):
    """کال‌بک استاندارد انتخاب رنگ برای تولید QR Code"""
    style: QRColorStyle


def get_qr_cancel_keyboard() -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای انصراف در مرحله انتظار برای دریافت عکس یا متن"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="❌ انصراف",
            callback_data="qr_cancel",
        )
    )
    return builder.as_markup()


def get_qr_color_keyboard() -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای انتخاب رنگ QR Code به همراه دکمه انصراف"""
    builder = InlineKeyboardBuilder()

    # ردیف اول: رنگ‌های کلاسیک و سرمه‌ای
    builder.row(
        InlineKeyboardButton(
            text=QR_COLOR_LABELS["classic"],
            callback_data=QRColorCallback(style="classic").pack(),
        ),
        InlineKeyboardButton(
            text=QR_COLOR_LABELS["navy"],
            callback_data=QRColorCallback(style="navy").pack(),
        ),
    )

    # ردیف دوم: رنگ زرشکی و دکمه انصراف
    builder.row(
        InlineKeyboardButton(
            text=QR_COLOR_LABELS["crimson"],
            callback_data=QRColorCallback(style="crimson").pack(),
        ),
        InlineKeyboardButton(
            text="❌ انصراف",
            callback_data="qr_from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.qr_service import QR_COLOR_LABELS, QRColorStyle


class QRColorCallback(CallbackData, prefix="qr_gen"):
    """کال‌بک استاندارد انتخاب رنگ برای تولید QR Code"""
    style: QRColorStyle


def get_qr_cancel_keyboard() -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای انصراف در مرحله انتظار برای دریافت عکس یا متن"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="❌ انصراف",
            callback_data="qr_cancel",
        )
    )
    return builder.as_markup()


def get_qr_color_keyboard() -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای انتخاب رنگ QR Code به همراه دکمه انصراف"""
    builder = InlineKeyboardBuilder()

    # ردیف اول: رنگ‌های کلاسیک و سرمه‌ای
    builder.row(
        InlineKeyboardButton(
            text=QR_COLOR_LABELS["classic"],
            callback_data=QRColorCallback(style="classic").pack(),
        ),
        InlineKeyboardButton(
            text=QR_COLOR_LABELS["navy"],
            callback_data=QRColorCallback(style="navy").pack(),
        ),
    )

    # ردیف دوم: رنگ زرشکی و دکمه انصراف
    builder.row(
        InlineKeyboardButton(
            text=QR_COLOR_LABELS["crimson"],
            callback_data=QRColorCallback(style="crimson").pack(),
        ),
        InlineKeyboardButton(
            text="❌ انصراف",
            callback_data="qr_cancel",
        ),
    )

    return builder.as_markup()cancel",
        ),
    )

    return builder.as_markup()
