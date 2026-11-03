"""
تست‌های ساختِ batch و میزبانیِ runner برای UI (Milestone 4.6).

پایگاهِ SQLiteِ موقت، فایل‌های Environment/Globalsِ موقت و IssueExecutorِ ساختگی؛
هیچ Jira، LLM یا Newmanِ واقعی‌ای صدا زده نمی‌شود و فایل‌های واقعیِ پیکربندی فقط
خوانده می‌شوند (برای بررسیِ مسیرِ پیش‌فرض).
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from src.batch_testing import models as m
from src.batch_testing import runner as runner_module
from src.batch_testing import service as service_module
from src.batch_testing.discovery import (
    DEFAULT_CRITERIA,
    BoardContext,
    Candidate,
    DiscoveryResult,
)
from src.batch_testing.recovery import recover_stale
from src.batch_testing.runner import BatchRunner, configuration_problem, file_sha256
from src.batch_testing.service import (
    MODE_RETRY,
    BatchService,
    ConfigurationError,
    build_new_batch,
    configuration_snapshot,
    create_batch,
    get_service,
    register_service,
    reset_services,
)
from src.batch_testing.storage import REPO_ROOT, BatchStore
from src.test_execution.newman import DEV_ENVIRONMENT_PATH, WORKSPACE_GLOBALS_PATH
from tests.unit.batch_testing.test_runner import (
    KEYS,
    WAIT_S,
    FakeExecutor,
    FakeTokenSource,
    Script,
    _batch,
    _dead_runner,
    _future,
    _wait,
)

SECRET = "s3cr3t-api-key-value"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    def forbidden(on_phase):
        raise AssertionError("The real IssueExecutor must not be used in unit tests.")

    monkeypatch.setattr(runner_module, "_default_executor", forbidden)
    reset_services()
    yield
    reset_services()
    alive = [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith(("batch-runner-monitor", "batch-runner-host"))
    ]
    assert alive == [], alive


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "batch.sqlite3"


@pytest.fixture
def store(db: Path):
    with BatchStore.open(db) as opened:
        yield opened


@pytest.fixture
def postman_files(tmp_path: Path) -> tuple[Path, Path]:
    environment, globals_path = tmp_path / "dev.json", tmp_path / "globals.json"
    environment.write_text(
        '{"values": [{"key": "apiKey", "value": "%s"}]}' % SECRET, encoding="utf-8"
    )
    globals_path.write_text('{"values": []}', encoding="utf-8")
    return environment, globals_path


# ── کشفِ ساختگی ──────────────────────────────────────────────────────────────
def _candidate(
    key: str, *failures: tuple[str, str], issue_type: str = "Story", parent: str = ""
) -> Candidate:
    return Candidate(
        key=key,
        summary=f"Summary {key}",
        status_id="10",
        status_name="Manual Test",
        labels=("backend",),
        issue_type=issue_type,
        is_subtask=bool(parent),
        parent_key=parent,
        in_active_sprint=True,
        reasons=tuple(reason for _, reason in failures),
        failed_criteria=tuple(criterion for criterion, _ in failures),
    )


def _discovery(*candidates: Candidate) -> DiscoveryResult:
    context = BoardContext(
        board_id=42,
        board_name="Podium",
        board_type="scrum",
        filter_id="100",
        sub_query="",
        column_name="QC Test",
        column_status_ids=frozenset({"10"}),
        target_status_ids=frozenset({"10"}),
        active_sprint_ids=(7,),
    )
    return DiscoveryResult(context=context, criteria=DEFAULT_CRITERIA, candidates=candidates)


DISCOVERY = _discovery(
    _candidate("DEMO-2"),
    _candidate("DEMO-9", ("label", "label 'backend' is missing")),
    _candidate("DEMO-1", issue_type="Sub-task", parent="DEMO-2"),
    _candidate("DEMO-5"),
)


def _new(postman_files, tmp_path, result=DISCOVERY, excluded=()):
    environment, globals_path = postman_files
    return build_new_batch(
        result,
        excluded,
        configuration=configuration_snapshot(environment, globals_path),
        artifact_root=tmp_path / "artifacts",
    )


# ── ساختِ batch ──────────────────────────────────────────────────────────────
class TestBatchCreation:
    def test_every_eligible_issue_is_selected_in_discovery_order(self, postman_files, tmp_path):
        new = _new(postman_files, tmp_path)
        assert [issue.issue_key for issue in new.issues] == ["DEMO-2", "DEMO-1", "DEMO-5"]
        assert (new.board_id, new.board_name, new.board_type) == (42, "Podium", "scrum")
        assert new.criteria == {"column": "QC TEST", "status": "Manual Test", "label": "backend"}
        sub_task = new.issues[1]
        assert (sub_task.issue_type, sub_task.parent_key) == ("Sub-task", "DEMO-2")

    def test_user_exclusions_are_recorded_and_the_snapshot_is_kept(self, postman_files, tmp_path):
        new = _new(postman_files, tmp_path, excluded={"DEMO-1"})
        assert [issue.issue_key for issue in new.issues] == ["DEMO-2", "DEMO-5"]
        assert new.discovery["eligible"] == ["DEMO-2", "DEMO-1", "DEMO-5"]
        assert new.discovery["user_excluded"] == ["DEMO-1"]
        assert new.discovery["selected"] == ["DEMO-2", "DEMO-5"]
        (ineligible,) = new.discovery["excluded"]
        assert ineligible["key"] == "DEMO-9"
        assert ineligible["reasons"] == [
            {"criterion": "label", "reason": "label 'backend' is missing"}
        ]
        assert "summary" not in ineligible  # ردیابیِ ساختاری، بدونِ متنِ آزادِ Jira

    def test_an_ineligible_issue_cannot_be_excluded_or_selected(self, postman_files, tmp_path):
        with pytest.raises(ValueError, match="Only eligible"):
            _new(postman_files, tmp_path, excluded={"DEMO-9"})

    @pytest.mark.parametrize(
        "result, excluded",
        [
            (DISCOVERY, {"DEMO-2", "DEMO-1", "DEMO-5"}),
            (
                _discovery(_candidate("DEMO-9", ("status", "status is 'Done', not 'Manual Test'"))),
                (),
            ),
        ],
    )
    def test_an_empty_batch_is_refused(self, postman_files, tmp_path, result, excluded):
        with pytest.raises(ValueError, match="at least one"):
            _new(postman_files, tmp_path, result=result, excluded=excluded)

    def test_hashes_are_sha256_of_the_raw_bytes_and_files_are_untouched(
        self, store, postman_files, tmp_path
    ):
        environment, globals_path = postman_files
        before = (environment.read_bytes(), globals_path.read_bytes())
        batch = store.create_batch(_new(postman_files, tmp_path))
        assert batch.environment_sha256 == hashlib.sha256(before[0]).hexdigest()
        assert batch.globals_sha256 == file_sha256(globals_path)
        assert Path(batch.environment_path) == environment.resolve()
        assert Path(batch.globals_path) == globals_path.resolve()
        assert configuration_problem(batch) == ""  # همان قراردادِ اعتبارسنجیِ runner
        assert (environment.read_bytes(), globals_path.read_bytes()) == before
        assert [issue.issue_key for issue in store.batch_issues(batch.id)] == [
            "DEMO-2",
            "DEMO-1",
            "DEMO-5",
        ]

    @pytest.mark.parametrize("missing", ["environment", "globals"])
    def test_a_missing_file_prevents_batch_creation(
        self, store, postman_files, tmp_path, monkeypatch, missing
    ):
        environment, globals_path = postman_files
        (environment if missing == "environment" else globals_path).unlink()
        # create_batch هش را خودش همان لحظه می‌گیرد (مسیرها: فایل‌های موقتِ این تست)
        monkeypatch.setattr(
            service_module,
            "configuration_snapshot",
            lambda: configuration_snapshot(environment, globals_path),
        )
        with pytest.raises(ConfigurationError, match=f"The {missing} file is missing"):
            create_batch(store, DISCOVERY, artifact_root=tmp_path / "artifacts")
        assert store.list_batches() == []

    @pytest.mark.parametrize("prefix", [b'{"values": [', b'["x", "', b"\xff\xfe"])
    def test_an_invalid_file_is_refused_without_exposing_its_content(self, postman_files, prefix):
        environment, globals_path = postman_files
        environment.write_bytes(prefix + SECRET.encode() + (b'"]' if b"x" in prefix else b""))
        before = environment.read_bytes()
        with pytest.raises(ConfigurationError) as raised:
            configuration_snapshot(environment, globals_path)
        assert SECRET not in str(raised.value)
        # استثنای decode (با بایت‌های فایل در repr) همراهِ خطا نیست
        assert raised.value.__cause__ is None and raised.value.__context__ is None
        assert environment.read_bytes() == before

    def test_the_default_paths_are_the_repository_configuration(self):
        snapshot = configuration_snapshot()  # فقط خواندن
        assert Path(snapshot.environment_path) == DEV_ENVIRONMENT_PATH.resolve()
        assert Path(snapshot.globals_path) == WORKSPACE_GLOBALS_PATH.resolve()
        assert REPO_ROOT.resolve() in Path(snapshot.environment_path).parents
        assert snapshot.environment_sha256 == file_sha256(DEV_ENVIRONMENT_PATH)


# ── میزبانیِ runner ──────────────────────────────────────────────────────────
class _Factory:
    """runnerهای واقعی با IssueExecutorِ ساختگی؛ همه‌ی نمونه‌ها نگه داشته می‌شوند."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.runners: list[BatchRunner] = []

    def __call__(self, db_path: Path) -> BatchRunner:
        runner = BatchRunner(
            db_path,
            executor_factory=lambda on_phase: FakeExecutor(self.script, on_phase),
            token_provider=FakeTokenSource(),
            heartbeat_interval=0.02,
            poll_interval=0.005,
        )
        self.runners.append(runner)
        return runner


def _service(db: Path, script: Script, **options) -> tuple[BatchService, _Factory]:
    factory = _Factory(script)
    options.setdefault("recover", lambda store: [])
    return BatchService(db, runner_factory=factory, **options), factory


def _finish(service: BatchService) -> None:
    assert service.wait(WAIT_S)


class TestRunnerHosting:
    def test_start_runs_in_the_background_with_one_runner(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script = Script(gates={"DEMO-3": threading.Event()})
        service, factory = _service(db, script)
        started = service.start(batch_id)
        assert started.ok and service.busy
        assert store.get_batch(batch_id).owner_token == factory.runners[0].token
        script.gates["DEMO-3"].set()
        _finish(service)
        assert len(factory.runners) == 1
        assert service.job.result.status == "completed"
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_COMPLETED, None)

    def test_a_second_launch_while_running_creates_no_runner(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        other = _batch(store, tmp_path / "other")
        script = Script(gates={"DEMO-3": threading.Event()})
        service, factory = _service(db, script)
        assert service.start(batch_id).ok
        for attempt in (
            lambda: service.start(batch_id),
            lambda: service.start(other),
            lambda: service.resume(batch_id),
            lambda: service.retry(batch_id, "DEMO-1"),
        ):
            refused = attempt()
            assert not refused.ok and "already in progress" in refused.message
        assert len(factory.runners) == 1
        script.gates["DEMO-3"].set()
        _finish(service)
        assert store.get_batch(other).status == m.BATCH_CREATED

    def test_concurrent_start_clicks_launch_exactly_one_runner(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script = Script(gates={"DEMO-3": threading.Event()})
        service, factory = _service(db, script)
        barrier = threading.Barrier(4)
        results = []

        def click() -> None:
            barrier.wait()
            results.append(service.start(batch_id))

        threads = [threading.Thread(target=click) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WAIT_S)
        assert sum(result.ok for result in results) == 1
        assert len(factory.runners) == 1
        script.gates["DEMO-3"].set()
        _finish(service)

    def test_the_runner_refusal_is_reported_and_nothing_changes(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        service, _ = _service(db, Script())
        assert service.start(batch_id).ok
        _finish(service)
        again = service.start(batch_id)
        assert not again.ok and "Only a CREATED batch" in again.message
        _finish(service)
        assert store.get_batch(batch_id).status == m.BATCH_COMPLETED

    def test_reruns_and_refreshes_reuse_the_process_service(self, db, store, tmp_path):
        recoveries = []
        first = get_service(db, recover=lambda s: recoveries.append(1) or [])
        assert get_service(db) is first and get_service(str(db)) is first
        assert recoveries == [1]  # بازیابیِ آغازین فقط یک بار

    def test_a_refresh_sees_the_running_batch_and_cannot_start_it_again(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script = Script(gates={"DEMO-3": threading.Event()})
        service, factory = _service(db, script)
        register_service(service)
        try:
            assert get_service(db).start(batch_id).ok
            # start() پس از گرفتنِ lease برمی‌گردد؛ RUNNING کمی بعد ثبت می‌شود
            assert script.started["DEMO-3"].wait(WAIT_S)
            refreshed = get_service(db)  # صفحه‌ی تازه: همان سرویس، وضعیت از SQLite
            assert refreshed is service and refreshed.job_for(batch_id).running
            with BatchStore.open(db) as reader:
                assert reader.get_batch(batch_id).status == m.BATCH_RUNNING
            assert not refreshed.start(batch_id).ok
        finally:
            script.gates["DEMO-3"].set()  # شکستِ بالا runner را برای تست‌های بعد رها نکند
        _finish(service)
        assert len(factory.runners) == 1

    def test_a_separate_service_cannot_take_the_batch_from_a_live_runner(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script = Script(gates={"DEMO-3": threading.Event()})
        service, _ = _service(db, script)
        assert service.start(batch_id).ok
        other_process, _ = _service(db, script)
        assert not other_process.resume(batch_id).ok
        assert not other_process.start(batch_id).ok
        other_process.wait(WAIT_S)
        script.gates["DEMO-3"].set()
        _finish(service)

    def test_the_worker_thread_ends_and_releases_the_lease(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        service, _ = _service(db, Script())
        service.start(batch_id)
        _finish(service)
        assert not service.job.thread.is_alive()
        assert store.get_batch(batch_id).owner_token is None


class TestControls:
    def test_pause_is_requested_through_the_database(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script = Script(gates={"DEMO-3": threading.Event()})
        service, _ = _service(db, script)
        service.start(batch_id)
        script.started["DEMO-3"].wait(WAIT_S)
        paused = service.pause(batch_id)
        assert paused.ok and "current issue finishes" in paused.message
        assert store.get_batch(batch_id).requested_action == m.ACTION_PAUSE
        assert store.get_batch(batch_id).status == m.BATCH_RUNNING  # هنوز تأیید نشده
        script.gates["DEMO-3"].set()
        _finish(service)
        assert store.get_batch(batch_id).status == m.BATCH_PAUSED
        assert [call[0] for call in script.calls] == ["DEMO-3"]

    def test_cancel_is_requested_and_only_the_runner_confirms_it(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        hold = threading.Event()
        script = Script(gates={"DEMO-3": threading.Event()}, hold_after_cancel=hold)
        service, _ = _service(db, script)
        service.start(batch_id)
        script.started["DEMO-3"].wait(WAIT_S)
        cancelled = service.cancel(batch_id)
        assert cancelled.ok and "Waiting for the runner" in cancelled.message
        script.saw_cancel.wait(WAIT_S)
        assert store.get_batch(batch_id).status == m.BATCH_RUNNING  # لغو هنوز تأیید نشده
        hold.set()
        _finish(service)
        assert store.get_batch(batch_id).status == m.BATCH_CANCELLED
        assert service.job.result.status == "cancelled"

    def test_retry_cancellation_targets_the_exact_active_runner(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script = Script()
        service, factory = _service(db, script)
        service.start(batch_id)
        _finish(service)
        script.gates["DEMO-1"] = threading.Event()
        retry = service.retry(batch_id, "DEMO-1")
        assert retry.ok and retry.job.mode == MODE_RETRY
        script.started["DEMO-1"].clear()
        _wait(lambda: len(script.calls) == 4)
        first_runner, retry_runner = factory.runners
        cancelled = service.cancel(batch_id)
        assert cancelled.ok and cancelled.job.runner is retry_runner
        assert retry_runner._cancel_requested.is_set()
        assert not first_runner._cancel_requested.is_set()
        _finish(service)
        attempts = store.executions(batch_id, "DEMO-1")
        assert [execution.status for execution in attempts] == [m.EXEC_PASS, m.EXEC_CANCELLED]
        assert attempts[1].previous_execution_id == attempts[0].id
        assert store.get_batch(batch_id).status == m.BATCH_COMPLETED
        assert service.cancel(batch_id).ok is False  # پس از پایان: هیچ ادعای لغوی

    def test_a_retry_hosted_elsewhere_is_not_cancelled_from_here(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        service, _ = _service(db, Script())
        service.start(batch_id)
        _finish(service)
        store.acquire_lease(batch_id, "other-process", pid=1, host="pc")
        refused = service.cancel(batch_id)
        assert not refused.ok and "another application process" in refused.message
        assert store.get_batch(batch_id).requested_action == m.ACTION_NONE

    def test_pause_needs_a_live_runner(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _dead_runner(store, batch_id)
        service, _ = _service(db, Script(), stale_after=0.0)
        refused = service.pause(batch_id)
        assert not refused.ok and "No live runner" in refused.message
        assert store.get_batch(batch_id).requested_action == m.ACTION_NONE

    def test_pause_is_refused_for_a_batch_that_is_not_running(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        service, _ = _service(db, Script())
        refused = service.pause(batch_id)
        assert not refused.ok
        assert store.get_batch(batch_id).requested_action == m.ACTION_NONE

    def test_a_finished_batch_cannot_be_cancelled(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        service, _ = _service(db, Script())
        service.start(batch_id)
        _finish(service)
        refused = service.cancel(batch_id)
        assert not refused.ok and "already COMPLETED" in refused.message
        assert store.get_batch(batch_id).status == m.BATCH_COMPLETED


class TestInactiveCancellation:
    def test_a_created_batch_is_cancelled_before_anything_runs(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        service, factory = _service(db, Script())
        cancelled = service.cancel(batch_id)
        assert cancelled.ok and "0 queued issue(s)" in cancelled.message
        assert store.get_batch(batch_id).status == m.BATCH_CANCELLED
        assert store.executions(batch_id) == [] and factory.runners == []
        assert not service.start(batch_id).ok  # CANCELLED: هیچ اجرایی بعداً
        _finish(service)
        assert store.executions(batch_id) == []

    def test_a_paused_batch_is_cancelled_without_resuming(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script = Script(gates={"DEMO-3": threading.Event()})
        service, factory = _service(db, script)
        service.start(batch_id)
        script.started["DEMO-3"].wait(WAIT_S)
        service.pause(batch_id)
        script.gates["DEMO-3"].set()
        _finish(service)
        assert store.get_batch(batch_id).status == m.BATCH_PAUSED
        (done,) = store.executions(batch_id, "DEMO-3")
        cancelled = service.cancel(batch_id)
        assert cancelled.ok and "2 queued issue(s)" in cancelled.message
        assert store.get_batch(batch_id).status == m.BATCH_CANCELLED
        assert store.get_execution(done.id) == done  # PASS و artifactش دست‌نخورده
        assert Path(done.artifact_dir, "marker.txt").is_file()
        for key in ("DEMO-1", "DEMO-2"):
            assert [e.status for e in store.executions(batch_id, key)] == [m.EXEC_CANCELLED]
        assert len(factory.runners) == 1 and len(script.calls) == 1  # چیزی اجرا نشد
        assert store.get_batch(batch_id).owner_token is None

    def test_an_interrupted_batch_keeps_its_interrupted_attempt(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _dead_runner(store, batch_id)
        lost = store.start_execution(store.executions(batch_id, "DEMO-3")[0].id)
        service, factory = _service(
            db, Script(), recover=lambda s: recover_stale(s, clock=_future())
        )
        service.ensure_recovered()
        assert store.get_batch(batch_id).status == m.BATCH_INTERRUPTED
        interrupted = store.get_execution(lost.id)
        assert interrupted.status == m.EXEC_INTERRUPTED
        assert service.cancel(batch_id).ok
        assert store.get_execution(lost.id) == interrupted
        statuses = {e.issue_key: e.status for e in store.executions(batch_id)}
        assert statuses == {
            "DEMO-3": m.EXEC_INTERRUPTED,
            "DEMO-1": m.EXEC_CANCELLED,
            "DEMO-2": m.EXEC_CANCELLED,
        }
        assert store.get_batch(batch_id).status == m.BATCH_CANCELLED
        assert factory.runners == []

    def test_a_stale_lease_is_released_through_recovery_first(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        store.acquire_lease(batch_id, "dead", pid=1, host="gone")
        store.transition_batch(batch_id, m.BATCH_RUNNING, owner_token="dead")
        store.create_execution(batch_id, "DEMO-3", trigger=m.TRIGGER_INITIAL)
        store.transition_batch(batch_id, m.BATCH_PAUSED, owner_token="dead")  # lease مانده
        service, _ = _service(
            db,
            Script(),
            stale_after=-1.0,
            recover=lambda s: recover_stale(s, clock=_future()),
        )
        assert service.cancel(batch_id).ok
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_CANCELLED, None)

    @staticmethod
    def _leftover_running(store, tmp_path) -> tuple[str, m.Execution]:
        """runnerی که batch را INTERRUPTED کرد و lease را آزاد کرد ولی بستنِ اجرای DEMO-3
        (مثلاً با خطای ذخیره‌سازی) را نتوانست — اجرای RUNNING بی‌مالک می‌ماند."""
        batch_id = _batch(store, tmp_path)
        store.acquire_lease(batch_id, "gone", pid=1, host="pc")
        store.transition_batch(batch_id, m.BATCH_RUNNING, owner_token="gone")
        execution = store.create_execution(batch_id, "DEMO-3", trigger=m.TRIGGER_INITIAL)
        running = store.start_execution(execution.id)
        store.transition_batch(batch_id, m.BATCH_INTERRUPTED, owner_token="gone")
        store.release_lease(batch_id, "gone")
        return batch_id, running

    def test_a_leftover_running_execution_is_refused_with_a_recovery_path(
        self, db, store, tmp_path
    ):
        batch_id, running = self._leftover_running(store, tmp_path)
        service, factory = _service(db, Script())
        refused = service.cancel(batch_id)
        assert not refused.ok
        assert "DEMO-3 is still marked RUNNING" in refused.message
        assert "Use Resume" in refused.message
        assert store.get_execution(running.id) == running  # هرگز پایانی نشد
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_INTERRUPTED, None)
        assert factory.runners == []

    def test_resume_is_the_safe_recovery_path_for_a_leftover_running_execution(
        self, db, store, tmp_path
    ):
        batch_id, running = self._leftover_running(store, tmp_path)
        service, _ = _service(db, Script())
        assert service.resume(batch_id).ok
        _finish(service)
        attempts = store.executions(batch_id, "DEMO-3")
        assert [e.status for e in attempts] == [m.EXEC_INTERRUPTED, m.EXEC_PASS]
        assert attempts[0].id == running.id and attempts[1].previous_execution_id == running.id
        assert store.get_batch(batch_id).status == m.BATCH_COMPLETED

    def test_a_batch_held_by_a_live_runner_is_not_cancelled_directly(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        store.acquire_lease(batch_id, "alive", pid=1, host="pc")
        service, _ = _service(db, Script())
        refused = service.cancel(batch_id)
        assert not refused.ok and "holds this batch" in refused.message
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_CREATED, "alive")

    def test_cancel_and_start_racing_never_leave_a_half_cancelled_batch(self, db, store, tmp_path):
        for round_ in range(5):
            batch_id = _batch(store, tmp_path / f"r{round_}")
            script = Script(gates={"DEMO-3": threading.Event()})
            starter, _ = _service(db, script)
            canceller, _ = _service(db, script)
            barrier = threading.Barrier(2)
            outcome = {}

            def run(name, call):
                barrier.wait()
                outcome[name] = call()

            threads = [
                threading.Thread(target=run, args=("start", lambda: starter.start(batch_id))),
                threading.Thread(target=run, args=("cancel", lambda: canceller.cancel(batch_id))),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(WAIT_S)
            script.gates["DEMO-3"].set()
            _finish(starter)
            batch = store.get_batch(batch_id)
            executions = store.executions(batch_id)
            cancel = outcome["cancel"]
            if cancel.ok and "was cancelled" in cancel.message:
                # لغوِ مستقیم پیش از lease: runner هیچ اجرایی نساخت
                assert batch.status == m.BATCH_CANCELLED and executions == []
            elif cancel.ok:
                # runner اول lease گرفت: لغو درخواستِ پایگاه شد و runner آن را تأیید کرد
                assert "Waiting for the runner" in cancel.message
                assert batch.status == m.BATCH_CANCELLED
                statuses = {e.issue_key: e.status for e in executions}
                # issueِ جاری ممکن است پیش از اثرِ لغو تمام شده باشد — نتیجه‌ی واقعی‌اش می‌ماند
                assert statuses.pop("DEMO-3") in (m.EXEC_CANCELLED, m.EXEC_PASS)
                assert statuses == {"DEMO-1": m.EXEC_CANCELLED, "DEMO-2": m.EXEC_CANCELLED}
            else:
                assert batch.status in (m.BATCH_COMPLETED, m.BATCH_RUNNING)
                assert len(executions) == 3 and script.calls
            assert batch.owner_token is None


class TestStartupRecovery:
    def test_recovery_runs_once_per_service(self, db):
        calls = []
        service = BatchService(db, recover=lambda store: calls.append(1) or [])
        for _ in range(3):
            service.ensure_recovered()
        assert calls == [1]

    def test_a_healthy_runner_is_never_stolen(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        script = Script(gates={"DEMO-3": threading.Event()})
        service, factory = _service(db, script)
        service.start(batch_id)
        script.started["DEMO-3"].wait(WAIT_S)
        restarted = BatchService(db)  # پروسه‌ی تازه با بازیابیِ واقعی
        reports = restarted.ensure_recovered()
        assert not any(report.recovered for report in reports)
        batch = store.get_batch(batch_id)
        assert (batch.status, batch.owner_token) == (m.BATCH_RUNNING, factory.runners[0].token)
        script.gates["DEMO-3"].set()
        _finish(service)
        assert store.get_batch(batch_id).status == m.BATCH_COMPLETED

    def test_a_stale_runner_is_recovered_and_nothing_is_resumed(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _dead_runner(store, batch_id)
        execution = store.executions(batch_id, "DEMO-3")[0]
        store.start_execution(execution.id)
        script = Script()
        service, factory = _service(db, script, recover=lambda s: recover_stale(s, clock=_future()))
        (report,) = service.ensure_recovered()
        assert report.recovered and report.batch_status == m.BATCH_INTERRUPTED
        assert store.get_execution(execution.id).status == m.EXEC_INTERRUPTED
        assert factory.runners == [] and script.calls == [] and service.job is None
        assert {e.status for e in store.executions(batch_id)} == {
            m.EXEC_INTERRUPTED,
            m.EXEC_QUEUED,
        }
        assert set(KEYS) == {e.issue_key for e in store.executions(batch_id)}

    def test_a_stale_paused_batch_is_not_resumed(self, db, store, tmp_path):
        batch_id = _batch(store, tmp_path)
        _dead_runner(store, batch_id)
        store.transition_batch(batch_id, m.BATCH_PAUSED, expected=m.BATCH_RUNNING)
        script = Script()
        service, factory = _service(db, script, recover=lambda s: recover_stale(s, clock=_future()))
        service.ensure_recovered()
        assert store.get_batch(batch_id).status == m.BATCH_PAUSED
        assert factory.runners == [] and script.calls == []
