"""
src/execution_gate/errors.py — خطاهای دامنه‌ی Execution Gate

پیام‌ها برای کاربرند و هرگز توکنِ مالکیت یا مسیرِ داخلیِ دیگری را ندارند.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .gate import GateOwner


class GateError(RuntimeError):
    """پایه‌ی همه‌ی خطاهای Gate."""


class GateBusy(GateError):
    """قفلِ سیستم‌عامل را مالکِ دیگری نگه داشته است؛ هیچ چیزی شروع نشد.

    holder فقط رکوردِ ثبت‌شده‌ی پایگاه است (برای نمایش) و ممکن است نبود یا کهنه باشد —
    تصمیمِ «مشغول بودن» را خودِ قفلِ سیستم‌عامل گرفته است، نه این رکورد.
    """

    def __init__(self, holder: GateOwner | None = None) -> None:
        self.holder = holder
        if holder is None:
            text = "Another execution holds the execution gate; nothing was started."
        else:
            text = (
                f"{holder.kind} {holder.ref} holds the execution gate (process {holder.pid} on "
                f"{holder.host}); nothing was started."
            )
        super().__init__(text)


class GateUnavailable(GateError):
    """Gate را نمی‌توان با اطمینان بررسی یا گرفت — همیشه fail-closed: چیزی شروع نمی‌شود."""


class GateLeaseLost(GateError):
    """این lease دیگر مالکیتِ قابلِ اثبات ندارد (رکوردِ پایگاه یا فایلِ قفل با آن نمی‌خواند).

    فراخواننده باید کارش را متوقف کند و release() را صدا بزند.
    """


class GateLeaseReleased(GateLeaseLost):
    """عملیات روی leaseی که مالکش قبلاً آزادش کرده است."""


__all__ = ["GateBusy", "GateError", "GateLeaseLost", "GateLeaseReleased", "GateUnavailable"]
