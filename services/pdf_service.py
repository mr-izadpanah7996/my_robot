import asyncio
import io
import logging
import re
import unicodedata
from typing import Generator, Tuple

import fitz  # PyMuPDF
from services.ocr import OCRError, OCRService

logger = logging.getLogger(__name__)


class PDFPageLimitExceeded(Exception):
    pass


class PDFEncryptedError(Exception):
    pass


class PDFEmptyError(Exception):
    pass


class PDFService:
    # حروف فارسی که به حرف بعدی خود می‌چسبند
    _JOINING_CHARS = set("بپتثجچحخسشصضطظعغفقکگلمنهیئ")
    _PERSIAN_CHARS = set("ابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهیآأإؤئة")

    def __init__(
        self,
        ocr_service: OCRService,
        max_pages: int = 1000,
        max_scanned_pages: int = 25,
    ) -> None:
        self.ocr_service = ocr_service
        self.max_pages = max_pages
        self.max_scanned_pages = max_scanned_pages

    def _open_document(self, file_bytes: bytes) -> fitz.Document:
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
        except Exception as e:
            raise PDFEmptyError("فایل پی‌دی‌اف معتبر نیست یا خراب است.") from e

        if doc.is_encrypted:
            raise PDFEncryptedError("این فایل پی‌دی‌اف با رمز محافظت شده است و قابل خواندن نیست.")

        if len(doc) == 0:
            raise PDFEmptyError("فایل پی‌دی‌اف خالی است.")

        if len(doc) > self.max_pages:
            raise PDFPageLimitExceeded(
                f"تعداد صفحات فایل ({len(doc)} صفحه) بیشتر از سقف مجاز ({self.max_pages} صفحه) است."
            )

        return doc

    @classmethod
    def _fix_reversed_persian_words(cls, text: str) -> str:
        def _repair_token(match: re.Match) -> str:
            word = match.group(0)
            if len(word) < 3:
                return word
            non_joiners = set("ادذرزژوآ")
            if word[0] in non_joiners and word[-1] in cls._JOINING_CHARS:
                reversed_w = word[::-1]
                if word not in {"است", "این", "انها", "آنها", "ایران", "ایمنی", "انرژی", "اصول", "امور"}:
                    return reversed_w
            return word

        return re.sub(r"[\u0600-\u06FF]{3,}", _repair_token, text)

    @classmethod
    def _clean_persian_text(cls, raw_text: str) -> str:
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

        text = re.sub(r"(?<=[\u0600-\u06FF])ال(?=[\u0600-\u06FF])", "لا", text)
        text = re.sub(r"(?<=[\u0600-\u06FF])ال(?=\s|$|[.،:؛)!؟])", "لا", text)

        text = re.sub(r"([بپتثجچحخسشصضطظعغفقکگلمنهیئ])\s*\n\s*([\u0600-\u06FF])", r"\1\2", text)
        text = re.sub(r"([بپتثجچحخسشصضطظعغفقکگلمنهیئ])\s+([ی|ه|ا|ن|م|ت|د|ر|س|ش|ل|ک|گ]\b)", r"\1\2", text)

        text = re.sub(r"\.{4,}", " ... ", text)

        cleaned_lines = []
        for line in text.splitlines():
            line = re.sub(r"[ \t]{2,}", " ", line).strip()
            if not line:
                continue
            line = cls._fix_reversed_persian_words(line)
            cleaned_lines.append(line)

        return "\n".join(cleaned_lines)

    def _is_text_based(self, doc: fitz.Document) -> bool:
        sample_pages = min(len(doc), 3)
        total_text_chars = 0

        for i in range(sample_pages):
            page = doc.load_page(i)
            text = page.get_text("text", sort=True)
            total_text_chars += len(text.strip())

        return total_text_chars > 50

    def iter_text_pages(self, doc: fitz.Document) -> Generator[str, None, None]:
        flags = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_DEHYPHENATE
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            raw_text = page.get_text("text", flags=flags, sort=True)
            cleaned_text = self._clean_persian_text(raw_text)
            header = f"\n--- صفحه {page_num + 1} ---\n"
            yield header + (cleaned_text if cleaned_text else "[این صفحه فاقد متن استاندارد بود]")

    def _get_page_image_sync(self, doc: fitz.Document, page_num: int, matrix: fitz.Matrix) -> bytes:
        page = doc.load_page(page_num)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        image_bytes = pix.tobytes("jpeg")
        pix = None
        return image_bytes

    async def process_pdf(self, file_bytes: bytes) -> Tuple[str, bool]:
        doc = await asyncio.to_thread(self._open_document, file_bytes)

        try:
            is_scanned = not await asyncio.to_thread(self._is_text_based, doc)
            total_pages = len(doc)

            # جلوگیری از پردازش فایل‌های اسکن‌شده سنگین پیش از مصرف سهمیه و منابع سرور
            if is_scanned and total_pages > self.max_scanned_pages:
                raise PDFPageLimitExceeded(
                    f"این فایل پی‌دی‌اف از نوع اسکن‌شده (تصویری) است و {total_pages} صفحه دارد.\n"
                    f"حداکثر سقف مجاز برای فایل‌های اسکن‌شده {self.max_scanned_pages} صفحه (و برای فایل‌های متنی {self.max_pages} صفحه) است."
                )

            result_chunks = []

            if not is_scanned:
                def _extract_all_text():
                    return [text for text in self.iter_text_pages(doc)]

                result_chunks = await asyncio.to_thread(_extract_all_text)

            else:
                dpi = 150
                zoom = dpi / 72
                matrix = fitz.Matrix(zoom, zoom)

                for page_index in range(total_pages):
                    img_bytes = await asyncio.to_thread(self._get_page_image_sync, doc, page_index, matrix)

                    try:
                        ocr_text = await self.ocr_service.extract_text(img_bytes)
                        result_chunks.append(f"\n--- صفحه {page_index + 1} (OCR) ---\n{ocr_text.strip()}")
                    except OCRError as e:
                        logger.warning(f"خطای OCR در صفحه {page_index + 1}: {e}")
                        result_chunks.append(
                            f"\n--- صفحه {page_index + 1} ---\n[خطا در پردازش هوش مصنوعی این صفحه. لطفاً مجدداً تلاش کنید.]"
                        )
                    except Exception as e:
                        logger.error(f"خطای غیرمنتظره در OCR صفحه {page_index + 1}: {e}")
                        result_chunks.append(
                            f"\n--- صفحه {page_index + 1} ---\n[خطای سیستمی ناشناخته در این صفحه.]"
                        )

                    await asyncio.sleep(0.5)

            full_text = "\n".join(result_chunks).strip()
            return full_text, is_scanned

        finally:
            doc.close()
            