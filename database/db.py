import os
import sqlite3
from pathlib import Path
from typing import Any


class DatabaseManager:
    def __init__(self, db_path: str = "data/delta.db") -> None:
        # تبدیل مسیر نسبی به مسیر مطلق نسبت به ریشه پروژه برای جلوگیری از گم شدن فایل در systemd
        base_dir = Path(__file__).resolve().parent.parent
        path_obj = Path(db_path)
        self.db_path = str(path_obj if path_obj.is_absolute() else base_dir / path_obj)

        self.conn: sqlite3.Connection | None = None
        self.cursor: sqlite3.Cursor | None = None

    def connect(self) -> "DatabaseManager":
        """برقراری اتصال و تنظیمات اولیه"""
        if self.conn is not None:
            return self

        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        self.conn = sqlite3.connect(self.db_path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()

        # فعال‌سازی کلید خارجی (در هر اتصال ضروری است)
        self.cursor.execute("PRAGMA foreign_keys = ON;")
        return self

    def __enter__(self) -> "DatabaseManager":
        return self.connect()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is not None and self.conn:
            self.conn.rollback()
        self.close()

    def execute_query(self, query: str, params: tuple = ()) -> int:
        """اجرای دستورات تغییردهنده (CREATE, INSERT, UPDATE, DELETE) با ذخیره خودکار"""
        if not self.cursor or not self.conn:
            raise RuntimeError("اتصال به دیتابیس برقرار نیست. از بلوک with استفاده کنید.")
        self.cursor.execute(query, params)
        self.conn.commit()
        return self.cursor.rowcount

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        """اجرای SELECT و برگرداندن تمام نتایج به شکل لیستی از دیکشنری‌ها"""
        if not self.cursor:
            raise RuntimeError("اتصال به دیتابیس برقرار نیست. از بلوک with استفاده کنید.")
        self.cursor.execute(query, params)
        return [dict(row) for row in self.cursor.fetchall()]

    def fetch_one(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        """اجرای SELECT و برگرداندن فقط یک سطر به شکل دیکشنری"""
        if not self.cursor:
            raise RuntimeError("اتصال به دیتابیس برقرار نیست. از بلوک with استفاده کنید.")
        self.cursor.execute(query, params)
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        """بستن ایمن اتصال دیتابیس"""
        if self.conn:
            self.conn.close()
            self.conn = None
            self.cursor = None

    # ==========================================================
    # متدهای آماده و کاربردی مخصوص ربات تلگرام
    # ==========================================================

    def init_tables(self) -> None:
        """ساخت جدول‌های اولیه و تنظیم حالت WAL یک‌بار برای همیشه هنگام استارت ربات"""
        if not self.cursor or not self.conn:
            raise RuntimeError("اتصال به دیتابیس برقرار نیست.")

        # حالت WAL در خود فایل ذخیره می‌شود؛ اجرای آن فقط هنگام استارت کافی است
        self.cursor.execute("PRAGMA journal_mode = WAL;")

        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()

    def add_or_update_user(self, user_id: int, full_name: str, username: str | None) -> None:
        """ثبت امن کاربر جدید یا بروزرسانی اطلاعات بدون تغییر تاریخ عضویت اولیه"""
        if not self.cursor or not self.conn:
            raise RuntimeError("اتصال به دیتابیس برقرار نیست.")

        # روش سازگار با تمام نسخه‌های SQLite و حفظ تاریخ joined_at
        self.cursor.execute(
            "INSERT OR IGNORE INTO users (user_id, full_name, username) VALUES (?, ?, ?)",
            (user_id, full_name, username),
        )
        self.cursor.execute(
            "UPDATE users SET full_name = ?, username = ? WHERE user_id = ?",
            (full_name, username, user_id),
        )
        self.conn.commit()

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        """گرفتن اطلاعات یک کاربر با آیدی تلگرام"""
        return self.fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))

    def get_total_users(self) -> int:
        """گرفتن تعداد کل کاربران ربات برای پنل مدیریت"""
        result = self.fetch_one("SELECT COUNT(*) as total FROM users")
        return result["total"] if result else 0

        