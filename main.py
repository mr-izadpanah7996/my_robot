import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from config import Config, load_config
from handlers import router
from middlewares.subscription import MandatorySubMiddleware
from middlewares.throttling import AntiSpamMiddleware

# ساخت لاگر مخصوص این فایل

logger = logging.getLogger(__name__)


async def main() -> None:

    # تنظیمات لاگینگ برای نمایش زنده در کنسول/ترمینال با سطح INFO

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger.info("در حال خواندن فایل تنظیمات...")
    config: Config = load_config()

    # اتصال به سرور محلی تلگرام در صورت تنظیم آدرس اختصاصی

    session: AiohttpSession | None = None
    if config.bot.api_server and config.bot.api_server != "https://api.telegram.org":
        logger.info(f"اتصال ربات به سرور محلی تلگرام: {config.bot.api_server}")
        server = TelegramAPIServer.from_base(config.bot.api_server, is_local=True)
        session = AiohttpSession(api=server)

    # ساخت شیء بات با نشست اختصاصی (یا پیش‌فرض)

    bot = Bot(
        token=config.bot.token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()

    # اتصال میدلورها به دیسپچر

    dp.message.outer_middleware(AntiSpamMiddleware(limit_seconds=1.0, cooldown_seconds=4.0))
    dp.message.outer_middleware(MandatorySubMiddleware(channels=config.bot.channels))

    # تزریق شیء کانفیگ به کل جریان ربات (قابل دسترسی در همه هندلرها)

    dp["config"] = config

    dp.include_router(router)

    # نادیده گرفتن آپدیت‌هایی که زمان خاموش بودن ربات ارسال شده‌اند

    
    await bot.delete_webhook(drop_pending_updates=True)

    logger.info("ربات با موفقیت روشن شد و در حال دریافت پیام‌ها است.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("ربات به صورت دستی توسط ادمین متوقف شد.")

