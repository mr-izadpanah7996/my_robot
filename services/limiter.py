import logging
import sqlite3
import time
from database.db import DatabaseManager

logger = logging.getLogger(__name__)


class DailyLimiter:
    """
    مدیریت محدودیت استفاده روزانه مبتنی بر دیتابیس SQLite
    (مقاوم در برابر ریستارت، هم‌زمانی و خطای جدول)
    """

    def __init__(
        self,
        max_requests: int = 15,
        window_seconds: int = 86400,
        db_path: str = "data/delta.db",
    ) -> None:
        if max_requests <= 0:
            raise ValueError("مقدار max_requests باید بزرگ‌تر از صفر باشد.")
        if window_seconds <= 0:
            raise ValueError("مقدار window_seconds باید بزرگ‌تر از صفر باشد.")

        self.max_requests: int = max_requests
        self.window_seconds: int = window_seconds
        self.db_path: str = db_path

    def _ensure_schema(self, db: DatabaseManager) -> None:
        """اطمینان از وجود جدول سهمیه‌ها در صورت حذف یا عدم راه‌اندازی اولیه"""
        db.init_tables()

    def check_and_consume(self, user_id: int, cost: int = 1) -> bool:
        """
        بررسی و کسر اتمیک سهمیه کاربر از دیتابیس:
        - اگر سهمیه کافی داشت، به اندازه cost کسر کرده و True برمی‌گرداند.
        - اگر سهمیه تمام شده بود، False برمی‌گرداند.
        """
        if cost < 0:
            raise ValueError("مقدار cost نمی‌تواند منفی باشد.")
        if cost == 0:
            return True

        now = time.time()
        try:
            with DatabaseManager(db_path=self.db_path) as db:
                try:
                    return db.try_consume_quota_atomic(
                        user_id=user_id,
                        cost=cost,
                        max_requests=self.max_requests,
                        window_seconds=self.window_seconds,
                        now_ts=now,
                    )
                except sqlite3.OperationalError as op_err:
                    # بازیابی خودکار در صورتی که جدول هنوز ساخته نشده باشد
                    if "no such table" in str(op_err).lower():
                        logger.warning("جدول user_quotas یافت نشد؛ در حال ایجاد خودکار جدول...")
                        self._ensure_schema(db)
                        return db.try_consume_quota_atomic(
                            user_id=user_id,
                            cost=cost,
                            max_requests=self.max_requests,
                            window_seconds=self.window_seconds,
                            now_ts=now,
                        )
                    raise

        except Exception as e:
            logger.exception(f"خطای غیرمنتظره در بررسی و کسر سهمیه کاربر {user_id}: {e}")
            return False

    def get_remaining_quota(self, user_id: int) -> int:
        """مشاهده تعداد سهمیه باقی‌مانده کاربر در بازه زمانی فعلی"""
        now = time.time()
        try:
            with DatabaseManager(db_path=self.db_path) as db:
                try:
                    record = db.get_user_quota(user_id=user_id)
                except sqlite3.OperationalError as op_err:
                    if "no such table" in str(op_err).lower():
                        self._ensure_schema(db)
                        return self.max_requests
                    raise

            if record is None:
                return self.max_requests

            window_start = float(record["window_start"])
            used_count = int(record["used_count"])

            # اگر بازه زمانی ۲۴ ساعته به پایان رسیده باشد، سهمیه کامل است
            if (now - window_start) >= self.window_seconds:
                return self.max_requests

            return max(0, self.max_requests - used_count)

        except Exception as e:
            logger.exception(f"خطای غیرمنتظره در دریافت سهمیه باقی‌مانده کاربر {user_id}: {e}")
            return 0

    def get_seconds_until_reset(self, user_id: int) -> int:
        """
        محاسبه ثانیه‌های باقی‌مانده تا شارژ مجدد سهمیه کاربر
        (مفید برای نمایش پیام دقیق به کاربر هنگام اتمام سهمیه)
        """
        now = time.time()
        try:
            with DatabaseManager(db_path=self.db_path) as db:
                record = db.get_user_quota(user_id=user_id)

            if record is None:
                return 0

            elapsed = now - float(record["window_start"])
            remaining = self.window_seconds - elapsed
            return max(0, int(remaining))

        except Exception as e:
            logger.exception(f"خطا در محاسبه زمان بازنشانی سهمیه کاربر {user_id}: {e}")
            return 0

        