"""
tests/unit/batch_testing/test_runner_gate.py — Execution Gate ِ مشترک در چرخه‌ی Batch (M7.2)

Gate ِ واقعی (قفلِ سیستم‌عامل + پایگاه) در پوشه‌ی موقتِ هر تست (conftest)؛ IssueExecutor
ساختگی یا IssueExecutor ِ واقعی با Jira/خطِ لوله/Newman ِ ساختگی — هیچ شبکه یا Newman ِ واقعی.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from src.batch_testing import models as m
from src.batch_testing import runner as runner_module
from src.batch_testing.executor import IssueExecutor
from src.batch_testing.runner import (
    ERROR_RUNNER_STOPPED,
    GATE_STOPPED_EXECUTION_MESSAGE,
    REFUSAL_GATE_BUSY,
    REFUSAL_GATE_UNAVAILABLE,
    RESULT_COMPLETED,
    RESULT_FAILED,
    RESULT_GATE_LOST,
    RESULT_GATE_UNAVAILABLE,
    RESULT_REFUSED,
    RESULT_RETRIED,
    BatchRunner,
)
from src.batch_testing.service import BatchService
from src.batch_testing.storage import BatchStore
from src.execution_gate import ExecutionGate, GateBusy, GateLease, GateUnavailable
from src.task_sources.jira import JiraSettings
from tests.unit.batch_testing.test_executor import FakeEngine
from tests.unit.batch_testing.test_runner import (
    KEYS,
    WAIT_S,
    FakeTokenSource,
    FlakyStore,
    Script,
    _Background,
    _batch,
    _outcome,
    _runner,
    _statuses,
    _wait,
)
from tests.unit.execution_gate.support import Child, raw_owner, write_owner
from ui import pipeline


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """همان قیدهای test_runner: نه IssueExecutor/TokenProvider ِ واقعی، نه threadِ یتیم."""

    def forbidden(*args, **kwargs):
        raise AssertionError("No real executor, token provider, Jira or LLM in unit tests.")

    monkeypatch.setattr(runner_module, "_default_executor", forbidden)
    monkeypatch.setattr(runner_module, "_default_token_provider", forbidden)
    # هیچ مسیری به Jira یا خطِ لوله‌ی واقعی (LLM) نمی‌رسد — هر تلاش تست را می‌شکند
    monkeypatch.setattr(pipeline, "load_jira_task", forbidden)
    monkeypatch.setattr(pipeline, "import_issue", forbidden)
    monkeypatch.setattr(pipeline, "run_all", forbidden)
    monkeypatch.setattr(JiraSettings, "from_config", classmethod(forbidden))
    monkeypatch.setattr(FlakyStore, "fail_finish", 0)
    monkeypatch.setattr(FlakyStore, "fail_heartbeat", False)
    monkeypatch.setattr(FlakyStore, "before", {})
    monkeypatch.setattr(FlakyStore, "stale_snapshot", None)
    yield
    alive = [t.name for t in threading.enumerate() if t.name.startswith("batch-runner-")]
    assert alive == [], alive


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "batch.sqlite3"


@pytest.fixture
def store(db: Path):
    with BatchStore.open(db) as opened:
        yield opened


# ── ابزارها ──────────────────────────────────────────────────────────────────


def _busy(gate_dir: Path) -> bool:
    """گیرنده‌ی مستقلِ نوعِ BOX (فقط API ِ مستقلِ Gate) Gate را مشغول می‌بیند؟"""
    try:
        lease = ExecutionGate(gate_dir).acquire("BOX", "probe")
    except GateBusy:
        return True
    lease.release()
    return False


def _events(gate_dir: Path) -> list[str]:
    path = (gate_dir / "gate.sqlite3").resolve()
    if not path.exists():
        return []
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as c:
        return [row[0] for row in c.execute("SELECT event FROM gate_events ORDER BY id")]


def _gate(gate_dir: Path) -> ExecutionGate:
    return ExecutionGate(gate_dir)


def _probe_newman(seen: list[Any]):
    """رفتارِ ساختگیِ یک issue که مثلِ IssueExecutor درست پیش از Newman حصار را صدا می‌زند."""

    def behaviour(request, directory):
        try:
            request.before_newman()
        except Exception:  # noqa: BLE001 — همان قراردادِ IssueExecutor: Newman شروع نمی‌شود
            seen.append(("refused", request.issue_key))
            return _outcome(
                m.EXEC_CANCELLED,
                directory,
                phase=m.PHASE_NEWMAN,
                error_stage=m.PHASE_NEWMAN,
                error_message="Newman was not started.",
            )
        seen.append(("newman", request.issue_key))
        return _outcome(m.EXEC_PASS, directory)

    return behaviour


def _interrupted(opened: BatchStore, batch_id: str) -> None:
    opened.transition_batch(batch_id, m.BATCH_RUNNING, expected=m.BATCH_CREATED)
    opened.transition_batch(batch_id, m.BATCH_INTERRUPTED, expected=m.BATCH_RUNNING)


# ── A. چرخه‌ی گرفتن و آزادسازی ──────────────────────────────────────────────


class TestLifecycle:
    def test_the_gate_is_held_before_the_token_and_every_issue(self, db, store, tmp_path, gate_dir):
        batch_id = _batch(store, tmp_path)
        seen: list[tuple[str, Any]] = []

        class RecordingTokens(FakeTokenSource):
            def fetch(self):
                seen.append(("token", _gate(gate_dir).status().owner))
                return super().fetch()

        def behaviour(request, directory):
            seen.append((request.issue_key, _gate(gate_dir).status().owner))
            assert _busy(gate_dir)
            return _outcome(m.EXEC_PASS, directory)

        script = Script(outcomes={key: [behaviour] for key in KEYS})
        result = _runner(db, script, token_provider=RecordingTokens()).start(batch_id)
        assert result.status == RESULT_COMPLETED
        assert [label for label, _ in seen] == ["token", *KEYS]
        assert all((o.kind, o.ref) == ("BATCH", batch_id) for _, o in seen)
        assert len({o.generation for _, o in seen}) == 1  # یک Gate برای کلِ نشست

    def test_the_gate_is_released_after_normal_completion(self, db, store, tmp_path, gate_dir):
        batch_id = _batch(store, tmp_path)
        result = _runner(db, Script()).start(batch_id)
        assert result.status == RESULT_COMPLETED and result.cleanup_errors == []
        assert _gate(gate_dir).status().owner is None
        assert _events(gate_dir) == ["ACQUIRED", "RELEASED"]
        assert not _busy(gate_dir)

    def test_the_batch_lease_is_released_before_the_gate(
        self, db, store, tmp_path, gate_dir, monkeypatch
    ):
        batch_id = _batch(store, tmp_path)
        owners_at_release: list[str | None] = []
        real = GateLease.release

        def release(self):
            with BatchStore.open(db) as other:
                owners_at_release.append(other.get_batch(batch_id).owner_token)
            return real(self)

        monkeypatch.setattr(GateLease, "release", release)
        assert _runner(db, Script()).start(batch_id).status == RESULT_COMPLETED
        assert owners_at_release == [None]

    def test_a_busy_gate_prevents_all_session_work(self, db, store, tmp_path, gate_dir):
        batch_id = _batch(store, tmp_path)
        tokens, script = FakeTokenSource(), Script()
        with _gate(gate_dir).acquire("BOX", "run-7"):
            result = _runner(db, script, token_provider=tokens).start(batch_id)
        assert (result.status, result.refusal) == (RESULT_REFUSED, REFUSAL_GATE_BUSY)
        assert "BOX run-7 holds the execution gate" in result.error
        assert tokens.calls == 0 and script.calls == []
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_CREATED, None)
        assert store.executions(batch_id) == []

    def test_an_unavailable_gate_prevents_all_session_work(self, db, store, tmp_path, gate_dir):
        batch_id = _batch(store, tmp_path)
        gate_dir.mkdir(parents=True)
        (gate_dir / "gate.sqlite3").write_bytes(b"corrupt" * 40)
        tokens, script = FakeTokenSource(), Script()
        result = _runner(db, script, token_provider=tokens).start(batch_id)
        assert (result.status, result.refusal) == (RESULT_REFUSED, REFUSAL_GATE_UNAVAILABLE)
        assert "nothing was started" in result.error
        assert tokens.calls == 0 and script.calls == []
        assert store.get_batch(batch_id).status == m.BATCH_CREATED

    def test_an_existing_batch_refusal_happens_before_the_gate(self, db, store, tmp_path, gate_dir):
        batch_id = _batch(store, tmp_path)
        result = _runner(db, Script()).resume(batch_id)  # CREATED → resume رد می‌شود
        assert result.status == RESULT_REFUSED and result.refusal == ""
        assert _events(gate_dir) == []  # Gate هرگز گرفته نشد

    def test_a_batch_lease_failure_releases_the_gate_without_any_work(
        self, db, store, tmp_path, gate_dir
    ):
        batch_id = _batch(store, tmp_path)
        store.acquire_lease(batch_id, "other-runner", pid=1, host="h")
        tokens, script = FakeTokenSource(), Script()
        result = _runner(db, script, token_provider=tokens).start(batch_id)
        assert result.status == RESULT_REFUSED and "Another runner owns the lease" in result.error
        assert tokens.calls == 0 and script.calls == []
        assert _events(gate_dir) == ["ACQUIRED", "RELEASED"]
        assert not _busy(gate_dir)

    def test_the_owner_token_never_appears_in_results(self, db, store, tmp_path, gate_dir):
        batch_id = _batch(store, tmp_path)
        with _gate(gate_dir).acquire("BOX", "run-7") as lease:
            result = _runner(db, Script()).start(batch_id)
            assert lease._token not in repr(result)


# ── B. همه‌ی ورودی‌های اجرا ─────────────────────────────────────────────────


class TestEntryPoints:
    @pytest.fixture
    def prepared(self, db, store, tmp_path):
        def make(mode: str) -> tuple[str, tuple]:
            batch_id = _batch(store, tmp_path / mode)
            if mode == "resume":
                _interrupted(store, batch_id)
                return batch_id, ()
            if mode == "retry":
                assert _runner(db, Script()).start(batch_id).status == RESULT_COMPLETED
                return batch_id, (KEYS[0],)
            return batch_id, ()

        return make

    @pytest.mark.parametrize("mode", ["start", "resume", "retry"])
    def test_every_entry_point_holds_the_gate_while_it_works(self, db, prepared, gate_dir, mode):
        batch_id, extra = prepared(mode)
        held: list[bool] = []

        def behaviour(request, directory):
            held.append(_busy(gate_dir))
            return _outcome(m.EXEC_PASS, directory)

        script = Script(outcomes={key: [behaviour] for key in KEYS})
        result = getattr(_runner(db, script), mode)(batch_id, *extra)
        assert result.status in (RESULT_COMPLETED, RESULT_RETRIED)
        assert held and all(held)
        assert not _busy(gate_dir)

    @pytest.mark.parametrize("mode", ["start", "resume", "retry"])
    def test_every_entry_point_is_refused_while_box_holds_the_gate(
        self, db, store, prepared, gate_dir, mode
    ):
        batch_id, extra = prepared(mode)
        before = len(store.executions(batch_id))
        script = Script()
        with _gate(gate_dir).acquire("BOX", "run-7"):
            result = getattr(_runner(db, script), mode)(batch_id, *extra)
        assert (result.status, result.refusal) == (RESULT_REFUSED, REFUSAL_GATE_BUSY)
        assert script.calls == [] and len(store.executions(batch_id)) == before

    @pytest.mark.parametrize("mode", ["start", "resume", "retry"])
    def test_the_service_reports_a_gate_refusal_instead_of_a_launch(
        self, db, prepared, gate_dir, mode
    ):
        batch_id, extra = prepared(mode)
        service = BatchService(
            db,
            runner_factory=lambda path: _runner(path, Script()),
            recover=lambda store: [],
        )
        with _gate(gate_dir).acquire("BOX", "run-7"):
            outcome = getattr(service, mode)(batch_id, *extra)
            service.wait(WAIT_S)
        assert not outcome.ok
        assert "BOX run-7 holds the execution gate" in outcome.message


# ── C. پروسه‌های جدا ─────────────────────────────────────────────────────────


class TestCrossProcess:
    def test_a_batch_session_blocks_other_batches_and_box_processes_until_released(
        self, db, store, tmp_path, gate_dir
    ):
        first, second = _batch(store, tmp_path / "a"), _batch(store, tmp_path / "b")
        release = threading.Event()
        script = Script(gates={KEYS[0]: release})
        children: list[Child] = []
        try:
            background = _Background(lambda: _runner(db, script).start(first))
            _wait(lambda: script.started[KEYS[0]].is_set())
            box = Child(gate_dir, "try")  # پروسه‌ی جدا، API ِ مستقلِ Gate با kind=BOX
            children.append(box)
            assert box.expect("BUSY") == "BUSY"
            other = _runner(db, Script()).start(second)  # Batch ِ دوم
            assert (other.status, other.refusal) == (RESULT_REFUSED, REFUSAL_GATE_BUSY)
            # heartbeatِ کهنه هیچ‌وقت راهِ گرفتن نیست، تا وقتی مالک زنده است
            write_owner(
                _gate(gate_dir), "UPDATE gate_owner SET heartbeat_at='2000-01-01T00:00:00.000Z'"
            )
            stale = Child(gate_dir, "try")
            children.append(stale)
            assert stale.expect("BUSY") == "BUSY"
            release.set()
            assert background.join().status == RESULT_COMPLETED
            after = Child(gate_dir, "try")
            children.append(after)
            assert after.expect("ACQUIRED").startswith("ACQUIRED")
        finally:
            release.set()
            for child in children:
                child.close()


# ── D. heartbeat و مالکیت ────────────────────────────────────────────────────


class TestHeartbeat:
    def test_the_monitor_heartbeats_the_gate(self, db, store, tmp_path, gate_dir):
        batch_id = _batch(store, tmp_path)
        release = threading.Event()
        script = Script(gates={KEYS[0]: release})
        background = _Background(lambda: _runner(db, script).start(batch_id))
        try:
            _wait(lambda: script.started[KEYS[0]].is_set())
            gate = _gate(gate_dir)
            first = raw_owner(gate)["heartbeat_at"]
            _wait(lambda: raw_owner(gate)["heartbeat_at"] > first)
        finally:
            release.set()
        assert background.join().status == RESULT_COMPLETED

    def test_a_lost_gate_stops_the_session_and_cancels_the_active_issue(
        self, db, store, tmp_path, gate_dir
    ):
        batch_id = _batch(store, tmp_path)
        script = Script(gates={KEYS[0]: threading.Event()})
        background = _Background(lambda: _runner(db, script).start(batch_id))
        _wait(lambda: script.started[KEYS[0]].is_set())
        write_owner(_gate(gate_dir), "UPDATE gate_owner SET owner_token = ?", ("f" * 32,))
        result = background.join()
        assert result.status == RESULT_GATE_LOST
        assert script.saw_cancel.is_set() and len(script.calls) == 1
        (execution,) = store.executions(batch_id, KEYS[0])
        assert (execution.status, execution.error_kind) == (
            m.EXEC_INTERRUPTED,
            ERROR_RUNNER_STOPPED,
        )
        assert execution.error_message == GATE_STOPPED_EXECUTION_MESSAGE
        assert store.get_batch(batch_id).status == m.BATCH_INTERRUPTED
        assert store.get_batch(batch_id).owner_token is None
        # رکورد مالِ این نشست نبود → پاک نشد؛ قفلِ خودش آزاد شد و یادداشت ماند
        assert any("could not be cleared" in note for note in result.cleanup_errors)
        assert not _busy(gate_dir)

    def test_an_unavailable_gate_heartbeat_stops_without_waiting(
        self, db, store, tmp_path, gate_dir, monkeypatch
    ):
        batch_id = _batch(store, tmp_path)
        script = Script(gates={KEYS[0]: threading.Event()})
        real = GateLease.heartbeat

        def broken(self):
            if script.started[KEYS[0]].is_set():  # فقط وقتی issue در حالِ کار است
                raise GateUnavailable("database is locked")
            return real(self)

        monkeypatch.setattr(GateLease, "heartbeat", broken)
        result = _runner(db, script).start(batch_id)
        assert result.status == RESULT_GATE_UNAVAILABLE
        statuses = _statuses(store, batch_id)
        assert statuses[KEYS[0]] == [m.EXEC_INTERRUPTED]
        assert all(statuses[key] == [m.EXEC_QUEUED] for key in KEYS[1:])  # شروع نشدند
        assert store.get_batch(batch_id).status == m.BATCH_INTERRUPTED
        assert not _busy(gate_dir)

    def test_a_batch_heartbeat_failure_still_releases_the_gate_after_the_session(
        self, db, store, tmp_path, gate_dir, monkeypatch
    ):
        batch_id = _batch(store, tmp_path)
        monkeypatch.setattr(FlakyStore, "fail_heartbeat", True)
        script = Script(gates={KEYS[0]: threading.Event()})
        runner = _runner(db, script, store_factory=FlakyStore.open, stale_after=0.2)
        result = runner.start(batch_id)
        assert result.status == runner_module.RESULT_HEARTBEAT_FAILED
        assert _events(gate_dir)[-1] == "RELEASED" and not _busy(gate_dir)


# ── E. مرزِ Newman ───────────────────────────────────────────────────────────


class TestNewmanBoundary:
    def test_gate_ownership_is_asserted_before_every_newman_start(
        self, db, store, tmp_path, gate_dir, monkeypatch
    ):
        batch_id = _batch(store, tmp_path)
        asserted: list[str] = []
        real = GateLease.assert_held

        def spy(self):
            asserted.append(self.ref)
            return real(self)

        monkeypatch.setattr(GateLease, "assert_held", spy)
        seen: list[Any] = []
        script = Script(outcomes={key: [_probe_newman(seen)] for key in KEYS})
        assert _runner(db, script).start(batch_id).status == RESULT_COMPLETED
        assert seen == [("newman", key) for key in KEYS]
        assert asserted == [batch_id] * len(KEYS)

    def test_a_lost_gate_prevents_the_next_newman_start(self, db, store, tmp_path, gate_dir):
        batch_id = _batch(store, tmp_path)
        seen: list[Any] = []
        newman = _probe_newman(seen)

        def lose_gate_then_start_newman(request, directory):
            write_owner(_gate(gate_dir), "UPDATE gate_owner SET owner_token = ?", ("e" * 32,))
            return newman(request, directory)

        # heartbeatِ پایش عمداً کند است تا حصار (نه پایش) از دست رفتن را ببیند
        script = Script(outcomes={KEYS[0]: [lose_gate_then_start_newman]})
        result = _runner(db, script, heartbeat_interval=60).start(batch_id)
        assert seen == [("refused", KEYS[0])]
        assert result.status == RESULT_GATE_LOST
        (execution,) = store.executions(batch_id, KEYS[0])
        assert execution.status == m.EXEC_INTERRUPTED
        assert execution.error_message == GATE_STOPPED_EXECUTION_MESSAGE
        assert len(script.calls) == 1  # issueِ بعدی شروع نشد

    # حصار در IssueExecutor ِ واقعی: tests/unit/batch_testing/test_executor.py::TestGateBoundary


# ── F. شکست‌ها و لغو ─────────────────────────────────────────────────────────


def _real_executor(**kwargs):
    return lambda on_phase: IssueExecutor(on_phase=on_phase, **kwargs)


class TestFailures:
    def test_a_jira_failure_ends_the_session_and_releases_the_gate(
        self, db, store, tmp_path, gate_dir
    ):
        batch_id = _batch(store, tmp_path, keys=("DEMO-1",))

        def jira(state, key):
            raise ConnectionError("jira down")

        engine = FakeEngine()
        runner = BatchRunner(
            db,
            executor_factory=_real_executor(engine=engine, load_task=jira),
            token_provider=FakeTokenSource(),
            heartbeat_interval=0.02,
            poll_interval=0.005,
        )
        assert runner.start(batch_id).status == RESULT_COMPLETED
        assert store.executions(batch_id)[0].status == m.EXEC_ERROR
        assert engine.calls == [] and not _busy(gate_dir)

    def test_an_llm_pipeline_failure_ends_the_session_and_releases_the_gate(
        self, db, store, tmp_path, gate_dir
    ):
        batch_id = _batch(store, tmp_path, keys=("DEMO-1",))

        def pipeline(*args, **kwargs):
            raise RuntimeError("llm failed")

        engine = FakeEngine()
        runner = BatchRunner(
            db,
            executor_factory=_real_executor(
                engine=engine, load_task=lambda state, key: None, run_pipeline=pipeline
            ),
            token_provider=FakeTokenSource(),
            heartbeat_interval=0.02,
            poll_interval=0.005,
        )
        assert runner.start(batch_id).status == RESULT_COMPLETED
        assert store.executions(batch_id)[0].status == m.EXEC_ERROR
        assert engine.calls == [] and not _busy(gate_dir)

    def test_a_newman_failure_keeps_the_gate_until_the_session_ends(
        self, db, store, tmp_path, gate_dir
    ):
        batch_id = _batch(store, tmp_path)

        def newman_error(request, directory):
            assert _busy(gate_dir)
            return _outcome(m.EXEC_ERROR, directory)

        script = Script(outcomes={key: [newman_error] for key in KEYS})
        assert _runner(db, script).start(batch_id).status == RESULT_COMPLETED
        assert len(script.calls) == len(KEYS) and not _busy(gate_dir)

    def test_cancellation_keeps_the_gate_until_the_active_issue_has_stopped(
        self, db, store, tmp_path, gate_dir
    ):
        batch_id = _batch(store, tmp_path)
        hold = threading.Event()
        script = Script(gates={KEYS[0]: threading.Event()}, hold_after_cancel=hold)
        background = _Background(lambda: _runner(db, script).start(batch_id))
        try:
            _wait(lambda: script.started[KEYS[0]].is_set())
            store.request_action(batch_id, m.ACTION_CANCEL)
            _wait(script.saw_cancel.is_set)
            assert _busy(gate_dir)  # لغو درخواست شده ولی issue هنوز در حالِ توقف است
        finally:
            hold.set()
        assert background.join().status == runner_module.RESULT_CANCELLED
        assert not _busy(gate_dir)

    def test_an_unexpected_exception_releases_the_gate(
        self, db, store, tmp_path, gate_dir, monkeypatch
    ):
        batch_id = _batch(store, tmp_path)

        def boom(self, *args):
            raise KeyError("unexpected")

        monkeypatch.setattr(BatchRunner, "_loop", boom)
        result = _runner(db, Script()).start(batch_id)
        assert result.status == RESULT_FAILED and "KeyError" in result.error
        assert store.get_batch(batch_id).status == m.BATCH_INTERRUPTED
        assert not _busy(gate_dir)

    def test_cleanup_failures_never_hide_the_primary_error(
        self, db, store, tmp_path, gate_dir, monkeypatch
    ):
        batch_id = _batch(store, tmp_path)

        def boom(self, *args):
            raise KeyError("unexpected")

        def release_fails(self, batch_id, token):
            raise sqlite3.OperationalError("disk I/O error")

        def clear_fails(self):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(BatchRunner, "_loop", boom)
        monkeypatch.setattr(FlakyStore, "release_lease", release_fails, raising=False)
        monkeypatch.setattr(GateLease, "_clear_record", clear_fails)
        result = _runner(db, Script(), store_factory=FlakyStore.open).start(batch_id)
        assert result.status == RESULT_FAILED
        assert result.error == "The runner failed unexpectedly (KeyError)."
        assert len(result.cleanup_errors) == 2
        assert "lease could not be released" in result.cleanup_errors[0]
        assert "owner record could not be cleared" in result.cleanup_errors[1]
        assert not _busy(gate_dir)  # قفلِ سیستم‌عامل در هر حال آزاد شد

    def test_a_refused_runner_never_releases_the_owner_gate(self, db, store, tmp_path, gate_dir):
        first, second = _batch(store, tmp_path / "a"), _batch(store, tmp_path / "b")
        release = threading.Event()
        script = Script(gates={KEYS[0]: release})
        background = _Background(lambda: _runner(db, script).start(first))
        try:
            _wait(lambda: script.started[KEYS[0]].is_set())
            refused = _runner(db, Script()).start(second)
            assert refused.refusal == REFUSAL_GATE_BUSY
            assert _busy(gate_dir)  # ردشده هیچ چیزی را آزاد نکرد
            assert _gate(gate_dir).status().owner.ref == first
        finally:
            release.set()
        assert background.join().status == RESULT_COMPLETED
