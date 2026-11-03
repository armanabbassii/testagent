"""
src/batch_testing/summary.py — خلاصه‌ی batch برای داشبورد و تاریخچه (فقط‌خواندنی)

  * هر issue با آخرین تلاشش (بیشترین attempt) شمرده می‌شود؛ تلاش‌های قبلی در
    تاریخچه‌ی همان issue می‌مانند.
  * «تمام‌شده» یعنی نتیجه‌ی پایانیِ واقعی: PASS، FAIL، ERROR، NOT_EXECUTED و
    CANCELLED — نه فقط PASS. INTERRUPTED جدا شمرده می‌شود و تمام‌شده نیست، چون
    resume برای آن تلاشِ تازه می‌سازد. issueِ بی‌تلاش یا QUEUED «در انتظار» است.
  * وضعیتِ بازبینی (NEEDS_REVIEW) مستقل از وضعیتِ اجراست: PASS + NEEDS_REVIEW
    همچنان PASS است.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from src.batch_testing.models import (
    EXEC_CANCELLED,
    EXEC_ERROR,
    EXEC_FAIL,
    EXEC_INTERRUPTED,
    EXEC_NOT_EXECUTED,
    EXEC_PASS,
    EXEC_QUEUED,
    EXEC_RUNNING,
    REVIEW_NEEDS_REVIEW,
    Batch,
    BatchIssue,
    Execution,
)
from src.batch_testing.recovery import STALE_AFTER_S, Clock, _utcnow, stale_cutoff

# issueی که هنوز هیچ تلاشی ندارد (batchِ CREATED) — فقط برای نمایش، در پایگاه نیست
STATUS_NOT_STARTED = "NOT_STARTED"

COMPLETED_STATUSES = frozenset(
    {EXEC_PASS, EXEC_FAIL, EXEC_ERROR, EXEC_NOT_EXECUTED, EXEC_CANCELLED}
)

# سلامتِ runnerِ صاحبِ lease
HEALTH_NONE = "none"  # هیچ runnerی lease ندارد
HEALTH_LIVE = "live"
HEALTH_STALE = "stale"  # heartbeat از آستانه‌ی کهنگی قدیمی‌تر است


@dataclass(frozen=True)
class IssueRow:
    issue: BatchIssue
    latest: Execution | None
    attempts: tuple[Execution, ...]

    @property
    def issue_key(self) -> str:
        return self.issue.issue_key

    @property
    def status(self) -> str:
        return self.latest.status if self.latest is not None else STATUS_NOT_STARTED


@dataclass(frozen=True)
class BatchCounts:
    total: int = 0
    completed: int = 0
    pending: int = 0
    running: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    not_executed: int = 0
    cancelled: int = 0
    interrupted: int = 0
    needs_review: int = 0

    @property
    def progress(self) -> float:
        """سهمِ issueهای با نتیجه‌ی پایانی (۰ تا ۱)."""
        return self.completed / self.total if self.total else 0.0


def issue_rows(issues: Iterable[BatchIssue], executions: Iterable[Execution]) -> list[IssueRow]:
    """هر issue به ترتیبِ منجمدِ batch با همه‌ی تلاش‌هایش (به ترتیبِ attempt)."""
    by_key: dict[str, list[Execution]] = {}
    for execution in executions:
        by_key.setdefault(execution.issue_key, []).append(execution)
    rows = []
    for issue in sorted(issues, key=lambda item: item.position):
        attempts = tuple(sorted(by_key.get(issue.issue_key, ()), key=lambda e: e.attempt))
        rows.append(IssueRow(issue, attempts[-1] if attempts else None, attempts))
    return rows


def count(rows: Iterable[IssueRow]) -> BatchCounts:
    tally = dict.fromkeys(BatchCounts.__dataclass_fields__, 0)
    field_of = {
        EXEC_PASS: "passed",
        EXEC_FAIL: "failed",
        EXEC_ERROR: "errors",
        EXEC_NOT_EXECUTED: "not_executed",
        EXEC_CANCELLED: "cancelled",
    }
    for row in rows:
        tally["total"] += 1
        status = row.status
        if status in COMPLETED_STATUSES:
            tally["completed"] += 1
            tally[field_of[status]] += 1
        elif status == EXEC_RUNNING:
            tally["running"] += 1
        elif status == EXEC_INTERRUPTED:
            tally["interrupted"] += 1
        elif status in (EXEC_QUEUED, STATUS_NOT_STARTED):
            tally["pending"] += 1
        if row.latest is not None and row.latest.review_status == REVIEW_NEEDS_REVIEW:
            tally["needs_review"] += 1
    return BatchCounts(**tally)


def current_row(rows: Iterable[IssueRow]) -> IssueRow | None:
    """issueِ در حالِ اجرا (حداکثر یکی؛ اجرا ترتیبی است)."""
    return next((row for row in rows if row.status == EXEC_RUNNING), None)


def runner_health(
    batch: Batch, *, stale_after: float = STALE_AFTER_S, clock: Clock = _utcnow
) -> str:
    """همان آستانه‌ی بازیابی: heartbeatِ قدیمی‌تر از stale_after یعنی runnerِ ازدست‌رفته."""
    if not batch.owner_token:
        return HEALTH_NONE
    if batch.heartbeat_at is None or batch.heartbeat_at < stale_cutoff(stale_after, clock):
        return HEALTH_STALE
    return HEALTH_LIVE


__all__ = [
    "COMPLETED_STATUSES",
    "HEALTH_LIVE",
    "HEALTH_NONE",
    "HEALTH_STALE",
    "STATUS_NOT_STARTED",
    "BatchCounts",
    "IssueRow",
    "count",
    "current_row",
    "issue_rows",
    "runner_health",
]
