"""
ui/swagger_refresh.py — وضعیتِ دکمه‌های Update قدم دوم (بدونِ Streamlit)

هر منبعِ Swagger (Admin، Customer) یک دکمه‌ی مستقل دارد با چهار حالت:

    idle    : دکمه فعال است
    loading : درخواست در جریان است — فقط دکمه‌ی همان منبع غیرفعال می‌شود
    success : فایلِ همان منبع جایگزین شد
    error   : خطا نمایش داده می‌شود و دکمه دوباره فعال است (تلاشِ دستی)

وضعیت در یک MutableMapping نگه داشته می‌شود (در UI همان st.session_state) تا
این قواعد بدونِ اجرای Streamlit قابلِ تست باشند — همان الگوی ui/workflow.py.

جریانِ دومرحله‌ای: کلیک فقط start_refresh را صدا می‌زند و صفحه دوباره کشیده
می‌شود؛ در اجرای بعد دکمه غیرفعال دیده می‌شود و run_pending_refresh درخواست را
می‌فرستد. پرچمِ loading همیشه (موفق، ناموفق یا استثنای غیرمنتظره) در finally
پاک می‌شود، پس دکمه هرگز قفل نمی‌ماند.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

from src.agents.test_case_generator.swagger_snapshots import (
    RefreshResult,
    SwaggerRefreshError,
    refresh_snapshot,
    source_for,
)

STATUS_SUCCESS = "success"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class RefreshStatus:
    """نتیجه‌ی آخرین تلاشِ تازه‌سازیِ یک منبع — برای پیامِ زیرِ دکمه."""

    kind: str
    message: str

    @property
    def ok(self) -> bool:
        return self.kind == STATUS_SUCCESS


def _loading_key(key: str) -> str:
    return f"_swagger_refresh_loading_{key}"


def _status_key(key: str) -> str:
    return f"_swagger_refresh_status_{key}"


def is_refreshing(state: MutableMapping[str, Any], key: str) -> bool:
    """آیا درخواستِ این منبع در جریان است؟ (دکمه‌اش باید غیرفعال باشد)"""
    return bool(state.get(_loading_key(key)))


def refresh_status(state: MutableMapping[str, Any], key: str) -> RefreshStatus | None:
    """نتیجه‌ی آخرین تلاش، یا None اگر هنوز تلاشی نشده باشد."""
    status = state.get(_status_key(key))
    return status if isinstance(status, RefreshStatus) else None


def start_refresh(state: MutableMapping[str, Any], key: str) -> bool:
    """کلیک روی دکمه — منبع را «در حالِ بارگذاری» می‌کند.

    خروجی False یعنی همین منبع از قبل در جریان است (کلیکِ تکراری نادیده گرفته
    می‌شود). پیامِ قبلی پاک می‌شود تا نتیجه‌ی کهنه کنارِ بارگذاری دیده نشود.
    """
    if is_refreshing(state, key):
        return False
    state[_loading_key(key)] = True
    state.pop(_status_key(key), None)
    return True


def success_message(result: RefreshResult) -> str:
    """پیامِ تأیید — کدام فایل، از کجا و با چند عملیات."""
    return (
        f"{result.label} Swagger refreshed from {result.url}. "
        f"data/swagger/{result.path.name} was updated "
        f"({result.operations} operations, {result.size_bytes / 1024:,.0f} KB). "
        f"Run Step 2 to map test cases against it."
    )


def run_pending_refresh(
    state: MutableMapping[str, Any],
    key: str,
    refresh: Callable[[str], RefreshResult] = refresh_snapshot,
) -> RefreshStatus | None:
    """اگر این منبع در حالِ بارگذاری است، یک درخواستِ تازه می‌فرستد.

    هر خطایی به یک RefreshStatusِ خطا تبدیل می‌شود (UI هرگز نمی‌شکند) و
    پرچمِ loading در هر حال پاک می‌شود تا دکمه برای تلاشِ دوباره آزاد شود.
    """
    if not is_refreshing(state, key):
        return None

    try:
        result = refresh(key)
        status = RefreshStatus(STATUS_SUCCESS, success_message(result))
    except SwaggerRefreshError as exc:
        status = RefreshStatus(STATUS_ERROR, f"{exc}\n\n{exc.hint}")
    except Exception as exc:  # noqa: BLE001 — خطای غیرمنتظره نباید UI را بترکاند
        label = source_for(key).label
        status = RefreshStatus(
            STATUS_ERROR,
            f"Failed to refresh {label} Swagger: unexpected error "
            f"({type(exc).__name__}: {exc}).\n\nThe existing file was kept. You can "
            f"try again.",
        )
    finally:
        state[_loading_key(key)] = False

    state[_status_key(key)] = status
    return status


__all__ = [
    "STATUS_ERROR",
    "STATUS_SUCCESS",
    "RefreshStatus",
    "is_refreshing",
    "refresh_status",
    "run_pending_refresh",
    "start_refresh",
    "success_message",
]
