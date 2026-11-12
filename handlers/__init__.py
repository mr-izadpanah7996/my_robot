from aiogram import Router
from handlers.start import start_router
from handlers.handler_ocr import ocr_router
from handlers.handler_pdf import pdf_router
from handlers.handler_describe_photo import describe_router
from handlers.handler_audio import router_audio
from handlers.handler_text_to_file import router_text
from handlers.audio_compressor_handler import router_audio_compressor


router = Router()

router.include_routers(

    start_router,

    ocr_router,

    pdf_router,

    router_audio,

    router_text,
    router_audio_compressor,

    describe_router
    

)
