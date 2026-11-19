import logging
import time
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from database.db import DatabaseManager

logger = logging.getLogger(__name__)


class UserTrackerMiddleware(BaseMiddleware):
    def __init__(self, cache_ttl: int = 300) -> None:
        # کش ساده در رم برای جلوگیری از نوشتن تکراری روی دیسک (پیش‌فرض: هر ۵ دقیقه یک‌بار)
        self.cache_ttl = cache_ttl
        self._cache: dict[int, tuple[str, str | None, float]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        db: DatabaseManager | None = data.get("db")
        user: User | None = data.get("event_from_user")

        if db and user and not user.is_bot:
            now = time.monotonic()
            cached = self._cache.get(user.id)

            # بررسی اینکه آیا کاربر جدید است یا نام/یوزرنیمش تغییر کرده یا زمان کش تمام شده است
            needs_update = (
                cached is None
                or cached[0] != user.full_name
                or cached[1] != user.username
                or (now - cached[2]) > self.cache_ttl
            )

            if needs_update:
                try:
                    db.add_or_update_user(
                        user_id=user.id,
                        full_name=user.full_name,
                        username=user.username,
                    )
                    self._cache[user.id] = (user.full_name, user.username, now)
                except Exception as e:
                    # ثبت خطا در لاگ بدون اینکه جریان پاسخ‌گویی به کاربر متوقف شود
                    logger.error(f"خطا در ثبت یا بروزرسانی کاربر {user.id} در دیتابیس: {e}")

        return await handler(event, data)

    