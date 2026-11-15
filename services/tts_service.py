import asyncio
import logging
import re
from typing import Literal

import aiohttp
import edge_tts

# ایمپورت ایمن استثناهای edge_tts جهت سازگاری با تمام نسخه‌ها
try:
    from edge_tts.exceptions import NoAudioReceived, WebSocketError
except ImportError:
    NoAudioReceived = Exception  # type: ignore[misc, assignment]
    WebSocketError = Exception  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

VoiceGender = Literal["male", "female"]


class TTSError(Exception):
    """خطای اختصاصی لایه تبدیل متن به صدا برای ارسال پیام تمیز به هندلر"""
    pass


class TTSService:
    """
    سرویس ناهمگام تبدیل متن به ویس با کیفیت بالا (Edge Neural TTS)
    - تشخیص خودکار زبان متن (فارسی/عربی و انگلیسی بومی)
    - انتخاب هوشمند گوینده (فرید و دلارام برای فارسی / Andrew و Ava برای انگلیسی)
    - پاک‌سازی هوشمند متن (حفظ نیم‌فاصله فارسی، حذف لینک‌ها و کاراکترهای مخرب SSML)
    - مدیریت جامع خطاهای شبکه، WebSocket، تایم‌اوت و متن‌های غیرقابل تلفظ
    """

    PERSIAN_VOICES: dict[VoiceGender, str] = {
        "male": "fa-IR-FaridNeural",
        "female": "fa-IR-DilaraNeural",
    }

    ENGLISH_VOICES: dict[VoiceGender, str] = {
        "male": "en-US-AndrewNeural",
        "female": "en-US-AvaNeural",
    }

    MAX_TEXT_LENGTH: int = 4000

    # الگوی حذف کاراکترهای کنترل نامعتبر XML که در موتور SSML مایکروسافت باعث خطا می‌شوند
    _INVALID_XML_CHARS_RE = re.compile(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f\ufdd0-\ufdef\ufffe\uffff]"
    )

    # الگوی تشخیص لینک‌های اینترنتی برای جلوگیری از هجی شدن طولانی آن‌ها در ویس
    _URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

    def __init__(self, max_text_length: int = 4000) -> None:
        self.max_text_length = max_text_length

    @classmethod
    def _clean_text_for_speech(cls, raw_text: str) -> str:
        """
        پاک‌سازی هوشمند متن پیش از ارسال به موتور صوتی:
        - حفظ نیم‌فاصله فارسی (\u200c) برای تلفظ صحیح کلمات فارسی
        - حذف کاراکترهای جهت‌دهی نامرئی و کاراکترهای غیرمجاز XML/SSML
        - حذف لینک‌های طولانی و علائم تزئینی تکراری
        """
        if not raw_text:
            return ""

        # حذف کاراکترهای جهت‌دهی نامرئی و BOM (با حفظ نیم‌فاصله \u200c)
        text = (
            raw_text.replace("\u200e", "")
            .replace("\u200f", "")
            .replace("\ufeff", "")
            .replace("\u200b", "")
        )

        # حذف کاراکترهای کنترلی نامعتبر XML
        text = cls._INVALID_XML_CHARS_RE.sub("", text)

        # حذف لینک‌های اینترنتی تا گوینده حروف URL را یکی‌یکی هجی نکند
        text = cls._URL_RE.sub(" ", text)

        # حذف کاراکترهای تزئینی تکراری (مثل === یا --- یا ***)
        text = re.sub(r"[*#_~=`]{2,}", " ", text)
        text = re.sub(r"[-=]{3,}", " ", text)

        # نرمال‌سازی فاصله‌ها و خطوط خالی
        lines = [re.sub(r"[ \t]{2,}", " ", line).strip() for line in text.splitlines()]
        cleaned = "\n".join(line for line in lines if line)
        return cleaned.strip()

    @classmethod
    def _detect_voice_name(cls, text: str, gender: VoiceGender) -> str:
        """
        انتخاب هوشمند نام گوینده بر اساس محتوای متن:
        - اگر متن دارای حروف فارسی/عربی باشد: فرید (مرد) یا دلارام (زن)
        - در غیر این صورت (متن انگلیسی): Andrew (مرد) یا Ava (زن)
        """
        has_persian_or_arabic = bool(re.search(r"[\u0600-\u06FF]", text))
        if has_persian_or_arabic:
            return cls.PERSIAN_VOICES[gender]
        return cls.ENGLISH_VOICES[gender]

    @staticmethod
    async def _stream_audio_bytes(text: str, voice_name: str) -> bytes:
        """دریافت جریانی بایت‌های صوتی MP3 از سرور Edge TTS در حافظه RAM"""
        audio_chunks = bytearray()
        try:
            communicate = edge_tts.Communicate(
                text=text,
                voice=voice_name,
            )
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    data = chunk.get("data")
                    if data:
                        audio_chunks.extend(data)
            return bytes(audio_chunks)
        finally:
            audio_chunks.clear()

    async def synthesize_speech(
        self,
        text: str,
        gender: VoiceGender = "male",
    ) -> bytes:
        """
        تبدیل متن به بایت‌های فایل صوتی MP3 به صورت ناهمگام (Async) در حافظه RAM.

        :param text: متن ورودی کاربر
        :param gender: جنسیت صدای گوینده ('male' یا 'female')
        :return: دیتای باینری فایل صوتی MP3
        :raises TTSError: در صورت خالی بودن متن، عبور از سقف مجاز یا بروز خطای ارتباطی
        """
        if gender not in ("male", "female"):
            raise TTSError("نوع صدای انتخاب‌شده نامعتبر است.")

        if not text or not text.strip():
            raise TTSError("متن ارسالی خالی است و امکان تبدیل به صدا وجود ندارد.")

        if len(text) > self.max_text_length:
            raise TTSError(
                f"طول متن ({len(text)} کاراکتر) بیشتر از سقف مجاز ({self.max_text_length} کاراکتر) است."
            )

        cleaned_text = self._clean_text_for_speech(text)
        if not cleaned_text or not re.search(r"[a-zA-Z0-9\u0600-\u06FF]", cleaned_text):
            raise TTSError(
                "متن ارسالی فاقد کلمات قابل خواندن است (لطفاً از ارسال ایموجی، لینک یا علائم به تنهایی خودداری کنید)."
            )

        voice_name = self._detect_voice_name(cleaned_text, gender)
        max_network_retries = 3
        attempt = 0

        while attempt < max_network_retries:
            attempt += 1
            try:
                audio_data = await asyncio.wait_for(
                    self._stream_audio_bytes(text=cleaned_text, voice_name=voice_name),
                    timeout=90,
                )

                if not audio_data:
                    raise TTSError("فایل صوتی تولید نشد. لطفاً متن خود را بررسی و مجدداً تلاش کنید.")

                return audio_data

            except asyncio.CancelledError:
                raise

            except NoAudioReceived as no_audio_err:
                logger.warning(f"NoAudioReceived در تبدیل متن به صدا (گوینده {voice_name}): {no_audio_err}")
                raise TTSError(
                    "متن ارسالی قابل تلفظ نبود (لطفاً از ارسال ایموجی یا کاراکترهای خاص به تنهایی خودداری کنید)."
                ) from no_audio_err

            except TTSError:
                raise

            except (
                WebSocketError,
                aiohttp.ClientError,
                asyncio.TimeoutError,
                TimeoutError,
                ConnectionError,
                OSError,
            ) as net_err:
                logger.warning(
                    f"خطای شبکه/اتصال در سرویس تبدیل متن به صدا (تلاش {attempt} از {max_network_retries}): {net_err}"
                )
                if attempt < max_network_retries:
                    # وقفه افزایشی (۲، ۴ ثانیه) برای پایداری در برابر اختلالات موقت شبکه
                    await asyncio.sleep(2 * attempt)
                    continue

                raise TTSError(
                    "ارتباط با سرور تولید صدا پس از چند بار تلاش برقرار نشد. لطفاً دقایقی دیگر امتحان کنید."
                ) from net_err

            except Exception as general_err:
                logger.exception(f"خطای پیش‌بینی‌نشده در سرویس تبدیل متن به صدا: {general_err}")
                raise TTSError("متأسفانه در فرآیند تبدیل متن به ویس خطای غیرمنتظره‌ای رخ داد.") from general_err

        raise TTSError("تولید فایل صوتی ناموفق بود. لطفاً مجدداً تلاش کنید.")

    