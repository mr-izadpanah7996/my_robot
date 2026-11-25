from aiogram import Router
from handlers.start import start_router
from handlers.handler_ocr import ocr_router
from handlers.handler_pdf import pdf_router
from handlers.handler_describe_photo import describe_router
from handlers.handler_audio import router_audio
from handlers.handler_text_to_file import router_text
from handlers.audio_compressor_handler import router_audio_compressor
from handlers.video_compressor_handler import router_video_compressor
from handlers.speech_handler import speech_router
from handlers.tts_handler import tts_router
from handlers.admin import admin_router
from handlers.qr_handler import qr_router


router = Router()

router.include_routers(

    admin_router,
    

    start_router,

    ocr_router,

    pdf_router,

    router_audio,

    qr_router,

    router_text,

    router_audio_compressor,

    router_video_compressor,

    speech_router,

    tts_router,

    describe_router
    

)
