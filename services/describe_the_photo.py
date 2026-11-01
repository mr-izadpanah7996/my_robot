import asyncio
import logging
from google import genai
from google.genai import types
from google.genai.errors import APIError

logger = logging.getLogger(__name__)


class ImageDescriptionError(Exception):
    """خطای اختصاصی لایه توصیف تصویر برای ارسال پیام تمیز به هندلر"""
    pass


class ImageDescriptionService:
    """
    سرویس ناهمگام توصیف تصویر و پاسخ به پرسش‌های تکمیلی با Google Gemini
    همراه با چرخش خودکار کلیدها و تلاش مجدد مستقل برای خطاهای شبکه و اینترنت
    """

    SYSTEM_PROMPT = (
        "شما یک دستیار هوش مصنوعی دقیق و حرفه‌ای برای تحلیل و توصیف تصاویر هستید. "
        "تصویر ارائه‌شده را با تمام جزئیات، دقیق، عمیق و به زبان فارسی روان، شیوا و خوانا توصیف کنید. "
        "به موضوع اصلی، عناصر پس‌زمینه و پیش‌زمینه، افراد یا اشیاء، رنگ‌ها، نورپردازی، موقعیت مکانی، "
        "حس‌وحال صحنه و هرگونه نوشته یا نکته شاخص در تصویر به شکلی ساختاریافته اشاره کنید. "
        "از توصیف‌های سطحی یا مبهم خودداری کنید و متنی کامل و منظم ارائه دهید."
    )

    FOLLOWUP_SYSTEM_PROMPT = (
        "شما یک دستیار هوش مصنوعی دقیق برای پاسخ به سوال تکمیلی درباره یک تصویر هستید. "
        "به شما تصویر، توصیف اولیه‌ای که قبلاً ارائه شده، و سوال کاربر ارسال می‌شود. "
        "با تکیه مستقیم بر خود تصویر و با در نظر گرفتن توصیف قبلی، پاسخی کامل، دقیق و متمرکز به زبان فارسی به پرسش کاربر بدهید."
    )

    def __init__(self, api_keys: list[str], model_name: str = "gemini-2.5-flash-lite") -> None:
        if not api_keys:
            raise ValueError("لیست کلیدهای API برای سرویس توصیف تصویر خالی است!")

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

    async def _execute_with_retry(
        self,
        contents: list,
        system_instruction: str,
        temperature: float = 0.4,
    ) -> str:
        """متد مرکزی برای ارسال درخواست، چرخش کلیدها و هندل خطاهای شبکه"""
        key_attempts = 0
        total_keys = len(self.api_keys)

        network_retries = 0
        max_network_retries = 4

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
        )

        while key_attempts < total_keys:
            client = self._get_current_client()

            try:
                response = await client.aio.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                )

                result_text = response.text.strip() if response.text else ""
                if not result_text:
                    raise ImageDescriptionError("مدل پاسخی برای تصویر ارائه نداد.")

                return result_text

            except APIError as e:
                # خطاهای مربوط به سهمیه، دسترسی یا خطای موقت سرور گوگل
                if e.code in (429, 403, 500, 503):
                    logger.warning(
                        f"خطای API گوگل (کد {e.code}) در کلید ایندکس {self.current_key_index}: {e.message}. سوییچ کلید..."
                    )
                    key_attempts += 1
                    self._rotate_key()
                    await asyncio.sleep(1)
                    continue

                logger.error(f"خطای غیرقابل بازگشت Google API: {e.message}")
                raise ImageDescriptionError("خطایی در ارتباط با سرویس هوش مصنوعی رخ داد.") from e

            except (TimeoutError, ConnectionError, OSError) as net_err:
                network_retries += 1
                logger.warning(
                    f"خطای شبکه/اینترنت (تلاش {network_retries} از {max_network_retries}): {net_err}. تلاش مجدد پس از ۳ ثانیه..."
                )
                if network_retries < max_network_retries:
                    await asyncio.sleep(3)
                    continue

                raise ImageDescriptionError("ارتباط اینترنت با سرور هوش مصنوعی پس از ۴ بار تلاش برقرار نشد.") from net_err

            except Exception as general_err:
                logger.exception(f"خطای پیش‌بینی‌نشده در پردازش تصویر: {general_err}")
                raise ImageDescriptionError("در پردازش تصویر خطای نامشخصی رخ داد.") from general_err

        raise ImageDescriptionError("تمام کلیدهای API موقتاً با محدودیت سهمیه مواجه شده‌اند. لطفاً دقایقی دیگر امتحان کنید.")

    async def describe_image(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
        """
        توصیف دقیق و کامل تصویر به صورت ناهمگام (Async).

        :param image_bytes: دیتای خام تصویر به صورت بایت
        :param mime_type: نوع فایل تصویر (پیش‌فرض image/jpeg)
        :return: متن توصیف کامل تصویر
        """
        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type=mime_type,
        )

        return await self._execute_with_retry(
            contents=[image_part],
            system_instruction=self.SYSTEM_PROMPT,
            temperature=0.35,
        )

    async def answer_followup(
        self,
        image_bytes: bytes,
        initial_description: str,
        user_question: str,
        mime_type: str = "image/jpeg",
    ) -> str:
        """
        پاسخ به سوال تکمیلی کاربر با ارسال مجدد بایت‌های تصویر و سابقه توصیف قبلی.

        :param image_bytes: دیتای خام تصویر به صورت بایت
        :param initial_description: متن توصیف اولیه‌ای که قبلاً تولید شده بود
        :param user_question: سوال تکمیلی کاربر
        :param mime_type: نوع فایل تصویر
        :return: پاسخ مدل به سوال تکمیلی کاربر
        """
        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type=mime_type,
        )

        prompt_context = (
            f"توصیف ارائه‌شده قبلی از تصویر:\n{initial_description}\n\n"
            f"سوال کاربر درباره این تصویر:\n{user_question}"
        )

        return await self._execute_with_retry(
            contents=[image_part, prompt_context],
            system_instruction=self.FOLLOWUP_SYSTEM_PROMPT,
            temperature=0.3,
        )

    