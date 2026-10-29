import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from handlers import router
from config import Config, load_config
from middlewares.throttling import AntiSpamMiddleware
from middlewares.subscription import MandatorySubMiddleware


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

    # ساخت شیء بات همراه با تنظیم پیش‌فرض HTML برای متون امن


    bot = Bot(
        token=config.bot.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()

    # اتصال میدلور ها به دیسپچر

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

