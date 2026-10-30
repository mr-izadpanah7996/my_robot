import asyncio
import logging
from google import genai
from google.genai import types
from google.genai.errors import APIError

logger = logging.getLogger(__name__)


class OCRError(Exception):


    """خطای اختصاصی لایه OCR برای ارسال پیام تمیز به هندلر"""

    pass


class OCRService:
    """
    سرویس ناهمگام استخراج متن از تصاویر با Google Gemini


    همراه با چرخش خودکار کلیدها و تلاش مجدد مستقل برای خطاهای شبکه و اینترنت
    """

    SYSTEM_PROMPT = (
        "You are an expert OCR engine. Extract all readable text from the provided image "
        "with maximum accuracy, preserving line breaks, paragraph structure, and formatting. "
        "Pay special attention to Persian, Arabic, and English characters. "
        "Output ONLY the extracted text directly. Do not add conversational remarks, "
        "explanations, or markdown notes."
    )

    def __init__(self, api_keys: list[str], model_name: str = "gemini-3.5-flash-lite") -> None:

        
        if not api_keys:
            raise ValueError("لیست کلیدهای API برای سرویس OCR خالی است!")

        self.api_keys: list[str] = api_keys
        self.model_name: str = model_name
        self.current_key_index: int = 0

    def _get_current_client(self) -> genai.Client:


        """ساخت کلاینت برای کلید فعال فعلی"""


        current_key = self.api_keys[self.current_key_index]
        return genai.Client(api_key=current_key)

    def _rotate_key(self) -> None:


        """انتقال شاخص به کلید بعدی"""


        old_index = self.current_key_index
        self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
        logger.warning(
            f"سوییچ از کلید ایندکس {old_index} به کلید ایندکس {self.current_key_index}."
        )

    async def extract_text(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
        """
        استخراج متن از بایت‌های تصویر به صورت ناهمگام (Async).
        
        :param image_bytes: دیتای خام تصویر به صورت بایت
        :param mime_type: نوع فایل تصویر (پیش‌فرض image/jpeg)
        :return: متن استخراج‌شده
        """
        key_attempts = 0
        total_keys = len(self.api_keys)

        network_retries = 0
        max_network_retries = 4

        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type=mime_type,
        )

        config = types.GenerateContentConfig(
            system_instruction=self.SYSTEM_PROMPT,
            temperature=0.0,
        )

        while key_attempts < total_keys:
            client = self._get_current_client()

            try:
                response = await client.aio.models.generate_content(
                    model=self.model_name,
                    contents=[image_part],
                    config=config,
                )

                extracted_text = response.text.strip() if response.text else ""
                if not extracted_text:
                    raise OCRError("هیچ متنی در تصویر ارسالی یافت نشد.")

                return extracted_text

            except APIError as e:


                # خطاهای مربوط به کلید، سهمیه یا خطای موقت سرور گوگل

                
                if e.code in (429, 403, 500, 503):
                    logger.warning(
                        f"خطای API گوگل (کد {e.code}) در کلید ایندکس {self.current_key_index}: {e.message}. سوییچ کلید..."
                    )
                    key_attempts += 1
                    self._rotate_key()
                    await asyncio.sleep(1)
                    continue

                logger.error(f"خطای غیرقابل بازگشت Google API: {e.message}")
                raise OCRError("خطایی در ارتباط با سرویس هوش مصنوعی رخ داد.") from e

            except (TimeoutError, ConnectionError, OSError) as net_err:
                network_retries += 1
                logger.warning(
                    f"خطای شبکه/اینترنت (تلاش {network_retries} از {max_network_retries}): {net_err}. تلاش مجدد پس از ۳ ثانیه..."
                )
                if network_retries < max_network_retries:
                    await asyncio.sleep(3)
                    continue

                raise OCRError("ارتباط اینترنت با سرور هوش مصنوعی پس از ۴ بار تلاش برقرار نشد.") from net_err

            except Exception as general_err:
                logger.exception(f"خطای پیش‌بینی‌نشده در استخراج متن: {general_err}")
                raise OCRError("در پردازش تصویر خطای نامشخصی رخ داد.") from general_err

        raise OCRError("تمام کلیدهای API موقتاً با محدودیت سهمیه مواجه شده‌اند. لطفاً دقایقی دیگر امتحان کنید.")

    