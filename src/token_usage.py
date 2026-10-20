"""
token_usage.py — شمارشِ مصرفِ توکنِ فراخوانی‌های واقعیِ LLM

هر فراخوانیِ ``LLMClient.chat()`` دقیقاً یک بار — و فقط یک بار — این‌جا گزارش
می‌شود. لایه‌های بالاتر (قدم‌های ویزارد) هیچ‌وقت خودشان فراخوانی را دوباره
حساب نمی‌کنند؛ فقط یک «گیرنده» نصب می‌کنند و اعدادِ ارائه‌دهنده را جمع می‌زنند.

``TokenUsage`` هیچ وابستگی‌ای به OpenAI یا Streamlit ندارد و اعداد فقط از
فراداده‌ی خودِ پاسخ خوانده می‌شوند — هیچ تخمینی از متنِ prompt یا پاسخ زده
نمی‌شود. اگر ارائه‌دهنده usage برنگرداند، مقدارها ``None`` می‌مانند (نه صفر)
تا «گزارش‌نشده» با «صفر» اشتباه نشود؛ خودِ فراخوانی با ``calls`` شمرده می‌شود.

گیرنده در یک ``ContextVar`` نگه داشته می‌شود، نه در یک متغیرِ سراسری: هر
نشستِ Streamlit در context خودش اجرا می‌شود و مصرفِ دو کاربر با هم قاطی نمی‌شود.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

UsageSink = Callable[["TokenUsage"], None]


def as_int(value: object) -> int | None:
    """مقدارِ توکن را فقط وقتی عددِ سالم است می‌پذیرد.

    ``bool`` عمداً رد می‌شود (در پایتون زیرکلاسِ int است) و هر چیز دیگری —
    از جمله صفر بودنِ مقدار یا نبودنِ آن — بدونِ خطا به ``None`` تبدیل می‌شود.
    """
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _add(left: int | None, right: int | None) -> int | None:
    """جمعِ دو مقدارِ اختیاری؛ اگر هیچ‌کدام گزارش نشده باشد ``None`` می‌ماند."""
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


@dataclass(frozen=True)
class TokenUsage:
    """مصرفِ یک یا چند فراخوانیِ LLM.

    ``calls`` همیشه تعدادِ فراخوانی‌های واقعی است، حتی وقتی usage گزارش نشده
    باشد. بقیه‌ی فیلدها ``None`` یعنی «ارائه‌دهنده گزارش نکرد» — با صفرِ واقعی
    یکی نیستند.
    """

    calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None

    @classmethod
    def from_response(cls, response: object) -> "TokenUsage":
        """usage را از پاسخِ ارائه‌دهنده می‌خواند — بدونِ تخمین و بدونِ خطا.

        ``total_tokens`` اگر ارائه‌دهنده نداده باشد ولی prompt و completion
        هر دو موجود باشند، از جمعِ همان دو عددِ واقعی ساخته می‌شود (جمعِ
        حسابی، نه تخمینِ متنی).
        """
        prompt = as_int(getattr(response, "prompt_tokens", None))
        completion = as_int(getattr(response, "completion_tokens", None))
        total = as_int(getattr(response, "total_tokens", None))
        if total is None and prompt is not None and completion is not None:
            total = prompt + completion
        return cls(
            calls=1,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cached_tokens=as_int(getattr(response, "cached_tokens", None)),
        )

    def combine(self, other: "TokenUsage") -> "TokenUsage":
        """دو مصرف را جمع می‌زند — برای دسته‌ها، تلاش‌های دوباره و چند قدم."""
        return TokenUsage(
            calls=self.calls + other.calls,
            prompt_tokens=_add(self.prompt_tokens, other.prompt_tokens),
            completion_tokens=_add(self.completion_tokens, other.completion_tokens),
            total_tokens=_add(self.total_tokens, other.total_tokens),
            cached_tokens=_add(self.cached_tokens, other.cached_tokens),
        )


# گیرنده‌ی فعالِ همین اجرا؛ پیش‌فرض None یعنی «هیچ‌کس مصرف را نمی‌خواهد».
_usage_sink: ContextVar[UsageSink | None] = ContextVar("llm_usage_sink", default=None)


def report_usage(response: object) -> None:
    """یک فراخوانیِ کامل‌شده را به گیرنده‌ی فعال گزارش می‌کند.

    دقیقاً از یک نقطه صدا زده می‌شود (``LLMClient.chat``) تا هیچ فراخوانی‌ای
    دوبار شمرده نشود. بدونِ گیرنده، کاری انجام نمی‌شود.
    """
    sink = _usage_sink.get()
    if sink is not None:
        sink(TokenUsage.from_response(response))


@contextmanager
def collect_usage(sink: UsageSink) -> Iterator[None]:
    """برای طولِ یک اجرا، هر فراخوانیِ LLM را به ``sink`` می‌دهد."""
    token = _usage_sink.set(sink)
    try:
        yield
    finally:
        _usage_sink.reset(token)


__all__ = [
    "TokenUsage",
    "as_int",
    "collect_usage",
    "report_usage",
]
