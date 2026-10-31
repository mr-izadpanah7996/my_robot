import io
import fitz  # PyMuPDF
import asyncio
import logging
from typing import Generator, Tuple
from services.ocr import OCRService, OCRError

logger = logging.getLogger(__name__)


class PDFPageLimitExceeded(Exception):
    """خطای عبور از سقف مجاز صفحات پی‌دی‌اف"""
    pass


class PDFEncryptedError(Exception):
    """خطای رمزدار بودن فایل پی‌دی‌اف"""
    pass


class PDFEmptyError(Exception):
    """خطای خالی یا نامعتبر بودن سند پی‌دی‌اف"""
    pass


class PDFService:
    def __init__(self, ocr_service: OCRService, max_pages: int = 300) -> None:
        self.ocr_service = ocr_service
        self.max_pages = max_pages

    def _open_document(self, file_bytes: bytes) -> fitz.Document:
        """باز کردن فایل از بایت‌های حافظه و اعتبارسنجی اولیه"""
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

    def _is_text_based(self, doc: fitz.Document) -> bool:
        """
        بررسی لایه متنی: اگر حداقل در ۳ صفحه اول بیش از ۵۰ کاراکتر باشد،
        فایل متنی در نظر گرفته می‌شود.
        """
        sample_pages = min(len(doc), 3)
        total_text_chars = 0

        for i in range(sample_pages):
            page = doc.load_page(i)
            text = page.get_text()
            total_text_chars += len(text.strip())

        return total_text_chars > 50

    def iter_text_pages(self, doc: fitz.Document) -> Generator[str, None, None]:
        """ژنراتور استخراج تنبل متن صفحات بدون اشغال حجم رم"""
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            text = page.get_text()
            header = f"\n--- صفحه {page_num + 1} ---\n"
            yield header + (text.strip() if text.strip() else "[این صفحه فاقد متن استاندارد بود]")

    def _get_page_image_sync(self, doc: fitz.Document, page_num: int, matrix: fitz.Matrix) -> bytes:
        """متد کمکی همگام برای استخراج بایت‌های تصویر یک صفحه خاص (جهت استفاده در توابع Async)"""
        page = doc.load_page(page_num)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        image_bytes = pix.tobytes("jpeg")
        pix = None  # آزادسازی فوری حافظه C
        return image_bytes

    async def process_pdf(self, file_bytes: bytes) -> Tuple[str, bool]:
        """
        پردازش کامل هیبریدی ناهمگام با محافظت از حافظه سرور
        خروجی: (متن استخراج‌شده, آیا_اسکن_بود؟)
        """
        # باز کردن سند با asyncio.to_thread برای جلوگیری از بلاک شدن ربات در فایل‌های حجیم
        doc = await asyncio.to_thread(self._open_document, file_bytes)
        
        try:
            is_scanned = not await asyncio.to_thread(self._is_text_based, doc)
            result_chunks = []

            if not is_scanned:
                # حالت متنی (۰ سهمیه)
                # از آنجا که get_text همگام و نسبتاً سبک است، استخراج کل صفحات را به یک Thread می‌فرستیم
                def _extract_all_text():
                    return [text for text in self.iter_text_pages(doc)]
                
                result_chunks = await asyncio.to_thread(_extract_all_text)

            else:
                # حالت اسکن‌شده (۳ سهمیه - استفاده از موتور OCR)
                dpi = 150
                zoom = dpi / 72
                matrix = fitz.Matrix(zoom, zoom)
                total_pages = len(doc)

                for page_index in range(total_pages):
                    # رندر گرفتن از صفحه PDF به عکس بسیار سنگین است، پس آن را به Thread پس‌زمینه می‌سپاریم
                    img_bytes = await asyncio.to_thread(self._get_page_image_sync, doc, page_index, matrix)

                    try:
                        # ارسال عکس به گوگل جمینای
                        ocr_text = await self.ocr_service.extract_text(img_bytes)
                        result_chunks.append(f"\n--- صفحه {page_index + 1} (OCR) ---\n{ocr_text.strip()}")
                    except OCRError as e:
                        logger.warning(f"خطای OCR در صفحه {page_index + 1}: {e}")
                        result_chunks.append(f"\n--- صفحه {page_index + 1} ---\n[خطا در پردازش هوش مصنوعی این صفحه. لطفاً مجدداً تلاش کنید.]")
                    except Exception as e:
                        logger.error(f"خطای غیرمنتظره در OCR صفحه {page_index + 1}: {e}")
                        result_chunks.append(f"\n--- صفحه {page_index + 1} ---\n[خطای سیستمی ناشناخته در این صفحه.]")
                        
                    # کمی مکث برای جلوگیری از اسپم شدن API گوگل بین صفحات
                    await asyncio.sleep(0.5)

            full_text = "\n".join(result_chunks).strip()
            return full_text, is_scanned

        finally:
            # گارد محافظ حافظه: تحت هر شرایطی (حتی در صورت کرش شدید کد در میانه راه)
            # این بلوک اجرا شده و فایل PDF از حافظه موقت رم کاملاً پاک می‌شود.
            doc.close()

            