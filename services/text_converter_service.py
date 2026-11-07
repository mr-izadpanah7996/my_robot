import csv
import html
import json
import logging
import re
import uuid
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

logger = logging.getLogger(__name__)


class TextConversionError(Exception):
    """استثنای اختصاصی برای خطاهای حین تبدیل متن به فایل"""
    pass


class TextToFileService:
    """
    سرویس جامع ساخت و تبدیل محتوای متنی به فرمت‌های گوناگون:
    TXT, CSV, HTML, PDF, Python (.py) و JSON.
    پشتیبانی کامل از زبان فارسی و انکودینگ‌های استاندارد UTF-8 / UTF-8-SIG.
    """

    def __init__(self, temp_dir: Path | str = "/tmp/delta_text_conversions") -> None:
        self._temp_dir = Path(temp_dir)
        self._ensure_temp_dir()

    def _ensure_temp_dir(self) -> None:
        """اطمینان از وجود پوشه موقت با دسترسی مجاز"""
        try:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error("خطا در ایجاد پوشه موقت %s: %s", self._temp_dir, e)
            raise TextConversionError(f"دسترسی به پوشه موقت امکان‌پذیر نیست: {e}") from e

    @staticmethod
    def sanitize_filename(name: str, default: str = "Delta_Bot") -> str:
        """
        پاک‌سازی نام فایل از کاراکترهای غیرمجاز در سیستم‌عامل‌های لینوکس و ویندوز.
        """
        if not name or not name.strip():
            return default

        # حذف کاراکترهای ممنوعه فایل‌سیستم
        cleaned = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", name).strip()
        # تبدیل فاصله‌های متوالی و کاراکترهای فاصله‌انداز به آندرلاین
        cleaned = re.sub(r"\s+", "_", cleaned)
        cleaned = cleaned.strip(". ")

        # محدودسازی طول نام به ۶۴ کاراکتر جهت رعایت استانداردهای تلگرام
        return cleaned[:64] if cleaned else default

    def create_file(self, text: str, filename: str, format_type: str) -> Path:
        """
        ایجاد فایل نهایی متناسب با نوع فرمت درخواستی.
        
        :param text: متن خام دریافتی از کاربر
        :param filename: نام پایه فایل بدون پسوند
        :param format_type: فرمت هدف (txt, csv, html, pdf, py, json)
        :return: شیء Path فایل تولیدشده روی دیسک
        :raises TextConversionError: در صورت نامعتبر بودن فرمت یا بروز خطای دیسک
        """
        if not text or not text.strip():
            raise TextConversionError("متن ارسالی خالی است و امکان تبدیل وجود ندارد.")

        self._ensure_temp_dir()
        fmt = format_type.strip().lower()
        clean_name = self.sanitize_filename(filename)
        unique_suffix = uuid.uuid4().hex[:6]
        base_target = f"{clean_name}_{unique_suffix}"

        builders = {
            "txt": self._build_txt,
            "csv": self._build_csv,
            "html": self._build_html,
            "pdf": self._build_pdf,
            "py": self._build_python,
            "json": self._build_json,
        }

        builder_func = builders.get(fmt)
        if not builder_func:
            raise TextConversionError(f"فرمت انتخابی نامعتبر یا پشتیبانی‌نشده است: {fmt}")

        try:
            target_path = builder_func(text=text, base_name=base_target)
            logger.info("فایل با موفقیت در فرمت %s ساخته شد: %s", fmt, target_path.name)
            return target_path
        except Exception as e:
            logger.exception("خطا حین ساخت فایل فرمت %s: %s", fmt, e)
            raise TextConversionError(f"خطا در ایجاد فایل {fmt}: {e}") from e

    def _build_txt(self, text: str, base_name: str) -> Path:
        file_path = self._temp_dir / f"{base_name}.txt"
        file_path.write_text(text, encoding="utf-8-sig")
        return file_path

    def _build_python(self, text: str, base_name: str) -> Path:
        file_path = self._temp_dir / f"{base_name}.py"
        content = text
        if not content.startswith("#"):
            content = f"# -*- coding: utf-8 -*-\n{content}"
        file_path.write_text(content, encoding="utf-8")
        return file_path

    def _build_csv(self, text: str, base_name: str) -> Path:
        """
        تبدیل خطوط متن به ردیف‌های استاندارد CSV همراه با BOM برای اکسل.
        """
        file_path = self._temp_dir / f"{base_name}.csv"
        lines = text.strip().splitlines()

        with file_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            for line in lines:
                if not line.strip():
                    continue

                if "\t" in line:
                    row = [cell.strip() for cell in line.split("\t")]
                elif "|" in line:
                    row = [cell.strip() for cell in line.split("|")]
                elif line.count(",") >= 2:
                    row = [cell.strip() for cell in line.split(",")]
                else:
                    row = [line]

                writer.writerow(row)

        return file_path

    def _build_html(self, text: str, base_name: str) -> Path:
        """
        تولید سند استاندارد HTML5 با پشتیبانی از راست‌چین (RTL) و امنیت در برابر تگ‌های تزریقی.
        """
        file_path = self._temp_dir / f"{base_name}.html"

        escaped_lines = [
            f"        <p>{html.escape(line.strip())}</p>"
            for line in text.splitlines()
            if line.strip()
        ]
        body_content = "\n".join(escaped_lines)

        html_template = f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html.escape(base_name)}</title>
    <style>
        body {{
            font-family: Tahoma, 'Vazirmatn', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 30px auto;
            max-width: 850px;
            line-height: 1.9;
            color: #2c3e50;
            background-color: #f8f9fa;
            padding: 0 15px;
        }}
        .card {{
            background: #ffffff;
            padding: 35px;
            border-radius: 12px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
            border: 1px solid #e9ecef;
        }}
        p {{
            margin-bottom: 14px;
            word-wrap: break-word;
            white-space: pre-wrap;
            font-size: 15px;
        }}
    </style>
</head>
<body>
    <div class="card">
{body_content}
    </div>
</body>
</html>
"""
        file_path.write_text(html_template, encoding="utf-8")
        return file_path

    def _build_json(self, text: str, base_name: str) -> Path:
        """
        اعتبارسنجی JSON: در صورت معتبر بودن ساختار مرتب می‌شود؛
        در غیر این صورت در قالب استاندارد و ایمن با پشتیبانی فارسی نگهداری می‌شود.
        """
        file_path = self._temp_dir / f"{base_name}.json"
        raw_text = text.strip()

        try:
            parsed = json.loads(raw_text)
            formatted_json = json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:
            safe_payload = {
                "source": "Delta Bot Converter",
                "content": raw_text,
                "lines": [line.strip() for line in raw_text.splitlines() if line.strip()],
            }
            formatted_json = json.dumps(safe_payload, ensure_ascii=False, indent=2)

        file_path.write_text(formatted_json, encoding="utf-8")
        return file_path

    def _build_pdf(self, text: str, base_name: str) -> Path:
        """
        تولید فایل PDF استاندارد با پشتیبانی کامل از حروف چسبیده و راست‌چین فارسی
        همراه با مدیریت خودکار شکست صفحات (Multi-page).
        """
        file_path = self._temp_dir / f"{base_name}.pdf"

        pdf_canvas = canvas.Canvas(str(file_path), pagesize=A4)
        width, height = A4

        # موقعیت شروع سطرها از بالای صفحه
        y = height - 50
        line_height = 20
        margin_x = width - 50  # لبه سمت راست برای راست‌چین بودن متن فارسی

        for raw_line in text.strip().splitlines():
            line = raw_line.strip()
            if not line:
                y -= line_height
                continue

            # اتصال حروف فارسی و اعمال الگوریتم دوجهته (RTL)
            reshaped_text = arabic_reshaper.reshape(line)
            bidi_text = get_display(reshaped_text)

            # اگر متن به انتهای پایین صفحه رسید، صفحه جدید باز شود
            if y < 50:
                pdf_canvas.showPage()
                y = height - 50

            # درج متن با تراز از راست
            pdf_canvas.drawRightString(margin_x, y, bidi_text)
            y -= line_height

        pdf_canvas.save()
        return file_path

    @staticmethod
    def safe_remove(file_path: Path | str | None) -> None:
        """
        حذف ایمن فایل‌های موقت بدون توقف یا پرتاب استثنا
        """
        if file_path is None:
            return
        try:
            target = Path(file_path)
            if target.exists():
                target.unlink()
                logger.debug("فایل موقت پاک‌سازی شد: %s", target)
        except OSError as e:
            logger.warning("خطا در حذف فایل موقت %s: %s", file_path, e)

            