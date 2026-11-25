import asyncio
import html
import io
import logging
import re
from dataclasses import dataclass
from typing import Literal

import qrcode
from PIL import Image, ImageOps
from pyzbar import pyzbar
from pyzbar.pyzbar import ZBarSymbol
from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q
from qrcode.exceptions import DataOverflowError

logger = logging.getLogger(__name__)

QRColorStyle = Literal["classic", "navy", "crimson"]

QR_COLOR_MAP: dict[QRColorStyle, tuple[str, str]] = {
    "classic": ("#000000", "#FFFFFF"),  # مشکی کلاسیک
    "navy": ("#0A2463", "#FFFFFF"),     # آبی سرمه‌ای
    "crimson": ("#8B0000", "#FFFFFF"),  # زرشکی تیره
}

QR_COLOR_LABELS: dict[QRColorStyle, str] = {
    "classic": "⬛ مشکی کلاسیک",
    "navy": "🔵 آبی سرمه‌ای",
    "crimson": "🔴 زرشکی تیره",
}


class QRServiceError(Exception):
    """خطای پایه سرویس QR Code"""


class QRNotFoundError(QRServiceError):
    """زمانی که هیچ QR Code معتبری در تصویر یافت نشود"""


class QRTextTooLongError(QRServiceError):
    """زمانی که متن ارسالی از ظرفیت استاندارد QR Code بیشتر باشد"""


@dataclass(slots=True)
class ScannedQRItem:
    raw_data: str
    qr_type: str
    formatted_html: str


class QRService:
    # سقف بایت واقعی استاندارد QR Version 40 با تصحیح خطای M (معادل ~۱۱۰۰ کاراکتر فارسی یا ~۲۳۰۰ کاراکتر انگلیسی)
    MAX_UTF8_BYTES = 2300
    MAX_IMAGE_DIMENSION = 1800
    MAX_SOURCE_PIXELS = 25_000_000  # جلوگیری از حمله Decompression Bomb (حداکثر ۲۵ مگاپیکسل)

    @staticmethod
    def _parse_wifi(raw: str) -> str | None:
        """تجزیه دقیق Wi-Fi QR Code با پشتیبانی کامل از کاراکترهای Escape شده"""
        if not raw.upper().startswith("WIFI:"):
            return None

        body = raw[5:]

        def extract_field(key: str) -> str | None:
            pattern = rf"(?:^|(?<!\\);){key}:((?:\\.|[^;])*)"
            match = re.search(pattern, body, flags=re.DOTALL)
            if not match:
                return None
            val = match.group(1)
            return (
                val.replace(r"\;", ";")
                .replace(r"\:", ":")
                .replace(r"\"", '"')
                .replace(r"\\", "\\")
            )

        ssid = extract_field("S") or "نامشخص"
        password = extract_field("P") or ""
        security = extract_field("T") or "نامشخص"

        lines = [
            "📶 <b>شبکه بی‌سیم (Wi-Fi)</b>",
            f"• <b>نام شبکه (SSID):</b> <code>{html.escape(ssid)}</code>",
        ]
        if password:
            lines.append(f"• <b>رمز عبور:</b> <code>{html.escape(password)}</code>")
        else:
            lines.append("• <b>رمز عبور:</b> بدون رمز (باز)")
        lines.append(f"• <b>نوع امنیت:</b> {html.escape(security)}")
        return "\n".join(lines)

    @staticmethod
    def _parse_vcard(raw: str) -> str | None:
        """تجزیه اطلاعات کارت ویزیت (vCard)"""
        if not raw.upper().startswith("BEGIN:VCARD"):
            return None

        name = ""
        phones: list[str] = []
        emails: list[str] = []
        org = ""

        for line in raw.splitlines():
            clean_line = line.strip()
            upper_line = clean_line.upper()
            if upper_line.startswith("FN:"):
                name = clean_line[3:].strip()
            elif upper_line.startswith("TEL"):
                _, _, val = clean_line.partition(":")
                if val.strip():
                    phones.append(val.strip())
            elif upper_line.startswith("EMAIL"):
                _, _, val = clean_line.partition(":")
                if val.strip():
                    emails.append(val.strip())
            elif upper_line.startswith("ORG:"):
                org = clean_line[4:].strip()

        lines = ["👤 <b>اطلاعات کارت ویزیت (vCard)</b>"]
        if name:
            lines.append(f"• <b>نام:</b> {html.escape(name)}")
        if org:
            lines.append(f"• <b>سازمان/شرکت:</b> {html.escape(org)}")
        for phone in phones:
            lines.append(f"• <b>شماره تماس:</b> <code>{html.escape(phone)}</code>")
        for email in emails:
            lines.append(f"• <b>ایمیل:</b> <code>{html.escape(email)}</code>")

        if len(lines) == 1:
            lines.append(f"<pre>{html.escape(raw)}</pre>")

        return "\n".join(lines)

    @classmethod
    def _format_decoded_payload(cls, raw_data: str) -> ScannedQRItem:
        """تشخیص نوع محتوای استخراج‌شده و قالب‌بندی امن آن"""
        text = raw_data.strip()

        wifi_formatted = cls._parse_wifi(text)
        if wifi_formatted:
            return ScannedQRItem(raw_data=text, qr_type="wifi", formatted_html=wifi_formatted)

        vcard_formatted = cls._parse_vcard(text)
        if vcard_formatted:
            return ScannedQRItem(raw_data=text, qr_type="vcard", formatted_html=vcard_formatted)

        # تشخیص لینک‌های کامل و لینک‌های تلگرام بدون پروتکل
        if re.match(r"^(?:https?://|t\.me/|telegram\.me/)\S+$", text, re.IGNORECASE):
            formatted = (
                "🔗 <b>لینک اینترنتی:</b>\n"
                f"<code>{html.escape(text)}</code>\n\n"
                "<i>⚠️ پیش از باز کردن لینک‌های ناشناس، از امنیت آدرس مطمئن شوید.</i>"
            )
            return ScannedQRItem(raw_data=text, qr_type="url", formatted_html=formatted)

        if text.lower().startswith("tel:"):
            phone = text[4:].strip()
            formatted = f"📞 <b>شماره تماس:</b> <code>{html.escape(phone)}</code>"
            return ScannedQRItem(raw_data=text, qr_type="phone", formatted_html=formatted)

        formatted = f"📝 <b>محتوای متنی:</b>\n<code>{html.escape(text)}</code>"
        return ScannedQRItem(raw_data=text, qr_type="text", formatted_html=formatted)

    @classmethod
    def _decode_image_sync(cls, image_bytes: bytes) -> list[ScannedQRItem]:
        """استخراج ایمن و سریع QR Code های موجود در تصویر"""
        rgb_img: Image.Image | None = None
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                width, height = img.size
                if width * height > cls.MAX_SOURCE_PIXELS:
                    raise QRServiceError("ابعاد تصویر بیش از حد مجاز است.")

                # اصلاح خودکار چرخش عکس‌های دوربین گوشی بر اساس EXIF
                transposed = ImageOps.exif_transpose(img)
                rgb_img = transposed.convert("RGB")
                if transposed is not img:
                    transposed.close()

            # کاهش ابعاد تصاویر بسیار بزرگ برای افزایش سرعت اسکن و کاهش مصرف RAM
            if max(rgb_img.size) > cls.MAX_IMAGE_DIMENSION:
                rgb_img.thumbnail(
                    (cls.MAX_IMAGE_DIMENSION, cls.MAX_IMAGE_DIMENSION),
                    Image.Resampling.LANCZOS,
                )

            # مرحله ۱: اسکن مستقیم تصویر اصلی (فقط نمادهای QRCODE)
            decoded_objects = pyzbar.decode(rgb_img, symbols=[ZBarSymbol.QRCODE])

            # مرحله ۲: بهبود کنتراست برای عکس‌های تار یا کم‌نور
            if not decoded_objects:
                gray_img = ImageOps.grayscale(rgb_img)
                try:
                    enhanced_img = ImageOps.autocontrast(gray_img)
                    try:
                        decoded_objects = pyzbar.decode(enhanced_img, symbols=[ZBarSymbol.QRCODE])
                    finally:
                        enhanced_img.close()

                    # مرحله ۳: معکوس کردن رنگ‌ها (برای QR کدهای سفید روی پس‌زمینه تیره / Dark Mode)
                    if not decoded_objects:
                        inverted_img = ImageOps.invert(gray_img)
                        try:
                            decoded_objects = pyzbar.decode(inverted_img, symbols=[ZBarSymbol.QRCODE])
                        finally:
                            inverted_img.close()
                finally:
                    gray_img.close()

            if not decoded_objects:
                raise QRNotFoundError(
                    "هیچ QR Code معتبری در این تصویر شناسایی نشد.\n"
                    "لطفاً مطمئن شوید تصویر واضح است و کد داخل کادر بریده نشده است."
                )

            results: list[ScannedQRItem] = []
            seen_raw: set[str] = set()

            for obj in decoded_objects:
                try:
                    raw_text = obj.data.decode("utf-8")
                except UnicodeDecodeError:
                    raw_text = obj.data.decode("latin-1", errors="replace")

                clean_raw = raw_text.strip()
                if not clean_raw or clean_raw in seen_raw:
                    continue

                seen_raw.add(clean_raw)
                results.append(cls._format_decoded_payload(clean_raw))

            if not results:
                raise QRNotFoundError("QR Code شناسایی شد اما محتوای متنی معتبری داخل آن وجود نداشت.")

            return results

        except QRServiceError:
            raise
        except Exception as e:
            logger.warning("خطا در باز کردن یا پردازش تصویر QR: %s", e)
            raise QRServiceError("فایل ارسالی یک تصویر معتبر نیست یا آسیب دیده است.") from e
        finally:
            if rgb_img is not None:
                rgb_img.close()

    @classmethod
    def _generate_qr_sync(cls, text: str, style: QRColorStyle = "classic") -> bytes:
        """تولید تصویر PNG باکیفیت با تنظیم هوشمند سطح تصحیح خطا بر اساس حجم بایت UTF-8"""
        clean_text = text.strip()
        if not clean_text:
            raise QRServiceError("متن ورودی برای ساخت QR Code نمی‌تواند خالی باشد.")

        byte_length = len(clean_text.encode("utf-8"))
        if byte_length > cls.MAX_UTF8_BYTES:
            raise QRTextTooLongError(
                f"حجم متن ارسالی بیش از ظرفیت استاندارد QR Code است ({len(clean_text)} کاراکتر).\n"
                "لطفاً متن کوتاه‌تری ارسال کنید (حداکثر حدود ۱۰۰۰ کاراکتر فارسی یا ۲۲۰۰ کاراکتر انگلیسی)."
            )

        # انتخاب خودکار سطح تصحیح خطا: برای متن‌های کوتاه بالاترین مقاومت (Q) و برای متن‌های بلندتر (M یا L)
        if byte_length <= 1500:
            error_level = ERROR_CORRECT_Q
        elif byte_length <= 2100:
            error_level = ERROR_CORRECT_M
        else:
            error_level = ERROR_CORRECT_L

        fill_color, back_color = QR_COLOR_MAP.get(style, QR_COLOR_MAP["classic"])

        try:
            qr = qrcode.QRCode(
                version=None,
                error_correction=error_level,
                box_size=12,
                border=4,
            )
            qr.add_data(clean_text)
            qr.make(fit=True)
        except DataOverflowError as e:
            raise QRTextTooLongError(
                "متن ارسالی بیش از ظرفیت استاندارد QR Code است. لطفاً متن کوتاه‌تری بفرستید."
            ) from e

        pil_wrapper = qr.make_image(fill_color=fill_color, back_color=back_color)
        rgb_qr = pil_wrapper.convert("RGB")
        output_buffer = io.BytesIO()
        try:
            rgb_qr.save(output_buffer, format="PNG", optimize=True)
            return output_buffer.getvalue()
        finally:
            rgb_qr.close()

    async def scan_qr_from_bytes(self, image_bytes: bytes) -> list[ScannedQRItem]:
        """نسخه ناهمگام (Async) اسکن QR Code بدون مسدود کردن Event Loop"""
        return await asyncio.to_thread(self._decode_image_sync, image_bytes)

    async def generate_qr_bytes(self, text: str, style: QRColorStyle = "classic") -> bytes:
        """نسخه ناهمگام (Async) ساخت تصویر QR Code بدون مسدود کردن Event Loop"""
        return await asyncio.to_thread(self._generate_qr_sync, text, style)

    