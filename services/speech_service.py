import asyncio
import logging
import os
import tempfile
from pathlib import Path
from google import genai
from google.genai import types
from google.genai.errors import APIError

logger = logging.getLogger(__name__)


class SpeechError(Exception):
    """خطای اختصاصی لایه تبدیل صوت به متن برای ارسال پیام تمیز به هندلر"""
    pass


class SpeechService:
    """
    سرویس ناهمگام تبدیل ویس و فایل‌های صوتی به متن با Google Gemini
    - پشتیبانی از تمام فرمت‌های صوتی (با تبدیل خودکار فرمت‌های قدیمی از طریق FFmpeg)
    - چرخش ایمن (Thread-Safe) بین کلیدهای API
    - مدیریت جامع خطاهای شبکه، سهمیه، فایل‌های خراب و سکوت مطلق
    """

    # فرمت‌هایی که Gemini API به صورت بومی و مستقیم پشتیبانی می‌کند
    NATIVE_GEMINI_EXTENSIONS: dict[str, str] = {
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".opus": "audio/ogg",
        ".mp3": "audio/mp3",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".aac": "audio/aac",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".webm": "audio/webm",
        ".aiff": "audio/aiff",
        ".aif": "audio/aiff",
    }

    # فرمت‌های قدیمی یا خاص که قبل از ارسال باید به MP3 استاندارد تبدیل شوند
    LEGACY_EXTENSIONS_NEEDING_CONVERSION: set[str] = {
        ".amr",
        ".wma",
        ".3gp",
        ".3gpp",
        ".ac3",
        ".ra",
        ".rm",
        ".mid",
        ".midi",
        ".caf",
    }

    SYSTEM_PROMPT = (
        "You are a professional, high-accuracy speech-to-text transcription engine. "
        "Listen carefully to the provided audio and transcribe every spoken word verbatim "
        "with maximum precision. Pay special attention to Persian, Arabic, and English speech, "
        "colloquial expressions, and technical terms. "
        "Apply proper punctuation (periods, commas, question marks) and organize long speech "
        "into clean, readable paragraphs without summarizing, translating, or altering the speaker's words. "
        "Output ONLY the transcribed text directly. Do not wrap output in markdown code blocks, "
        "and do not add timestamps or introductory/closing remarks. "
        "If the audio contains no intelligible speech, only background noise, or is completely silent, "
        "output exactly: [NO_SPEECH]"
    )

    def __init__(self, api_keys: list[str], model_name: str = "gemini-3.5-flash-lite") -> None:
        if not api_keys:
            raise ValueError("لیست کلیدهای API برای سرویس تبدیل صوت به متن خالی است!")

        self.api_keys: list[str] = api_keys
        self.model_name: str = model_name
        self.current_key_index: int = 0
        self._key_lock = asyncio.Lock()

    def _get_current_client(self) -> tuple[genai.Client, int]:
        """ساخت کلاینت برای کلید فعال فعلی همراه با ایندکس آن"""
        idx = self.current_key_index
        return genai.Client(api_key=self.api_keys[idx]), idx

    async def _rotate_key(self, failed_index: int) -> None:
        """انتقال ایمن شاخص به کلید بعدی (جلوگیری از سوییچ مضاعف در درخواست‌های هم‌زمان)"""
        async with self._key_lock:
            if self.current_key_index == failed_index:
                old_index = self.current_key_index
                self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
                logger.warning(
                    f"سوییچ کلید در سرویس صوتی از ایندکس {old_index} به ایندکس {self.current_key_index}."
                )

    @classmethod
    def resolve_mime_type(cls, file_name: str | None = None, mime_type: str | None = None) -> str:
        """تشخیص دقیق MIME Type برای فرمت‌های استاندارد صوتی"""
        if file_name:
            ext = Path(file_name).suffix.lower()
            if ext in cls.NATIVE_GEMINI_EXTENSIONS:
                return cls.NATIVE_GEMINI_EXTENSIONS[ext]

        if mime_type:
            normalized = mime_type.strip().lower().split(";")[0]
            mime_aliases = {
                "audio/x-m4a": "audio/mp4",
                "audio/m4a": "audio/mp4",
                "audio/mpeg": "audio/mp3",
                "audio/x-mp3": "audio/mp3",
                "audio/x-wav": "audio/wav",
                "audio/wave": "audio/wav",
                "audio/opus": "audio/ogg",
                "audio/vorbis": "audio/ogg",
            }
            if normalized in mime_aliases:
                return mime_aliases[normalized]
            if normalized in cls.NATIVE_GEMINI_EXTENSIONS.values():
                return normalized

        return "audio/ogg"

    @classmethod
    def _needs_ffmpeg_conversion(cls, file_name: str | None, mime_type: str | None) -> bool:
        """بررسی نیاز فایل به تبدیل فرمت پیش از ارسال به هوش مصنوعی"""
        if file_name:
            ext = Path(file_name).suffix.lower()
            if ext in cls.LEGACY_EXTENSIONS_NEEDING_CONVERSION:
                return True
            if ext and ext not in cls.NATIVE_GEMINI_EXTENSIONS:
                return True

        if mime_type:
            normalized = mime_type.strip().lower().split(";")[0]
            if any(legacy in normalized for legacy in ("amr", "wma", "3gp", "ac3", "caf")):
                return True

        return False

    async def _convert_to_mp3_with_ffmpeg(self, audio_bytes: bytes, ext: str) -> bytes:
        """تبدیل ناهمگام فایل‌های صوتی قدیمی یا خاص به MP3 استاندارد با FFmpeg"""
        in_fd, in_path = tempfile.mkstemp(suffix=ext or ".audio")
        out_fd, out_path = tempfile.mkstemp(suffix=".mp3")
        os.close(in_fd)
        os.close(out_fd)

        try:
            await asyncio.to_thread(Path(in_path).write_bytes, audio_bytes)

            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                in_path,
                "-vn",
                "-acodec",
                "libmp3lame",
                "-q:a",
                "4",
                out_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=45)

            if process.returncode != 0 or not Path(out_path).exists():
                err_msg = stderr.decode("utf-8", errors="ignore")[-300:]
                logger.error(f"خطای FFmpeg در تبدیل فرمت صوتی ({ext}): {err_msg}")
                raise SpeechError("فایل صوتی ارسالی خراب است یا فرمت آن قابل رمزگشایی نیست.")

            converted_bytes = await asyncio.to_thread(Path(out_path).read_bytes)
            if not converted_bytes:
                raise SpeechError("خروجی تبدیل فایل صوتی خالی بود؛ لطفاً فایل سالم‌تری ارسال کنید.")

            return converted_bytes

        except asyncio.TimeoutError as timeout_err:
            logger.error("تایم‌اوت FFmpeg در تبدیل فرمت صوتی")
            raise SpeechError("زمان پردازش و تبدیل فرمت این فایل صوتی بیش از حد طولانی شد.") from timeout_err
        except FileNotFoundError as fnf_err:
            logger.error("ابزار FFmpeg روی سرور یافت نشد.")
            raise SpeechError("پشتیبانی از این فرمت خاص صوتی موقتاً در دسترس نیست.") from fnf_err
        finally:
            for p in (in_path, out_path):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass

    @staticmethod
    def _clean_transcription_output(raw_text: str) -> str:
        """پاک‌سازی حصارهای احتمالی مارک‌داون از خروجی مدل"""
        text = raw_text.strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            if len(lines) >= 2:
                text = "\n".join(lines[1:-1]).strip()
        return text

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> str:
        """
        تبدیل بایت‌های ویس یا فایل صوتی به متن به صورت ناهمگام (Async).
        """
        if not audio_bytes:
            raise SpeechError("فایل صوتی دریافت‌شده خالی است.")

        working_bytes = audio_bytes
        if self._needs_ffmpeg_conversion(file_name=file_name, mime_type=mime_type):
            ext = Path(file_name).suffix.lower() if file_name else ".audio"
            logger.info(f"تبدیل خودکار فایل صوتی با فرمت {ext} به MP3 پیش از ارسال به جمینای...")
            working_bytes = await self._convert_to_mp3_with_ffmpeg(working_bytes, ext=ext)
            resolved_mime = "audio/mp3"
        else:
            resolved_mime = self.resolve_mime_type(file_name=file_name, mime_type=mime_type)

        audio_part = types.Part.from_bytes(
            data=working_bytes,
            mime_type=resolved_mime,
        )

        config = types.GenerateContentConfig(
            system_instruction=self.SYSTEM_PROMPT,
            temperature=0.0,
        )

        key_attempts = 0
        total_keys = len(self.api_keys)
        max_network_retries = 4

        try:
            while key_attempts < total_keys:
                client, used_key_index = self._get_current_client()
                network_retries = 0

                while network_retries < max_network_retries:
                    try:
                        response = await asyncio.wait_for(
                            client.aio.models.generate_content(
                                model=self.model_name,
                                contents=[audio_part],
                                config=config,
                            ),
                            timeout=120,
                        )

                        # بررسی مسدود شدن پاسخ توسط فیلترهای ایمنی جمینای
                        if response.candidates:
                            finish_reason = str(response.candidates[0].finish_reason or "")
                            if "SAFETY" in finish_reason.upper():
                                raise SpeechError(
                                    "محتوای این فایل صوتی به دلیل قوانین ایمنی هوش مصنوعی قابل پردازش نیست."
                                )

                        raw_text = ""
                        try:
                            raw_text = response.text or ""
                        except ValueError:
                            raw_text = ""

                        cleaned_text = self._clean_transcription_output(raw_text)
                        if not cleaned_text or "[NO_SPEECH]" in cleaned_text:
                            raise SpeechError("هیچ گفتار قابل تشخیصی در این فایل صوتی یافت نشد.")

                        return cleaned_text

                    except SpeechError:
                        raise

                    except APIError as e:
                        if e.code in (429, 403, 500, 503):
                            logger.warning(
                                f"خطای API گوگل در پردازش صدا (کد {e.code}) روی کلید ایندکس {used_key_index}: {e.message}. سوییچ کلید..."
                            )
                            key_attempts += 1
                            await self._rotate_key(used_key_index)
                            await asyncio.sleep(1)
                            break  # خروج از حلقه شبکه و تست کلید بعدی

                        if e.code == 400:
                            logger.error(f"فایل صوتی نامعتبر یا خراب برای Gemini API: {e.message}")
                            raise SpeechError(
                                "فایل صوتی ارسالی خراب است یا فرمت آن قابل خواندن نیست."
                            ) from e

                        logger.error(f"خطای غیرقابل بازگشت Google API در سرویس صوتی: {e.message}")
                        raise SpeechError("خطایی در ارتباط با سرویس هوش مصنوعی رخ داد.") from e

                    except (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError) as net_err:
                        network_retries += 1
                        logger.warning(
                            f"خطای شبکه/تایم‌اوت در سرویس صوتی (تلاش {network_retries} از {max_network_retries}): {net_err}"
                        )
                        if network_retries < max_network_retries:
                            await asyncio.sleep(3)
                            continue

                        raise SpeechError(
                            "ارتباط اینترنت با سرور هوش مصنوعی پس از ۴ بار تلاش برقرار نشد."
                        ) from net_err

                    except Exception as general_err:
                        logger.exception(f"خطای پیش‌بینی‌نشده در تبدیل صدا به متن: {general_err}")
                        raise SpeechError("در پردازش فایل صوتی خطای نامشخصی رخ داد.") from general_err

            raise SpeechError(
                "تمام کلیدهای API موقتاً با محدودیت سهمیه مواجه شده‌اند. لطفاً دقایقی دیگر امتحان کنید."
            )
        finally:
            working_bytes = b""
            audio_part = None

            