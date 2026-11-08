"""
env_loader.py — بارگذاریِ فایلِ .env، بدونِ هیچ متغیرِ الزامی

همان قاعده‌ی قبلیِ src/config.py: مسیر از ENV_FILE (پیش‌فرض «.env» نسبت به پوشه‌ی جاری)؛
اگر فایل باشد با load_dotenv بار می‌شود و محیطِ موجود بازنویسی نمی‌شود. هر فراخوانی همین کار
را دوباره می‌کند (همان رفتارِ importlib.reload(config) در تست‌ها).

چرا جدا: src/config.py متغیرهای LLM و Embedding را الزامی می‌خواهد؛ ماژول‌هایی که به LLM
نیازی ندارند (مثلاً همگام‌سازیِ Swagger) با همین تابع .env را می‌خوانند و هرگز به آن
متغیرها وابسته نمی‌شوند. هیچ مقداری خوانده، برگردانده یا چاپ نمی‌شود.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def env_file_path() -> Path:
    return Path(os.getenv("ENV_FILE", ".env"))


def load_env() -> Path:
    """.env را (اگر هست) بدونِ بازنویسیِ محیطِ موجود بار می‌کند و مسیرش را برمی‌گرداند."""
    env_file = env_file_path()
    if env_file.exists():
        load_dotenv(env_file)
    return env_file


__all__ = ["env_file_path", "load_env"]
