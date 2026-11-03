"""
src/batch_testing/recovery.py — بازیابیِ runnerِ ازکارافتاده (Milestone 4.5)

    reports = recover_stale(store)

runnerی که heartbeatش بیش از STALE_AFTER_S ثانیه قدیمی است «مرده» حساب می‌شود:

  * lease با BatchStore.take_over_lease پس گرفته می‌شود — یک UPDATEِ شرطی
    («هنوز همان مالک و هنوز کهنه»)؛ leaseِ runnerِ سالم، یا leaseی که مالکش
    عوض شده، هرگز گرفته نمی‌شود و هر نوشتنِ بعدی با توکنِ بازیابی محافظت می‌شود؛
  * هر اجرای RUNNINGِ آن batch → INTERRUPTED (phase، مسیرها و فراداده می‌مانند؛
    حدس زده نمی‌شود Newman یا خطِ لوله تمام شده یا نه)؛
  * batchِ RUNNING → INTERRUPTED (یا CANCELLED اگر CANCEL درخواست شده بود)؛
  * lease آزاد می‌شود. resume برای issueِ قطع‌شده تلاشِ تازه می‌سازد.

اجرای دوباره بی‌ضرر است: هر گام شرطی است و اجرای پایانی هرگز دست نمی‌خورد.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from src.batch_testing.models import (
    ACTION_CANCEL,
    BATCH_CANCELLED,
    BATCH_INTERRUPTED,
    BATCH_RUNNING,
    EXEC_CANCELLED,
    EXEC_INTERRUPTED,
    EXEC_QUEUED,
    EXEC_RUNNING,
    Batch,
)
from src.batch_testing.storage import BatchStore, LeaseError, OwnershipError

STALE_AFTER_S = 60.0
ERROR_RUNNER_LOST = "RUNNER_LOST"
RUNNER_LOST_MESSAGE = "The runner stopped before this execution finished."

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def stale_cutoff(stale_after: float = STALE_AFTER_S, clock: Clock = _utcnow) -> str:
    """همان قالبِ storage.now() تا مقایسه‌ی رشته‌ای درست باشد."""
    moment = clock() - timedelta(seconds=stale_after)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def interrupt_running(
    store: BatchStore,
    batch_id: str,
    *,
    owner_token: str | None,
    error_kind: str = ERROR_RUNNER_LOST,
    message: str = RUNNER_LOST_MESSAGE,
) -> list[str]:
    """اجراهای RUNNINGِ batch → INTERRUPTED؛ phase و همه‌ی فراداده دست‌نخورده.

    هر تغییر با owner_token در همان تراکنشِ بررسیِ مالکیت انجام می‌شود.
    """
    interrupted = []
    for execution in store.executions(batch_id):
        if execution.status == EXEC_RUNNING:
            store.finish_execution(
                execution.id,
                EXEC_INTERRUPTED,
                owner_token=owner_token,
                phase=execution.phase,
                error_stage=execution.phase,
                error_kind=error_kind,
                error_message=message,
            )
            interrupted.append(execution.id)
    return interrupted


def cancel_queued(store: BatchStore, batch_id: str, *, owner_token: str | None) -> list[str]:
    """اجراهای QUEUEDِ batch → CANCELLED (هیچ‌وقت شروع نشدند) — با بررسیِ مالکیت."""
    cancelled = []
    for execution in store.executions(batch_id):
        if execution.status == EXEC_QUEUED:
            store.finish_execution(
                execution.id, EXEC_CANCELLED, owner_token=owner_token, phase=execution.phase
            )
            cancelled.append(execution.id)
    return cancelled


@dataclass
class RecoveryReport:
    batch_id: str
    recovered: bool = False
    # stale_lease | orphan_running | skipped
    reason: str = ""
    batch_status: str = ""
    interrupted_executions: list[str] = field(default_factory=list)
    cancelled_executions: list[str] = field(default_factory=list)


def recover_stale(
    store: BatchStore,
    *,
    stale_after: float = STALE_AFTER_S,
    clock: Clock = _utcnow,
    pid: int | None = None,
    host: str | None = None,
) -> list[RecoveryReport]:
    """همه‌ی batchهای با leaseِ کهنه، و batchِ RUNNINGِ بی‌مالک، بازیابی می‌شوند."""
    cutoff = stale_cutoff(stale_after, clock)
    candidates: dict[str, Batch] = {batch.id: batch for batch in store.stale_leases(cutoff)}
    active = store.active_batch()
    if active is not None and active.owner_token is None and active.status == BATCH_RUNNING:
        candidates.setdefault(active.id, active)
    return [
        _recover(
            store,
            batch,
            cutoff,
            pid=os.getpid() if pid is None else pid,
            host=socket.gethostname() if host is None else host,
        )
        for batch in candidates.values()
    ]


def _recover(store: BatchStore, seen: Batch, cutoff: str, *, pid: int, host: str) -> RecoveryReport:
    report = RecoveryReport(batch_id=seen.id)
    token = f"recovery-{uuid.uuid4().hex}"
    try:
        if seen.owner_token:
            # یک UPDATEِ شرطی: هنوز همان مالک و هنوز کهنه — وگرنه هیچ تغییری
            store.take_over_lease(
                seen.id,
                expected_token=seen.owner_token,
                new_token=token,
                stale_before=cutoff,
                pid=pid,
                host=host,
            )
            report.reason = "stale_lease"
        elif seen.status == BATCH_RUNNING:
            # بی‌مالک: acquire خودش داخلِ تراکنش بررسی می‌کند که مالکی نیست
            store.acquire_lease(seen.id, token, pid=pid, host=host)
            report.reason = "orphan_running"
        else:
            raise LeaseError("Nothing to recover.")
    except LeaseError:
        report.reason, report.batch_status = "skipped", store.get_batch(seen.id).status
        return report
    try:
        report.interrupted_executions = interrupt_running(store, seen.id, owner_token=token)
        batch = store.get_batch(seen.id)
        if batch.status == BATCH_RUNNING:
            if batch.requested_action == ACTION_CANCEL:
                report.cancelled_executions = cancel_queued(store, seen.id, owner_token=token)
                target = BATCH_CANCELLED
            else:
                target = BATCH_INTERRUPTED
            store.transition_batch(seen.id, target, expected=BATCH_RUNNING, owner_token=token)
        report.recovered = True
    except OwnershipError:
        # بازیابیِ دیگری این بازیابی را کهنه دید و lease را گرفت — دیگر چیزی نمی‌نویسیم
        report.reason, report.batch_status = "skipped", store.get_batch(seen.id).status
        return report
    try:
        store.release_lease(seen.id, token)
    except OwnershipError:
        pass
    report.batch_status = store.get_batch(seen.id).status
    return report


__all__ = [
    "ERROR_RUNNER_LOST",
    "STALE_AFTER_S",
    "RecoveryReport",
    "cancel_queued",
    "interrupt_running",
    "recover_stale",
    "stale_cutoff",
]
