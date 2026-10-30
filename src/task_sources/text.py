"""
src/task_sources/text.py — پاک‌سازیِ متنِ دریافتی، بدونِ دست‌زدن به محتوا

فقط چیزهایی عوض می‌شوند که معنا ندارند: پایانِ خطِ CRLF/CR، فاصله‌ی
نشکن (NBSP و هم‌خانواده‌هایش)، BOM، فاصله‌ی انتهای خط و خط‌های خالیِ پشتِ‌هم.
نیم‌فاصله (ZWNJ, U+200C) عمداً می‌ماند: در فارسی معنا دارد.
"""

from __future__ import annotations

import re

_NBSP = str.maketrans({" ": " ", " ": " ", " ": " ", "﻿": None})
_BLANK_RUN = re.compile(r"\n{3,}")


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_text(text: str) -> str:
    """CRLF → LF، NBSP → فاصله، حذفِ BOM، فاصله‌ی انتهای خط و بیش از یک خطِ خالی."""
    text = normalize_newlines(text or "").translate(_NBSP)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_RUN.sub("\n\n", text).strip("\n")


def fence_block(content: str, language: str = "") -> str:
    """محتوا داخلِ fenceی (~~~~) که از هر رشته‌ی ` یا ~ داخلِ خودِ محتوا بلندتر است.

    مرزِ قطعه‌ی کد/پیوست با بقیه‌ی متن روشن می‌ماند و محتوا نمی‌تواند fence را
    زودتر ببندد.
    """
    longest = max((len(run) for run in re.findall(r"`+|~+", content)), default=0)
    fence = "~" * max(4, longest + 1)
    return f"{fence}{language}\n{content.strip(chr(10))}\n{fence}"


__all__ = ["fence_block", "normalize_newlines", "normalize_text"]
