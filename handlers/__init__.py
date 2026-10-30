from aiogram import Router
from handlers.start import start_router
from handlers.handler_ocr import ocr_router
from handlers.handler_pdf import pdf_router


router = Router()

router.include_routers(

    start_router,

    ocr_router,

    pdf_router

)
