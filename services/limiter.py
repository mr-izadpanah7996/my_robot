import time
from typing import Dict, List


class DailyLimiter:
    """
    مدیریت محدودیت استفاده روزانه در رم سرور (In-Memory Sliding Window)
    """

    def __init__(self, max_requests: int = 15, window_seconds: int = 86400) -> None:
        """
        :param max_requests: حداکثر تعداد درخواست مجاز (پیش‌فرض ۱۵ عدد)
        :param window_seconds: بازه زمانی بررسی به ثانیه (پیش‌فرض ۲۴ ساعت یا ۸۶۴۰۰ ثانیه)
        """


        self.max_requests: int = max_requests
        self.window_seconds: int = window_seconds


        # ساختار: {user_id: [timestamp_1, timestamp_2, ...]}


        self._user_timestamps: Dict[int, List[float]] = {}

    def _cleanup_old_records(self, user_id: int, current_time: float) -> None:

        """حذف تایم‌استمپ‌های قدیمی‌تر از بازه زمانی ۲۴ ساعت"""

        if user_id not in self._user_timestamps:
            return

        threshold = current_time - self.window_seconds

        # فقط رکوردهای داخل بازه ۲۴ ساعت اخیر نگه داشته می‌شوند


        valid_timestamps = [t for t in self._user_timestamps[user_id] if t > threshold]

        if valid_timestamps:
            self._user_timestamps[user_id] = valid_timestamps
        else:
            del self._user_timestamps[user_id]

    def check_and_consume(self, user_id: int) -> bool:
        """
        بررسی سهمیه کاربر:

        اگر سهمیه داشت، ۱ واحد مصرف کرده و True برمی‌گرداند.
        اگر سهمیه تمام شده بود، False برمی‌گرداند.
        """
        now = time.time()
        self._cleanup_old_records(user_id=user_id, current_time=now)

        current_usage = len(self._user_timestamps.get(user_id, []))

        if current_usage < self.max_requests:
            if user_id not in self._user_timestamps:
                self._user_timestamps[user_id] = []
            self._user_timestamps[user_id].append(now)
            return True

        return False

    def get_remaining_quota(self, user_id: int) -> int:


        """مشاهده تعداد درخواست‌های باقی‌مانده کاربر در بازه فعلی"""

        
        now = time.time()
        self._cleanup_old_records(user_id=user_id, current_time=now)
        used = len(self._user_timestamps.get(user_id, []))
        return max(0, self.max_requests - used)

    