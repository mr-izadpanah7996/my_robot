from datetime import datetime
from aiogram import Router, html, F, Bot
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.enums import ChatMemberStatus

from config import Config
from keyboards.reply_user import get_main_menu

# تعریف روتر مخصوص دستور استارت


start_router = Router()


@start_router.message(CommandStart())
async def handle_start(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    # دریافت اطلاعات کاربر همراه با مدیریت مواردی که مقدار ندارند


    first_name = user.first_name
    last_name = user.last_name or "ثبت نشده"
    username = f"@{user.username}" if user.username else "ندارد"
    user_id = user.id

    # دریافت تاریخ و ساعت جاری


    now = datetime.now()
    current_time = now.strftime("%H:%M:%S")
    current_date = now.strftime("%Y/%m/%d")

    # ساخت پیام تمیز با متدهای امن html


    text = (
        f"سلام {html.bold(first_name)} عزیز، به ربات خوش آمدید!\n\n"
        f"👤 {html.bold('نام:')} {html.bold(first_name)}\n"
        f"👥 {html.bold('نام خانوادگی:')} {html.bold(last_name)}\n"
        f"🆔 {html.bold('نام کاربری:')} {html.code(username)}\n"
        f"🔢 {html.bold('شناسه عددی:')} {html.code(str(user_id))}\n\n"
        f"📅 {html.bold('تاریخ:')} {html.code(current_date)}\n"
        f"⏰ {html.bold('ساعت:')} {html.code(current_time)}"
    )

    await message.answer(text, reply_markup=get_main_menu())


# هندلر پردازش کلیک روی دکمه شیشه‌ای «عضو شدم، بررسی عضویت»


@start_router.callback_query(F.data == "check_membership")
async def check_membership_callback(callback: CallbackQuery, bot: Bot, config: Config) -> None:
    user = callback.from_user
    if not user:
        return

    channels = config.bot.channels

    # بررسی مجدد تک‌تک کانال‌ها


    for channel in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user.id)
            if member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED]:
                await callback.answer(


                    "❌ شما هنوز در تمام کانال‌ها عضو نشده‌اید!",
                    show_alert=True
                )
                return
        except Exception:
            pass

    # حذف پیام درخواست عضویت اجباری

    
    if callback.message:
        await callback.message.delete()

    # ارسال تاییدیه همراه با منوی اصلی
    await callback.message.answer(
        "✅ عضویت شما تایید شد! حالا می‌توانید از خدمات ربات استفاده کنید:",
        reply_markup=get_main_menu()
    )
    await callback.answer()

