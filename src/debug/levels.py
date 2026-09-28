"""
debug/levels.py — سطح‌بندی debug

ترتیب از پایین به بالا:
  TRACE   : جزئیات خیلی ریز (ورودی/خروجی هر تابع، مقادیر متغیر)
  DEBUG   : اطلاعات تشخیص مشکل (فراخوانی LLM، نتیجه parsing، ...)
  INFO    : رویدادهای معمول (شروع/پایان ایجنت، تصمیم گرفته‌شده، ...)
  WARNING : موارد غیرمنتظره که مانع اجرا نمی‌شوند
  ERROR   : خطاهایی که اجرا را مختل می‌کنند

هر سطح پیام‌های خودش و سطوح بالاتر را می‌نویسد.
"""

from enum import IntEnum


class DebugLevel(IntEnum):
    TRACE   = 0
    DEBUG   = 10
    INFO    = 20
    WARNING = 30
    ERROR   = 40
    OFF     = 100   # غیرفعال کردن کامل لاگ برای یک ایجنت خاص

    @classmethod
    def from_str(cls, value: str) -> "DebugLevel":
        """از رشته مثل 'debug' یا 'INFO' به enum تبدیل می‌کند."""
        try:
            return cls[value.upper()]
        except KeyError:
            valid = [m.name for m in cls]
            raise ValueError(f"سطح نامعتبر: '{value}'. مقادیر مجاز: {valid}")

    def label(self) -> str:
        return self.name