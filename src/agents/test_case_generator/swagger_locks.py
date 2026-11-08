"""
swagger_locks.py — قفل‌های مشترکِ snapshotهای Swagger (دکمه‌ی Update و همگام‌سازیِ روزانه)

    with write_lock(directory):        # هر نوشتنِ فایلِ snapshot — UI و job
        ...
    lock = try_run_lock(directory)     # فقط job: یک اجرا در هر لحظه (None = اجرای دیگری هست)

هر دو روی OsLock ِ execution_gate اند (قفلِ انحصاریِ سیستم‌عامل؛ با مرگِ پروسه آزاد می‌شود).
مسیرِ قفل‌ها از پوشه‌ی snapshot مشتق می‌شود تا UI و job همیشه روی یک فایل توافق کنند:

  * پوشه‌ی واقعی (data/swagger) → <repo>/logs/swagger_sync/ (در .gitignore)؛
  * هر پوشه‌ی دیگر (مثلاً tmp ِ تست‌ها) → پوشه‌ای مخصوصِ همان مسیر در temp ِ سیستم.

پس هیچ فایلِ قفلی کنارِ snapshotها یا در Git ساخته نمی‌شود. فایلِ قفل هرگز حذف نمی‌شود.
قفلِ نوشتن فقط حولِ جایگزینیِ اتمی (چند میلی‌ثانیه) گرفته می‌شود، پس UI در طولِ اجرای
طولانیِ job منتظر نمی‌ماند؛ قفلِ اجرا مستقل از آن است.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from src.execution_gate import GateUnavailable, OsLock

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SNAPSHOT_DIR = REPO_ROOT / "data" / "swagger"
DEFAULT_LOCK_DIR = REPO_ROOT / "logs" / "swagger_sync"
WRITE_LOCK_NAME = "write.lock"
RUN_LOCK_NAME = "run.lock"
WRITE_LOCK_WAIT_S = 30.0
_POLL_S = 0.1


class SwaggerLockBusy(RuntimeError):
    """قفل در زمانِ مجاز گرفته نشد — هیچ فایلی نوشته نشد."""


class SwaggerLockUnavailable(RuntimeError):
    """وضعیتِ قفل معلوم نشد (fail-closed) — هیچ فایلی نوشته نشد."""


def lock_dir_for(directory: Path | str) -> Path:
    resolved = Path(directory).resolve()
    if resolved == DEFAULT_SNAPSHOT_DIR.resolve():
        return DEFAULT_LOCK_DIR
    digest = hashlib.sha256(str(resolved).lower().encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "swagger_sync_locks" / digest


def _lock(directory: Path | str, name: str) -> OsLock:
    folder = lock_dir_for(directory)
    folder.mkdir(parents=True, exist_ok=True)
    return OsLock(folder / name)


@contextmanager
def write_lock(directory: Path | str, *, wait_s: float = WRITE_LOCK_WAIT_S) -> Iterator[None]:
    """قفلِ نوشتنِ snapshotها؛ تا wait_s ثانیه منتظر می‌ماند، بعد SwaggerLockBusy."""
    lock = _lock(directory, WRITE_LOCK_NAME)
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            acquired = lock.try_acquire()
        except GateUnavailable as exc:
            raise SwaggerLockUnavailable(str(exc)) from None
        if acquired:
            break
        if time.monotonic() >= deadline:
            raise SwaggerLockBusy("Another process is writing the Swagger snapshots.")
        time.sleep(_POLL_S)
    try:
        yield
    finally:
        lock.release()


def try_run_lock(directory: Path | str) -> OsLock | None:
    """قفلِ اجرای job (بی‌انتظار): OsLock ِ گرفته‌شده، یا None اگر اجرای دیگری هست."""
    lock = _lock(directory, RUN_LOCK_NAME)
    try:
        return lock if lock.try_acquire() else None
    except GateUnavailable as exc:
        raise SwaggerLockUnavailable(str(exc)) from None


__all__ = [
    "DEFAULT_LOCK_DIR",
    "RUN_LOCK_NAME",
    "WRITE_LOCK_NAME",
    "WRITE_LOCK_WAIT_S",
    "SwaggerLockBusy",
    "SwaggerLockUnavailable",
    "lock_dir_for",
    "try_run_lock",
    "write_lock",
]
