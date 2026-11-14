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
    def __init__(
        self,
        ocr_service: OCRService,
        max_pages: int = 300,
        batch_size: int = 10,
    ) -> None:
        self.ocr_service = ocr_service
        self.max_pages = max_pages
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

    async def process_pdf(
        self,
        file_bytes: bytes,
        max_allowed_pages: int | None = None,
    ) -> Tuple[str, int, int]:
        """
        پردازش کامل فایل PDF با موتور هوش مصنوعی (Gemini Batch OCR)
        خروجی: (متن استخراج‌شده, تعداد صفحات پردازش‌شده, تعداد کل صفحات فایل)
        """
        doc = await asyncio.to_thread(self._open_document, file_bytes)

        try:
            total_pages = len(doc)
            pages_to_process = (
                min(total_pages, max_allowed_pages)
                if max_allowed_pages is not None
                else total_pages
            )

            dpi = 140
            zoom = dpi / 72
            matrix = fitz.Matrix(zoom, zoom)

            result_chunks: list[str] = []
            successful_batches = 0
            last_ocr_error: OCRError | None = None

            for start_idx in range(0, pages_to_process, self.batch_size):
                end_idx = min(start_idx + self.batch_size, pages_to_process)
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

                if end_idx < pages_to_process:
                    await asyncio.sleep(1.0)

            # اگر هیچ دسته‌ای موفق نبود، خطا را به هندلر پاس بده تا سهمیه کاربر کسر نشود
            if successful_batches == 0 and last_ocr_error is not None:
                raise last_ocr_error

            full_text = "\n\n".join(result_chunks).strip()
            return full_text, pages_to_process, total_pages

        finally:
            doc.close()

            