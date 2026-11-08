"""
src/batch_testing/runner.py — اجرای ترتیبیِ issueهای یک batch (Milestone 4.5)

    runner = BatchRunner(db_path)
    result = runner.start(batch_id)     # CREATED → RUNNING → …
    result = runner.resume(batch_id)    # PAUSED / INTERRUPTED → RUNNING → …
    result = runner.retry(batch_id, "PODIUM-12")   # فقط در batchِ COMPLETED / CANCELLED

  * فقط با leaseِ BatchStore و توکنِ یکتای همین نمونه؛ هر نوشتن owner_token
    می‌دهد و BatchStore مالکیت را در همان تراکنشِ تغییر بررسی می‌کند، پس runnerی
    که lease را از دست داد هیچ چیزی نمی‌نویسد و issueِ تازه‌ای شروع نمی‌کند.
  * پیش از هر issue، پیش از retry و پیش از resume، Environment و Globals با
    هشِ ثبت‌شده در batch مقایسه می‌شوند؛ فایلِ گم‌شده یا تغییرکرده یعنی توقف
    (خطای CONFIGURATION_CHANGED) — هرگز اجرا با پیکربندیِ دیگر.
  * توکنِ دسترسی: هر نشست (start، resume، retry) پیش از اولین issue — پیش از Jira و
    LLM — یک توکنِ تازه از TokenProvider می‌گیرد و برای همه‌ی issueهای همان نشست به
    کار می‌برد (فقط در حافظه). شکست: issueِ بعدی اجرا نمی‌شود و با ERROR /
    AUTHENTICATION_FAILED ثبت می‌شود و batch مثلِ CONFIGURATION_CHANGED، INTERRUPTED
    و قابلِ resume می‌ماند؛ در retry هیچ تلاشی ساخته نمی‌شود. پس از 401 در Newman
    توکن خودکار تازه نمی‌شود — batchِ خیلی طولانی ممکن است به انقضای توکن بخورد.
  * request_cancel(): لغوِ درون‌پروسه‌ای، تنها راهِ لغوِ retry (batchِ پایانی
    گذاری ندارد و CANCELِ پایگاه فقط برای batchِ RUNNING پذیرفته می‌شود).
  * یک threadِ پایش (با اتصالِ جدای خودش) heartbeat می‌زند — حتی وقتی
    IssueExecutor دقیقه‌ها در LLM یا Newman است — و CANCEL را به cancel_eventِ
    issueِ جاری می‌رساند. با پایانِ runner متوقف و join می‌شود.
  * issueها به ترتیبِ منجمدِ batch و یکی‌یکی؛ FAIL/ERRORِ یک issue بقیه را
    متوقف نمی‌کند. هر کارِ issue (Jira، خطِ لوله، Newman، artifact) مالِ
    IssueExecutor است.
  * PAUSE: issueِ جاری کامل تمام می‌شود و پیش از issueِ بعدی batch → PAUSED
    (اگر کاری نمانده باشد COMPLETED). CANCEL: cancel_eventِ issueِ جاری،
    اجراهای QUEUED → CANCELLED و batch → CANCELLED. نتیجه‌ی واقعیِ issueی که
    پیش از اثرِ لغو تمام شده بود هرگز با CANCELLED جایگزین نمی‌شود.
  * اگر ذخیره‌کردن خودش شکست بخورد، runner می‌ایستد و گزارش می‌دهد.
  * Execution Gate ِ مشترک (src/execution_gate): هر نشست (start، resume، retry) در _owned،
    پس از بررسی‌های فقط‌خواندنیِ _refusal و پیش از leaseِ batch، Gate را با kind="BATCH"
    می‌گیرد و فقط پس از پایانِ کاملِ نشست و آزادسازیِ leaseِ batch آزاد می‌کند — Jira، LLM و
    Newman همه زیرِ آن‌اند (TestCase Box هرگز هم‌زمان اجرا نمی‌شود). Gate ِ مشغول یا
    نامطمئن → RESULT_REFUSED با refusal ِ gate_busy / gate_unavailable و هیچ کاری انجام
    نمی‌شود. پایش heartbeatِ Gate هم می‌زند؛ شکستش (gate_lost / gate_unavailable) بی‌مهلت
    نشست را متوقف و batch را INTERRUPTED (قابلِ resume) می‌کند. درست پیش از هر پروسه‌ی
    Newman حصارِ _before_newman مالکیت را assert می‌کند؛ رد → Newman شروع نمی‌شود. هیچ
    بازپس‌گیریِ مبتنی بر heartbeat نیست: مرگِ پروسه قفلِ سیستم‌عامل را آزاد می‌کند.
"""

from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from src.auth import AccessToken, AuthenticationError, TokenProvider
from src.batch_testing.executor import (
    ERROR_AUTHENTICATION,
    STAGE_AUTHENTICATION,
    ExecutionOutcome,
    ExecutionRequest,
    IssueExecutor,
)
from src.batch_testing.models import (
    ACTION_CANCEL,
    ACTION_PAUSE,
    BATCH_CANCELLED,
    BATCH_COMPLETED,
    BATCH_CREATED,
    BATCH_FINAL,
    BATCH_INTERRUPTED,
    BATCH_PAUSED,
    BATCH_RUNNING,
    EXEC_CANCELLED,
    EXEC_ERROR,
    EXEC_INTERRUPTED,
    EXEC_QUEUED,
    EXECUTION_ACTIVE,
    EXECUTION_FINAL,
    PHASE_FINALIZE,
    PHASE_QUEUED,
    TRIGGER_INITIAL,
    TRIGGER_RESUME,
    TRIGGER_RETRY,
    Batch,
    Execution,
)
from src.batch_testing.recovery import STALE_AFTER_S, cancel_queued, interrupt_running
from src.batch_testing.storage import (
    DEFAULT_DB_PATH,
    BatchStore,
    LeaseError,
    NotFoundError,
    OwnershipError,
    StorageError,
)
from src.execution_gate import (
    ExecutionGate,
    GateBusy,
    GateError,
    GateLease,
    GateLeaseLost,
    ReleaseOutcome,
)
from src.test_execution.newman import NewmanOptions

HEARTBEAT_INTERVAL_S = 10.0
POLL_INTERVAL_S = 1.0

# error_kindهایی که خودِ runner ثبت می‌کند
ERROR_RUNNER_EXCEPTION = "RUNNER_EXCEPTION"
ERROR_OUTCOME_REJECTED = "OUTCOME_REJECTED"
ERROR_RUNNER_STOPPED = "RUNNER_STOPPED"
# Environment/Globals با هشِ ثبت‌شده در batch نمی‌خواند — issue اجرا نشد
ERROR_CONFIGURATION_CHANGED = "CONFIGURATION_CHANGED"

# نتیجه‌ی یک بارِ اجرای runner
RESULT_COMPLETED = "completed"
RESULT_PAUSED = "paused"
RESULT_CANCELLED = "cancelled"
RESULT_RETRIED = "retried"
RESULT_REFUSED = "refused"  # هیچ چیزی تغییر نکرد (lease، وضعیتِ batch، issue)
RESULT_LEASE_LOST = "lease_lost"  # runner دیگر مالک نیست — هیچ نوشتنی پس از آن
RESULT_HEARTBEAT_FAILED = "heartbeat_failed"
RESULT_STORAGE_FAILED = "storage_failed"
RESULT_FAILED = "failed"  # استثنای پیش‌بینی‌نشده در خودِ runner
RESULT_CONFIGURATION_CHANGED = "configuration_changed"
RESULT_AUTHENTICATION_FAILED = "authentication_failed"
# Execution Gate ِ مشترک (Batch و TestCase Box هرگز هم‌زمان اجرا نمی‌شوند)
RESULT_GATE_LOST = "gate_lost"  # رکوردِ Gate دیگر مالِ این نشست نیست — نشست متوقف شد
RESULT_GATE_UNAVAILABLE = "gate_unavailable"  # مالکیتِ Gate قابلِ تأیید نبود — نشست متوقف شد
# دلیلِ ردِ پیش از هر کار (RunnerResult.refusal؛ وضعیت همان RESULT_REFUSED است)
REFUSAL_GATE_BUSY = "gate_busy"  # نشستِ دیگری Gate را دارد
REFUSAL_GATE_UNAVAILABLE = "gate_unavailable"  # Gate را نمی‌توان با اطمینان گرفت

LEASE_LOST_MESSAGE = "The runner lost its batch lease."
GATE_LOST_MESSAGE = (
    "The runner lost the shared execution gate; the session was stopped and can be resumed."
)
GATE_UNAVAILABLE_MESSAGE = (
    "The shared execution gate could not be confirmed; the session was stopped and can be resumed."
)
GATE_STOPPED_EXECUTION_MESSAGE = (
    "The runner stopped this execution because the shared execution gate could not be confirmed."
)

_STORAGE_ERRORS = (StorageError, sqlite3.Error)


class Executor(Protocol):
    def execute(self, request: ExecutionRequest) -> ExecutionOutcome: ...


class TokenSource(Protocol):
    def fetch(self) -> AccessToken: ...


ExecutorFactory = Callable[[Callable[[str], None]], Executor]
StoreFactory = Callable[[Path], BatchStore]


def _default_executor(on_phase: Callable[[str], None]) -> Executor:
    return IssueExecutor(on_phase=on_phase)


def _default_token_provider() -> TokenSource:
    return TokenProvider()


def _default_gate() -> ExecutionGate:
    """Gate ِ مشترکِ data/execution_gate/ — تست‌ها این را به پوشه‌ی موقت می‌برند."""
    return ExecutionGate()


@dataclass
class RunnerResult:
    batch_id: str
    status: str
    batch_status: str = ""
    # شناسه‌ی اجراهایی که همین runner شروع کرد، به ترتیب
    executions: list[str] = field(default_factory=list)
    # پیامِ ثابت؛ هرگز دادهٔ issue
    error: str = ""
    # دلیلِ ماشینیِ ردِ پیش از هر کار (REFUSAL_*)؛ خالی برای ردهای قبلی
    refusal: str = ""
    # خطاهای پاک‌سازی (آزادسازیِ lease و Gate) — جدا از error تا خطای اصلی پنهان نشود
    cleanup_errors: list[str] = field(default_factory=list)


class _Stop(Exception):  # noqa: N818 — کنترلِ جریانِ داخلی، نه خطای عمومی
    def __init__(self, status: str, error: str) -> None:
        super().__init__(error)
        self.status, self.error = status, error


class _Monitor(threading.Thread):
    """heartbeat + پایشِ CANCEL روی اتصالِ جدای خودش؛ هر خطا ثبت می‌شود و runner را
    متوقف می‌کند — هیچ استثنایی بی‌صدا گم نمی‌شود.

    با gate_lease، هر heartbeatِ موفقِ batch یک heartbeatِ Gate هم می‌زند. شکستِ heartbeatِ
    Gate خطای ایمنی است و بی‌مهلت: runner متوقف و issueِ جاری لغو می‌شود. قفلِ سیستم‌عامل
    همچنان دستِ همین نشست می‌ماند تا runner آن را در پاک‌سازی آزاد کند."""

    def __init__(
        self,
        open_store: Callable[[], BatchStore],
        batch_id: str,
        token: str,
        *,
        heartbeat_interval: float,
        poll_interval: float,
        failure_after: float,
        clock: Callable[[], float] = time.monotonic,
        gate_lease: GateLease | None = None,
    ) -> None:
        super().__init__(name=f"batch-runner-monitor-{batch_id[:8]}", daemon=True)
        self._open_store = open_store
        self._batch_id, self._token = batch_id, token
        self._heartbeat_interval = heartbeat_interval
        self._poll_interval = poll_interval
        self._failure_after = failure_after
        self._clock = clock
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._active: threading.Event | None = None
        self.cancel_requested = threading.Event()
        self.failure: tuple[str, str] | None = None  # (status, error)
        self.beats = 0
        self._gate_lease = gate_lease
        self.gate_beats = 0

    def set_active(self, event: threading.Event | None) -> None:
        with self._lock:
            self._active = event
            if event is not None and (self.cancel_requested.is_set() or self.failure):
                event.set()

    def _signal(self) -> None:
        with self._lock:
            if self._active is not None:
                self._active.set()

    def request_cancel(self) -> None:
        """لغوِ کاربر (درون‌پروسه‌ای) — مثلِ CANCELِ پایگاه."""
        self.cancel_requested.set()
        self._signal()

    def stop_active(self) -> None:
        """توقفِ issueِ جاری به دلیلِ خودِ runner (مثلاً از دست رفتنِ lease)."""
        self._signal()

    def _fail(self, status: str, error: str) -> None:
        self.failure = (status, error)
        self._signal()  # issueِ جاری بی‌lease ادامه پیدا نکند

    def halt(self) -> None:
        self._halt.set()
        self.join()

    def run(self) -> None:
        try:
            store = self._open_store()
        except Exception as exc:  # noqa: BLE001
            self._fail(RESULT_HEARTBEAT_FAILED, f"Heartbeat store failed ({type(exc).__name__}).")
            return
        try:
            last_ok = next_beat = self._clock()
            while True:
                moment = self._clock()
                if moment >= next_beat:
                    try:
                        store.heartbeat(self._batch_id, self._token)
                        self.beats += 1
                        last_ok, next_beat = moment, moment + self._heartbeat_interval
                    except LeaseError:
                        self._fail(RESULT_LEASE_LOST, LEASE_LOST_MESSAGE)
                        return
                    except Exception as exc:  # noqa: BLE001
                        if moment - last_ok >= self._failure_after:
                            self._fail(
                                RESULT_HEARTBEAT_FAILED,
                                f"Heartbeat kept failing ({type(exc).__name__}).",
                            )
                            return
                        next_beat = moment + self._poll_interval
                    else:
                        if not self._beat_gate():
                            return
                try:
                    if store.get_batch(self._batch_id).requested_action == ACTION_CANCEL:
                        self.cancel_requested.set()
                        self._signal()
                except Exception:  # noqa: BLE001, S110 — سلامتِ پایگاه را heartbeat می‌سنجد
                    pass
                if self._halt.wait(self._poll_interval):
                    return
        finally:
            store.close()

    def _beat_gate(self) -> bool:
        """False یعنی Gate دیگر قابلِ تأیید نیست و runner متوقف شد."""
        if self._gate_lease is None:
            return True
        try:
            self._gate_lease.heartbeat()
        except GateLeaseLost:
            self._fail(RESULT_GATE_LOST, GATE_LOST_MESSAGE)
            return False
        except Exception:  # noqa: BLE001 — GateUnavailable یا هر خطای دیگر: fail-closed
            self._fail(RESULT_GATE_UNAVAILABLE, GATE_UNAVAILABLE_MESSAGE)
            return False
        self.gate_beats += 1
        return True


def file_sha256(path: Path | str) -> str | None:
    """SHA-256ِ بایت‌های خامِ فایل (همان قراردادِ environment_sha256 / globals_sha256)؛
    None یعنی فایل نیست یا خواندنی نیست. محتوا هرگز جایی نوشته نمی‌شود."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def configuration_problem(batch: Batch) -> str:
    """پیامِ ثابت اگر Environment یا Globals با هشِ ثبت‌شده در batch نمی‌خواند."""
    for label, path, expected in (
        ("environment", batch.environment_path, batch.environment_sha256),
        ("globals", batch.globals_path, batch.globals_sha256),
    ):
        actual = file_sha256(path)
        if actual is None:
            return f"The {label} file of the batch is missing or unreadable."
        if actual != expected:
            return f"The {label} file no longer matches the hash recorded for the batch."
    return ""


class BatchRunner:
    """یک نمونه = یک توکنِ مالکیت؛ هر بار فقط یک batch."""

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        *,
        executor_factory: ExecutorFactory = _default_executor,
        store_factory: StoreFactory = BatchStore.open,
        token_provider: TokenSource | None = None,
        newman_options: NewmanOptions | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
        poll_interval: float = POLL_INTERVAL_S,
        stale_after: float = STALE_AFTER_S,
        gate_factory: Callable[[], ExecutionGate] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.token = uuid.uuid4().hex
        # None = _default_gate در لحظه‌ی هر نشست (تست‌ها آن را به پوشه‌ی موقت می‌برند)
        self._gate_factory = gate_factory
        self._gate_lease: GateLease | None = None
        self._gate_failure: tuple[str, str] | None = None
        self._executor_factory = executor_factory
        self._store_factory = store_factory
        self._token_provider = token_provider or _default_token_provider()
        # توکنِ نشستِ جاری — در آغاز و پایانِ هر نشست پاک می‌شود
        self._access_token: AccessToken | None = None
        self._newman_options = newman_options or NewmanOptions()
        self._heartbeat_interval = heartbeat_interval
        self._poll_interval = poll_interval
        # heartbeatی که نیمی از آستانه‌ی کهنگی شکست بخورد یعنی توقف — پیش از آنکه
        # بازیابی بتواند lease را بگیرد
        self._failure_after = stale_after / 2
        self._busy = threading.Lock()
        self._monitor: _Monitor | None = None
        self._phase_failure: tuple[str, str] | None = None
        self._cancel_requested = threading.Event()
        self._batch_id = ""

    # ── ورودی‌ها ────────────────────────────────────────────────────────────
    def start(self, batch_id: str) -> RunnerResult:
        return self._session(batch_id, "start")

    def resume(self, batch_id: str) -> RunnerResult:
        return self._session(batch_id, "resume")

    def retry(self, batch_id: str, issue_key: str) -> RunnerResult:
        return self._session(batch_id, "retry", issue_key)

    def request_cancel(self) -> bool:
        """لغوِ درون‌پروسه‌ای (از threadِ دیگر) — همان مسیرِ CANCELِ پایگاه.

        برای retry لازم است: batchِ COMPLETED/CANCELLED گذاری ندارد و request_action
        فقط برای batchِ RUNNING پذیرفته می‌شود. False یعنی runner کاری در دست ندارد.
        """
        monitor = self._monitor
        if monitor is None:
            return False
        self._cancel_requested.set()
        monitor.request_cancel()
        return True

    # ── نشست: lease، پایش، و آزادسازی در هر مسیری ─────────────────────────
    def _session(self, batch_id: str, mode: str, issue_key: str = "") -> RunnerResult:
        if not self._busy.acquire(blocking=False):
            return RunnerResult(batch_id, RESULT_REFUSED, error="This runner is already busy.")
        try:
            store = self._store_factory(self.db_path)
            try:
                return self._owned(store, batch_id, mode, issue_key)
            finally:
                store.close()
        finally:
            self._busy.release()

    def _owned(self, store: BatchStore, batch_id: str, mode: str, issue_key: str) -> RunnerResult:
        try:
            batch = store.get_batch(batch_id)
        except NotFoundError:
            return RunnerResult(batch_id, RESULT_REFUSED, error="The batch does not exist.")
        refusal = self._refusal(store, batch, mode, issue_key)
        if refusal:
            return RunnerResult(batch_id, RESULT_REFUSED, batch.status, error=refusal)
        # Gate ِ مشترک پیش از هر کارِ نشست (lease، توکن، Jira، LLM، Newman) و تا پایانِ آن
        gate = self._gate_factory or _default_gate
        try:
            lease = gate().acquire("BATCH", batch_id)
        except GateBusy as exc:
            return RunnerResult(
                batch_id, RESULT_REFUSED, batch.status, error=str(exc), refusal=REFUSAL_GATE_BUSY
            )
        except GateError as exc:
            return RunnerResult(
                batch_id,
                RESULT_REFUSED,
                batch.status,
                error=str(exc),
                refusal=REFUSAL_GATE_UNAVAILABLE,
            )
        self._gate_lease, self._gate_failure = lease, None
        result: RunnerResult | None = None
        try:
            result = self._leased(store, batch, mode, issue_key)
            return result
        finally:
            # فقط پس از پایانِ واقعیِ نشست و آزادسازیِ leaseِ batch (درونِ _leased)
            self._gate_lease = None
            problem = self._release_gate(lease)
            if problem and result is not None:
                result.cleanup_errors.append(problem)

    @staticmethod
    def _release_gate(lease: GateLease) -> str:
        """آزادسازی توسطِ همان مالک؛ هرگز استثنایی بیرون نمی‌دهد که خطای اصلی را بپوشاند."""
        try:
            outcome = lease.release()
        except Exception as exc:  # noqa: BLE001 — قفلِ سیستم‌عامل در release آزاد شده است
            return f"The shared execution gate release failed ({type(exc).__name__})."
        if outcome is ReleaseOutcome.RECORD_NOT_CLEARED:
            return (
                "The shared execution gate was released, but its owner record could not be "
                "cleared; the next session reconciles it."
            )
        return ""

    def _leased(self, store: BatchStore, batch: Batch, mode: str, issue_key: str) -> RunnerResult:
        batch_id = batch.id
        try:
            store.acquire_lease(batch_id, self.token, pid=os.getpid(), host=socket.gethostname())
        except LeaseError:
            return RunnerResult(
                batch_id, RESULT_REFUSED, batch.status, error="Another runner owns the lease."
            )
        self._batch_id, self._phase_failure = batch_id, None
        self._access_token = None  # هر نشست توکنِ تازه می‌گیرد
        self._cancel_requested.clear()
        result = RunnerResult(batch_id, "")
        monitor = _Monitor(
            lambda: self._store_factory(self.db_path),
            batch_id,
            self.token,
            heartbeat_interval=self._heartbeat_interval,
            poll_interval=self._poll_interval,
            failure_after=self._failure_after,
            gate_lease=self._gate_lease,
        )
        self._monitor = monitor
        monitor.start()
        owned = True
        try:
            if mode == "retry":
                self._retry(store, batch_id, issue_key, result)
            else:
                self._prepare(store, batch_id, mode)
                self._loop(store, batch_id, result)
        except _Stop as stop:
            result.status, result.error = stop.status, stop.error
            if stop.status == RESULT_LEASE_LOST:
                owned = False
            else:
                self._interrupt_batch(store, batch_id)
        except Exception as exc:  # noqa: BLE001 — batch قابلِ resume می‌ماند، نه نیمه‌کاره
            result.status = RESULT_FAILED
            result.error = f"The runner failed unexpectedly ({type(exc).__name__})."
            self._interrupt_batch(store, batch_id)
        finally:
            self._monitor = None
            self._access_token = None
            monitor.halt()
            if monitor.failure and monitor.failure[0] == RESULT_LEASE_LOST:
                owned = False
            if owned:
                self._release(store, batch_id, result)
        try:
            result.batch_status = store.get_batch(batch_id).status
        except _STORAGE_ERRORS:
            pass
        return result

    @staticmethod
    def _refusal(store: BatchStore, batch: Batch, mode: str, issue_key: str) -> str:
        if mode == "start" and batch.status != BATCH_CREATED:
            return f"Only a CREATED batch can be started, not {batch.status}."
        if mode == "resume" and batch.status not in (BATCH_PAUSED, BATCH_INTERRUPTED):
            return f"Only a PAUSED or INTERRUPTED batch can be resumed, not {batch.status}."
        if mode == "retry":
            if batch.status not in BATCH_FINAL:
                return f"Issues can be retried only in a finished batch, not {batch.status}."
            if issue_key not in {issue.issue_key for issue in store.batch_issues(batch.id)}:
                return "The issue is not part of this batch."
            latest = store.latest_executions(batch.id).get(issue_key)
            if latest is not None and latest.status in EXECUTION_ACTIVE:
                return "The issue already has an active execution."
        return ""

    def _release(self, store: BatchStore, batch_id: str, result: RunnerResult) -> None:
        try:
            store.release_lease(batch_id, self.token)
        except LeaseError:
            result.status = result.status or RESULT_LEASE_LOST
        except _STORAGE_ERRORS as exc:
            # lease پس از آستانه‌ی کهنگی با بازیابی پس گرفته می‌شود
            problem = f"The lease could not be released ({type(exc).__name__})."
            result.error = result.error or problem
            result.cleanup_errors.append(problem)  # خطای اصلی (اگر بود) در error می‌ماند

    def _interrupt_batch(self, store: BatchStore, batch_id: str) -> None:
        """توقفِ فنی: batch قابلِ resume می‌ماند — بهترین تلاش، فقط با leaseِ خودمان."""
        try:
            if store.get_batch(batch_id).status == BATCH_RUNNING:
                store.transition_batch(
                    batch_id, BATCH_INTERRUPTED, expected=BATCH_RUNNING, owner_token=self.token
                )
        except (*_STORAGE_ERRORS, ValueError):
            pass  # خطای اصلی در result هست؛ بازیابی بعداً batch را می‌بندد

    # ── مالکیت ──────────────────────────────────────────────────────────────
    def _check_health(self) -> None:
        """شکستِ heartbeat یا ذخیره‌ی phase → توقف (بدونِ خواندنِ پایگاه)."""
        monitor = self._monitor
        if monitor is not None and monitor.failure:
            raise _Stop(*monitor.failure)
        if self._gate_failure:
            raise _Stop(*self._gate_failure)
        if self._phase_failure:
            raise _Stop(*self._phase_failure)

    def _before_newman(self) -> None:
        """حصارِ درست پیش از شروعِ پروسه‌ی Newman (از درونِ IssueExecutor): پایش سالم است و
        Gate هنوز مالِ همین نشست است. در غیرِ این صورت issueِ جاری متوقف می‌شود (cancel_event
        ست می‌شود → INTERRUPTED و قابلِ resume)، نشست پس از آن می‌ایستد و استثنا Newman را
        پیش از شروع باز می‌دارد."""
        monitor, lease = self._monitor, self._gate_lease
        failure = monitor.failure if monitor is not None else None
        if failure is None and lease is None:
            failure = (RESULT_GATE_UNAVAILABLE, GATE_UNAVAILABLE_MESSAGE)
        if failure is None:
            try:
                lease.assert_held()
            except GateLeaseLost:
                failure = (RESULT_GATE_LOST, GATE_LOST_MESSAGE)
            except Exception:  # noqa: BLE001 — GateUnavailable یا هر خطای دیگر: fail-closed
                failure = (RESULT_GATE_UNAVAILABLE, GATE_UNAVAILABLE_MESSAGE)
        if failure is None:
            return
        if monitor is None or monitor.failure is None:
            self._gate_failure = self._gate_failure or failure
        if monitor is not None:
            monitor.stop_active()
        raise RuntimeError("The shared execution gate does not permit starting Newman.")

    def _read_batch(self, store: BatchStore) -> Batch:
        """batch برای تصمیم‌گیری (PAUSE/CANCEL). مالکیتِ دیده‌شده این‌جا فقط خروجِ زودهنگام
        است؛ ضمانت را خودِ نوشتن‌ها می‌دهند که توکن را در همان تراکنش بررسی می‌کنند."""
        self._check_health()
        try:
            batch = store.get_batch(self._batch_id)
        except _STORAGE_ERRORS as exc:
            raise _Stop(
                RESULT_STORAGE_FAILED, f"The batch could not be read ({type(exc).__name__})."
            ) from exc
        if batch.owner_token != self.token:
            raise _Stop(RESULT_LEASE_LOST, LEASE_LOST_MESSAGE)
        return batch

    def _write(self, store: BatchStore, action: Callable[[], Any], what: str) -> Any:
        """یک نوشتنِ token-guarded (action باید owner_token=self.token بدهد)."""
        self._check_health()
        try:
            return action()
        except OwnershipError as exc:
            raise _Stop(RESULT_LEASE_LOST, LEASE_LOST_MESSAGE) from exc
        except _STORAGE_ERRORS as exc:
            raise _Stop(
                RESULT_STORAGE_FAILED, f"Persisting {what} failed ({type(exc).__name__})."
            ) from exc

    def _check_configuration(self, batch: Batch) -> None:
        problem = configuration_problem(batch)
        if problem:
            raise _Stop(RESULT_CONFIGURATION_CHANGED, problem)

    # ── آماده‌سازی و حلقه ───────────────────────────────────────────────────
    def _prepare(self, store: BatchStore, batch_id: str, mode: str) -> None:
        batch = self._read_batch(store)
        token = self.token
        if mode == "resume":
            self._check_configuration(batch)  # پیش از هر تغییری
            # با leaseِ ما هیچ اجرای RUNNINGی در این batch مالِ runnerِ زنده نیست
            self._write(
                store,
                lambda: interrupt_running(store, batch_id, owner_token=token),
                "interrupted runs",
            )
        self._write(
            store,
            lambda: store.transition_batch(
                batch_id, BATCH_RUNNING, expected=batch.status, owner_token=token
            ),
            "the batch status",
        )
        latest = store.latest_executions(batch_id)
        for issue in store.batch_issues(batch_id):
            if issue.issue_key not in latest:
                self._write(
                    store,
                    lambda key=issue.issue_key: store.create_execution(
                        batch_id,
                        key,
                        trigger=TRIGGER_INITIAL,
                        runner_token=token,
                        owner_token=token,
                    ),
                    "the queue",
                )

    def _cancel_wanted(self, batch: Batch) -> bool:
        return batch.requested_action == ACTION_CANCEL or self._cancel_requested.is_set()

    def _loop(self, store: BatchStore, batch_id: str, result: RunnerResult) -> None:
        token = self.token
        for issue in store.batch_issues(batch_id):
            batch = self._read_batch(store)
            if self._cancel_wanted(batch):
                self._cancel(store, batch_id, result)
                return
            latest = store.latest_executions(batch_id).get(issue.issue_key)
            if latest is not None and latest.status in EXECUTION_FINAL and not _rerun(latest):
                continue  # نتیجه‌ی پایانی دارد — دوباره اجرا نمی‌شود
            if batch.requested_action == ACTION_PAUSE:
                self._write(
                    store,
                    lambda: store.transition_batch(
                        batch_id, BATCH_PAUSED, expected=BATCH_RUNNING, owner_token=token
                    ),
                    "the pause",
                )
                result.status = RESULT_PAUSED
                return
            execution = self._next_execution(store, batch_id, issue.issue_key, latest)
            problem = configuration_problem(batch)
            if problem:
                self._block(store, execution, problem)
            if self._access_token is None:
                self._authenticate(store, execution)
            self._run_one(store, batch, execution, result)
        batch = self._read_batch(store)
        if self._cancel_wanted(batch):
            self._cancel(store, batch_id, result)
            return
        self._write(
            store,
            lambda: store.transition_batch(
                batch_id, BATCH_COMPLETED, expected=BATCH_RUNNING, owner_token=token
            ),
            "the batch completion",
        )
        result.status = RESULT_COMPLETED

    def _authenticate(self, store: BatchStore, execution: Execution | None) -> None:
        """یک توکنِ تازه برای این نشست، پیش از هر کارِ issue. شکست → issue اجرا نمی‌شود
        (اگر execution داده شده باشد ERROR ثبت می‌شود) و نشست می‌ایستد."""
        try:
            self._access_token = self._token_provider.fetch()
        except AuthenticationError as exc:
            # پیامِ AuthenticationError ثابت و بی‌راز است
            if execution is None:
                raise _Stop(RESULT_AUTHENTICATION_FAILED, str(exc)) from None
            self._block(
                store,
                execution,
                str(exc),
                kind=ERROR_AUTHENTICATION,
                stage=STAGE_AUTHENTICATION,
                status=RESULT_AUTHENTICATION_FAILED,
            )

    def _block(
        self,
        store: BatchStore,
        execution: Execution,
        problem: str,
        *,
        kind: str = ERROR_CONFIGURATION_CHANGED,
        stage: str = PHASE_QUEUED,
        status: str = RESULT_CONFIGURATION_CHANGED,
    ) -> None:
        """Environment/Globals تغییر کرده یا توکن گرفته نشد: issue اجرا نمی‌شود و خطای
        فنیِ مشخص ثبت می‌شود؛ resume (پس از رفعِ مشکل) برای همین issue تلاشِ تازه می‌سازد."""
        token = self.token
        self._write(
            store,
            lambda: store.start_execution(execution.id, owner_token=token),
            "the blocked execution",
        )
        self._write(
            store,
            lambda: store.finish_execution(
                execution.id,
                EXEC_ERROR,
                owner_token=token,
                phase=PHASE_QUEUED,
                error_stage=stage,
                error_kind=kind,
                error_message=f"{problem} The issue was not executed.",
            ),
            "the blocked execution",
        )
        raise _Stop(status, problem)

    def _next_execution(
        self, store: BatchStore, batch_id: str, issue_key: str, latest: Execution | None
    ) -> Execution:
        if latest is not None and latest.status == EXEC_QUEUED:
            return latest
        trigger = TRIGGER_RESUME if latest is not None else TRIGGER_INITIAL
        token = self.token
        return self._write(
            store,
            lambda: store.create_execution(
                batch_id, issue_key, trigger=trigger, runner_token=token, owner_token=token
            ),
            "a new attempt",
        )

    def _cancel(self, store: BatchStore, batch_id: str, result: RunnerResult) -> None:
        token = self.token
        self._write(
            store, lambda: cancel_queued(store, batch_id, owner_token=token), "the cancelled queue"
        )
        self._write(
            store,
            lambda: store.transition_batch(
                batch_id, BATCH_CANCELLED, expected=BATCH_RUNNING, owner_token=token
            ),
            "the cancellation",
        )
        result.status = RESULT_CANCELLED

    def _retry(
        self, store: BatchStore, batch_id: str, issue_key: str, result: RunnerResult
    ) -> None:
        batch = self._read_batch(store)
        self._check_configuration(batch)  # پیش از ساختنِ تلاشِ تازه — هیچ رکوردی ساخته نمی‌شود
        self._authenticate(store, None)  # همین‌طور: شکست → هیچ رکوردی
        token = self.token
        execution = self._write(
            store,
            lambda: store.create_execution(
                batch_id, issue_key, trigger=TRIGGER_RETRY, runner_token=token, owner_token=token
            ),
            "the retry attempt",
        )
        self._run_one(store, batch, execution, result)
        result.status = RESULT_RETRIED

    # ── یک issue ────────────────────────────────────────────────────────────
    def _run_one(
        self, store: BatchStore, batch: Batch, execution: Execution, result: RunnerResult
    ) -> None:
        token = self.token
        self._write(
            store,
            lambda: store.start_execution(execution.id, owner_token=token),
            "the execution start",
        )
        result.executions.append(execution.id)
        cancel_event = threading.Event()
        monitor = self._monitor
        assert monitor is not None
        monitor.set_active(cancel_event)
        request = ExecutionRequest(
            batch_id=batch.id,
            issue_key=execution.issue_key,
            execution_id=execution.id,
            attempt=execution.attempt,
            artifact_root=Path(batch.artifact_root),
            environment_path=Path(batch.environment_path),
            globals_path=Path(batch.globals_path),
            newman_options=self._newman_options,
            cancel_event=cancel_event,
            access_token=self._access_token,
            before_newman=self._before_newman,
        )
        outcome: ExecutionOutcome | None = None
        failure = ""
        try:
            executor = self._executor_factory(
                lambda phase: self._record_phase(store, execution.id, phase)
            )
            outcome = executor.execute(request)
        except Exception as exc:  # noqa: BLE001 — هیچ استثنایی نتیجه‌ی ساختگی نمی‌سازد
            failure = type(exc).__name__
        finally:
            monitor.set_active(None)
        # لغوی که خودِ runner (نه کاربر) داده: issue قطع شده، نه لغوشده
        runner_stopped = cancel_event.is_set() and not monitor.cancel_requested.is_set()
        self._persist(store, execution.id, outcome, failure, runner_stopped)
        self._check_health()  # شکستِ heartbeat یا ذخیره‌ی phase → توقف پیش از issueِ بعدی

    def _record_phase(self, store: BatchStore, execution_id: str, phase: str) -> None:
        """پیشرفت از درونِ IssueExecutor — خطا خودِ اجرا را خراب نمی‌کند ولی runner را
        پس از این issue متوقف می‌کند."""
        try:
            store.update_execution(execution_id, owner_token=self.token, phase=phase)
        except OwnershipError:
            self._phase_failure = self._phase_failure or (RESULT_LEASE_LOST, LEASE_LOST_MESSAGE)
            monitor = self._monitor
            if monitor is not None:
                monitor.stop_active()
        except (*_STORAGE_ERRORS, ValueError) as exc:
            self._phase_failure = self._phase_failure or (
                RESULT_STORAGE_FAILED,
                f"Persisting the execution phase failed ({type(exc).__name__}).",
            )

    def _persist(
        self,
        store: BatchStore,
        execution_id: str,
        outcome: ExecutionOutcome | None,
        failure: str,
        runner_stopped: bool,
    ) -> None:
        def finish(status: str, **fields: Any) -> None:
            try:
                store.finish_execution(execution_id, status, owner_token=self.token, **fields)
            except OwnershipError as exc:  # بی‌lease هیچ چیزی نوشته نشد
                raise _Stop(RESULT_LEASE_LOST, LEASE_LOST_MESSAGE) from exc
            except _STORAGE_ERRORS as exc:
                raise _Stop(
                    RESULT_STORAGE_FAILED,
                    f"Persisting the execution result failed ({type(exc).__name__}).",
                ) from exc

        current = store.get_execution(execution_id)
        if outcome is None:
            finish(
                EXEC_ERROR,
                phase=current.phase,
                error_stage=current.phase,
                error_kind=ERROR_RUNNER_EXCEPTION,
                error_message=f"The issue executor raised {failure}.",
            )
            return
        status, fields = outcome.status, outcome.storage_fields()
        if runner_stopped and status == EXEC_CANCELLED:
            # resume باید این issue را دوباره اجرا کند
            status = EXEC_INTERRUPTED
            gate_stopped = bool(self._gate_failure) or (
                self._monitor is not None
                and self._monitor.failure is not None
                and self._monitor.failure[0] in (RESULT_GATE_LOST, RESULT_GATE_UNAVAILABLE)
            )
            fields.update(
                error_stage=outcome.phase,
                error_kind=ERROR_RUNNER_STOPPED,
                error_message=GATE_STOPPED_EXECUTION_MESSAGE
                if gate_stopped
                else "The runner stopped this execution.",
            )
        try:
            finish(status, **fields)
        except ValueError:  # نتیجه با قواعدِ BatchStore نمی‌خواند — خطای فنی، نه حدس
            finish(
                EXEC_ERROR,
                phase=PHASE_FINALIZE,
                error_stage=PHASE_FINALIZE,
                error_kind=ERROR_OUTCOME_REJECTED,
                error_message="The issue executor returned an outcome the store rejected.",
                artifact_dir=outcome.artifact_dir,
            )


def _rerun(latest: Execution) -> bool:
    """قطع‌شده، یا به‌خاطرِ تغییرِ Environment/Globals یا نبودِ توکن اصلاً اجرا نشده —
    resume دوباره."""
    return latest.status == EXEC_INTERRUPTED or (
        latest.status == EXEC_ERROR
        and latest.error_kind in (ERROR_CONFIGURATION_CHANGED, ERROR_AUTHENTICATION)
    )


__all__ = [
    "ERROR_CONFIGURATION_CHANGED",
    "ERROR_OUTCOME_REJECTED",
    "ERROR_RUNNER_EXCEPTION",
    "ERROR_RUNNER_STOPPED",
    "HEARTBEAT_INTERVAL_S",
    "POLL_INTERVAL_S",
    "RESULT_CANCELLED",
    "RESULT_COMPLETED",
    "RESULT_AUTHENTICATION_FAILED",
    "RESULT_CONFIGURATION_CHANGED",
    "GATE_LOST_MESSAGE",
    "GATE_STOPPED_EXECUTION_MESSAGE",
    "GATE_UNAVAILABLE_MESSAGE",
    "REFUSAL_GATE_BUSY",
    "REFUSAL_GATE_UNAVAILABLE",
    "RESULT_FAILED",
    "RESULT_GATE_LOST",
    "RESULT_GATE_UNAVAILABLE",
    "RESULT_HEARTBEAT_FAILED",
    "RESULT_LEASE_LOST",
    "RESULT_PAUSED",
    "RESULT_REFUSED",
    "RESULT_RETRIED",
    "RESULT_STORAGE_FAILED",
    "BatchRunner",
    "Executor",
    "ExecutorFactory",
    "RunnerResult",
    "configuration_problem",
    "file_sha256",
]
