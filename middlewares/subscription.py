import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.enums import ChatMemberStatus
from aiogram.types import CallbackQuery, Message, TelegramObject

from keyboards.inline import get_join_channels_keyboard

logger = logging.getLogger(__name__)


class MandatorySubMiddleware(BaseMiddleware):
    def __init__(self, channels: list[str]) -> None:
        super().__init__()

        self.channels = channels

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:

        
        # اگر هیچ کانالی در فایل تنظیمات مشخص نشده باشد، همه چیز رد شود

        if not self.channels:
            return await handler(event, data)

        # استخراج کاربر و شیء ربات

        user = data.get("event_from_user")
        bot = data.get("bot")

        if not user or not bot:
            return await handler(event, data)

        # استثنا: به کلیک روی دکمه شیشه‌ای «عضو شدم» اجازه عبور بده تا هندلرش بتواند کار کند


        if isinstance(event, CallbackQuery) and event.data == "check_membership":
            return await handler(event, data)

        # بررسی عضویت کاربر در تک‌تک کانال‌ها


        unsubscribed_channels: list[str] = []

        for channel in self.channels:
            try:
                member = await bot.get_chat_member(chat_id=channel, user_id=user.id)


                # بررسی وضعیت‌هایی که کاربر عضو نیست


                if member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED]:
                    unsubscribed_channels.append(channel)
            except Exception as error:
                logger.error(f"خطا در استعلام عضویت کانال {channel}: {error}")


                # در صورت خطای تلگرام یا ادمین نبودن ربات، کاربر مسدود نشود



        # اگر حتی در یک کانال عضو نبود، جلویش گرفته شود و پیام همراه کیبورد ارسال شود


        if unsubscribed_channels:
            text = (
                "⚠️ <b>کاربر گرامی!</b>\n\n"
                "برای استفاده از خدمات ربات، ابتدا باید در کانال‌های زیر عضو شوید:\n"
                "پس از عضویت، دکمه <b>«عضو شدم، بررسی عضویت»</b> را بزنید."
            )
            keyboard = get_join_channels_keyboard(self.channels)

            if isinstance(event, Message):
                await event.answer(text, reply_markup=keyboard)
            elif isinstance(event, CallbackQuery):
                await event.answer("⚠️ هنوز در تمام کانال‌ها عضو نشده‌اید!", show_alert=True)

            return None  # مسدود کردن پیام و جلوگیری از رسیدن به هندلرها

        # اگر در همه کانال‌ها عضو بود، به پردازش ادامه بده

        
        return await handler(event, data)

    