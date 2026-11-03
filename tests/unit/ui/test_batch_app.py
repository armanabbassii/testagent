"""
تست‌های صفحه‌ی Batch Testing (ui/batch_app.py) روی runtimeِ واقعیِ Streamlit.

پایگاهِ موقت، Jiraِ ساختگی (فقط کشف و فهرستِ بُردها) و IssueExecutorِ ساختگی؛ هیچ
Jira، LLM یا Newmanِ واقعی‌ای صدا زده نمی‌شود. نکته‌ی اصلی: rerun یا صفحه‌ی تازه
هرگز runnerِ دوم نمی‌سازد و وضعیت از SQLite بازسازی می‌شود.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

streamlit = pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from src.batch_testing import models as m  # noqa: E402
from src.batch_testing import runner as runner_module  # noqa: E402
from src.batch_testing.service import (  # noqa: E402
    BatchService,
    configuration_snapshot,
    create_batch,
    register_service,
    reset_services,
)
from src.batch_testing.storage import BatchStore  # noqa: E402
from src.task_sources.jira.agile import Board  # noqa: E402
from tests.unit.batch_testing.test_runner import WAIT_S, Script, _batch  # noqa: E402
from tests.unit.batch_testing.test_service import DISCOVERY, _Factory  # noqa: E402
from ui import batch_app  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PAGE = "from ui import batch_app\nbatch_app.render()\n"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("No real Jira, executor or default database in unit tests.")

    monkeypatch.setattr(runner_module, "_default_executor", forbidden)
    monkeypatch.setattr(batch_app, "jira_session", forbidden)
    monkeypatch.setattr(batch_app, "DB_PATH", tmp_path / "batch.sqlite3")
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
def script() -> Script:
    return Script(gates={"DEMO-3": threading.Event()})


@pytest.fixture
def hosted(db: Path, script: Script):
    factory = _Factory(script)
    service = register_service(BatchService(db, runner_factory=factory, recover=lambda s: []))
    yield service, factory
    for gate in script.gates.values():
        gate.set()
    assert service.wait(WAIT_S)


def _page(view: str | None = None) -> AppTest:
    app = AppTest.from_string(_PAGE, default_timeout=60)
    if view:
        app.session_state["batch_view"] = view
    return app.run()


def _button(app: AppTest, key: str):
    matches = [button for button in app.button if button.key == key]
    assert matches, f"button {key!r} not rendered"
    return matches[0]


def _keys(app: AppTest) -> set[str]:
    return {button.key for button in app.button}


class TestNavigation:
    def test_the_wizard_stays_the_default_page(self):
        app = AppTest.from_file(str(_REPO_ROOT / "ui" / "app.py"), default_timeout=120).run()
        assert not app.exception
        assert app.title[0].value == "Test case generator"

    def test_the_batch_page_opens_without_calling_jira(self, hosted):
        app = _page()
        assert not app.exception
        assert app.title[0].value == "Batch testing"
        assert app.session_state["batch_view"] == batch_app.VIEW_NEW
        assert "batch_load_boards" in _keys(app)


class TestBatchCreation:
    def test_board_discovery_and_creation(self, hosted, db, tmp_path, monkeypatch):
        environment, globals_path = tmp_path / "env.json", tmp_path / "globals.json"
        environment.write_text("{}", encoding="utf-8")
        globals_path.write_text("{}", encoding="utf-8")
        calls = []

        @contextmanager
        def fake_session():
            calls.append("session")
            yield object()

        monkeypatch.setattr(batch_app, "jira_session", fake_session)
        monkeypatch.setattr(
            batch_app,
            "list_boards",
            lambda client, name="": [Board(id=42, name="Podium", type="scrum", project_key="PO")],
        )
        monkeypatch.setattr(batch_app, "discover", lambda client, board_id: DISCOVERY)
        snapshot = configuration_snapshot(environment, globals_path)
        monkeypatch.setattr(batch_app, "configuration_snapshot", lambda: snapshot)
        monkeypatch.setattr(
            batch_app,
            "create_batch",
            lambda store, result, excluded: create_batch(
                store,
                result,
                excluded,
                configuration=configuration_snapshot(environment, globals_path),
                artifact_root=tmp_path / "artifacts",
            ),
        )

        app = _page()
        _button(app, "batch_load_boards").click().run()
        assert app.selectbox(key="batch_board").value.id == 42
        _button(app, "batch_discover").click().run()
        assert not app.exception
        create = _button(app, "batch_create")
        assert create.label == "Create batch with 3 issues" and not create.disabled
        assert "SHA-256" in " ".join(item.value for item in app.markdown)
        create.click().run()
        assert not app.exception
        assert app.session_state["batch_view"] == batch_app.VIEW_BATCH
        with BatchStore.open(db) as store:
            (batch,) = store.list_batches()
            assert batch.status == m.BATCH_CREATED
            assert [issue.issue_key for issue in store.batch_issues(batch.id)] == [
                "DEMO-2",
                "DEMO-1",
                "DEMO-5",
            ]
            assert batch.environment_sha256 == snapshot.environment_sha256
        assert calls == ["session", "session"]  # فهرستِ بُردها + کشف — هیچ چیزِ دیگری

    def test_a_configuration_error_blocks_creation(self, hosted, tmp_path, monkeypatch):
        from src.batch_testing.service import ConfigurationError

        @contextmanager
        def fake_session():
            yield object()

        def missing():
            raise ConfigurationError("The globals file is missing or unreadable.")

        monkeypatch.setattr(batch_app, "jira_session", fake_session)
        monkeypatch.setattr(
            batch_app, "list_boards", lambda client, name="": [Board(42, "Podium", "scrum")]
        )
        monkeypatch.setattr(batch_app, "discover", lambda client, board_id: DISCOVERY)
        monkeypatch.setattr(batch_app, "configuration_snapshot", missing)
        app = _page()
        _button(app, "batch_load_boards").click().run()
        _button(app, "batch_discover").click().run()
        assert _button(app, "batch_create").disabled
        assert any("globals file is missing" in error.value for error in app.error)


class TestRunnerHosting:
    def test_start_launches_one_runner_and_reruns_never_duplicate_it(
        self, hosted, db, tmp_path, script
    ):
        service, factory = hosted
        with BatchStore.open(db) as store:
            batch_id = _batch(store, tmp_path)
        app = _page(batch_app.VIEW_BATCH)
        _button(app, "batch_start").click().run()
        assert not app.exception
        script.started["DEMO-3"].wait(WAIT_S)
        for _ in range(3):
            app.run()
        assert len(factory.runners) == 1
        assert "batch_start" not in _keys(app)  # RUNNING: دیگر دکمه‌ی Start نیست

        refreshed = _page()  # صفحه‌ی تازه، session خالی — وضعیت از SQLite
        assert not refreshed.exception
        assert refreshed.session_state["batch_view"] == batch_app.VIEW_BATCH
        assert refreshed.session_state["batch_selected_id"] == batch_id
        assert "batch_start" not in _keys(refreshed)
        assert {"batch_pause", "batch_cancel"} <= _keys(refreshed)
        assert len(factory.runners) == 1

        script.gates["DEMO-3"].set()
        assert service.wait(WAIT_S)
        with BatchStore.open(db) as store:
            assert store.get_batch(batch_id).status == m.BATCH_COMPLETED

    def test_pause_and_cancel_go_to_the_running_batch(self, hosted, db, tmp_path, script):
        service, factory = hosted
        hold = script.hold_after_cancel = threading.Event()  # runner لغو را هنوز تأیید نکند
        with BatchStore.open(db) as store:
            batch_id = _batch(store, tmp_path)
        assert service.start(batch_id).ok
        script.started["DEMO-3"].wait(WAIT_S)
        app = _page(batch_app.VIEW_BATCH)
        _button(app, "batch_pause").click().run()
        with BatchStore.open(db) as store:
            assert store.get_batch(batch_id).requested_action == m.ACTION_PAUSE
        assert _button(app, "batch_pause").disabled
        _button(app, "batch_cancel").click().run()
        assert script.saw_cancel.wait(WAIT_S)
        app.run()
        with BatchStore.open(db) as store:
            batch = store.get_batch(batch_id)
            assert batch.requested_action == m.ACTION_CANCEL
            assert batch.status == m.BATCH_RUNNING  # تا تأییدِ runner هنوز لغو نشده
        assert any("waiting for the runner" in w.value.lower() for w in app.warning)
        hold.set()
        assert service.wait(WAIT_S)
        with BatchStore.open(db) as store:
            assert store.get_batch(batch_id).status == m.BATCH_CANCELLED
        assert len(factory.runners) == 1

    def test_retry_and_its_cancellation_target_the_retry_runner(self, hosted, db, tmp_path, script):
        service, factory = hosted
        script.gates.clear()
        with BatchStore.open(db) as store:
            batch_id = _batch(store, tmp_path)
        assert service.start(batch_id).ok and service.wait(WAIT_S)
        script.gates["DEMO-1"] = threading.Event()
        assert service.retry(batch_id, "DEMO-1").ok
        app = _page(batch_app.VIEW_BATCH)
        cancel = _button(app, "batch_cancel")
        assert cancel.label == "Cancel retry"
        cancel.click().run()
        assert factory.runners[-1]._cancel_requested.is_set()
        assert not factory.runners[0]._cancel_requested.is_set()
        assert service.wait(WAIT_S)
        with BatchStore.open(db) as store:
            statuses = [e.status for e in store.executions(batch_id, "DEMO-1")]
            assert statuses == [m.EXEC_PASS, m.EXEC_CANCELLED]
            assert store.get_batch(batch_id).status == m.BATCH_COMPLETED


class TestHistory:
    def test_history_lists_batches_with_their_counts(self, hosted, db, tmp_path, script):
        service, _ = hosted
        script.gates.clear()
        with BatchStore.open(db) as store:
            batch_id = _batch(store, tmp_path)
        service.start(batch_id)
        assert service.wait(WAIT_S)
        app = _page(batch_app.VIEW_HISTORY)
        assert not app.exception
        table = app.dataframe[0].value
        row = table.iloc[0]
        assert (row["Status"], row["Issues"], row["PASS"]) == (m.BATCH_COMPLETED, 3, 3)
        assert row["Batch"] == batch_id[:8]


_DETAILS = """
from ui import batch_app
from src.batch_testing.summary import issue_rows

svc = batch_app.service()
with svc.open_store() as store:
    batch = store.get_batch({batch_id!r})
    rows = issue_rows(store.batch_issues(batch.id), store.executions(batch.id))
batch_app._render_issue_details(svc, batch, next(r for r in rows if r.issue_key == "DEMO-1"))
"""


class TestIssueDetails:
    def _completed(self, hosted, db, tmp_path, script) -> str:
        service, _ = hosted
        script.gates.clear()
        script.outcomes["DEMO-1"] = [m.EXEC_ERROR]
        with BatchStore.open(db) as store:
            batch_id = _batch(store, tmp_path)
        assert service.start(batch_id).ok and service.wait(WAIT_S)
        return batch_id

    def test_attempts_errors_and_only_published_artifacts_are_shown(
        self, hosted, db, tmp_path, script
    ):
        batch_id = self._completed(hosted, db, tmp_path, script)
        with BatchStore.open(db) as store:
            (execution,) = store.executions(batch_id, "DEMO-1")
        Path(execution.artifact_dir, "execution_summary.json").write_text("{}", encoding="utf-8")
        app = AppTest.from_string(_DETAILS.format(batch_id=batch_id), default_timeout=60).run()
        assert not app.exception
        assert any("NEWMAN_ERROR" in error.value for error in app.error)
        downloads = app.get("download_button")
        assert len(downloads) == 1  # فقط فایلِ منتشرشده؛ marker.txt فهرستِ مجاز نیست
        assert "Unavailable for this attempt" in " ".join(c.value for c in app.caption)

    def test_retry_creates_a_new_linked_attempt(self, hosted, db, tmp_path, script):
        service, factory = hosted
        batch_id = self._completed(hosted, db, tmp_path, script)
        app = AppTest.from_string(_DETAILS.format(batch_id=batch_id), default_timeout=60).run()
        retry = _button(app, f"batch_retry_{batch_id}_DEMO-1")
        assert not retry.disabled
        retry.click().run()
        assert service.wait(WAIT_S)
        assert len(factory.runners) == 2
        with BatchStore.open(db) as store:
            first, second = store.executions(batch_id, "DEMO-1")
            assert (first.status, second.status) == (m.EXEC_ERROR, m.EXEC_PASS)
            assert (second.attempt, second.trigger) == (2, m.TRIGGER_RETRY)
            assert second.previous_execution_id == first.id
            assert len(store.executions(batch_id)) == 4  # فقط همین issue دوباره اجرا شد
        app.run()
        assert app.get("expander")[0].proto.label.startswith("Attempt 2")

    def test_retry_is_disabled_while_the_batch_is_unfinished(self, hosted, db, tmp_path):
        with BatchStore.open(db) as store:
            batch_id = _batch(store, tmp_path)
            execution = store.create_execution(batch_id, "DEMO-1", trigger=m.TRIGGER_INITIAL)
            store.start_execution(execution.id)
            store.finish_execution(execution.id, m.EXEC_PASS)
        app = AppTest.from_string(_DETAILS.format(batch_id=batch_id), default_timeout=60).run()
        assert _button(app, f"batch_retry_{batch_id}_DEMO-1").disabled


class TestInactiveCancellationUi:
    def test_a_created_batch_is_cancelled_after_confirmation(self, hosted, db, tmp_path):
        service, factory = hosted
        with BatchStore.open(db) as store:
            batch_id = _batch(store, tmp_path)
        app = _page(batch_app.VIEW_BATCH)
        assert "batch_cancel" not in _keys(app)  # بدونِ runner: نه درخواستِ پایگاه
        _button(app, "batch_cancel_inactive").click().run()
        assert not app.exception
        assert any("The batch was cancelled" in item.value for item in app.success)
        with BatchStore.open(db) as store:
            assert store.get_batch(batch_id).status == m.BATCH_CANCELLED
            assert store.executions(batch_id) == []
        assert factory.runners == []
        assert "batch_cancel_inactive" not in _keys(app)

    def test_a_retry_in_another_process_is_not_offered_for_cancellation(
        self, hosted, db, tmp_path, script
    ):
        service, _ = hosted
        script.gates.clear()
        with BatchStore.open(db) as store:
            batch_id = _batch(store, tmp_path)
        assert service.start(batch_id).ok and service.wait(WAIT_S)
        with BatchStore.open(db) as store:
            store.acquire_lease(batch_id, "other-process", pid=1, host="pc")  # retry ِ دیگری
        app = _page(batch_app.VIEW_BATCH)
        assert not {"batch_cancel", "batch_cancel_inactive"} & _keys(app)
        assert any("another server process" in item.value for item in app.caption)

    def test_a_leftover_running_execution_is_explained_with_the_resume_path(
        self, hosted, db, tmp_path
    ):
        service, factory = hosted
        with BatchStore.open(db) as store:
            batch_id = _batch(store, tmp_path)
            store.acquire_lease(batch_id, "gone", pid=1, host="pc")
            store.transition_batch(batch_id, m.BATCH_RUNNING, owner_token="gone")
            execution = store.create_execution(batch_id, "DEMO-3", trigger=m.TRIGGER_INITIAL)
            running = store.start_execution(execution.id)
            store.transition_batch(batch_id, m.BATCH_INTERRUPTED, owner_token="gone")
            store.release_lease(batch_id, "gone")
        app = _page(batch_app.VIEW_BATCH)
        assert not app.exception
        (warning,) = [w.value for w in app.warning if "still marked RUNNING" in w.value]
        assert "DEMO-3" in warning and "Use Resume" in warning
        assert not _button(app, "batch_resume").disabled
        # هیچ خطی آن را «در حالِ اجرا» نشان نمی‌دهد
        assert not any(":material/play_circle:" in item.value for item in app.markdown)
        with BatchStore.open(db) as store:
            assert store.get_execution(running.id) == running
            assert store.get_batch(batch_id).status == m.BATCH_INTERRUPTED
        assert factory.runners == []


class TestDiscoveryEligibilityUi:
    @staticmethod
    def _discover(app_monkeypatch, result):
        @contextmanager
        def fake_session():
            yield object()

        app_monkeypatch.setattr(batch_app, "jira_session", fake_session)
        app_monkeypatch.setattr(
            batch_app, "list_boards", lambda client, name="": [Board(42, "Podium", "scrum")]
        )
        app_monkeypatch.setattr(batch_app, "discover", lambda client, board_id: result)
        app = _page()
        _button(app, "batch_load_boards").click().run()
        _button(app, "batch_discover").click().run()
        assert not app.exception
        return app

    def test_an_issue_outside_the_active_sprint_is_offered_and_sprint_is_informational(
        self, hosted, monkeypatch
    ):
        from dataclasses import replace

        outside = replace(DISCOVERY.candidates[3], in_active_sprint=False)  # DEMO-5
        result = replace(DISCOVERY, candidates=(*DISCOVERY.candidates[:3], outside))
        app = self._discover(monkeypatch, result)
        assert _button(app, "batch_create").label == "Create batch with 3 issues"
        captions = " ".join(item.value for item in app.caption)
        assert "for information only" in captions and "does not affect eligibility" in captions
        assert batch_app._sprint_cell(outside) == {"Active sprint": "No"}

    def test_a_kanban_board_has_no_sprint_column(self, hosted, monkeypatch):
        from dataclasses import replace

        kanban = replace(
            DISCOVERY,
            context=replace(DISCOVERY.context, board_type="kanban", active_sprint_ids=None),
            candidates=tuple(replace(c, in_active_sprint=None) for c in DISCOVERY.candidates),
        )
        app = self._discover(monkeypatch, kanban)
        assert _button(app, "batch_create").label == "Create batch with 3 issues"
        assert "information only" not in " ".join(item.value for item in app.caption)
        assert batch_app._sprint_cell(kanban.candidates[0]) == {}
