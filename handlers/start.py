from datetime import datetime
from aiogram import Router, html
from aiogram.filters import CommandStart
from aiogram.types import Message
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

