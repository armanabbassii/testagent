"""
tests/unit/testcase_box/test_rerun_service.py — rerun ِ کامل و میزبانِ اجرای Box (M7.6)

از راهِ BoxRunService ِ واقعی (برنامه‌ریز، storage، Gate، runner) با موتور و توکنِ ساختگی، پایگاه و
پیکربندیِ موقت. هیچ Newman/Node/HTTP/Jira/LLM.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing

import pytest

from src.execution_gate import ExecutionGate
from src.testcase_box import models as m
from src.testcase_box import recovery
from src.testcase_box import runner as runner_module
from src.testcase_box.models import NewCase, NewRun, NewRunCase, NewRunUnit, NewVersion
from src.testcase_box.profiles import HostIntent
from src.testcase_box.run_profile import RunProfile
from src.testcase_box.run_service import BoxRunService, PlanningProblem
from src.testcase_box.storage import BoxStore
from tests.unit.testcase_box.runner_fakes import (
    ENV_SECRET,
    TOKEN,
    FakeEngine,
    FakeTokens,
    blocking,
    write_configuration,
)
from tests.unit.testcase_box.test_run_planning import item


@pytest.fixture(autouse=True)
def _no_real_defaults(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("No real gate, engine or token provider in unit tests.")

    for name in ("_default_gate", "_default_engine", "_default_token_provider"):
        monkeypatch.setattr(runner_module, name, forbidden)
    monkeypatch.setattr(recovery, "_default_gate", forbidden)
    yield
    for lease in list(runner_module._UNRELEASED_GATES):
        runner_module._UNRELEASED_GATES.remove(lease)
        lease.release()


@pytest.fixture
def world(tmp_path):
    environment, globals_path = write_configuration(tmp_path / "config")
    db = tmp_path / "box.sqlite3"
    store = BoxStore.open(db)
    yield {
        "db": db,
        "store": store,
        "environment": environment,
        "globals": globals_path,
        "tmp": tmp_path,
    }
    store.close()


def _service(world, engine=None, **options) -> BoxRunService:
    return BoxRunService(
        world["db"],
        environment_path=world["environment"],
        globals_path=world["globals"],
        gate_factory=lambda: ExecutionGate(world["tmp"] / "gate"),
        artifact_root=world["tmp"] / "runs",
        stale_after=0.05,
        runner_options={
            "engine": engine if engine is not None else FakeEngine(),
            "token_provider": FakeTokens(),
            "heartbeat_interval": 0.02,
            "poll_interval": 0.005,
            "stale_after": 0.5,
        },
        **options,
    )


def _cases(world, *payloads):
    return [
        world["store"].create_case(NewCase(title=f"C{n}", payload=p))
        for n, p in enumerate(payloads)
    ]


def _run_and_wait(service, plan):
    started = service.start(plan)
    assert started.ok, started.message
    assert service.wait(30)
    return service.job.result


def _source_run(world, service, payloads=None):
    selected = _cases(world, *(payloads or (item(path="a"), item(path="b"), item(path="c"))))
    result = _run_and_wait(
        service, service.plan_new([c.id for c in selected], RunProfile(HostIntent.NORMAL))
    )
    assert result.status == "COMPLETED"
    return selected, result.run_id


def _rows(store, run_id):
    return [
        tuple(r)
        for r in store._db.execute(
            "SELECT * FROM box_run_cases WHERE run_id = ? ORDER BY id", (run_id,)
        )
    ]


class TestRerun:
    def test_a_rerun_uses_pinned_versions_order_and_a_new_identity(self, world):
        service = _service(world)
        selected, source = _source_run(world, service)
        store = world["store"]
        before = (store.get_run(source), store.run_units(source), _rows(store, source))
        for case in selected:  # نسخه‌های تازه پس از run ِ مبدأ
            store.add_version(case.id, NewVersion(payload=item(path=f"new-{case.id}")))
        plan = service.plan_rerun(source)
        assert plan.kind == "RERUN" and plan.rerun_of_run_id == source
        assert [e.version_number for e in plan.entries] == [1, 1, 1]
        assert [e.case_id for e in plan.entries] == [c.id for c in selected]
        result = _run_and_wait(service, plan)
        assert result.status == "COMPLETED" and result.run_id != source
        rerun = store.get_run(result.run_id)
        assert rerun.retry_of_run_id == source and rerun.metadata["plan_kind"] == "RERUN"
        source_cases = store.run_cases(source)
        rerun_cases = store.run_cases(result.run_id)
        assert [(c.case_id, c.case_version_id) for c in rerun_cases] == [
            (c.case_id, c.case_version_id) for c in source_cases
        ]
        engine = service.job.runner._engine
        urls = [call.collection["item"][0]["request"]["url"]["raw"] for call in engine.calls[-3:]]
        assert all("/api/new-" not in url for url in urls)  # هرگز نسخه‌ی جاری
        assert (store.get_run(source), store.run_units(source), _rows(store, source)) == before
        assert result.artifact_dir and result.artifact_dir != service_artifact_dir(store, source)

    def test_archived_cases_rerun_but_never_start_new_runs(self, world):
        service = _service(world)
        selected, source = _source_run(world, service, (item(path="a"),))
        world["store"].archive_case(selected[0].id)
        new_plan = service.plan_new([selected[0].id], RunProfile(HostIntent.NORMAL))
        assert {f.code for f in new_plan.entries[0].blocking} == {"CASE_ARCHIVED"}
        rerun = service.plan_rerun(source)
        assert rerun.entries[0].eligible and rerun.entries[0].case_status == m.CASE_ARCHIVED
        assert _run_and_wait(service, rerun).status == "COMPLETED"

    def test_the_source_profile_is_the_default(self, world):
        service = _service(world)
        selected = _cases(world, item(path="a"))
        profile = RunProfile(HostIntent.NORMAL, request_timeout_ms=7_000, process_timeout_s=120)
        result = _run_and_wait(service, service.plan_new([selected[0].id], profile))
        rerun = service.plan_rerun(result.run_id)
        assert rerun.profile == profile
        assert service.job.runner._engine.calls[-1].request.options.request_timeout_ms == 7_000

    def test_a_rerun_blocks_scripts_and_empty_event_arrays(self, world):
        service = _service(world)
        store = world["store"]
        good, empty, scripted = _cases(
            world,
            item(path="g"),
            item(path="e", event=[]),
            item(path="s", event=[{"listen": "test", "script": {"exec": ["x()"]}}]),
        )
        # run ِ مبدأ که این نسخه‌ها را سنجاق کرده بود (بیرون از برنامه‌ریز ساخته شده)
        source = store.create_run(
            NewRun(
                units=tuple(
                    NewRunUnit(n, (NewRunCase(c.id, c.current_version_id, 1),))
                    for n, c in enumerate((good, empty, scripted), 1)
                )
            )
        )
        for run_case in store.run_cases(source.id):
            store.set_run_case_status(run_case.id, m.CASE_RUN_CANCELLED)
        for unit in store.run_units(source.id):
            store.set_unit_status(unit.id, m.UNIT_CANCELLED)
        store.set_run_status(source.id, m.RUN_CANCELLED)
        engine = FakeEngine()
        service = _service(world, engine)
        plan = service.plan_rerun(source.id, RunProfile(HostIntent.NORMAL))
        codes = [{f.code for f in e.blocking} for e in plan.entries]
        assert codes[0] == set()
        assert "EXECUTION_EVENTS_PRESENT" in codes[1] and "EXECUTION_SCRIPTS_PRESENT" in codes[2]
        result = _run_and_wait(service, plan)
        assert len(engine.calls) == 1  # فقط caseِ بی‌event
        assert result.status == "COMPLETED"

    def test_a_missing_required_variable_blocks_the_rerun_safely(self, world):
        service = _service(world)
        _, source = _source_run(world, service, (item(path="a"),))
        write_configuration(
            world["tmp"] / "config",
            values=[
                {"key": "base_url", "value": "http://podium.test"},
                {"key": "base_url_vdi", "value": "x"},
            ],
        )
        plan = service.plan_rerun(source)
        assert not plan.executable
        assert "VARIABLE_UNAVAILABLE" in {f.code for f in plan.entries[0].blocking}
        refused = service.start(plan)
        assert not refused.ok and refused.refusal == "NOTHING_TO_EXECUTE"

    def test_an_ineligible_source_run_is_rejected_truthfully(self, world):
        service = _service(world)
        (case,) = _cases(world, item())
        active = world["store"].create_run(
            NewRun(units=(NewRunUnit(1, (NewRunCase(case.id, case.current_version_id, 1),)),))
        )
        with pytest.raises(PlanningProblem, match="finished"):
            service.plan_rerun(active.id, RunProfile(HostIntent.NORMAL))
        with pytest.raises(PlanningProblem, match="does not exist"):
            service.plan_rerun(999, RunProfile(HostIntent.NORMAL))
        with pytest.raises(PlanningProblem, match="no valid execution profile"):
            service.plan_rerun(active.id)  # بی‌پروفایلِ ثبت‌شده

    def test_no_secret_value_is_persisted_by_a_rerun(self, world):
        service = _service(world)
        _, source = _source_run(world, service)
        _run_and_wait(service, service.plan_rerun(source))
        with closing(sqlite3.connect(world["db"])) as connection:
            text = repr(
                [
                    connection.execute(f"SELECT * FROM {t}").fetchall()
                    for t in ("box_runs", "box_run_units", "box_run_cases")
                ]
            )
        assert ENV_SECRET not in text and TOKEN not in text


def service_artifact_dir(store, run_id):
    return store.get_run(run_id).metadata.get("artifact_dir")


class TestService:
    def test_a_second_start_is_refused_while_a_run_is_active(self, world):
        started, release = threading.Event(), threading.Event()
        service = _service(world, FakeEngine([blocking(started, release)]))
        (case,) = _cases(world, item())
        plan = service.plan_new([case.id], RunProfile(HostIntent.NORMAL))
        first = service.start(plan)
        try:
            assert first.ok and first.run_id is not None
            started.wait(20)
            again = service.start(plan)
            assert not again.ok and again.refusal == "ALREADY_RUNNING"
        finally:
            release.set()
        assert service.wait(30) and service.job.result.status == "COMPLETED"

    def test_cancellation_goes_through_storage_and_the_runner(self, world):
        started, release = threading.Event(), threading.Event()
        service = _service(world, FakeEngine([blocking(started, release)]))
        (case,) = _cases(world, item())
        first = service.start(service.plan_new([case.id], RunProfile(HostIntent.NORMAL)))
        started.wait(20)
        cancelled = service.cancel(first.run_id)
        assert cancelled.ok
        assert service.wait(30) and service.job.result.status == "CANCELLED"
        assert not service.cancel(first.run_id).ok  # دوباره: run دیگر فعال نیست

    def test_a_busy_gate_is_reported_and_no_run_is_created(self, world):
        service = _service(world)
        (case,) = _cases(world, item())
        plan = service.plan_new([case.id], RunProfile(HostIntent.NORMAL))
        with ExecutionGate(world["tmp"] / "gate").acquire("BATCH", "batch-1"):
            refused = service.start(plan)
        assert (
            not refused.ok and refused.refusal == "GATE_BUSY" and "BATCH batch-1" in refused.message
        )
        assert world["store"].list_runs() == []

    def test_unreadable_configuration_is_a_planning_problem(self, world):
        world["environment"].write_text("{broken", encoding="utf-8")
        with pytest.raises(PlanningProblem, match="environment"):
            _service(world).plan_new([1], RunProfile(HostIntent.NORMAL))

    def test_recovery_is_refused_while_this_application_runs_the_run(self, world):
        started, release = threading.Event(), threading.Event()
        service = _service(world, FakeEngine([blocking(started, release)]))
        (case,) = _cases(world, item())
        first = service.start(service.plan_new([case.id], RunProfile(HostIntent.NORMAL)))
        try:
            started.wait(20)
            blocked = service.recover(first.run_id)
            assert blocked.status == "BLOCKED"
        finally:
            release.set()
        service.wait(30)
