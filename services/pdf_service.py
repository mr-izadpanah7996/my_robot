import asyncio
import io
import logging
import re
import unicodedata
from typing import Tuple

import pymupdf as fitz  # رفع اخطار FutureWarning در نسخه‌های جدید PyMuPDF
import pytesseract
from PIL import Image
from services.ocr import OCRService

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

    def __init__(
        self,
        ocr_service: OCRService | None = None,
        max_pages: int = 1000,
        max_scanned_pages: int = 150,
    ) -> None:
        self.ocr_service = ocr_service
        self.max_pages = max_pages
        self.max_scanned_pages = max_scanned_pages
        self.tesseract_lang = "fas+eng"
        # --oem 1 (موتور شبکه عصبی LSTM) و --psm 3 (تشخیص خودکار صفحات دو ستونه و پاراگراف‌ها)
        self.tesseract_config = "--oem 1 --psm 3"

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
        تشخیص اینکه آیا لایه متنی PDF دارای فونت فارسی معکوس (Visual Order) یا به‌هم‌ریخته است.
        """
        words = re.findall(r"[\u0600-\u06FF]{3,}", text)
        if not words:
            return False

        reversed_suspects = 0
        non_joiners = set("ادذرزژوآ")

        for w in words:
            # در فارسی استاندارد، کلمات بسیار کمی با د/ر/و/ا شروع و به حروف چسبان مثل ب/ت/س/ط ختم می‌شوند
            # اما وقتی کلمه برعکس ذخیره شده باشد (مثل "تاحالطصا" به جای "اصطلاحات") این الگو تکرار می‌شود
            if w[0] in non_joiners and w[-1] in cls._STRICT_JOINING_END_CHARS:
                reversed_suspects += 1

        ratio = reversed_suspects / len(words)
        # اگر بیش از ۱۲ درصد کلمات ساختار معکوس داشته باشند، لایه متنی PDF خراب است
        return ratio > 0.12

    def _analyze_pdf_type_sync(self, doc: fitz.Document) -> bool:
        """
        بررسی می‌کند آیا می‌توان به متن خام PDF اعتماد کرد (True)
        یا باید صفحات با Tesseract OCR خوانده شوند (False).
        """
        sample_pages = min(len(doc), 5)
        sampled_text_parts = []

        for i in range(sample_pages):
            page = doc.load_page(i)
            text = page.get_text("text", sort=True).strip()
            if text:
                sampled_text_parts.append(text)

        combined_sample = "\n".join(sampled_text_parts)

        # ۱. اگر متن بسیار کم است، فایل اسکن تصویری است -> نیاز به OCR
        if len(combined_sample) <= 50:
            return False

        # ۲. اگر متن دارد اما فونت فارسی آن برعکس یا خراب کدگذاری شده -> نیاز به OCR
        if self._is_garbled_or_reversed_persian(combined_sample):
            logger.info("فونت فارسی معکوس/خراب در لایه متنی PDF شناسایی شد. سوییچ خودکار به موتور Tesseract OCR.")
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
        """استخراج سریع برای PDFهایی که لایه متنی کاملاً استاندارد و سالم دارند"""
        results = []
        flags = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_DEHYPHENATE
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            raw_text = page.get_text("text", flags=flags, sort=True)
            cleaned_text = self._clean_text(raw_text)
            header = f"--- صفحه {page_num + 1} ---\n"
            results.append(header + (cleaned_text if cleaned_text else "[این صفحه فاقد متن بود]"))
        return results

    def _ocr_single_page_tesseract_sync(self, doc: fitz.Document, page_num: int, matrix: fitz.Matrix) -> str:
        """رندر یک صفحه در RAM و استخراج متن فارسی/انگلیسی با Tesseract"""
        page = doc.load_page(page_num)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        img_bytes = pix.tobytes("png")
        pix = None  # آزادسازی فوری حافظه Pixmap

        with Image.open(io.BytesIO(img_bytes)) as img:
            gray_img = img.convert("L")
            ocr_raw = pytesseract.image_to_string(
                gray_img,
                lang=self.tesseract_lang,
                config=self.tesseract_config,
            )

        del img_bytes
        cleaned = self._clean_text(ocr_raw)
        return cleaned if cleaned else "[متنی در این صفحه یافت نشد]"

    async def process_pdf(self, file_bytes: bytes) -> Tuple[str, bool]:
        doc = await asyncio.to_thread(self._open_document, file_bytes)

        try:
            has_healthy_text = await asyncio.to_thread(self._analyze_pdf_type_sync, doc)
            needs_ocr = not has_healthy_text
            total_pages = len(doc)

            if needs_ocr and total_pages > self.max_scanned_pages:
                raise PDFPageLimitExceeded(
                    f"این فایل پی‌دی‌اف نیازمند پردازش تصویری (OCR) است و {total_pages} صفحه دارد.\n"
                    f"حداکثر سقف مجاز برای این نوع فایل‌ها {self.max_scanned_pages} صفحه است."
                )

            result_chunks = []

            if not needs_ocr:
                result_chunks = await asyncio.to_thread(self._extract_clean_native_text_sync, doc)
            else:
                # DPI 180 تعادل عالی بین سرعت روی سرور ضعیف و دقت بالای خواندن فونت فارسی
                dpi = 180
                zoom = dpi / 72
                matrix = fitz.Matrix(zoom, zoom)

                for page_index in range(total_pages):
                    try:
                        page_text = await asyncio.to_thread(
                            self._ocr_single_page_tesseract_sync,
                            doc,
                            page_index,
                            matrix,
                        )
                        result_chunks.append(f"--- صفحه {page_index + 1} ---\n{page_text}")
                    except Exception as e:
                        logger.error(f"خطا در Tesseract OCR صفحه {page_index + 1}: {e}")
                        result_chunks.append(
                            f"--- صفحه {page_index + 1} ---\n[خطا در استخراج متن این صفحه]"
                        )

                    # تنفس کوتاه به Event Loop تا ربات در حین پردازش صفحات زیاد قفل نکند
                    await asyncio.sleep(0.05)

            full_text = "\n\n".join(result_chunks).strip()
            return full_text, needs_ocr

        finally:
            doc.close()
            