"""
src/execution_gate — Gate ِ مشترکِ اجرا: حداکثر یک اجرا (Batch یا TestCase Box) در هر لحظه

API عمومی:

    gate = ExecutionGate()                    # پیش‌فرض: data/execution_gate/ — چیزی ساخته نمی‌شود
    lease = gate.acquire("BOX", "run-42")     # GateLease؛ یا GateBusy / GateUnavailable
    try:
        lease.heartbeat()                     # دوره‌ای؛ GateLeaseLost / GateUnavailable
        lease.assert_held()                   # حصار پیش از هر کارِ اجرایی
    finally:
        lease.release()                       # ReleaseOutcome؛ idempotent
    status = gate.status()                    # GateStatus — فقط‌خواندنی، بدونِ توکن

    with gate.acquire("BATCH", batch_id) as lease: ...   # همان، با آزادسازیِ خودکار

kind فقط "BATCH" یا "BOX" است. lease فقط kind، ref، generation و acquired_at را نشان
می‌دهد؛ توکنِ مالکیت هرگز در خطا، repr یا status نمی‌آید.

معماریِ ترکیبی:
  * data/execution_gate/gate.lock — قفلِ سیستم‌عامل، مرجعِ انحصارِ متقابل. Windows:
    msvcrt.locking (بایتِ صفر، غیرمسدودکننده)؛ Linux/POSIX: fcntl.flock(LOCK_EX|LOCK_NB).
    سکوی دیگر پشتیبانی نمی‌شود (GateUnavailable). فایل هرگز حذف یا جایگزین نمی‌شود.
  * data/execution_gate/gate.sqlite3 — رکوردِ مالک (kind، ref، pid، host، heartbeat،
    generation) و تاریخچه‌ی فقط-افزودنیِ ACQUIRED / RELEASED / RECLAIMED. فقط با قفلِ در دست
    نوشته می‌شود.

قاعده‌ی heartbeatِ کهنه: heartbeatِ کهنه هرگز اجازه‌ی گرفتن نمی‌دهد. تا وقتی پروسه‌ای قفلِ
سیستم‌عامل را دارد — حتی زنده ولی هنگ‌کرده — همه مسدودند. رکوردِ مالکِ قبلی فقط پس از آنکه
گیرنده‌ی تازه خودش قفلِ سیستم‌عامل را گرفته باشد بازپس گرفته می‌شود (RECLAIMED). مرگِ پروسه
قفل را از راهِ پاک‌سازیِ سیستم‌عامل آزاد می‌کند، پس قفلِ دائمی از crash نمی‌ماند.

fail-closed: هر وضعیتی که نتوان با اطمینان سنجید — پوشه یا فایلِ قفلِ ناخوانا، پایگاهِ
خراب، بیگانه یا با نسخه‌ی دیگر، خطای نوشتنِ رکورد، فایلِ قفلِ جایگزین‌شده — یعنی
GateUnavailable و هیچ چیزی شروع نمی‌شود. پایگاهِ ناسازگار هرگز بازنویسی یا تعمیر نمی‌شود.

محدودیت: فقط دیسکِ محلی و یک میزبان. سیستم‌فایلِ شبکه، چند میزبان و قفلِ توزیع‌شده پشتیبانی
نمی‌شوند (مسیرِ UNC روی Windows رد می‌شود؛ درایوِ شبکه‌ی نگاشت‌شده قابلِ تشخیص نیست و نباید
به کار رود). روی POSIX، fork بدونِ exec در حالِ نگه داشتنِ قفل باعث نمی‌شود فرزند قفل را
زنده نگه دارد (نسخه‌ی فرزند بسته می‌شود).

این بسته به Batch، TestCase Box، Newman، Jira یا توکن وابسته نیست.
"""

from .errors import GateBusy, GateError, GateLeaseLost, GateLeaseReleased, GateUnavailable

# قفلِ عمومیِ سیستم‌عامل — برای قفل‌های دیگرِ پروژه (مثلاً snapshotهای Swagger)؛ رفتارِ Gate عوض نمی‌شود
from ._oslock import OsLock
from .gate import (
    DEFAULT_GATE_DIRECTORY,
    KINDS,
    ExecutionGate,
    GateLease,
    GateOwner,
    GateStatus,
    ReleaseOutcome,
)

__all__ = [
    "DEFAULT_GATE_DIRECTORY",
    "KINDS",
    "ExecutionGate",
    "GateBusy",
    "GateError",
    "GateLease",
    "GateLeaseLost",
    "GateLeaseReleased",
    "GateOwner",
    "GateStatus",
    "GateUnavailable",
    "OsLock",
    "ReleaseOutcome",
]
