"""
src/batch_testing/models.py — وضعیت‌ها، گذارهای مجاز و رکوردهای Batch

سه چیزِ مستقل:

  * وضعیتِ batch  (CREATED، RUNNING، PAUSED، …) + درخواستِ کنترلیِ جدا
    (requested_action: NONE / PAUSE / CANCEL) که UI ثبت می‌کند و runner اجرا؛
  * وضعیتِ اجرای هر issue (یک رکورد برای هر تلاش: QUEUED، RUNNING، PASS، …)؛
  * وضعیتِ بازبینی (review_status: NONE / NEEDS_REVIEW) — کاملاً جدا از اجرا؛
    وضعیتِ قدمِ ۵ (final_review_status) هم جداگانه نگه داشته می‌شود.

رکوردِ اجرا وقتی به یک وضعیتِ پایانی رسید دیگر تغییر نمی‌کند؛ تلاشِ دوباره
(resume بعد از قطع، یا retry) همیشه رکوردِ تازه‌ای با attemptِ بعدی است.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── batch ────────────────────────────────────────────────────────────────────
BATCH_CREATED = "CREATED"
BATCH_RUNNING = "RUNNING"
BATCH_PAUSED = "PAUSED"
BATCH_COMPLETED = "COMPLETED"
BATCH_CANCELLED = "CANCELLED"
# runner بی‌آنکه batch را ببندد از بین رفت (تشخیص در بازیابی، Milestone 4.5)
BATCH_INTERRUPTED = "INTERRUPTED"
BATCH_STATUSES = (
    BATCH_CREATED,
    BATCH_RUNNING,
    BATCH_PAUSED,
    BATCH_COMPLETED,
    BATCH_CANCELLED,
    BATCH_INTERRUPTED,
)
BATCH_FINAL = frozenset({BATCH_COMPLETED, BATCH_CANCELLED})

BATCH_TRANSITIONS: dict[str, frozenset[str]] = {
    BATCH_CREATED: frozenset({BATCH_RUNNING, BATCH_CANCELLED}),
    BATCH_RUNNING: frozenset({BATCH_PAUSED, BATCH_COMPLETED, BATCH_CANCELLED, BATCH_INTERRUPTED}),
    BATCH_PAUSED: frozenset({BATCH_RUNNING, BATCH_CANCELLED}),
    BATCH_INTERRUPTED: frozenset({BATCH_RUNNING, BATCH_CANCELLED}),
    BATCH_COMPLETED: frozenset(),
    BATCH_CANCELLED: frozenset(),
}

ACTION_NONE = "NONE"
ACTION_PAUSE = "PAUSE"
ACTION_CANCEL = "CANCEL"
REQUESTED_ACTIONS = (ACTION_NONE, ACTION_PAUSE, ACTION_CANCEL)

# ── اجرای یک issue ──────────────────────────────────────────────────────────
EXEC_QUEUED = "QUEUED"
EXEC_RUNNING = "RUNNING"
EXEC_PASS = "PASS"
EXEC_FAIL = "FAIL"
EXEC_ERROR = "ERROR"
EXEC_NOT_EXECUTED = "NOT_EXECUTED"
EXEC_CANCELLED = "CANCELLED"
EXEC_INTERRUPTED = "INTERRUPTED"
EXECUTION_STATUSES = (
    EXEC_QUEUED,
    EXEC_RUNNING,
    EXEC_PASS,
    EXEC_FAIL,
    EXEC_ERROR,
    EXEC_NOT_EXECUTED,
    EXEC_CANCELLED,
    EXEC_INTERRUPTED,
)
EXECUTION_ACTIVE = frozenset({EXEC_QUEUED, EXEC_RUNNING})
EXECUTION_FINAL = frozenset(EXECUTION_STATUSES) - EXECUTION_ACTIVE

EXECUTION_TRANSITIONS: dict[str, frozenset[str]] = {
    EXEC_QUEUED: frozenset({EXEC_RUNNING, EXEC_CANCELLED, EXEC_INTERRUPTED}),
    EXEC_RUNNING: frozenset(EXECUTION_FINAL),
    **{status: frozenset() for status in EXECUTION_FINAL},
}

# چرا این تلاش ساخته شد
TRIGGER_INITIAL = "initial"
TRIGGER_RESUME = "resume"  # تلاشِ تازه برای issueِ قطع‌شده
TRIGGER_RETRY = "retry"  # تلاشِ تازه به درخواستِ کاربر (Steps 1–5 + Newman)
TRIGGERS = (TRIGGER_INITIAL, TRIGGER_RESUME, TRIGGER_RETRY)

# مرحله‌ی جاریِ یک اجرا
PHASE_QUEUED = "queued"
PHASE_JIRA_FETCH = "jira_fetch"
PHASE_STEP1 = "step1"
PHASE_STEP2 = "step2"
PHASE_STEP3 = "step3"
PHASE_STEP4 = "step4"
PHASE_STEP5 = "step5"
PHASE_NEWMAN = "newman"
PHASE_FINALIZE = "finalize"
PHASE_DONE = "done"
PHASES = (
    PHASE_QUEUED,
    PHASE_JIRA_FETCH,
    PHASE_STEP1,
    PHASE_STEP2,
    PHASE_STEP3,
    PHASE_STEP4,
    PHASE_STEP5,
    PHASE_NEWMAN,
    PHASE_FINALIZE,
    PHASE_DONE,
)

# بازبینی — مستقل از وضعیتِ اجرا
REVIEW_NONE = "NONE"
REVIEW_NEEDS_REVIEW = "NEEDS_REVIEW"
REVIEW_STATUSES = (REVIEW_NONE, REVIEW_NEEDS_REVIEW)

# وضعیتِ قدمِ ۵ (final_review) — "" یعنی قدم ۵ اجرا نشد
FINAL_REVIEW_STATUSES = ("", "ready", "needs_review", "blocked")

BOARD_TYPES = ("scrum", "kanban")


class InvalidTransitionError(ValueError):
    """گذارِ وضعیتِ غیرمجاز — هیچ چیزی ذخیره نشد."""


def check_batch_transition(current: str, target: str) -> None:
    if target not in BATCH_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransitionError(f"A batch cannot go from {current} to {target}.")


def check_execution_transition(current: str, target: str) -> None:
    if target not in EXECUTION_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransitionError(f"An execution cannot go from {current} to {target}.")


# ── ورودی‌ها ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IssueSnapshot:
    """یک issueِ واجدِ شرایط، همان لحظه‌ی ساختِ batch — بعداً تغییر نمی‌کند."""

    issue_key: str
    summary: str = ""
    issue_type: str = ""
    status_name: str = ""
    labels: tuple[str, ...] = ()
    parent_key: str = ""
    eligibility: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NewBatch:
    board_id: int
    board_name: str
    board_type: str
    column_name: str
    criteria: dict[str, Any]
    # همه‌ی نامزدها — واجد و کنارگذاشته — با دلیل؛ فقط برای ردیابی
    discovery: dict[str, Any]
    issues: tuple[IssueSnapshot, ...]
    environment_path: str
    environment_sha256: str
    globals_path: str
    globals_sha256: str
    artifact_root: str


# ── رکوردهای ذخیره‌شده ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Batch:
    id: str
    created_at: str
    updated_at: str
    board_id: int
    board_name: str
    board_type: str
    column_name: str
    criteria: dict[str, Any]
    discovery: dict[str, Any]
    status: str
    requested_action: str
    owner_token: str | None
    owner_pid: int | None
    owner_host: str | None
    heartbeat_at: str | None
    started_at: str | None
    finished_at: str | None
    environment_path: str
    environment_sha256: str
    globals_path: str
    globals_sha256: str
    artifact_root: str


@dataclass(frozen=True)
class BatchIssue:
    batch_id: str
    issue_key: str
    position: int
    summary: str
    issue_type: str
    status_name: str
    labels: tuple[str, ...]
    parent_key: str
    eligibility: dict[str, Any]


@dataclass(frozen=True)
class Execution:
    id: str
    batch_id: str
    issue_key: str
    attempt: int
    trigger: str
    previous_execution_id: str | None
    status: str
    phase: str
    review_status: str
    review_reasons: list[Any]
    final_review_status: str
    error_stage: str
    error_kind: str
    error_message: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    heartbeat_at: str | None
    runner_token: str | None
    artifact_dir: str
    collection_path: str
    # فراداده‌ی منبعِ تسک (کلید، نوع/برچسبِ منبع، SHA-256) — هرگز متنِ خامِ Jira
    task_source: dict[str, Any]
    newman_run_id: str
    newman_status: str
    newman_error_kind: str
    newman_no_assertions: bool
    newman_run_dir: str
    newman_result_path: str
    requests_executed: int | None
    assertions_total: int | None
    assertions_failed: int | None
    cases_total: int | None
    cases_executable: int | None
    cases_not_executed: int | None

    @property
    def final(self) -> bool:
        return self.status in EXECUTION_FINAL


__all__ = [
    "ACTION_CANCEL",
    "ACTION_NONE",
    "ACTION_PAUSE",
    "BATCH_CANCELLED",
    "BATCH_COMPLETED",
    "BATCH_CREATED",
    "BATCH_FINAL",
    "BATCH_INTERRUPTED",
    "BATCH_PAUSED",
    "BATCH_RUNNING",
    "BATCH_STATUSES",
    "BATCH_TRANSITIONS",
    "BOARD_TYPES",
    "Batch",
    "BatchIssue",
    "EXECUTION_ACTIVE",
    "EXECUTION_FINAL",
    "EXECUTION_STATUSES",
    "EXECUTION_TRANSITIONS",
    "EXEC_CANCELLED",
    "EXEC_ERROR",
    "EXEC_FAIL",
    "EXEC_INTERRUPTED",
    "EXEC_NOT_EXECUTED",
    "EXEC_PASS",
    "EXEC_QUEUED",
    "EXEC_RUNNING",
    "Execution",
    "FINAL_REVIEW_STATUSES",
    "InvalidTransitionError",
    "IssueSnapshot",
    "NewBatch",
    "PHASES",
    "PHASE_DONE",
    "PHASE_FINALIZE",
    "PHASE_JIRA_FETCH",
    "PHASE_NEWMAN",
    "PHASE_QUEUED",
    "PHASE_STEP1",
    "PHASE_STEP2",
    "PHASE_STEP3",
    "PHASE_STEP4",
    "PHASE_STEP5",
    "REQUESTED_ACTIONS",
    "REVIEW_NEEDS_REVIEW",
    "REVIEW_NONE",
    "REVIEW_STATUSES",
    "TRIGGERS",
    "TRIGGER_INITIAL",
    "TRIGGER_RESUME",
    "TRIGGER_RETRY",
    "check_batch_transition",
    "check_execution_transition",
]
