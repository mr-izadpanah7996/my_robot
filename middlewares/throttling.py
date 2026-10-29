import time
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message


class AntiSpamMiddleware(BaseMiddleware):
    def __init__(self, limit_seconds: float = 1.0, cooldown_seconds: float = 4.0) -> None:


        """
        :param limit_seconds: حداقل فاصله زمانی مجاز بین دو پیام (مثلا ۱ ثانیه)
        :param cooldown_seconds: زمانی که کاربر بعد از اسپم باید ساکت بماند تا ریست شود (۵ ثانیه)
        """


        self.limit_seconds = limit_seconds
        self.cooldown_seconds = cooldown_seconds


        # ذخیره وضعیت کاربران در رم:

        # { user_id: {"last_time": 1729000000.0, "warned": False} }

        self.users_status: Dict[int, Dict[str, Any]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        
    
        if not isinstance(event, Message):
            return await handler(event, data)

        user = event.from_user
        if not user:
            return await handler(event, data)

        user_id = user.id
        current_time = time.time()

    
        if user_id in self.users_status:
            user_data = self.users_status[user_id]
            time_passed = current_time - user_data["last_time"]

            # سناریو ۱: کاربر بعد از مدتی استراحت پیام داده (بیشتر از کول‌داون)


            if time_passed > self.cooldown_seconds:
                user_data["warned"] = False
                user_data["last_time"] = current_time

            # سناریو ۲: اسپم شناسایی شد (فاصله پیام کمتر از حد مجاز است)


            elif time_passed < self.limit_seconds:
                user_data["last_time"] = current_time

                # فقط اگر قبلا هشدار نگرفته، یک بار هشدار بده


                if not user_data["warned"]:
                    user_data["warned"] = True
                    await event.answer("⚠️ لطفاً کمی صبر کنید! پیام‌های شما خیلی سریع ارسال می‌شوند.")

                # پیام متوقف می‌شود و به هندلرها نمی‌رسد


                return None

            # سناریو ۳: فاصله پیام عادی و بیشتر از ۱ ثانیه است


            else:
                user_data["warned"] = False
                user_data["last_time"] = current_time

        else:


            # اولین بار است که کاربر پیام می‌دهد


            self.users_status[user_id] = {
                "last_time": current_time,
                "warned": False,
            }

        # اگر مشکلی نبود، پیام به سمت هندلرها هدایت می‌شود

        
        return await handler(event, data)

    