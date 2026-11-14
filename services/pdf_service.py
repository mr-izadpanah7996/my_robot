import asyncio
import logging
import re
import unicodedata
from typing import Tuple

import pymupdf as fitz
from services.ocr import OCRError, OCRService

logger = logging.getLogger(__name__)


class PDFPageLimitExceeded(Exception):
    pass


class PDFEncryptedError(Exception):
    pass


class PDFEmptyError(Exception):
    pass


class PDFService:
    # حروف فارسی که فقط در ابتدا یا وسط کلمه می‌آیند و اگر در آخر کلمه باشند نشانه معکوس بودن فونت است
    _STRICT_JOINING_END_CHARS = set("بپتثجچحخسشصضطظعغفقکگلمنئ")

    # کلمات سالم و پرکاربرد فارسی که با حروف غیرچسبان شروع و با حروف چسبان تمام می‌شوند (جلوگیری از تشخیص اشتباه)
    _COMMON_VALID_WORDS = {
        "است", "این", "انها", "آنها", "ایران", "ایمنی", "انرژی", "اصول", "امور",
        "دست", "راست", "دوست", "درست", "رشت", "داشت", "رفت", "گفت", "وقت",
        "اصل", "اهل", "اول", "دول", "روشن", "امن", "آهن", "آب", "اسب", "ادب",
        "اسپ", "امید", "آباد", "آزاد", "ارشد", "افراد", "ایجاد", "اسناد", "اعداد",
    }

    def __init__(
        self,
        ocr_service: OCRService,
        max_pages: int = 1000,
        max_scanned_pages: int = 150,
        batch_size: int = 10,
    ) -> None:
        self.ocr_service = ocr_service
        self.max_pages = max_pages
        self.max_scanned_pages = max_scanned_pages
        self.batch_size = batch_size

    def _open_document(self, file_bytes: bytes) -> fitz.Document:
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
        except Exception as e:
            raise PDFEmptyError("فایل پی‌دی‌اف معتبر نیست یا خراب است.") from e

        if doc.is_encrypted:
            doc.close()
            raise PDFEncryptedError("این فایل پی‌دی‌اف با رمز محافظت شده است و قابل خواندن نیست.")

        if len(doc) == 0:
            doc.close()
            raise PDFEmptyError("فایل پی‌دی‌اف خالی است.")

        if len(doc) > self.max_pages:
            total = len(doc)
            doc.close()
            raise PDFPageLimitExceeded(
                f"تعداد صفحات فایل ({total} صفحه) بیشتر از سقف مجاز ({self.max_pages} صفحه) است."
            )

        return doc

    @classmethod
    def _is_garbled_or_reversed_persian(cls, text: str) -> bool:
        """
        تشخیص دقیق خرابی فونت فارسی در لایه متنی PDF:
        ۱. کلمات معکوس (Visual Order)
        ۲. شکستگی حروف وسط کلمات (تک‌حرفی‌های سرگردان مثل 'بهداشت ی')
        ۳. ترکیب معکوس 'لا' به صورت 'ال' (مثل 'اصطالحات' و 'باال')
        """
        words = re.findall(r"[\u0600-\u06FF]{3,}", text)
        if not words:
            return False

        # الف) بررسی نسبت کلمات آینه‌ای (برعکس) با حذف کلمات سالم پرکاربرد
        reversed_suspects = 0
        non_joiners = set("ادذرزژوآ")
        for w in words:
            if w in cls._COMMON_VALID_WORDS:
                continue
            if w[0] in non_joiners and w[-1] in cls._STRICT_JOINING_END_CHARS:
                reversed_suspects += 1

        if (reversed_suspects / len(words)) > 0.08:
            return True

        # ب) بررسی تک‌حرفی‌های سرگردان فارسی (به جز 'و' ربط) که نشانه شکستگی فونت است
        isolated_letters = re.findall(r"(?:^|\s)[بپتثجچحخدذرزژسشصضطظعغفقکگلمنهیئ](?:\s|$)", text)
        if len(isolated_letters) >= 5 and (len(isolated_letters) / len(words)) > 0.04:
            return True

        # ج) بررسی وجود ترکیب معکوس 'ال' وسط کلمات فارسی (مانند اصطالحات، اسالمی، باال)
        broken_la = re.findall(r"[\u0600-\u06FF]ال[\u0600-\u06FF]", text)
        if len(broken_la) >= 2:
            return True

        return False

    def _analyze_pdf_type_sync(self, doc: fitz.Document) -> bool:
        """
        بررسی سلامت لایه متنی PDF:
        True = فونت استاندارد و سالم (استخراج مستقیم و رایگان)
        False = اسکن تصویری یا فونت فارسی شکسته/معکوس (نیازمند OCR دسته‌ای با جمینای)
        """
        sample_pages = min(len(doc), 5)
        sampled_text_parts = []

        for i in range(sample_pages):
            page = doc.load_page(i)
            text = page.get_text("text", sort=True).strip()
            if text:
                sampled_text_parts.append(text)

        combined_sample = "\n".join(sampled_text_parts)

        # ۱. اگر متن بسیار کم است، فایل اسکن تصویری است
        if len(combined_sample) <= 50:
            return False

        # ۲. اگر فونت فارسی شکسته یا برعکس است، سوییچ به OCR دسته‌ای
        if self._is_garbled_or_reversed_persian(combined_sample):
            logger.info("فونت فارسی شکسته/معکوس شناسایی شد. سوییچ به پردازش دسته‌ای Gemini OCR.")
            return False

        return True

    @classmethod
    def _clean_text(cls, raw_text: str) -> str:
        if not raw_text:
            return ""

        text = unicodedata.normalize("NFKC", raw_text)

        replacements = {
            "ي": "ی",
            "ى": "ی",
            "ك": "ک",
            "ة": "ه",
            "ـ": "",
            "\u200b": "",
            "\u200e": "",
            "\u200f": "",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)

        cleaned_lines = []
        for line in text.splitlines():
            line = re.sub(r"[ \t]{2,}", " ", line).strip()
            if line:
                cleaned_lines.append(line)

        return "\n".join(cleaned_lines)

    def _extract_clean_native_text_sync(self, doc: fitz.Document) -> list[str]:
        results = []
        flags = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_DEHYPHENATE
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            raw_text = page.get_text("text", flags=flags, sort=True)
            cleaned_text = self._clean_text(raw_text)
            header = f"--- صفحه {page_num + 1} ---\n"
            results.append(header + (cleaned_text if cleaned_text else "[این صفحه فاقد متن بود]"))
        return results

    def _render_batch_images_sync(
        self,
        doc: fitz.Document,
        start_idx: int,
        end_idx: int,
        matrix: fitz.Matrix,
    ) -> list[tuple[int, bytes]]:
        """رندر یک دسته (مثلاً ۱۰ صفحه) به JPEG فشرده در حافظه RAM"""
        batch_pages: list[tuple[int, bytes]] = []
        for idx in range(start_idx, end_idx):
            page = doc.load_page(idx)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            img_bytes = pix.tobytes("jpeg", jpg_quality=82)
            pix = None
            batch_pages.append((idx + 1, img_bytes))
        return batch_pages

    async def process_pdf(self, file_bytes: bytes) -> Tuple[str, bool]:
        doc = await asyncio.to_thread(self._open_document, file_bytes)

        try:
            has_healthy_text = await asyncio.to_thread(self._analyze_pdf_type_sync, doc)
            needs_ocr = not has_healthy_text
            total_pages = len(doc)

            if needs_ocr and total_pages > self.max_scanned_pages:
                raise PDFPageLimitExceeded(
                    f"این فایل پی‌دی‌اف از نوع اسکن‌شده یا دارای فونت غیراستاندارد است و {total_pages} صفحه دارد.\n"
                    f"حداکثر سقف مجاز برای این نوع فایل‌ها {self.max_scanned_pages} صفحه است "
                    f"(فایل‌های متنی استاندارد تا {self.max_pages} صفحه پشتیبانی می‌شوند)."
                )

            result_chunks = []

            if not needs_ocr:
                result_chunks = await asyncio.to_thread(self._extract_clean_native_text_sync, doc)
            else:
                dpi = 140
                zoom = dpi / 72
                matrix = fitz.Matrix(zoom, zoom)
                successful_batches = 0
                last_ocr_error: OCRError | None = None

                for start_idx in range(0, total_pages, self.batch_size):
                    end_idx = min(start_idx + self.batch_size, total_pages)
                    batch_pages: list[tuple[int, bytes]] = []

                    try:
                        batch_pages = await asyncio.to_thread(
                            self._render_batch_images_sync,
                            doc,
                            start_idx,
                            end_idx,
                            matrix,
                        )
                        batch_text = await self.ocr_service.extract_text_batch(batch_pages)
                        result_chunks.append(self._clean_text(batch_text))
                        successful_batches += 1
                    except OCRError as e:
                        last_ocr_error = e
                        logger.warning(f"خطای OCR در صفحات {start_idx + 1} تا {end_idx}: {e}")
                        result_chunks.append(
                            f"--- صفحات {start_idx + 1} تا {end_idx} ---\n"
                            f"[خطا در پردازش هوشمند این بخش: {e}]"
                        )
                    finally:
                        batch_pages.clear()

                    if end_idx < total_pages:
                        await asyncio.sleep(1.0)

                # اگر هیچ دسته‌ای موفق نبود، خطا را به هندلر پاس بده تا سهمیه کاربر نسوزد
                if successful_batches == 0 and last_ocr_error is not None:
                    raise last_ocr_error

            full_text = "\n\n".join(result_chunks).strip()
            return full_text, needs_ocr

        finally:
            doc.close()

            