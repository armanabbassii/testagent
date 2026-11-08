"""
src/testcase_box/recovery.py — بازیابیِ امنِ runهای رهاشده (M7.6)

    candidates = inspect_runs(store_path, gate_factory=...)          # فقط خواندن
    result = recover_run(store_path, run_id, gate_factory=..., expected_generation=...)

runِ رهاشده: RUNNING یا QUEUED با lease ای که heartbeatش کهنه است — پروسه‌ی برنامه وسطِ اجرا مُرد،
finish_run شکست خورد، یا lease از دست رفت. هیچ چیزی این‌جا اجرا نمی‌شود: نه Newman، نه درخواست،
نه Jira/LLM؛ نسخه‌ها و caseها دست نمی‌خورند. فقط storage ِ مالکیت‌دار و Execution Gate ِ مشترک.

ترتیبِ بازیابی (هر گام fail-closed):
  1. بررسیِ فقط‌خواندنی: run هست، هنوز فعال است، lease دارد و heartbeatش از stale_after کهنه‌تر است؛
  2. Gate ِ مشترک با kind="BOX" — اگر دستِ دیگری است (Batch، Box، پروسه‌ی زنده‌ی هنگ‌کرده، Gate ِ
     قرنطینه‌شده‌ی پایانِ تأییدنشده) بازیابی رد می‌شود. کهنگیِ heartbeat به‌تنهایی هرگز کافی نیست:
     runner در تمامِ نشست Gate را دارد و مرگِ پروسه‌اش (با Job Object ِ supervision.py) درختِ Newman
     را هم می‌کشد، پس گرفتنِ Gate یعنی هیچ اجرای زنده‌ای نیست؛
  3. با Gate ِ در دست: خواندنِ دوباره، بررسیِ دوباره‌ی کهنگی و generation (اگر عوض شده → رد)؛
  4. take_over_run ِ شرطی (generation + آستانه‌ی کهنگی) — مالکِ قبلی با fencing دیگر هیچ چیزی
     نمی‌نویسد؛
  5. یک finish_run ِ اتمی: نتیجه‌های ثبت‌شده دست نمی‌خورند؛ unitِ RUNNING (نتیجه نامعلوم) → ERROR با
     classification=OUTCOME_UNKNOWN (هرگز PASS/FAIL)؛ unitهای شروع‌نشده → NOT_RUN (یا CANCELLED اگر
     لغو درخواست شده بود)؛ وضعیتِ run از نتیجه‌های ثبت‌شده: همه‌ی کار انجام شده → COMPLETED، لغوِ
     درخواست‌شده → CANCELLED، وگرنه INTERRUPTED — با metadata.recovery و stop_reason؛
  6. آزادسازیِ Gate.

idempotent: runِ پایانی → ALREADY_FINAL بی‌هیچ تغییری (بی‌Gate، بی‌افزایشِ generation). اگر finish_run
شکست بخورد، rollback کامل است، run همچنان RUNNING و با lease ِ بازیابی می‌ماند و پس از کهنه شدن
دوباره قابلِ بازیابی است — هرگز گزارشِ موفقیتِ ساختگی.
"""

from __future__ import annotations

import os
import socket
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.execution_gate import ExecutionGate, GateBusy, GateError
from src.testcase_box.db import NotFoundError, RunOwnershipError, StorageError, now
from src.testcase_box.models import (
    CASE_RUN_CANCELLED,
    CASE_RUN_ERROR,
    CASE_RUN_NOT_EXECUTABLE,
    CASE_RUN_NOT_RUN,
    CASE_RUN_QUEUED,
    RUN_ACTION_CANCEL,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FINAL,
    RUN_INTERRUPTED,
    UNIT_CANCELLED,
    UNIT_ERROR,
    UNIT_FAIL,
    UNIT_NOT_RUN,
    UNIT_QUEUED,
    UNIT_RUNNING,
    BoxRun,
)
from src.testcase_box.storage import BoxStore

STALE_AFTER_S = 60.0

RECOVERED = "RECOVERED"
ALREADY_FINAL = "ALREADY_FINAL"
BLOCKED = "BLOCKED"
FAILED = "FAILED"

BLOCKED_NOT_FOUND = "NOT_FOUND"
BLOCKED_NOT_LEASED = "NOT_LEASED"  # فعال ولی بی‌مالک — هرگز از runner به وجود نمی‌آید
BLOCKED_NOT_STALE = "NOT_STALE"
BLOCKED_GATE_BUSY = "GATE_BUSY"
BLOCKED_GATE_UNAVAILABLE = "GATE_UNAVAILABLE"
BLOCKED_OWNER_CHANGED = "OWNER_CHANGED"

# metadata.recovery.reason و metadata.stop_reason
REASON_APPLICATION_TERMINATED = "RECOVERED_AFTER_APPLICATION_TERMINATION"
REASON_FINALIZATION = "RECOVERED_FINALIZATION"
STOP_USER_CANCELLED = "USER_CANCELLED"
CLASS_OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
NOT_VERIFIED = "NOT_VERIFIED"

_STOPPED_CLASSIFICATIONS = frozenset({"NOT_RUN", "PRE_EXECUTION_BLOCKED", "CANCELLED"})


def _default_gate() -> ExecutionGate:
    return ExecutionGate()


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _cutoff(stale_after: float) -> str:
    moment = datetime.now(UTC) - timedelta(seconds=stale_after)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class RecoveryCandidate:
    """وضعیتِ یک runِ فعال برای نمایش — بی‌توکن. recoverable فقط یعنی «ارزشِ تلاش دارد»: اینکه
    Gate آزاد است فقط با تلاشِ واقعیِ بازیابی معلوم می‌شود (holder فقط رکوردِ ثبت‌شده است)."""

    run_id: int
    status: str
    leased: bool
    lease_generation: int
    heartbeat_at: str | None
    heartbeat_age_s: float | None
    stale: bool
    requested_action: str
    recorded_units: int
    open_units: int
    gate_holder: str
    recoverable: bool
    blocked_reason: str


@dataclass(frozen=True)
class RecoveryResult:
    status: str  # RECOVERED | ALREADY_FINAL | BLOCKED | FAILED
    run_id: int
    reason: str = ""  # BLOCKED_* یا REASON_*
    final_status: str = ""
    message: str = ""


def _gate_holder(gate_factory: Callable[[], ExecutionGate]) -> str:
    try:
        owner = gate_factory().status().owner
    except GateError:
        return "unknown (the execution gate could not be read)"
    if owner is None:
        return ""
    return f"{owner.kind} {owner.ref} (process {owner.pid} on {owner.host})"


def inspect_runs(
    store_path,
    *,
    gate_factory: Callable[[], ExecutionGate] | None = None,
    stale_after: float = STALE_AFTER_S,
) -> list[RecoveryCandidate]:
    """runهای فعال (RUNNING یا دارای lease) — فقط خواندن؛ هیچ Gate ای گرفته نمی‌شود."""
    gate = gate_factory or _default_gate
    holder = _gate_holder(gate)
    moment = datetime.now(UTC)
    candidates = []
    with BoxStore.open(store_path) as store:
        for active in store.active_runs():
            ownership = store.run_ownership(active.id)
            units = store.run_units(active.id)
            age = (
                (moment - _parse(ownership.heartbeat_at)).total_seconds()
                if ownership.heartbeat_at
                else None
            )
            stale = age is not None and age >= stale_after
            if not ownership.leased:
                reason = BLOCKED_NOT_LEASED
            elif not stale:
                reason = BLOCKED_NOT_STALE
            else:
                reason = ""
            candidates.append(
                RecoveryCandidate(
                    run_id=active.id,
                    status=ownership.status,
                    leased=ownership.leased,
                    lease_generation=ownership.lease_generation,
                    heartbeat_at=ownership.heartbeat_at,
                    heartbeat_age_s=age,
                    stale=stale,
                    requested_action=ownership.requested_action,
                    recorded_units=sum(
                        1 for u in units if u.status not in (UNIT_QUEUED, UNIT_RUNNING)
                    ),
                    open_units=sum(1 for u in units if u.status in (UNIT_QUEUED, UNIT_RUNNING)),
                    gate_holder=holder,
                    recoverable=not reason,
                    blocked_reason=reason,
                )
            )
    return candidates


def recover_run(
    store_path,
    run_id: int,
    *,
    gate_factory: Callable[[], ExecutionGate] | None = None,
    stale_after: float = STALE_AFTER_S,
    expected_generation: int | None = None,
    pid: int | None = None,
    host: str | None = None,
) -> RecoveryResult:
    """بازیابیِ یک run — ترتیب در docstring ِ ماژول. هیچ استثنایی بیرون نمی‌دهد."""
    pid = os.getpid() if pid is None else pid
    host = socket.gethostname() if host is None else host
    try:
        with BoxStore.open(store_path) as store:
            first = _check(store, run_id, stale_after, expected_generation)
            if isinstance(first, RecoveryResult):
                return first
            gate = gate_factory or _default_gate
            try:
                gate_lease = gate().acquire("BOX", f"box-recovery-{run_id}")
            except GateBusy as exc:
                return RecoveryResult(
                    BLOCKED,
                    run_id,
                    BLOCKED_GATE_BUSY,
                    message=f"Recovery is blocked: {exc} A live execution may still be running.",
                )
            except GateError as exc:
                return RecoveryResult(
                    BLOCKED, run_id, BLOCKED_GATE_UNAVAILABLE, message=f"Recovery is blocked: {exc}"
                )
            try:
                return _recover_with_gate(store, run_id, first, stale_after, pid, host)
            finally:
                gate_lease.release()  # بازیابی هیچ پروسه‌ای شروع نکرد — آزادسازی امن است
    except (StorageError, sqlite3.Error) as exc:
        return RecoveryResult(
            FAILED,
            run_id,
            message=f"Recovery failed ({type(exc).__name__}); the run is unchanged and can be "
            "recovered again later.",
        )


def _check(store: BoxStore, run_id: int, stale_after: float, expected_generation: int | None):
    """RecoveryResult یعنی «کاری نکن»؛ وگرنه generation ِ دیده‌شده."""
    try:
        run = store.get_run(run_id)
    except NotFoundError:
        return RecoveryResult(BLOCKED, run_id, BLOCKED_NOT_FOUND, message="The run does not exist.")
    if run.status in RUN_FINAL:
        return RecoveryResult(
            ALREADY_FINAL,
            run_id,
            final_status=run.status,
            message=f"The run is already {run.status}; nothing was changed.",
        )
    ownership = store.run_ownership(run_id)
    if not ownership.leased:
        return RecoveryResult(
            BLOCKED,
            run_id,
            BLOCKED_NOT_LEASED,
            message="The run is active but has no owner record; it cannot be taken over safely.",
        )
    if expected_generation is not None and ownership.lease_generation != expected_generation:
        return RecoveryResult(
            BLOCKED,
            run_id,
            BLOCKED_OWNER_CHANGED,
            message="The run owner changed since it was inspected; inspect it again.",
        )
    if not ownership.heartbeat_at or ownership.heartbeat_at >= _cutoff(stale_after):
        return RecoveryResult(
            BLOCKED,
            run_id,
            BLOCKED_NOT_STALE,
            message=f"The run heartbeat is not older than {stale_after:g} seconds; its runner may "
            "still be alive.",
        )
    return ownership.lease_generation


def _recover_with_gate(
    store: BoxStore, run_id: int, seen_generation: int, stale_after: float, pid: int, host: str
) -> RecoveryResult:
    again = _check(store, run_id, stale_after, seen_generation)  # با Gate ِ در دست، دوباره
    if isinstance(again, RecoveryResult):
        return again
    previous = store.run_ownership(run_id)
    try:
        lease = store.take_over_run(
            run_id,
            expected_generation=seen_generation,
            stale_before=_cutoff(stale_after),
            pid=pid,
            host=host,
        )
    except RunOwnershipError:
        return RecoveryResult(
            BLOCKED,
            run_id,
            BLOCKED_OWNER_CHANGED,
            message="The run owner changed during recovery; nothing was changed.",
        )
    run = store.get_run(run_id)
    final_status, reason, plan = _plan_closure(store, run, previous.requested_action)
    metadata = {
        **run.metadata,
        "summary": plan["summary"],
        "recovery": {
            "reason": reason,
            "recovered_at": now(),
            "previous_owner_pid": previous.owner_pid,
            "previous_owner_host": previous.owner_host,
            "previous_generation": seen_generation,
            "unknown_outcome_units": plan["running"],
            "not_started_units": plan["queued"],
        },
    }
    if final_status != RUN_COMPLETED:
        metadata["stop_reason"] = (
            STOP_USER_CANCELLED if final_status == RUN_CANCELLED else REASON_APPLICATION_TERMINATED
        )
    cancelled = final_status == RUN_CANCELLED
    marker = {"recovered": True, "recovery_reason": reason}
    try:
        store.finish_run(
            lease,
            final_status,
            metadata=metadata,
            queued_unit_status=UNIT_CANCELLED if cancelled else UNIT_NOT_RUN,
            running_unit_status=UNIT_ERROR,
            open_case_status=CASE_RUN_CANCELLED if cancelled else CASE_RUN_NOT_RUN,
            child_metadata=marker,
            running_case_status=CASE_RUN_ERROR,
            running_child_metadata={
                **marker,
                "classification": CLASS_OUTCOME_UNKNOWN,
                "verification": NOT_VERIFIED,
            },
        )
    except (StorageError, sqlite3.Error, ValueError) as exc:
        return RecoveryResult(
            FAILED,
            run_id,
            reason,
            message=f"Recovery could not finalize the run ({type(exc).__name__}); it stays "
            "recoverable and was not reported as finished.",
        )
    return RecoveryResult(
        RECOVERED,
        run_id,
        reason,
        final_status=final_status,
        message=f"The run was recovered and marked {final_status}.",
    )


def _plan_closure(store: BoxStore, run: BoxRun, requested_action: str) -> tuple[str, str, dict]:
    """وضعیتِ پایانی فقط از نتیجه‌های ثبت‌شده؛ نتیجه‌ی ثبت‌شده هرگز بازنویسی نمی‌شود."""
    units = store.run_units(run.id)
    cases = {unit.id: store.run_cases(run.id, unit_id=unit.id) for unit in units}
    running = [u.unit_number for u in units if u.status == UNIT_RUNNING]
    queued_eligible = [
        u.unit_number
        for u in units
        if u.status == UNIT_QUEUED and any(c.status == CASE_RUN_QUEUED for c in cases[u.id])
    ]
    stopped = [
        u.unit_number
        for u in units
        if u.status == UNIT_CANCELLED
        or (
            u.status == UNIT_NOT_RUN
            and u.metadata.get("classification") in _STOPPED_CLASSIFICATIONS
            and not all(c.status == CASE_RUN_NOT_EXECUTABLE for c in cases[u.id])
        )
    ]
    if not running and not queued_eligible and not stopped:
        final, reason = RUN_COMPLETED, REASON_FINALIZATION
    elif requested_action == RUN_ACTION_CANCEL:
        final, reason = RUN_CANCELLED, REASON_APPLICATION_TERMINATED
    else:
        final, reason = RUN_INTERRUPTED, REASON_APPLICATION_TERMINATED
    # وضعیتِ پیش‌بینی‌شده‌ی هر unit پس از همین finish_run (همان قواعدی که finish_run اعمال می‌کند)
    queued_target = UNIT_CANCELLED if final == RUN_CANCELLED else UNIT_NOT_RUN
    predicted = [
        UNIT_ERROR
        if u.status == UNIT_RUNNING
        else queued_target
        if u.status == UNIT_QUEUED
        else u.status
        for u in units
    ]
    summary = {
        "units": len(units),
        "verified_pass": sum(1 for u in units if u.metadata.get("verification") == "VERIFIED"),
        "unverified": sum(1 for u in units if u.metadata.get("verification") == "UNVERIFIED"),
        "failed": predicted.count(UNIT_FAIL),
        "errors": predicted.count(UNIT_ERROR),
        "not_run": predicted.count(UNIT_NOT_RUN),
        "cancelled": predicted.count(UNIT_CANCELLED),
    }
    return final, reason, {"summary": summary, "running": running, "queued": queued_eligible}


__all__ = [
    "ALREADY_FINAL",
    "BLOCKED",
    "FAILED",
    "RECOVERED",
    "RecoveryCandidate",
    "RecoveryResult",
    "inspect_runs",
    "recover_run",
]
