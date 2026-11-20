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
        batch_size: int = 4,
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

    @staticmethod
    def _has_repetition_loop(text: str) -> bool:
        """
        تشخیص گیر کردن مدل در حلقه تکرار (مانند تکرار پی‌درپی یک عبارت بیش از ۵ بار)
        """
        if not text:
            return False
        # هر عبارت ۳ تا ۶۰ کاراکتری (غیر از خط تیره یا نقطه) که ۶ بار یا بیشتر پشت سر هم تکرار شود
        return bool(re.search(r"([^\s.\-_]{2,}.{0,58}?)\1{5,}", text))

    @staticmethod
    def _collapse_repetitions(text: str) -> str:
        """پاک‌سازی اضطراری حلقه‌های تکرار در صورت تکرار مجدد پس از تلاش دوم"""
        return re.sub(r"([^\s.\-_]{2,}.{0,58}?)\1{4,}", r"\1", text)

    @staticmethod
    def _render_batch_images_sync(
        doc: fitz.Document,
        start_idx: int,
        end_idx: int,
        matrix: fitz.Matrix,
    ) -> list[tuple[int, bytes]]:
        """رندر یک دسته صفحات به JPEG باکیفیت در حافظه RAM"""
        batch_pages: list[tuple[int, bytes]] = []
        for idx in range(start_idx, end_idx):
            page = doc.load_page(idx)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            img_bytes = pix.tobytes("jpeg", jpg_quality=88)
            pix = None
            batch_pages.append((idx + 1, img_bytes))
        return batch_pages

    @staticmethod
    def _has_missing_or_empty_pages(batch_text: str, expected_page_numbers: list[int]) -> bool:
        """
        بررسی می‌کند آیا در خروجی دسته‌ای:
        ۱. صفحه‌ای اصلاً وجود ندارد
        ۲. یا هدر صفحه وجود دارد اما زیر آن هیچ متنی استخراج نشده است (مانند صفحه ۱۴۹)
        """
        if len(expected_page_numbers) <= 1:
            return False

        trans_table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
        normalized_text = batch_text.translate(trans_table)

        # یافتن موقعیت شروع هدر هر صفحه
        header_matches = list(
            re.finditer(r"(?m)^[^\n]*صفحه\s*(\d+)[^\n]*$", normalized_text)
        )
        found_pages: dict[int, tuple[int, int]] = {}
        for i, match in enumerate(header_matches):
            p_num = int(match.group(1))
            content_start = match.end()
            content_end = header_matches[i + 1].start() if i + 1 < len(header_matches) else len(normalized_text)
            if p_num not in found_pages:
                found_pages[p_num] = (content_start, content_end)

        for page_num in expected_page_numbers:
            if page_num not in found_pages:
                return True
            start_pos, end_pos = found_pages[page_num]
            page_body = normalized_text[start_pos:end_pos].strip()
            # اگر زیر هدر صفحه کمتر از ۳ کاراکتر متن باشد، مشکوک به جا افتادن است
            if len(page_body) < 3:
                return True

        return False

    async def _process_pages_individually(
        self,
        batch_pages: list[tuple[int, bytes]],
    ) -> tuple[str, int, OCRError | None]:
        """
        مکانیزم Fallback: پردازش تک‌صفحه‌ای در صورت شکست، ناقص بودن یا حلقه تکرار در خروجی دسته‌ای
        """
        page_results: list[str] = []
        success_count = 0
        last_err: OCRError | None = None

        for page_num, img_bytes in batch_pages:
            try:
                single_text = await self.ocr_service.extract_text_batch([(page_num, img_bytes)])
                cleaned = self._clean_text(single_text)

                # اگر در حالت تک‌صفحه‌ای هم مدل دچار حلقه تکرار شد، یک بار دیگر تلاش کن
                if self._has_repetition_loop(cleaned):
                    logger.warning("تشخیص حلقه تکرار در صفحه %s؛ تلاش مجدد...", page_num)
                    await asyncio.sleep(1.0)
                    retry_text = await self.ocr_service.extract_text_batch([(page_num, img_bytes)])
                    cleaned = self._clean_text(retry_text)
                    if self._has_repetition_loop(cleaned):
                        cleaned = self._collapse_repetitions(cleaned)

                # اطمینان از وجود هدر شماره صفحه در خروجی
                trans_table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
                if not re.search(rf"صفحه\s*{page_num}\b", cleaned.translate(trans_table)):
                    cleaned = f"--- صفحه {page_num} ---\n{cleaned}"

                page_results.append(cleaned)
                success_count += 1
            except OCRError as e:
                last_err = e
                logger.warning("خطای OCR تک‌صفحه‌ای در صفحه %s: %s", page_num, e)
                page_results.append(
                    f"--- صفحه {page_num} ---\n[خطا در استخراج متن این صفحه: {e}]"
                )
            await asyncio.sleep(0.5)

        return "\n\n".join(page_results), success_count, last_err

    async def process_pdf(
        self,
        file_bytes: bytes,
        max_allowed_pages: int | None = None,
    ) -> Tuple[str, int, int]:
        """
        پردازش کامل فایل PDF با موتور هوش مصنوعی (Gemini Batch OCR + Single-Page Fallback)
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

            # وضوح ۲۰۰ برای خوانش دقیق حروف فارسی، نقطه‌ها، اعراب و پاورقی‌ها
            dpi = 200
            zoom = dpi / 72
            matrix = fitz.Matrix(zoom, zoom)

            result_chunks: list[str] = []
            successful_units = 0
            last_ocr_error: OCRError | None = None

            for start_idx in range(0, pages_to_process, self.batch_size):
                end_idx = min(start_idx + self.batch_size, pages_to_process)
                expected_pages = list(range(start_idx + 1, end_idx + 1))
                batch_pages: list[tuple[int, bytes]] = []

                try:
                    batch_pages = await asyncio.to_thread(
                        self._render_batch_images_sync,
                        doc,
                        start_idx,
                        end_idx,
                        matrix,
                    )

                    try:
                        batch_text = await self.ocr_service.extract_text_batch(batch_pages)
                        cleaned_batch = self._clean_text(batch_text)

                        has_loop = self._has_repetition_loop(cleaned_batch)
                        has_missing = self._has_missing_or_empty_pages(cleaned_batch, expected_pages)

                        if has_loop or has_missing:
                            reason = "حلقه تکرار" if has_loop else "صفحه جاافتاده یا خالی"
                            logger.warning(
                                "تشخیص %s در صفحات %s تا %s؛ سوییچ به پردازش تک‌صفحه‌ای.",
                                reason,
                                start_idx + 1,
                                end_idx,
                            )
                            fallback_text, ok_count, err = await self._process_pages_individually(batch_pages)
                            result_chunks.append(fallback_text)
                            successful_units += ok_count
                            if err:
                                last_ocr_error = err
                        else:
                            result_chunks.append(cleaned_batch)
                            successful_units += len(batch_pages)

                    except OCRError as e:
                        logger.warning(
                            "خطای OCR دسته‌ای در صفحات %s تا %s (%s)؛ سوییچ به پردازش تک‌صفحه‌ای.",
                            start_idx + 1,
                            end_idx,
                            e,
                        )
                        fallback_text, ok_count, err = await self._process_pages_individually(batch_pages)
                        result_chunks.append(fallback_text)
                        successful_units += ok_count
                        last_ocr_error = err or e

                finally:
                    batch_pages.clear()

                if end_idx < pages_to_process:
                    await asyncio.sleep(0.8)

            # اگر هیچ صفحه‌ای با موفقیت پردازش نشد، خطا را به هندلر پاس بده تا سهمیه کسر نشود
            if successful_units == 0 and last_ocr_error is not None:
                raise last_ocr_error

            full_text = "\n\n".join(result_chunks).strip()
            return full_text, pages_to_process, total_pages

        finally:
            doc.close()

            