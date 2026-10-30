import io
import fitz  # PyMuPDF
from typing import Generator, Tuple
from services.ocr import OCRService


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

    def __init__(self, ocr_service: OCRService, max_pages: int = 70) -> None:


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
        بررسی لایه متنی: اگر حداقل در صفحات نمونه متن قابل استخراج وجود داشته باشد،
        فایل متنی در نظر گرفته می‌شود.
        """
        sample_pages = min(len(doc), 3)
        total_text_chars = 0


        for i in range(sample_pages):

            page = doc.load_page(i)
            text = page.get_text()
            total_text_chars += len(text.strip())

        # اگر در ۳ صفحه اول بیش از ۵۰ کاراکتر متن استاندارد باشد، متنی تلقی می‌شود


        return total_text_chars > 50

    def iter_text_pages(self, doc: fitz.Document) -> Generator[str, None, None]:


        """ژنراتور استخراج تنبل متن صفحات بدون اشغال حجم رم"""


        for page_num in range(len(doc)):


            page = doc.load_page(page_num)
            text = page.get_text()
            header = f"\n--- صفحه {page_num + 1} ---\n"
            yield header + (text.strip() if text.strip() else "[این صفحه متنی نداشت]")

    def iter_page_images(self, doc: fitz.Document, dpi: int = 150) -> Generator[bytes, None, None]:


        """ژنراتور تبدیل تنبل صفحات به تصویر و آزادسازی فوری حافظه صفحه قبلی"""


        # ضریب بزرگ‌نمایی برای وضوح مناسب OCR


        zoom = dpi / 72
        matrix = fitz.Matrix(zoom, zoom)

        for page_num in range(len(doc)):


            page = doc.load_page(page_num)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image_bytes = pix.tobytes("jpeg")
            pix = None  # آزادسازی حافظه تصویر از رم
            yield image_bytes

    async def process_pdf(self, file_bytes: bytes) -> Tuple[str, bool]:
        """
        پردازش کامل هیبریدی:
        خروجی: (متن استخراج‌شده, آیا_اسکن_بود؟)
        """
        doc = self._open_document(file_bytes)
        is_scanned = not self._is_text_based(doc)

        result_chunks = []

        if not is_scanned:


            # حالت متنی (۰ سهمیه - سریع و بدون مصرف هوش مصنوعی)


            for page_text in self.iter_text_pages(doc):

                result_chunks.append(page_text)

        else:


            # حالت اسکن‌شده (۳ سهمیه - استفاده از ژنراتور تصویر و فراخوانی OCR)


            page_index = 1

            for img_bytes in self.iter_page_images(doc):
                
                ocr_text = await self.ocr_service.extract_text(img_bytes)
                result_chunks.append(f"\n--- صفحه {page_index} (OCR) ---\n{ocr_text.strip()}")
                page_index += 1

        doc.close()
        full_text = "\n".join(result_chunks).strip()
        return full_text, is_scanned

    