import os
from dataclasses import dataclass, field
from dotenv import load_dotenv


@dataclass(slots=True)
class BotConfig:
    token: str
    admin_ids: list[int] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    api_server: str = "https://api.telegram.org"
    video_archive_channel_id: int = 0


@dataclass(slots=True)
class AIConfig:
    api_keys: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Config:
    bot: BotConfig
    ai: AIConfig


def load_config() -> Config:
    # پارامتر override=True تضمین می‌کند با تغییر فایل .env، مقادیر جدید حتما جایگزین شوند
    load_dotenv(override=True)

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("خطای بحرانی: مقدار BOT_TOKEN در فایل .env تنظیم نشده است!")

    # پردازش آیدی ادمین‌ها: استخراج مقادیر معتبر عددی
    admin_ids_raw = os.getenv("ADMIN_IDS", "")
    admin_ids: list[int] = [
        int(item.strip())
        for item in admin_ids_raw.split(",")
        if item.strip().isdigit()
    ]

    # پردازش کلیدهای API: فیلتر کردن مقادیر خالی یا مقدار پیش‌فرض اولیه
    ai_keys_raw = os.getenv("AI_API_KEYS", "")
    api_keys: list[str] = [
        key.strip()
        for key in ai_keys_raw.split(",")
        if key.strip() and key.strip() != "YOUR_AI_API_KEY_HERE"
    ]

    # پردازش یوزرنیم کانال‌ها: پاک‌سازی فاصله‌ها و اطمینان از وجود @ اول آیدی
    channels_raw = os.getenv("CHANNELS", "")
    channels: list[str] = [
        f"@{ch.strip().lstrip('@')}"
        for ch in channels_raw.split(",")
        if ch.strip()
    ]

    # آدرس سرور تلگرام (پیش‌فرض رسمی، در صورت وجود لوکال از .env خوانده می‌شود)
    api_server = os.getenv("TELEGRAM_API_SERVER", "https://api.telegram.org").strip()

    # پردازش آیدی عددی کانال خصوصی آرشیو موقت ویدیو
    archive_channel_raw = os.getenv("VIDEO_ARCHIVE_CHANNEL_ID", "").strip()
    if not archive_channel_raw:
        raise ValueError("خطای بحرانی: مقدار VIDEO_ARCHIVE_CHANNEL_ID در فایل .env تنظیم نشده است!")

    if not archive_channel_raw.lstrip("-").isdigit():
        raise ValueError("خطای بحرانی: مقدار VIDEO_ARCHIVE_CHANNEL_ID باید یک عدد معتبر باشد!")

    video_archive_channel_id = int(archive_channel_raw)

    return Config(
        bot=BotConfig(
            token=bot_token,
            admin_ids=admin_ids,
            channels=channels,
            api_server=api_server,
            video_archive_channel_id=video_archive_channel_id,
        ),
        ai=AIConfig(api_keys=api_keys),
    )

