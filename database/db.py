import os
import sqlite3
from pathlib import Path
from typing import Any


class DatabaseManager:
    def __init__(self, db_path: str = "data/delta.db") -> None:
        # تبدیل مسیر نسبی به مسیر مطلق نسبت به ریشه پروژه
        base_dir = Path(__file__).resolve().parent.parent
        path_obj = Path(db_path)
        self.db_path = str(path_obj if path_obj.is_absolute() else base_dir / path_obj)

        self.conn: sqlite3.Connection | None = None
        self.cursor: sqlite3.Cursor | None = None

    def connect(self) -> "DatabaseManager":
        """برقراری اتصال ایمن و مقاوم در برابر هم‌زمانی"""
        if self.conn is not None:
            return self

        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self.conn = sqlite3.connect(self.db_path, timeout=20.0)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()

        # تنظیمات حیاتی برای جلوگیری از خطای database is locked و حفظ یکپارچگی
        self.cursor.execute("PRAGMA busy_timeout = 20000;")
        self.cursor.execute("PRAGMA foreign_keys = ON;")
        self.cursor.execute("PRAGMA synchronous = NORMAL;")
        return self

    def __enter__(self) -> "DatabaseManager":
        return self.connect()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if exc_type is not None and self.conn:
                self.conn.rollback()
        finally:
            self.close()

    def _ensure_connected(self) -> tuple[sqlite3.Connection, sqlite3.Cursor]:
        """بررسی فعال بودن اتصال پیش از اجرای هر کوئری"""
        if self.conn is None or self.cursor is None:
            raise RuntimeError("اتصال به دیتابیس برقرار نیست. از بلوک with استفاده کنید.")
        return self.conn, self.cursor

    def execute_query(self, query: str, params: tuple = ()) -> int:
        """اجرای دستورات تغییردهنده با ذخیره خودکار و برگشت تراکنش در صورت خطا"""
        conn, cursor = self._ensure_connected()
        try:
            cursor.execute(query, params)
            conn.commit()
            return cursor.rowcount
        except Exception:
            conn.rollback()
            raise

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        """اجرای SELECT و برگرداندن تمام نتایج به شکل لیستی از دیکشنری‌ها"""
        _, cursor = self._ensure_connected()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def fetch_one(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        """اجرای SELECT و برگرداندن فقط یک سطر به شکل دیکشنری"""
        _, cursor = self._ensure_connected()
        cursor.execute(query, params)
        row = cursor.fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        """بستن تضمین‌شده اتصال دیتابیس حتی در صورت بروز خطا"""
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None
                self.cursor = None

    # ==========================================================
    # متدهای آماده و کاربردی مخصوص ربات تلگرام
    # ==========================================================

    def init_tables(self) -> None:
        """ساخت جدول‌های اولیه و تنظیم حالت WAL هنگام استارت ربات"""
        conn, cursor = self._ensure_connected()
        try:
            cursor.execute("PRAGMA journal_mode = WAL;")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    username TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_quotas (
                    user_id INTEGER PRIMARY KEY,
                    used_count INTEGER NOT NULL DEFAULT 0 CHECK(used_count >= 0),
                    window_start REAL NOT NULL
                )
            """)

            # ایندکس روی تاریخ عضویت برای سرعت بالای مرتب‌سازی در لیست کاربران
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_users_joined_at ON users(joined_at DESC)
            """)

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def add_or_update_user(self, user_id: int, full_name: str, username: str | None) -> None:
        """ثبت یا بروزرسانی اتمیک کاربر در یک کوئری واحد بدون تغییر تاریخ عضویت"""
        safe_name = (full_name or "کاربر بدون نام").strip()
        self.execute_query(
            """
            INSERT INTO users (user_id, full_name, username)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name = excluded.full_name,
                username = excluded.username
            """,
            (user_id, safe_name, username),
        )

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        """گرفتن اطلاعات یک کاربر با آیدی تلگرام"""
        return self.fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))

    def get_total_users(self) -> int:
        """گرفتن تعداد کل کاربران ربات"""
        result = self.fetch_one("SELECT COUNT(*) as total FROM users")
        return int(result["total"]) if result else 0

    def get_all_users(self) -> list[dict[str, Any]]:
        """گرفتن لیست تمام کاربران به ترتیب جدیدترین عضویت"""
        return self.fetch_all(
            "SELECT user_id, full_name, username, joined_at FROM users ORDER BY joined_at DESC"
        )

    # ==========================================================
    # متدهای مدیریت سهمیه روزانه (مقاوم در برابر هم‌زمانی)
    # ==========================================================

    def get_user_quota(self, user_id: int) -> dict[str, Any] | None:
        """دریافت وضعیت سهمیه مصرف‌شده و زمان شروع بازه زمانی کاربر"""
        return self.fetch_one(
            "SELECT user_id, used_count, window_start FROM user_quotas WHERE user_id = ?",
            (user_id,),
        )

    def upsert_user_quota(self, user_id: int, used_count: int, window_start: float) -> None:
        """ثبت یا بروزرسانی مستقیم سهمیه مصرف‌شده و زمان شروع بازه"""
        safe_count = max(0, used_count)
        self.execute_query(
            """
            INSERT INTO user_quotas (user_id, used_count, window_start)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                used_count = excluded.used_count,
                window_start = excluded.window_start
            """,
            (user_id, safe_count, window_start),
        )

    def try_consume_quota_atomic(
        self,
        user_id: int,
        cost: int,
        max_requests: int,
        window_seconds: int,
        now_ts: float,
    ) -> bool:
        """
        بررسی و کسر اتمیک سهمیه با قفل تراکنش (BEGIN IMMEDIATE)
        برای جلوگیری کامل از باگ Race Condition هنگام ارسال هم‌زمان چند درخواست توسط یک کاربر
        """
        if cost <= 0:
            return True

        conn, cursor = self._ensure_connected()
        try:
            cursor.execute("BEGIN IMMEDIATE;")
            cursor.execute(
                "SELECT used_count, window_start FROM user_quotas WHERE user_id = ?",
                (user_id,),
            )
            row = cursor.fetchone()

            if row is None or (now_ts - float(row["window_start"])) >= window_seconds:
                # پنجره جدید یا کاربر جدید
                if cost > max_requests:
                    conn.rollback()
                    return False
                cursor.execute(
                    """
                    INSERT INTO user_quotas (user_id, used_count, window_start)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        used_count = excluded.used_count,
                        window_start = excluded.window_start
                    """,
                    (user_id, cost, now_ts),
                )
                conn.commit()
                return True

            current_used = int(row["used_count"])
            if current_used + cost > max_requests:
                conn.rollback()
                return False

            cursor.execute(
                "UPDATE user_quotas SET used_count = ? WHERE user_id = ?",
                (current_used + cost, user_id),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

        