"""
tests/unit/ui/test_box_execution.py — بخش‌های «Run tests» و «Runs» در صفحه‌ی TestCase Box (M7.7)

با BoxRunService ِ واقعی (برنامه‌ریز، storage، Gate، runner) که برای پایگاهِ موقت ثبت می‌شود؛ موتور و
توکن ساختگی‌اند، Gate و artifactها در tmp_path. هیچ Newman/Node/HTTP/Jira/LLM و هیچ پایگاه یا Gate
ِ پیش‌فرضی لمس نمی‌شود (پیش‌فرض‌های واقعی در هر تست ممنوع‌اند).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

streamlit = pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from src.execution_gate import ExecutionGate  # noqa: E402
from src.testcase_box import models as m  # noqa: E402
from src.testcase_box import recovery  # noqa: E402
from src.testcase_box import run_service as run_service_module  # noqa: E402
from src.testcase_box import runner as runner_module  # noqa: E402
from src.testcase_box.models import NewCase  # noqa: E402
from src.testcase_box.profiles import HostIntent  # noqa: E402
from src.testcase_box.run_profile import RunProfile  # noqa: E402
from src.testcase_box.run_service import (  # noqa: E402
    BoxRunService,
    register_run_service,
    reset_run_services,
)
from src.testcase_box.runner import BoxRunner  # noqa: E402
from src.testcase_box.storage import BoxStore  # noqa: E402
from tests.unit.testcase_box.runner_fakes import (  # noqa: E402
    ENV_SECRET,
    TOKEN,
    FakeEngine,
    FakeTokens,
    blocking,
    outcome,
    write_configuration,
)
from tests.unit.testcase_box.test_run_planning import item  # noqa: E402
from ui import box_app  # noqa: E402

_PAGE = "from ui import box_app\nbox_app.render()\n"
STALE = 0.05


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("No real gate, engine or token provider in UI tests.")

    for name in ("_default_gate", "_default_engine", "_default_token_provider"):
        monkeypatch.setattr(runner_module, name, forbidden)
    monkeypatch.setattr(recovery, "_default_gate", forbidden)
    monkeypatch.setattr(runner_module, "DEFAULT_ARTIFACT_ROOT", tmp_path / "forbidden-runs")
    monkeypatch.setattr(box_app, "DB_PATH", tmp_path / "library" / "box.sqlite3")
    monkeypatch.setattr(box_app, "BATCH_ROOT", tmp_path / "artifacts")
    reset_run_services()
    yield
    service = run_service_module._services.get(box_app.DB_PATH.resolve())
    if service is not None:
        service.wait(30)
    reset_run_services()
    for lease in list(runner_module._UNRELEASED_GATES):
        runner_module._UNRELEASED_GATES.remove(lease)
        lease.release()
    assert not (tmp_path / "forbidden-runs").exists()


@pytest.fixture
def world(tmp_path):
    environment, globals_path = write_configuration(tmp_path / "config")
    box_app.DB_PATH.parent.mkdir(parents=True)
    store = BoxStore.open(box_app.DB_PATH)
    yield {
        "db": box_app.DB_PATH,
        "store": store,
        "environment": environment,
        "globals": globals_path,
        "gate_dir": tmp_path / "gate",
        "runs": tmp_path / "runs",
        "tmp": tmp_path,
    }
    store.close()


def _register(world, engine=None) -> BoxRunService:
    return register_run_service(
        BoxRunService(
            world["db"],
            environment_path=world["environment"],
            globals_path=world["globals"],
            gate_factory=lambda: ExecutionGate(world["gate_dir"]),
            artifact_root=world["runs"],
            stale_after=STALE,
            runner_options={
                "engine": engine if engine is not None else FakeEngine(),
                "token_provider": FakeTokens(),
                "heartbeat_interval": 0.02,
                "poll_interval": 0.005,
                "stale_after": 0.5,
            },
        )
    )


def _cases(world, *payloads):
    return [
        world["store"].create_case(NewCase(title=f"Case {n}", payload=p))
        for n, p in enumerate(payloads)
    ]


def _page(section: str, **state) -> AppTest:
    app = AppTest.from_string(_PAGE, default_timeout=60)
    app.session_state["box_section"] = section
    for key, value in state.items():
        app.session_state[key] = value
    return app.run()


def _texts(app: AppTest) -> str:
    parts = []
    for kind in ("markdown", "caption", "info", "warning", "error", "success", "subheader"):
        parts.extend(str(e.value) for e in getattr(app, kind))
    for frame in app.dataframe:
        parts.append(frame.value.to_csv())
    for metric in app.metric:
        parts.append(f"{metric.label}={metric.value}")
    return "\n".join(parts)


def _button(app: AppTest, prefix: str):
    matches = [b for b in app.button if str(b.key or "").startswith(prefix)]
    assert len(matches) == 1, [b.key for b in app.button]
    return matches[0]


def _planned(world, case_ids, host="NORMAL") -> AppTest:
    app = _page("Run tests", box_run_cases=list(case_ids), box_run_host=host)
    _button(app, "box_run_preview").click()
    return app.run()


def _start(world, case_ids) -> tuple[AppTest, int]:
    app = _planned(world, case_ids)
    _button(app, "box_run_start_").click()
    app.run()
    service = run_service_module._services[world["db"].resolve()]
    assert service.wait(30)
    return app, service.job.result.run_id


class TestSections:
    def test_library_stays_the_default_and_execution_sections_exist(self, world):
        app = AppTest.from_string(_PAGE, default_timeout=60).run()
        radio = app.radio(key="box_section")
        assert radio.value == "Library"
        assert list(radio.options) == ["Library", "Import from Batch", "Run tests", "Runs"]
        assert not app.exception

    def test_an_absent_library_is_never_created_by_viewing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(box_app, "DB_PATH", tmp_path / "nothing" / "box.sqlite3")
        for section in ("Run tests", "Runs"):
            app = _page(section)
            assert not app.exception
        assert not (tmp_path / "nothing").exists()


class TestNewRun:
    def test_archived_cases_are_not_selectable_and_are_listed(self, world):
        _register(world)
        active, archived = _cases(world, item(path="a"), item(path="b"))
        world["store"].archive_case(archived.id)
        app = _page("Run tests")
        options = app.multiselect(key="box_run_cases").options
        assert len(options) == 1 and m.case_key(active.id) in options[0]
        assert m.case_key(archived.id) in _texts(app) and "Archived" in _texts(app)

    def test_preview_requires_an_explicit_host(self, world):
        _register(world)
        (case,) = _cases(world, item(path="a"))
        app = _page("Run tests", box_run_cases=[case.id])
        _button(app, "box_run_preview").click()
        app.run()
        assert any("Choose the host" in w.value for w in app.warning)
        assert not [b for b in app.button if str(b.key).startswith("box_run_start_")]

    def test_the_plan_preview_shows_eligible_blocked_reasons_and_variable_names(self, world):
        _register(world)
        good, empty, scripted = _cases(
            world,
            item(path="g"),
            item(path="e", event=[]),
            item(path="s", event=[{"listen": "test", "script": {"exec": ["x()"]}}]),
        )
        app = _planned(world, [good.id, empty.id, scripted.id])
        text = _texts(app)
        metrics = {metric.label: metric.value for metric in app.metric}
        assert metrics == {"Newman units": "3", "Will run": "1", "Blocked": "2"}
        assert "1 test case = 1 unit" in text
        assert "event: []" in text and "base_url" in text
        assert "TLS certificates always verified" in text and "bail off" in text
        plan_frame = app.dataframe[0].value
        assert list(plan_frame["Will run"]) == ["Yes", "No", "No"]
        assert all(plan_frame["Blocking reasons"][1:])
        assert ENV_SECRET not in text and TOKEN not in text
        assert not _button(app, "box_run_start_").disabled

    def test_a_plan_with_nothing_eligible_cannot_start(self, world):
        _register(world)
        (case,) = _cases(world, item(path="e", event=[]))
        app = _planned(world, [case.id])
        assert _button(app, "box_run_start_").disabled
        assert "cannot start" in _texts(app)

    def test_start_runs_once_and_opens_the_run(self, world):
        _register(world, FakeEngine([outcome(no_assertions=False), outcome()]))
        first, second = _cases(world, item(path="a"), item(path="b"))
        app, run_id = _start(world, [first.id, second.id])
        assert not app.exception
        assert app.session_state["box_section"] == "Runs"
        assert any(f"Run #{run_id} started" in s.value for s in app.success)
        runs = world["store"].list_runs()
        assert [r.id for r in runs] == [run_id] and runs[0].status == m.RUN_COMPLETED

    def test_a_stale_click_never_starts_a_second_run(self, world):
        _register(world)
        (case,) = _cases(world, item(path="a"))
        app = _planned(world, [case.id])
        start = _button(app, "box_run_start_")
        token = app.session_state["_box_run_token"] if "_box_run_token" in app.session_state else 0
        app.session_state["_box_run_token"] = token + 1  # یعنی کلیکِ قبلی پردازش شده
        start.click()
        app.run()
        assert world["store"].list_runs() == []
        assert any("already handled" in i.value for i in app.info)

    def test_a_busy_gate_is_reported_truthfully_and_nothing_is_created(self, world):
        _register(world)
        (case,) = _cases(world, item(path="a"))
        app = _planned(world, [case.id])
        with ExecutionGate(world["gate_dir"]).acquire("BATCH", "batch-7"):
            _button(app, "box_run_start_").click()
            app.run()
        assert any("did not start" in e.value and "BATCH batch-7" in e.value for e in app.error)
        assert world["store"].list_runs() == []


class TestRuns:
    def test_history_details_unverified_and_artifacts(self, world):
        _register(world, FakeEngine([outcome(no_assertions=False), outcome()]))
        first, second = _cases(world, item(path="a"), item(path="b"))
        _, run_id = _start(world, [first.id, second.id])
        app = _page("Runs")
        assert not app.exception
        text = _texts(app)
        history = app.dataframe[0].value
        assert list(history["Run"]) == [run_id]
        assert history["PASS (verified)"][0] == 1 and history["UNVERIFIED"][0] == 1
        metrics = {metric.label: metric.value for metric in app.metric}
        assert metrics["PASS (verified)"] == "1" and metrics["UNVERIFIED"] == "1"
        assert "not verified by assertions" in text
        units = app.dataframe[1].value
        assert list(units["Result"]) == ["PASS (verified)", "UNVERIFIED"]
        downloads = [d for d in app.get("download_button")]
        assert downloads, "published artifacts must be downloadable"
        assert ENV_SECRET not in text and TOKEN not in text
        assert not [b for b in app.button if str(b.key).startswith("box_run_cancel_")]

    def test_a_live_run_can_be_cancelled_through_the_service(self, world):
        started, release = threading.Event(), threading.Event()
        service = _register(world, FakeEngine([blocking(started, release)]))
        (case,) = _cases(world, item(path="a"))
        result = service.start(service.plan_new([case.id], RunProfile(HostIntent.NORMAL)))
        try:
            assert result.ok and started.wait(20)
            app = _page("Runs")
            assert any("RUNNING" in s.value for s in app.subheader)
            _button(app, "box_run_cancel_").click()
            app.run()
            assert any("Cancellation requested" in i.value for i in app.info), _texts(app)
        finally:
            release.set()
        assert service.wait(30) and service.job.result.status == "CANCELLED"
        assert world["store"].get_run(result.run_id).status == m.RUN_CANCELLED

    def test_rerun_uses_pinned_versions_and_creates_a_linked_run(self, world):
        engine = FakeEngine()
        _register(world, engine)
        (case,) = _cases(world, item(path="a"))
        _, source = _start(world, [case.id])
        world["store"].archive_case(case.id)
        app = _page("Runs")
        _button(app, f"box_rerun_prepare_{source}").click()
        app.run()
        assert "same pinned test case versions" in _texts(app)
        _button(app, f"box_rerun_start_{source}_").click()
        app.run()
        service = run_service_module._services[world["db"].resolve()]
        assert service.wait(30)
        rerun = world["store"].get_run(service.job.result.run_id)
        assert rerun.id != source and rerun.retry_of_run_id == source
        assert rerun.status == m.RUN_COMPLETED and len(engine.calls) == 2
        assert world["store"].get_run(source).status == m.RUN_COMPLETED

    def test_a_missing_variable_blocks_the_rerun_with_a_clear_message(self, world):
        _register(world)
        (case,) = _cases(world, item(path="a"))
        _, source = _start(world, [case.id])
        write_configuration(
            world["tmp"] / "config",
            values=[
                {"key": "base_url", "value": "http://podium.test"},
                {"key": "base_url_vdi", "value": "x"},
            ],
        )
        app = _page("Runs")
        _button(app, f"box_rerun_prepare_{source}").click()
        app.run()
        assert any("must be supplied there again" in w.value for w in app.warning)
        assert _button(app, f"box_rerun_start_{source}_").disabled


class TestRecovery:
    def _abandoned(self, world) -> int:
        service = _register(world)
        (case,) = _cases(world, item(path="a"))
        plan = service.plan_new([case.id], RunProfile(HostIntent.NORMAL))
        store = world["store"]
        run, lease = store.create_owned_run(BoxRunner._new_run(plan), pid=4242, host="old-host")
        store.start_run(lease)
        store.start_unit(lease, store.run_units(run.id)[0].id)
        time.sleep(STALE * 3)
        return run.id

    def test_a_stale_run_is_recovered_through_the_gate(self, world):
        run_id = self._abandoned(world)
        app = _page("Runs")
        assert "There is no way to force recovery" in _texts(app)
        assert not [b for b in app.button if "force" in str(b.key).lower()]
        _button(app, f"box_recover_{run_id}_").click()
        app.run()
        assert any(f"Run #{run_id}" in s.value for s in app.success)
        run = world["store"].get_run(run_id)
        assert run.status == m.RUN_INTERRUPTED
        assert run.metadata["recovery"]["reason"] == recovery.REASON_APPLICATION_TERMINATED

    def test_recovery_is_refused_while_the_gate_is_held(self, world):
        run_id = self._abandoned(world)
        app = _page("Runs")
        with ExecutionGate(world["gate_dir"]).acquire("BATCH", "batch-hung"):
            _button(app, f"box_recover_{run_id}_").click()
            app.run()
        assert any(f"Run #{run_id}" in w.value for w in app.warning)
        assert world["store"].get_run(run_id).status == m.RUN_RUNNING


def test_the_page_module_never_imports_the_engine_or_writes_sql():
    source = (Path(box_app.__file__).parent / "box_runs.py").read_text(encoding="utf-8")
    for forbidden in (
        "src.test_execution",
        "subprocess",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
        "newman.",
    ):
        assert forbidden not in source


def test_a_pass_is_never_shown_as_verified_without_verified_assertions():
    from src.testcase_box.run_views import UnitRow
    from ui.box_runs import _result_label

    def label(status, verification):
        return _result_label(UnitRow(1, 1, "TC-1", 1, 1, status, "", "", verification, ()))

    assert label("PASS", "VERIFIED") == "PASS (verified)"
    assert label("PASS", "UNVERIFIED") == "UNVERIFIED"
    assert label("PASS", "") == "UNVERIFIED"
    assert label("ERROR", "NOT_VERIFIED") == "ERROR"
    assert label("ERROR", "VERIFIED") == "ERROR"
    assert label("FAIL", "FAILED") == "FAIL"


def test_changing_the_selection_after_preview_discards_the_plan(world):
    _register(world)
    first, second = _cases(world, item(path="a"), item(path="b"))
    app = _planned(world, [first.id])
    assert [b for b in app.button if str(b.key).startswith("box_run_start_")]
    app.multiselect(key="box_run_cases").select(second.id)
    app.run()
    assert not [b for b in app.button if str(b.key).startswith("box_run_start_")]
    assert "Preview the run plan again" in _texts(app)
    assert world["store"].list_runs() == []
