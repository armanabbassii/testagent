"""
تست‌های runner و بازیابیِ Batch (Milestone 4.5).

همه روی پایگاهِ SQLiteِ موقت و با IssueExecutorِ ساختگی؛ هیچ Jira، LLM یا Newmanِ
واقعی‌ای صدا زده نمی‌شود. هم‌زمانی با Eventها کنترل می‌شود، نه با sleepهای دلبخواه.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.auth import AccessToken
from src.batch_testing import models as m
from src.batch_testing import runner as runner_module
from src.batch_testing.executor import ExecutionOutcome, ExecutionRequest
from src.batch_testing.recovery import ERROR_RUNNER_LOST, recover_stale
from src.batch_testing.runner import (
    ERROR_OUTCOME_REJECTED,
    ERROR_RUNNER_EXCEPTION,
    ERROR_RUNNER_STOPPED,
    BatchRunner,
    RunnerResult,
    file_sha256,
)
from src.batch_testing.storage import BatchStore, LeaseError
from tests.unit.batch_testing.test_storage import _new_batch

KEYS = ("DEMO-3", "DEMO-1", "DEMO-2")  # ترتیبِ منجمد عمداً الفبایی نیست
WAIT_S = 10.0


# ── ابزارها ──────────────────────────────────────────────────────────────────


def _wait(predicate: Callable[[], bool], timeout: float = WAIT_S) -> None:
    """منتظرِ یک شرطِ مشخص (نه یک مدتِ دلبخواه)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("The condition was not reached in time.")
        time.sleep(0.005)


def _future(seconds: float = 120.0) -> Callable[[], datetime]:
    return lambda: datetime.now(UTC) + timedelta(seconds=seconds)


@dataclass
class Script:
    """رفتارِ IssueExecutorِ ساختگی برای هر issue."""

    outcomes: dict[str, list[Any]] = field(default_factory=dict)
    gates: dict[str, threading.Event] = field(default_factory=dict)
    phases: dict[str, list[str]] = field(default_factory=dict)
    honour_cancel: bool = True
    hold_after_cancel: threading.Event | None = None
    started: dict[str, threading.Event] = field(
        default_factory=lambda: defaultdict(threading.Event)
    )
    saw_cancel: threading.Event = field(default_factory=threading.Event)
    calls: list[tuple[str, int, str]] = field(default_factory=list)
    requests: list[ExecutionRequest] = field(default_factory=list)
    active: int = 0
    max_active: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def next(self, key: str) -> Any:
        queue = self.outcomes.get(key)
        return queue.pop(0) if queue else m.EXEC_PASS


def _outcome(status: str, directory: Path, **overrides: Any) -> ExecutionOutcome:
    values: dict[str, Any] = {"status": status, "artifact_dir": str(directory)}
    if status in (m.EXEC_PASS, m.EXEC_FAIL):
        values.update(newman_run_id="run-1", newman_status=status, requests_executed=2)
    if status == m.EXEC_ERROR:
        values.update(error_stage="newman", error_kind="NEWMAN_ERROR", error_message="Newman.")
    if status == m.EXEC_NOT_EXECUTED:
        values.update(phase=m.PHASE_STEP3, review_status=m.REVIEW_NEEDS_REVIEW)
    values.update(overrides)
    return ExecutionOutcome(**values)


class FakeExecutor:
    def __init__(self, script: Script, on_phase: Callable[[str], None]) -> None:
        self.script, self.on_phase = script, on_phase

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        script, key = self.script, request.issue_key
        with script.lock:
            script.active += 1
            script.max_active = max(script.max_active, script.active)
            script.calls.append((key, request.attempt, request.execution_id))
            script.requests.append(request)
        try:
            directory = (
                Path(request.artifact_root)
                / request.batch_id
                / key
                / f"execution_{request.attempt}"
            )
            directory.mkdir(parents=True, exist_ok=False)  # تلاشِ قبلی هرگز بازنویسی نمی‌شود
            (directory / "marker.txt").write_text(request.execution_id, encoding="utf-8")
            phase = m.PHASE_STEP1
            for phase in script.phases.get(key, []):
                self.on_phase(phase)
            script.started[key].set()
            gate = script.gates.get(key)
            deadline = time.monotonic() + WAIT_S
            while gate is not None and not gate.wait(0.005):
                if script.honour_cancel and request.cancel_event.is_set():
                    script.saw_cancel.set()
                    if script.hold_after_cancel is not None:
                        script.hold_after_cancel.wait(WAIT_S)
                    return _outcome(
                        m.EXEC_CANCELLED,
                        directory,
                        phase=phase,
                        error_stage=phase,
                        error_message="The execution was cancelled.",
                    )
                if time.monotonic() > deadline:
                    raise TimeoutError("test gate was never released")
            behaviour = script.next(key)
            if callable(behaviour):
                return behaviour(request, directory)
            return _outcome(behaviour, directory)
        finally:
            with script.lock:
                script.active -= 1


class FakeTokenSource:
    """منبعِ توکنِ ساختگی — هیچ شبکه‌ای. failure: AuthenticationErrorی که fetch بالا می‌اندازد."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = 0

    def fetch(self) -> AccessToken:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return AccessToken(f"synthetic-access-token-{self.calls}")


class FlakyStore(BatchStore):
    """BatchStoreِ واقعی با خرابیِ قابلِ تزریق."""

    fail_finish = 0
    fail_heartbeat = False
    # نامِ متد → تابعی که درست پیش از آن متد (پس از هر بررسیِ runner) اجرا می‌شود
    before: dict[str, Callable[[], None]] = {}
    stale_snapshot: list | None = None  # stale_leasesِ «دیده‌شده» با اطلاعاتِ قدیمی

    def _hook(self, name: str) -> None:
        hook = FlakyStore.before.pop(name, None)
        if hook is not None:
            hook()

    def transition_batch(self, batch_id, target, **kwargs):
        self._hook("transition_batch")
        return super().transition_batch(batch_id, target, **kwargs)

    def stale_leases(self, older_than):
        if FlakyStore.stale_snapshot is not None:
            return FlakyStore.stale_snapshot
        return super().stale_leases(older_than)

    def finish_execution(self, execution_id, status, **fields):
        self._hook("finish_execution")
        if FlakyStore.fail_finish > 0:
            FlakyStore.fail_finish -= 1
            raise sqlite3.OperationalError("disk I/O error")
        return super().finish_execution(execution_id, status, **fields)

    def heartbeat(self, batch_id, token):
        if FlakyStore.fail_heartbeat:
            raise sqlite3.OperationalError("disk I/O error")
        return super().heartbeat(batch_id, token)


@pytest.fixture(autouse=True)
def _no_real_executor_and_no_leaked_threads(monkeypatch):
    def forbidden(on_phase):
        raise AssertionError("The real IssueExecutor must not be used in unit tests.")

    def forbidden_tokens():
        raise AssertionError("The real TokenProvider must not be used in unit tests.")

    monkeypatch.setattr(runner_module, "_default_executor", forbidden)
    monkeypatch.setattr(runner_module, "_default_token_provider", forbidden_tokens)
    monkeypatch.setattr(FlakyStore, "fail_finish", 0)
    monkeypatch.setattr(FlakyStore, "fail_heartbeat", False)
    monkeypatch.setattr(FlakyStore, "before", {})
    monkeypatch.setattr(FlakyStore, "stale_snapshot", None)
    yield
    alive = [t.name for t in threading.enumerate() if t.name.startswith("batch-runner-monitor")]
    assert alive == [], alive


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "batch.sqlite3"


@pytest.fixture
def store(db: Path):
    with BatchStore.open(db) as opened:
        yield opened


def _batch(store: BatchStore, tmp_path: Path, keys=KEYS) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    environment = tmp_path / "env.json"
    globals_path = tmp_path / "globals.json"
    for path in (environment, globals_path):
        path.write_text("{}", encoding="utf-8")
    new = _new_batch(
        keys,
        artifact_root=str(tmp_path / "artifacts"),
        environment_path=str(environment),
        globals_path=str(globals_path),
        environment_sha256=file_sha256(environment),
        globals_sha256=file_sha256(globals_path),
    )
    return store.create_batch(new).id


def _runner(db: Path, script: Script, **kwargs: Any) -> BatchRunner:
    kwargs.setdefault("heartbeat_interval", 0.02)
    kwargs.setdefault("poll_interval", 0.005)
    kwargs.setdefault("token_provider", FakeTokenSource())
    return BatchRunner(
        db, executor_factory=lambda on_phase: FakeExecutor(script, on_phase), **kwargs
    )


class _Background:
    """runner در threadِ جدا، برای کنترل از تست."""

    def __init__(self, call: Callable[[], RunnerResult]) -> None:
        self.result: RunnerResult | None = None
        self.thread = threading.Thread(target=self._run, args=(call,), daemon=True)
        self.thread.start()

    def _run(self, call: Callable[[], RunnerResult]) -> None:
        self.result = call()

    def join(self) -> RunnerResult:
        self.thread.join(WAIT_S)
        assert not self.thread.is_alive()
        assert self.result is not None
        return self.result


def _statuses(store: BatchStore, batch_id: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = defaultdict(list)
    for execution in store.executions(batch_id):
        found[execution.issue_key].append(execution.status)
    return dict(found)


def _dead_runner(store: BatchStore, batch_id: str, *, token: str = "dead") -> None:
    """runnerی که lease گرفت، batch را RUNNING کرد و بعد بی‌صدا از بین رفت."""
    store.acquire_lease(batch_id, token, pid=1, host="gone")
    store.transition_batch(batch_id, m.BATCH_RUNNING, expected=m.BATCH_CREATED)
    for key in KEYS:
        store.create_execution(batch_id, key, trigger=m.TRIGGER_INITIAL, runner_token=token)


# ── اجرای ترتیبی ─────────────────────────────────────────────────────────────


class TestSequentialExecution:
    def test_issues_run_in_frozen_order_one_at_a_time(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script = Script()
        result = _runner(db, script).start(batch_id)
        assert (result.status, result.batch_status) == ("completed", m.BATCH_COMPLETED)
        assert [call[0] for call in script.calls] == list(KEYS)
        assert script.max_active == 1
        assert _statuses(store, batch_id) == {key: [m.EXEC_PASS] for key in KEYS}
        batch = store.get_batch(batch_id)
        assert batch.owner_token is None and batch.finished_at
        assert result.executions == [call[2] for call in script.calls]

    def test_the_request_carries_the_batch_configuration(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path, keys=("DEMO-1",))
        script = Script()
        _runner(db, script).start(batch_id)
        batch, request = store.get_batch(batch_id), script.requests[0]
        assert request.artifact_root == Path(batch.artifact_root)
        assert request.environment_path == Path(batch.environment_path)
        assert request.globals_path == Path(batch.globals_path)
        assert request.attempt == 1 and request.cancel_event is not None

    def test_fail_and_error_do_not_stop_later_issues(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script = Script(outcomes={"DEMO-3": [m.EXEC_FAIL], "DEMO-1": [m.EXEC_ERROR]})
        result = _runner(db, script).start(batch_id)
        assert result.status == "completed"
        assert _statuses(store, batch_id) == {
            "DEMO-3": [m.EXEC_FAIL],
            "DEMO-1": [m.EXEC_ERROR],
            "DEMO-2": [m.EXEC_PASS],
        }
        error = store.executions(batch_id, "DEMO-1")[0]
        assert error.error_kind == "NEWMAN_ERROR"  # نتیجه‌ی واقعیِ اجراگر، دست‌نخورده

    def test_review_status_is_independent_of_execution(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script = Script(
            outcomes={
                "DEMO-3": [m.EXEC_NOT_EXECUTED],
                "DEMO-1": [
                    lambda request, directory: _outcome(
                        m.EXEC_PASS,
                        directory,
                        review_status=m.REVIEW_NEEDS_REVIEW,
                        final_review_status="needs_review",
                    )
                ],
            }
        )
        result = _runner(db, script).start(batch_id)
        assert result.status == "completed"  # NEEDS_REVIEW batch را متوقف نمی‌کند
        latest = store.latest_executions(batch_id)
        assert (latest["DEMO-3"].status, latest["DEMO-3"].review_status) == (
            m.EXEC_NOT_EXECUTED,
            m.REVIEW_NEEDS_REVIEW,
        )
        assert latest["DEMO-3"].newman_run_id == ""
        assert (latest["DEMO-1"].status, latest["DEMO-1"].final_review_status) == (
            m.EXEC_PASS,
            "needs_review",
        )
        assert latest["DEMO-2"].status == m.EXEC_PASS

    def test_an_executor_exception_is_persisted_as_a_technical_error(self, db, store, tmp_path):
        def explode(request, directory):
            raise RuntimeError("secret text that must not be stored")

        batch_id = _batch(store, tmp_path)
        script = Script(outcomes={"DEMO-3": [explode]})
        result = _runner(db, script).start(batch_id)
        assert result.status == "completed"
        failed = store.executions(batch_id, "DEMO-3")[0]
        assert (failed.status, failed.error_kind) == (m.EXEC_ERROR, ERROR_RUNNER_EXCEPTION)
        assert failed.error_message == "The issue executor raised RuntimeError."
        assert _statuses(store, batch_id)["DEMO-2"] == [m.EXEC_PASS]

    def test_an_outcome_the_store_rejects_becomes_an_error_not_a_guess(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path, keys=("DEMO-1", "DEMO-2"))
        bad = [lambda request, directory: ExecutionOutcome(status=m.EXEC_ERROR)]  # بدونِ kind
        script = Script(outcomes={"DEMO-1": bad})
        _runner(db, script).start(batch_id)
        rejected = store.executions(batch_id, "DEMO-1")[0]
        assert (rejected.status, rejected.error_kind) == (m.EXEC_ERROR, ERROR_OUTCOME_REJECTED)
        assert _statuses(store, batch_id)["DEMO-2"] == [m.EXEC_PASS]

    def test_phases_reported_by_the_executor_are_persisted(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path, keys=("DEMO-1",))
        gate = threading.Event()
        script = Script(
            gates={"DEMO-1": gate}, phases={"DEMO-1": [m.PHASE_JIRA_FETCH, m.PHASE_STEP2]}
        )
        background = _Background(lambda: _runner(db, script).start(batch_id))
        script.started["DEMO-1"].wait(WAIT_S)
        running = store.executions(batch_id, "DEMO-1")[0]
        assert (running.status, running.phase) == (m.EXEC_RUNNING, m.PHASE_STEP2)
        gate.set()
        assert background.join().status == "completed"

    def test_start_is_refused_for_a_batch_that_is_not_created(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _runner(db, Script()).start(batch_id)
        result = _runner(db, Script()).start(batch_id)
        assert result.status == "refused"
        assert _runner(db, Script()).start("missing").status == "refused"


# ── lease و heartbeat ────────────────────────────────────────────────────────


class TestLeaseAndHeartbeat:
    def test_only_one_runner_holds_the_lease(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        other_id = _batch(store, tmp_path / "other")
        gate = threading.Event()
        script = Script(gates={"DEMO-3": gate})
        first = _runner(db, script)
        background = _Background(lambda: first.start(batch_id))
        script.started["DEMO-3"].wait(WAIT_S)
        second_script = Script()
        second = _runner(db, second_script)
        assert second.start(other_id).status == "refused"  # leaseِ پایگاه یکی است
        assert store.get_batch(batch_id).owner_token == first.token
        with pytest.raises(LeaseError):
            store.acquire_lease(batch_id, second.token, pid=2, host="h")
        assert second_script.calls == []
        gate.set()
        assert background.join().status == "completed"
        assert script.max_active == 1

    def test_the_same_runner_instance_cannot_run_twice_at_once(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        gate = threading.Event()
        script = Script(gates={"DEMO-3": gate})
        runner = _runner(db, script)
        background = _Background(lambda: runner.start(batch_id))
        script.started["DEMO-3"].wait(WAIT_S)
        assert runner.resume(batch_id).status == "refused"
        gate.set()
        background.join()

    def test_heartbeat_continues_during_a_long_execution(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path, keys=("DEMO-1",))
        gate = threading.Event()
        script = Script(gates={"DEMO-1": gate})
        background = _Background(lambda: _runner(db, script).start(batch_id))
        script.started["DEMO-1"].wait(WAIT_S)
        seen: set[str | None] = set()

        def advanced() -> bool:
            seen.add(store.get_batch(batch_id).heartbeat_at)
            return len(seen) >= 3  # heartbeat دست‌کم دو بار جلو رفت

        _wait(advanced)
        assert store.executions(batch_id)[0].status == m.EXEC_RUNNING  # هنوز در حالِ اجرا
        gate.set()
        assert background.join().status == "completed"

    def test_a_healthy_runner_lease_cannot_be_stolen(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        gate = threading.Event()
        script = Script(gates={"DEMO-3": gate})
        runner = _runner(db, script)
        background = _Background(lambda: runner.start(batch_id))
        script.started["DEMO-3"].wait(WAIT_S)
        assert recover_stale(store) == []
        assert _runner(db, Script()).resume(batch_id).status == "refused"
        assert store.get_batch(batch_id).owner_token == runner.token
        gate.set()
        assert background.join().status == "completed"

    def test_heartbeat_failure_stops_scheduling(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        gate = threading.Event()
        script = Script(gates={"DEMO-3": gate})
        runner = _runner(db, script, store_factory=FlakyStore.open, stale_after=0.1)
        background = _Background(lambda: runner.start(batch_id))
        script.started["DEMO-3"].wait(WAIT_S)
        FlakyStore.fail_heartbeat = True
        result = background.join()  # issueِ جاری از طریقِ cancel_event متوقف شد
        assert result.status == "heartbeat_failed" and "OperationalError" in result.error
        assert [call[0] for call in script.calls] == ["DEMO-3"]
        interrupted = store.executions(batch_id, "DEMO-3")[0]
        assert (interrupted.status, interrupted.error_kind) == (
            m.EXEC_INTERRUPTED,
            ERROR_RUNNER_STOPPED,
        )
        assert _statuses(store, batch_id)["DEMO-1"] == [m.EXEC_QUEUED]
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_INTERRUPTED, None)

        FlakyStore.fail_heartbeat = False
        resumed = _runner(db, Script()).resume(batch_id)
        assert resumed.status == "completed"
        assert _statuses(store, batch_id)["DEMO-3"] == [m.EXEC_INTERRUPTED, m.EXEC_PASS]

    def test_lease_loss_stops_scheduling_and_writing(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        gate = threading.Event()
        script = Script(gates={"DEMO-3": gate})
        runner = _runner(db, script)
        background = _Background(lambda: runner.start(batch_id))
        script.started["DEMO-3"].wait(WAIT_S)
        store.release_lease(batch_id, runner.token)  # مثلاً بازیابیِ اشتباه
        store.acquire_lease(batch_id, "thief", pid=9, host="elsewhere")
        result = background.join()
        assert result.status == "lease_lost"
        assert [call[0] for call in script.calls] == ["DEMO-3"]
        assert store.executions(batch_id, "DEMO-3")[0].status == m.EXEC_RUNNING  # ننوشت
        assert store.get_batch(batch_id).owner_token == "thief"  # leaseِ دیگری آزاد نشد


# ── Pause و Resume ───────────────────────────────────────────────────────────


class TestPauseAndResume:
    def test_pause_lets_the_issue_finish_and_is_observed_before_the_next(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        gate = threading.Event()
        script = Script(gates={"DEMO-3": gate})
        background = _Background(lambda: _runner(db, script).start(batch_id))
        script.started["DEMO-3"].wait(WAIT_S)
        store.request_action(batch_id, m.ACTION_PAUSE)
        assert script.saw_cancel.is_set() is False
        gate.set()
        result = background.join()
        assert (result.status, result.batch_status) == ("paused", m.BATCH_PAUSED)
        assert _statuses(store, batch_id) == {
            "DEMO-3": [m.EXEC_PASS],  # کامل تمام شد، با Newman
            "DEMO-1": [m.EXEC_QUEUED],
            "DEMO-2": [m.EXEC_QUEUED],
        }
        batch = store.get_batch(batch_id)
        assert (batch.owner_token, batch.requested_action) == (None, m.ACTION_NONE)

        resumed_script = Script()
        resumed = _runner(db, resumed_script).resume(batch_id)
        assert resumed.status == "completed"
        assert [call[0] for call in resumed_script.calls] == ["DEMO-1", "DEMO-2"]
        assert _statuses(store, batch_id) == {key: [m.EXEC_PASS] for key in KEYS}

    def test_pause_during_the_last_issue_completes_the_batch(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path, keys=("DEMO-1",))
        gate = threading.Event()
        script = Script(gates={"DEMO-1": gate})
        background = _Background(lambda: _runner(db, script).start(batch_id))
        script.started["DEMO-1"].wait(WAIT_S)
        store.request_action(batch_id, m.ACTION_PAUSE)
        gate.set()
        result = background.join()
        assert (result.status, result.batch_status) == ("completed", m.BATCH_COMPLETED)

    def test_pause_cannot_override_cancel(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        gate = threading.Event()
        script = Script(gates={"DEMO-3": gate})
        background = _Background(lambda: _runner(db, script).start(batch_id))
        script.started["DEMO-3"].wait(WAIT_S)
        store.request_action(batch_id, m.ACTION_CANCEL)
        with pytest.raises(m.InvalidTransitionError):
            store.request_action(batch_id, m.ACTION_PAUSE)
        assert store.get_batch(batch_id).requested_action == m.ACTION_CANCEL
        result = background.join()
        assert (result.status, result.batch_status) == ("cancelled", m.BATCH_CANCELLED)

    def test_resume_is_refused_unless_paused_or_interrupted(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        assert _runner(db, Script()).resume(batch_id).status == "refused"  # CREATED
        assert store.get_batch(batch_id).owner_token is None


# ── Cancel ───────────────────────────────────────────────────────────────────


class TestCancel:
    def test_cancel_racing_completion_keeps_the_real_result_and_stops_scheduling(
        self, db, store, tmp_path
    ):
        batch_id = _batch(store, tmp_path)
        gate = threading.Event()
        # فراخوانیِ LLMِ غیرقابلِ قطع: لغو را نمی‌بیند و با نتیجه‌ی واقعی برمی‌گردد
        script = Script(gates={"DEMO-3": gate}, honour_cancel=False)
        runner = _runner(db, script)
        background = _Background(lambda: runner.start(batch_id))
        script.started["DEMO-3"].wait(WAIT_S)
        store.request_action(batch_id, m.ACTION_CANCEL)
        _wait(lambda: script.requests[0].cancel_event.is_set())
        gate.set()
        result = background.join()
        assert (result.status, result.batch_status) == ("cancelled", m.BATCH_CANCELLED)
        assert [call[0] for call in script.calls] == ["DEMO-3"]
        assert _statuses(store, batch_id) == {
            "DEMO-3": [m.EXEC_PASS],  # هرگز با CANCELLED بازنویسی نشد
            "DEMO-1": [m.EXEC_CANCELLED],
            "DEMO-2": [m.EXEC_CANCELLED],
        }  # و هیچ تلاشِ دومی ساخته نشد
        assert store.get_batch(batch_id).owner_token is None

    @pytest.mark.parametrize("phase", [m.PHASE_STEP2, m.PHASE_NEWMAN])
    def test_cancel_signals_the_active_issue_and_is_confirmed_before_persisting(
        self, db, store, tmp_path, phase
    ):
        batch_id = _batch(store, tmp_path)
        gate, hold = threading.Event(), threading.Event()
        script = Script(gates={"DEMO-3": gate}, phases={"DEMO-3": [phase]}, hold_after_cancel=hold)
        background = _Background(lambda: _runner(db, script).start(batch_id))
        script.started["DEMO-3"].wait(WAIT_S)
        store.request_action(batch_id, m.ACTION_CANCEL)
        assert script.saw_cancel.wait(WAIT_S)  # cancel_eventِ همان ExecutionRequest
        # تا اجراگر لغو را تأیید نکرده، اجرا RUNNING می‌ماند
        assert store.executions(batch_id, "DEMO-3")[0].status == m.EXEC_RUNNING
        hold.set()
        result = background.join()
        assert result.status == "cancelled"
        cancelled = store.executions(batch_id, "DEMO-3")[0]
        assert (cancelled.status, cancelled.phase) == (m.EXEC_CANCELLED, phase)
        assert _statuses(store, batch_id)["DEMO-1"] == [m.EXEC_CANCELLED]
        gate.set()

    def test_cancel_before_the_next_issue_prevents_scheduling(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        cancel_now: list[str] = []

        def pass_then_cancel(request, directory):
            with BatchStore.open(db) as other:
                other.request_action(batch_id, m.ACTION_CANCEL)
            cancel_now.append(request.issue_key)
            return _outcome(m.EXEC_PASS, directory)

        script = Script(outcomes={"DEMO-3": [pass_then_cancel]})
        result = _runner(db, script).start(batch_id)
        assert result.status == "cancelled"
        assert [call[0] for call in script.calls] == ["DEMO-3"]
        assert _statuses(store, batch_id)["DEMO-3"] == [m.EXEC_PASS]


# ── Retry و بازیابی ──────────────────────────────────────────────────────────


class TestRetry:
    def test_retry_creates_a_new_attempt_and_preserves_the_old_one(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _runner(db, Script(outcomes={"DEMO-1": [m.EXEC_FAIL]})).start(batch_id)
        before = {e.id: e for e in store.executions(batch_id)}
        old = store.executions(batch_id, "DEMO-1")[0]
        old_marker = Path(old.artifact_dir) / "marker.txt"
        assert old_marker.read_text(encoding="utf-8") == old.id

        script = Script()
        result = _runner(db, script).retry(batch_id, "DEMO-1")
        assert (result.status, result.batch_status) == ("retried", m.BATCH_COMPLETED)
        assert [call[:2] for call in script.calls] == [("DEMO-1", 2)]  # بقیه اجرا نشدند
        first, second = store.executions(batch_id, "DEMO-1")
        assert first == before[first.id]  # تلاشِ قبلی دست‌نخورده
        assert (second.attempt, second.trigger, second.previous_execution_id) == (
            2,
            m.TRIGGER_RETRY,
            first.id,
        )
        assert second.status == m.EXEC_PASS
        assert second.artifact_dir != first.artifact_dir
        assert old_marker.read_text(encoding="utf-8") == old.id
        for execution in store.executions(batch_id):
            if execution.issue_key != "DEMO-1":
                assert execution == before[execution.id]
        assert store.get_batch(batch_id).owner_token is None

    def test_retry_is_refused_for_running_batches_unknown_or_active_issues(
        self, db, store, tmp_path
    ):
        batch_id = _batch(store, tmp_path)
        assert _runner(db, Script()).retry(batch_id, "DEMO-1").status == "refused"  # CREATED
        store.create_execution(batch_id, "DEMO-1", trigger=m.TRIGGER_INITIAL)
        store.transition_batch(batch_id, m.BATCH_CANCELLED)
        script = Script()
        assert _runner(db, script).retry(batch_id, "DEMO-1").status == "refused"  # QUEUED
        assert _runner(db, script).retry(batch_id, "NOPE-1").status == "refused"
        assert script.calls == []
        assert len(store.executions(batch_id, "DEMO-1")) == 1

    def test_retry_respects_the_single_runner_lease(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _runner(db, Script()).start(batch_id)
        store.acquire_lease(batch_id, "someone", pid=1, host="h")
        script = Script()
        assert _runner(db, script).retry(batch_id, "DEMO-1").status == "refused"
        assert script.calls == []


class TestRecovery:
    def test_a_stale_runner_is_recovered_and_its_issue_retried_as_a_new_attempt(
        self, db, store, tmp_path
    ):
        batch_id = _batch(store, tmp_path)
        _dead_runner(store, batch_id)
        done = store.executions(batch_id, "DEMO-3")[0]
        store.start_execution(done.id)
        store.finish_execution(done.id, m.EXEC_PASS, newman_status="PASS")
        running = store.executions(batch_id, "DEMO-1")[0]
        store.start_execution(running.id)
        directory = tmp_path / "artifacts" / batch_id / "DEMO-1" / "execution_1"
        directory.mkdir(parents=True)
        (directory / "marker.txt").write_text(running.id, encoding="utf-8")
        store.update_execution(running.id, phase=m.PHASE_NEWMAN, artifact_dir=str(directory))
        completed_before = store.get_execution(done.id)

        assert recover_stale(store) == []  # heartbeatِ تازه — سالم حساب می‌شود
        assert store.get_batch(batch_id).owner_token == "dead"

        (report,) = recover_stale(store, clock=_future())
        assert (report.recovered, report.reason) == (True, "stale_lease")
        assert report.interrupted_executions == [running.id]
        interrupted = store.get_execution(running.id)
        assert (interrupted.status, interrupted.phase, interrupted.error_kind) == (
            m.EXEC_INTERRUPTED,
            m.PHASE_NEWMAN,  # حدس زده نشد که Newman تمام شده
            ERROR_RUNNER_LOST,
        )
        assert interrupted.artifact_dir == str(directory)
        assert store.get_execution(done.id) == completed_before
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_INTERRUPTED, None)

        # اجرای دوباره هیچ چیزی را عوض نمی‌کند
        snapshot = store.executions(batch_id)
        assert recover_stale(store, clock=_future()) == []
        assert store.executions(batch_id) == snapshot

        script = Script()
        result = _runner(db, script).resume(batch_id)
        assert result.status == "completed"
        assert [call[:2] for call in script.calls] == [("DEMO-1", 2), ("DEMO-2", 1)]
        retried = store.executions(batch_id, "DEMO-1")[1]
        assert (retried.trigger, retried.previous_execution_id) == (
            m.TRIGGER_RESUME,
            running.id,
        )
        assert retried.artifact_dir != str(directory)
        assert (directory / "marker.txt").read_text(encoding="utf-8") == running.id
        assert store.get_execution(done.id) == completed_before
        assert store.get_execution(running.id) == interrupted

    def test_a_crash_after_the_last_result_keeps_it_and_resume_completes(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _dead_runner(store, batch_id)
        for execution in store.executions(batch_id):
            store.start_execution(execution.id)
            store.finish_execution(execution.id, m.EXEC_FAIL, newman_status="FAIL")
        results = store.executions(batch_id)
        (report,) = recover_stale(store, clock=_future())
        assert report.interrupted_executions == [] and report.batch_status == m.BATCH_INTERRUPTED
        assert store.executions(batch_id) == results
        script = Script()
        result = _runner(db, script).resume(batch_id)
        assert (result.status, result.batch_status) == ("completed", m.BATCH_COMPLETED)
        assert script.calls == []
        assert store.executions(batch_id) == results

    def test_recovery_honours_a_pending_cancel(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _dead_runner(store, batch_id)
        running = store.executions(batch_id, "DEMO-3")[0]
        store.start_execution(running.id)
        store.request_action(batch_id, m.ACTION_CANCEL)
        (report,) = recover_stale(store, clock=_future())
        assert report.batch_status == m.BATCH_CANCELLED
        assert _statuses(store, batch_id) == {
            "DEMO-3": [m.EXEC_INTERRUPTED],
            "DEMO-1": [m.EXEC_CANCELLED],
            "DEMO-2": [m.EXEC_CANCELLED],
        }

    def test_a_running_batch_without_an_owner_is_recovered(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _dead_runner(store, batch_id)
        store.release_lease(batch_id, "dead")  # RUNNING ولی بی‌مالک
        (report,) = recover_stale(store)
        assert (report.reason, report.batch_status) == ("orphan_running", m.BATCH_INTERRUPTED)
        assert store.get_batch(batch_id).owner_token is None

    def test_a_stale_lease_on_a_paused_batch_is_only_released(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _dead_runner(store, batch_id)
        store.transition_batch(batch_id, m.BATCH_PAUSED)  # مرد پیش از آزاد کردنِ lease
        (report,) = recover_stale(store, clock=_future())
        assert (report.recovered, report.batch_status) == (True, m.BATCH_PAUSED)
        assert store.get_batch(batch_id).owner_token is None
        assert _statuses(store, batch_id) == {key: [m.EXEC_QUEUED] for key in KEYS}


# ── مسیرهای خطا ─────────────────────────────────────────────────────────────


class TestFailurePaths:
    def test_a_database_write_failure_stops_scheduling(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        FlakyStore.fail_finish = 1
        script = Script()
        result = _runner(db, script, store_factory=FlakyStore.open).start(batch_id)
        assert result.status == "storage_failed" and "OperationalError" in result.error
        assert [call[0] for call in script.calls] == ["DEMO-3"]  # issueِ بعدی شروع نشد
        assert _statuses(store, batch_id) == {
            "DEMO-3": [m.EXEC_RUNNING],  # چیزی وانمود نشد
            "DEMO-1": [m.EXEC_QUEUED],
            "DEMO-2": [m.EXEC_QUEUED],
        }
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_INTERRUPTED, None)

        resumed = _runner(db, Script()).resume(batch_id)
        assert resumed.status == "completed"
        assert _statuses(store, batch_id)["DEMO-3"] == [m.EXEC_INTERRUPTED, m.EXEC_PASS]

    def test_the_monitor_thread_exits_on_every_path(self, db, store, tmp_path):
        def explode(request, directory):
            raise KeyboardInterrupt  # حتی BaseException

        batch_id = _batch(store, tmp_path)
        runner = _runner(db, Script(outcomes={"DEMO-3": [explode]}))
        with pytest.raises(KeyboardInterrupt):
            runner.start(batch_id)
        assert runner._monitor is None
        assert store.get_batch(batch_id).owner_token is None  # lease آزاد شد


# ── اصلاحِ هم‌زمانی و سازگاری (Milestone 4.5) ─────────────────────────────────


def _steal(db: Path, batch_id: str, token: str, new_owner: str = "B") -> Callable[[], None]:
    """runnerِ B درست پس از آخرین بررسیِ A و پیش از نوشتنِ A مالک می‌شود."""

    def steal() -> None:
        with BatchStore.open(db) as other:
            other.release_lease(batch_id, token)
            other.acquire_lease(batch_id, new_owner, pid=99, host="other")

    return steal


class TestAtomicOwnership:
    def test_a_result_write_after_losing_the_lease_is_rejected(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script = Script()
        runner = _runner(db, script, store_factory=FlakyStore.open)
        FlakyStore.before["finish_execution"] = lambda: _steal(db, batch_id, runner.token)()
        result = runner.start(batch_id)
        assert result.status == "lease_lost"
        assert [call[0] for call in script.calls] == ["DEMO-3"]
        # نتیجه‌ی A نوشته نشد و وضعیتِ B دست‌نخورده ماند
        assert _statuses(store, batch_id) == {
            "DEMO-3": [m.EXEC_RUNNING],
            "DEMO-1": [m.EXEC_QUEUED],
            "DEMO-2": [m.EXEC_QUEUED],
        }
        batch = store.get_batch(batch_id)
        assert (batch.owner_token, batch.status) == ("B", m.BATCH_RUNNING)

    def test_a_lifecycle_change_after_losing_the_lease_is_rejected(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path, keys=("DEMO-1",))
        runner = _runner(db, Script(), store_factory=FlakyStore.open)
        state: dict[str, Any] = {}

        def steal_before_completion() -> None:
            _steal(db, batch_id, runner.token)()
            state["executions"] = store.executions(batch_id)

        # اولین transition (RUNNING) عادی؛ دومی (COMPLETED) پس از دزدیده شدنِ lease
        FlakyStore.before["transition_batch"] = lambda: FlakyStore.before.__setitem__(
            "transition_batch", steal_before_completion
        )
        result = runner.start(batch_id)
        assert result.status == "lease_lost"
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_RUNNING, "B")  # نه COMPLETED
        assert store.executions(batch_id) == state["executions"]

    def test_runner_writes_carry_the_owner_token(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path, keys=("DEMO-1",))
        seen: list[tuple[str, Any]] = []

        class Recording(BatchStore):
            def transition_batch(self, batch_id, target, **kwargs):
                seen.append(("transition_batch", kwargs.get("owner_token")))
                return super().transition_batch(batch_id, target, **kwargs)

            def create_execution(self, batch_id, issue_key, **kwargs):
                seen.append(("create_execution", kwargs.get("owner_token")))
                return super().create_execution(batch_id, issue_key, **kwargs)

            def start_execution(self, execution_id, **kwargs):
                seen.append(("start_execution", kwargs.get("owner_token")))
                return super().start_execution(execution_id, **kwargs)

            def update_execution(self, execution_id, **kwargs):
                seen.append(("update_execution", kwargs.get("owner_token")))
                return super().update_execution(execution_id, **kwargs)

            def finish_execution(self, execution_id, status, **kwargs):
                seen.append(("finish_execution", kwargs.get("owner_token")))
                return super().finish_execution(execution_id, status, **kwargs)

        script = Script(phases={"DEMO-1": [m.PHASE_STEP2]})
        runner = _runner(db, script, store_factory=Recording.open)
        assert runner.start(batch_id).status == "completed"
        assert {name for name, _ in seen} == {
            "transition_batch",
            "create_execution",
            "start_execution",
            "update_execution",
            "finish_execution",
        }
        assert all(token == runner.token for _, token in seen)


class TestRecoveryRaces:
    def test_two_concurrent_recoveries_have_exactly_one_winner(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _dead_runner(store, batch_id)
        running = store.executions(batch_id, "DEMO-3")[0]
        store.start_execution(running.id)
        barrier = threading.Barrier(2)
        reports: list[Any] = []
        lock = threading.Lock()

        class Racing(BatchStore):
            def stale_leases(self, older_than):
                found = super().stale_leases(older_than)
                barrier.wait(WAIT_S)  # هر دو همان leaseِ کهنه را دیده‌اند
                return found

        def recover() -> None:
            with Racing.open(db) as own:
                found = recover_stale(own, clock=_future())
            with lock:
                reports.extend(found)

        threads = [threading.Thread(target=recover) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WAIT_S)
        assert sorted(report.recovered for report in reports) == [False, True]
        assert _statuses(store, batch_id) == {
            "DEMO-3": [m.EXEC_INTERRUPTED],  # یک بار، نه دو بار
            "DEMO-1": [m.EXEC_QUEUED],
            "DEMO-2": [m.EXEC_QUEUED],
        }
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_INTERRUPTED, None)

    def test_stale_information_about_a_changed_owner_cannot_take_over(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _dead_runner(store, batch_id)
        FlakyStore.stale_snapshot = store.stale_leases("2999-01-01T00:00:00.000Z")  # مالک: dead
        store.release_lease(batch_id, "dead")
        store.acquire_lease(batch_id, "new-owner", pid=5, host="pc")
        before = (store.get_batch(batch_id), store.executions(batch_id))
        with FlakyStore.open(db) as stale_view:
            (report,) = recover_stale(stale_view, clock=_future())
        assert (report.recovered, report.reason) == (False, "skipped")
        assert (store.get_batch(batch_id), store.executions(batch_id)) == before

    def test_stale_information_about_a_healthy_lease_cannot_take_over(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _dead_runner(store, batch_id)
        FlakyStore.stale_snapshot = store.stale_leases("2999-01-01T00:00:00.000Z")
        store.heartbeat(batch_id, "dead")  # در این فاصله زنده شد
        before = (store.get_batch(batch_id), store.executions(batch_id))
        with FlakyStore.open(db) as stale_view:
            (report,) = recover_stale(stale_view)  # آستانه‌ی واقعی: heartbeat تازه است
        assert report.recovered is False
        assert (store.get_batch(batch_id), store.executions(batch_id)) == before


class TestRetryCancellation:
    def test_a_retry_can_be_cancelled_without_touching_other_records(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _runner(db, Script(outcomes={"DEMO-1": [m.EXEC_FAIL]})).start(batch_id)
        before = {e.id: e for e in store.executions(batch_id)}
        gate = threading.Event()
        script = Script(gates={"DEMO-1": gate})
        runner = _runner(db, script)
        background = _Background(lambda: runner.retry(batch_id, "DEMO-1"))
        script.started["DEMO-1"].wait(WAIT_S)
        assert runner.request_cancel() is True
        assert script.saw_cancel.wait(WAIT_S)
        result = background.join()
        assert (result.status, result.batch_status) == ("retried", m.BATCH_COMPLETED)
        first, second = store.executions(batch_id, "DEMO-1")
        assert (second.attempt, second.status, second.trigger) == (
            2,
            m.EXEC_CANCELLED,
            m.TRIGGER_RETRY,
        )
        for execution in store.executions(batch_id):
            if execution.id != second.id:
                assert execution == before[execution.id]
        assert [call[0] for call in script.calls] == ["DEMO-1"]
        assert store.get_batch(batch_id).owner_token is None
        gate.set()

    def test_a_late_cancellation_never_overwrites_a_completed_retry(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _runner(db, Script(outcomes={"DEMO-1": [m.EXEC_FAIL]})).start(batch_id)
        holder: dict[str, BatchRunner] = {}

        def complete_then_cancel(request, directory):
            outcome = _outcome(m.EXEC_PASS, directory)
            holder["runner"].request_cancel()  # لغو پس از پایانِ کار، پیش از ثبت
            return outcome

        runner = _runner(db, Script(outcomes={"DEMO-1": [complete_then_cancel]}))
        holder["runner"] = runner
        result = runner.retry(batch_id, "DEMO-1")
        assert result.status == "retried"
        retried = store.executions(batch_id, "DEMO-1")[1]
        assert retried.status == m.EXEC_PASS
        assert runner.request_cancel() is False  # کاری در دست نیست
        assert store.get_execution(retried.id) == retried
        with pytest.raises(m.InvalidTransitionError):
            store.request_action(batch_id, m.ACTION_CANCEL)  # batchِ پایانی

    def test_in_process_cancel_also_stops_a_batch_run(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        gate = threading.Event()
        script = Script(gates={"DEMO-3": gate})
        runner = _runner(db, script)
        background = _Background(lambda: runner.start(batch_id))
        script.started["DEMO-3"].wait(WAIT_S)
        runner.request_cancel()
        result = background.join()
        assert (result.status, result.batch_status) == ("cancelled", m.BATCH_CANCELLED)
        assert _statuses(store, batch_id) == {
            "DEMO-3": [m.EXEC_CANCELLED],
            "DEMO-1": [m.EXEC_CANCELLED],
            "DEMO-2": [m.EXEC_CANCELLED],
        }
        gate.set()


class TestConfigurationIntegrity:
    @pytest.mark.parametrize("name", ["env.json", "globals.json"])
    def test_a_changed_file_blocks_the_next_issue(self, db, store, tmp_path, name):
        batch_id = _batch(store, tmp_path)
        gate = threading.Event()
        script = Script(gates={"DEMO-3": gate})
        background = _Background(lambda: _runner(db, script).start(batch_id))
        script.started["DEMO-3"].wait(WAIT_S)
        changed = '{"values": [{"key": "token", "value": "Changed-Secret-77"}]}'
        (tmp_path / name).write_text(changed, encoding="utf-8")
        gate.set()
        result = background.join()
        assert result.status == "configuration_changed"
        assert "Changed-Secret-77" not in result.error
        assert [call[0] for call in script.calls] == ["DEMO-3"]  # DEMO-1 اجرا نشد
        assert _statuses(store, batch_id) == {
            "DEMO-3": [m.EXEC_PASS],
            "DEMO-1": [m.EXEC_ERROR],
            "DEMO-2": [m.EXEC_QUEUED],
        }
        blocked = store.executions(batch_id, "DEMO-1")[0]
        assert blocked.error_kind == runner_module.ERROR_CONFIGURATION_CHANGED
        assert "not executed" in blocked.error_message
        assert "Changed-Secret-77" not in blocked.error_message
        assert blocked.artifact_dir == "" and blocked.newman_run_id == ""
        assert (tmp_path / name).read_text(encoding="utf-8") == changed  # دست‌نخورده
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_INTERRUPTED, None)

    def test_a_missing_file_blocks_execution(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        (tmp_path / "env.json").unlink()
        script = Script()
        result = _runner(db, script).start(batch_id)
        assert result.status == "configuration_changed" and "missing" in result.error
        assert script.calls == []
        assert _statuses(store, batch_id)["DEMO-3"] == [m.EXEC_ERROR]

    def test_resume_validates_and_continues_once_the_file_is_restored(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        globals_file = tmp_path / "globals.json"
        original = globals_file.read_bytes()
        done = Script(gates={"DEMO-3": threading.Event()})
        background = _Background(lambda: _runner(db, done).start(batch_id))
        done.started["DEMO-3"].wait(WAIT_S)
        globals_file.write_text('{"changed": 1}', encoding="utf-8")
        done.gates["DEMO-3"].set()
        assert background.join().status == "configuration_changed"
        completed = store.executions(batch_id, "DEMO-3")[0]

        script = Script()
        refused = _runner(db, script).resume(batch_id)
        assert refused.status == "configuration_changed"
        assert script.calls == []
        assert store.get_batch(batch_id).status == m.BATCH_INTERRUPTED
        assert len(store.executions(batch_id)) == 3  # هیچ رکوردِ تازه‌ای

        globals_file.write_bytes(original)
        resumed = _runner(db, script).resume(batch_id)
        assert resumed.status == "completed"
        assert [call[:2] for call in script.calls] == [("DEMO-1", 2), ("DEMO-2", 1)]
        blocked, rerun = store.executions(batch_id, "DEMO-1")
        assert (rerun.trigger, rerun.previous_execution_id) == (m.TRIGGER_RESUME, blocked.id)
        assert blocked.error_kind == runner_module.ERROR_CONFIGURATION_CHANGED
        assert store.get_execution(completed.id) == completed

    def test_retry_validates_the_recorded_hashes(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _runner(db, Script()).start(batch_id)
        before = store.executions(batch_id)
        (tmp_path / "env.json").write_text('{"changed": true}', encoding="utf-8")
        script = Script()
        result = _runner(db, script).retry(batch_id, "DEMO-1")
        assert result.status == "configuration_changed"
        assert script.calls == [] and store.executions(batch_id) == before
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_COMPLETED, None)


def _auth_error(kind: str = "rejected") -> Exception:
    from src.auth import AuthenticationError

    return AuthenticationError(kind, "The token endpoint rejected the refresh token (HTTP 401).")


class TestAuthentication:
    def test_one_token_per_session_is_shared_by_every_issue(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script, tokens = Script(), FakeTokenSource()
        assert _runner(db, script, token_provider=tokens).start(batch_id).status == "completed"
        assert tokens.calls == 1  # نه یکی برای هر issue
        values = {request.access_token.value for request in script.requests}
        assert values == {"synthetic-access-token-1"} and len(script.requests) == 3

    def test_the_token_is_fetched_before_any_issue_work(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        order: list[str] = []

        class Tokens(FakeTokenSource):
            def fetch(self):
                order.append("token")
                return super().fetch()

        def behaviour(request, directory):
            order.append(request.issue_key)
            return _outcome(m.EXEC_PASS, directory)

        script = Script(outcomes={key: [behaviour] for key in KEYS})
        _runner(db, script, token_provider=Tokens()).start(batch_id)
        assert order == ["token", *KEYS]

    def test_a_failure_blocks_the_first_issue_and_interrupts_the_batch(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script, tokens = Script(), FakeTokenSource(failure=_auth_error())
        result = _runner(db, script, token_provider=tokens).start(batch_id)
        assert result.status == runner_module.RESULT_AUTHENTICATION_FAILED
        assert "HTTP 401" in result.error
        assert script.calls == []  # نه Jira، نه LLM، نه Newman
        assert _statuses(store, batch_id) == {
            "DEMO-3": [m.EXEC_ERROR],
            "DEMO-1": [m.EXEC_QUEUED],
            "DEMO-2": [m.EXEC_QUEUED],
        }
        blocked = store.executions(batch_id, "DEMO-3")[0]
        assert (blocked.error_kind, blocked.error_stage) == (
            "AUTHENTICATION_FAILED",
            "authentication",
        )
        assert blocked.newman_error_kind == "" and blocked.newman_run_id == ""
        assert "not executed" in blocked.error_message
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_INTERRUPTED, None)

    def test_resume_fetches_a_fresh_token_and_retries_the_blocked_issue(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _runner(db, Script(), token_provider=FakeTokenSource(failure=_auth_error())).start(batch_id)
        script, tokens = Script(), FakeTokenSource()
        resumed = _runner(db, script, token_provider=tokens).resume(batch_id)
        assert resumed.status == "completed"
        assert tokens.calls == 1
        assert [call[:2] for call in script.calls] == [("DEMO-3", 2), ("DEMO-1", 1), ("DEMO-2", 1)]
        blocked, rerun = store.executions(batch_id, "DEMO-3")
        assert blocked.error_kind == "AUTHENTICATION_FAILED"
        assert (rerun.status, rerun.trigger) == (m.EXEC_PASS, m.TRIGGER_RESUME)

    def test_every_session_gets_its_own_token(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        tokens = FakeTokenSource()
        gate = threading.Event()
        first = Script(gates={"DEMO-1": gate})
        runner = _runner(db, first, token_provider=tokens)
        background = _Background(lambda: runner.start(batch_id))
        first.started["DEMO-1"].wait(WAIT_S)
        store.request_action(batch_id, m.ACTION_PAUSE)
        gate.set()
        assert background.join().status == "paused"
        second = Script()
        assert _runner(db, second, token_provider=tokens).resume(batch_id).status == "completed"
        assert tokens.calls == 2
        assert {r.access_token.value for r in first.requests} == {"synthetic-access-token-1"}
        assert {r.access_token.value for r in second.requests} == {"synthetic-access-token-2"}

    def test_retry_fetches_a_fresh_token(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        tokens = FakeTokenSource()
        runner = _runner(db, Script(), token_provider=tokens)
        runner.start(batch_id)
        script = Script()
        runner = _runner(db, script, token_provider=tokens)
        assert runner.retry(batch_id, "DEMO-1").status == "retried"
        assert tokens.calls == 2
        assert script.requests[0].access_token.value == "synthetic-access-token-2"
        assert runner._access_token is None  # پس از نشست در حافظه نمی‌ماند

    def test_a_retry_failure_creates_no_attempt(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _runner(db, Script()).start(batch_id)
        before = store.executions(batch_id)
        script = Script()
        result = _runner(db, script, token_provider=FakeTokenSource(failure=_auth_error())).retry(
            batch_id, "DEMO-1"
        )
        assert result.status == runner_module.RESULT_AUTHENTICATION_FAILED
        assert script.calls == [] and store.executions(batch_id) == before
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_COMPLETED, None)

    def test_a_refused_session_fetches_no_token(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _runner(db, Script()).start(batch_id)  # COMPLETED
        tokens = FakeTokenSource()
        refused = _runner(db, Script(), token_provider=tokens).resume(batch_id)
        assert refused.status == "refused" and tokens.calls == 0

    def test_the_token_never_reaches_the_database(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _runner(db, Script(), token_provider=FakeTokenSource()).start(batch_id)
        store.close()
        for path in db.parent.glob(db.name + "*"):
            assert b"synthetic-access-token" not in path.read_bytes()
