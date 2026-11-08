"""
tests/unit/testcase_box/test_runner.py — BoxRunner (M7.5) با موتور و توکنِ ساختگی

پایگاه، پیکربندی، Gate و artifactها همه در tmp. هیچ Newman، Node، HTTP، Jira یا LLM ای اجرا
نمی‌شود؛ پیش‌فرض‌های واقعیِ runner (Gate، موتور، توکن، ریشه‌ی artifact) در هر تست ممنوع‌اند.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from src.execution_gate import ExecutionGate, GateBusy, GateLease
from src.test_execution.newman.models import (
    ERROR_NETWORK,
    ERROR_NEWMAN_NOT_FOUND,
    ERROR_NEWMAN_RUNTIME,
    ERROR_PROCESS_TIMEOUT,
    ERROR_SANITIZATION_FAILED,
    NewmanOptions,
)
from src.testcase_box import models as m
from src.testcase_box import runner as runner_module
from src.testcase_box.models import NewCase, NewVersion
from src.testcase_box.profiles import HostIntent, variable_profile_from_documents
from src.testcase_box.run_planning import plan_new_run, plan_rerun
from src.testcase_box.run_profile import RunProfile
from src.testcase_box.runner import BoxRunner
from src.testcase_box.storage import BoxStore
from tests.unit.testcase_box.runner_fakes import (
    AUTH_FAILURE,
    ENV_SECRET,
    TOKEN,
    FakeEngine,
    FakeTokens,
    blocking,
    outcome,
    published,
    wait_for,
    write_configuration,
)
from tests.unit.testcase_box.test_run_planning import event, item

PROFILE = RunProfile(HostIntent.NORMAL)


# ── fixtureها ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_real_defaults(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("No real gate, engine or token provider in unit tests.")

    monkeypatch.setattr(runner_module, "_default_gate", forbidden)
    monkeypatch.setattr(runner_module, "_default_engine", forbidden)
    monkeypatch.setattr(runner_module, "_default_token_provider", forbidden)
    monkeypatch.setattr(runner_module, "DEFAULT_ARTIFACT_ROOT", tmp_path / "forbidden-runs")
    yield
    for lease in list(runner_module._UNRELEASED_GATES):  # فقط Gateهای tmp ِ همین تست‌ها
        runner_module._UNRELEASED_GATES.remove(lease)
        lease.release()
    assert not (tmp_path / "forbidden-runs").exists()
    alive = [t.name for t in threading.enumerate() if t.name.startswith("box-runner-monitor")]
    assert alive == [], alive


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
        "gate_dir": tmp_path / "gate",
        "runs": tmp_path / "runs",
        "tmp": tmp_path,
    }
    store.close()


def variables(world):
    load = lambda p: json.loads(Path(p).read_text(encoding="utf-8"))  # noqa: E731
    return variable_profile_from_documents(
        "dev", load(world["environment"]), load(world["globals"])
    )


def cases(world, *payloads) -> list[m.BoxCase]:
    payloads = payloads or (item(path="a"), item(path="b"), item(path="c"))
    return [
        world["store"].create_case(NewCase(title=f"C{n}", payload=p))
        for n, p in enumerate(payloads)
    ]


def plan(world, selected, profile=PROFILE):
    ids = [c.id if isinstance(c, m.BoxCase) else c for c in selected]
    return plan_new_run(world["store"], ids, profile=profile, variables=variables(world))


def runner(world, engine=None, tokens=None, **kwargs) -> BoxRunner:
    kwargs.setdefault("heartbeat_interval", 0.02)
    kwargs.setdefault("poll_interval", 0.005)
    kwargs.setdefault("stale_after", 0.5)
    return BoxRunner(
        world["db"],
        gate_factory=lambda: ExecutionGate(world["gate_dir"]),
        engine=engine if engine is not None else FakeEngine(),
        token_provider=tokens if tokens is not None else FakeTokens(),
        environment_path=world["environment"],
        globals_path=world["globals"],
        artifact_root=world["runs"],
        **kwargs,
    )


class Background:
    def __init__(self, call) -> None:
        self.result = None
        self.thread = threading.Thread(target=lambda: setattr(self, "result", call()), daemon=True)
        self.thread.start()

    def join(self):
        self.thread.join(30)
        assert not self.thread.is_alive()
        return self.result


def gate_busy(world) -> bool:
    try:
        lease = ExecutionGate(world["gate_dir"]).acquire("BATCH", "probe")
    except GateBusy:
        return True
    lease.release()
    return False


def dump(world) -> str:
    with closing(sqlite3.connect(world["db"])) as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return "\n".join(repr(c.execute(f"SELECT * FROM {t}").fetchall()) for t in tables)


def units(world, run_id):
    store = world["store"]
    return [(u, store.run_cases(run_id, unit_id=u.id)[0]) for u in store.run_units(run_id)]


# ── چرخه‌ی کامل ──────────────────────────────────────────────────────────────


class TestLifecycle:
    def test_a_multi_unit_run_executes_each_case_in_its_own_unit(self, world):
        selected = cases(world)
        engine = FakeEngine()
        result = runner(world, engine).run(plan(world, selected))
        assert result.status == "COMPLETED" and result.cleanup_errors == ()
        assert len(engine.calls) == 3 and engine.max_active == 1  # ترتیبی
        for call, case in zip(engine.calls, selected, strict=True):
            (only,) = call.collection["item"]  # دقیقاً یک case در هر unit
            assert only["name"] == f"{m.case_key(case.id)} v1"
            assert call.request.options == NewmanOptions(
                request_timeout_ms=30_000, process_timeout_s=900.0, bail=False, insecure=False
            )
            assert call.request.options.delay_request_ms == 0  # پیش‌فرضِ موتور
            assert call.request.access_token == TOKEN
            assert call.request.environment_path == world["environment"]
        run = world["store"].get_run(result.run_id)
        assert run.status == m.RUN_COMPLETED and not world["store"].run_ownership(run.id).leased
        assert run.metadata["summary"]["unverified"] == 3
        assert run.metadata["summary"]["verified_pass"] == 0

    def test_pinned_versions_and_order_follow_the_plan(self, world):
        a, b, c = cases(world)
        engine = FakeEngine()
        planned = plan(world, [c, a, b])
        result = runner(world, engine).run(planned)
        names = [call.collection["item"][0]["name"] for call in engine.calls]
        assert names == [f"{m.case_key(x.id)} v1" for x in (c, a, b)]
        recorded = units(world, result.run_id)
        assert [(u.unit_number, rc.case_id, rc.case_version_id) for u, rc in recorded] == [
            (e.position, e.case_id, e.version_id) for e in planned.entries
        ]

    def test_blocked_cases_are_recorded_and_never_executed(self, world):
        good, scripted = cases(world, item(path="a"), item(event()))
        engine = FakeEngine()
        result = runner(world, engine).run(plan(world, [good, scripted]))
        assert result.status == "COMPLETED" and len(engine.calls) == 1
        (_, first), (blocked_unit, blocked_case) = units(world, result.run_id)
        assert blocked_unit.status == m.UNIT_NOT_RUN
        assert blocked_unit.metadata["classification"] == "BLOCKED"
        assert blocked_case.status == m.CASE_RUN_NOT_EXECUTABLE
        assert "EXECUTION_SCRIPTS_PRESENT" in blocked_case.result_metadata["blocking"]

    def test_a_rerun_uses_the_pinned_versions(self, world):
        (case,) = cases(world, item(path="old"))
        first = runner(world).run(plan(world, [case]))
        world["store"].add_version(case.id, NewVersion(payload=item(path="new")))
        rerun = plan_rerun(
            world["store"], first.run_id, profile=PROFILE, variables=variables(world)
        )
        engine = FakeEngine()
        result = runner(world, engine).run(rerun)
        assert result.status == "COMPLETED"
        assert "/api/old/" in engine.calls[0].collection["item"][0]["request"]["url"]["raw"]
        assert world["store"].get_run(result.run_id).retry_of_run_id == first.run_id

    def test_a_failed_finalization_never_reports_success(self, world, monkeypatch):
        (case,) = cases(world, item())

        def broken(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(BoxStore, "finish_run", broken)
        result = runner(world).run(plan(world, [case]))
        assert result.status == "INTERRUPTED" and result.stop_reason == "STORAGE_FAILED"
        assert any("could not be finalized" in note for note in result.cleanup_errors)
        assert world["store"].get_run(result.run_id).status == m.RUN_RUNNING
        assert not gate_busy(world)  # پروسه تمام شده بود — Gate آزاد شد

    def test_nothing_executable_is_refused_without_a_run(self, world):
        (scripted,) = cases(world, item(event()))
        engine = FakeEngine()
        result = runner(world, engine).run(plan(world, [scripted]))
        assert (result.status, result.refusal) == ("REFUSED", "NOTHING_TO_EXECUTE")
        assert engine.calls == [] and world["store"].list_runs() == []

    def test_unrecordable_selections_are_refused_not_dropped(self, world):
        (case,) = cases(world, item())
        result = runner(world).run(plan(world, [case, 999, case]))
        assert (result.status, result.refusal) == ("REFUSED", "PLAN_NOT_RECORDABLE")
        assert "2, 3" in result.message and world["store"].list_runs() == []


# ── نتیجه‌های Newman ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("behaviour", "status", "classification", "verification"),
    [
        (outcome(no_assertions=False), m.UNIT_PASS, "VERIFIED_PASS", "VERIFIED"),
        (outcome(), m.UNIT_PASS, "UNVERIFIED", "UNVERIFIED"),
        (outcome(status="FAIL", no_assertions=False), m.UNIT_FAIL, "ASSERTION_FAILED", "FAILED"),
        (
            outcome(status="ERROR", error_kind=ERROR_NEWMAN_RUNTIME),
            m.UNIT_ERROR,
            "ENGINE_ERROR",
            "NOT_VERIFIED",
        ),
        (
            outcome(status="ERROR", error_kind=ERROR_PROCESS_TIMEOUT, timed_out=True),
            m.UNIT_ERROR,
            "PROCESS_TIMEOUT",
            "NOT_VERIFIED",
        ),
        (
            outcome(status="ERROR", error_kind=ERROR_NETWORK),
            m.UNIT_ERROR,
            "NETWORK_ERROR",
            "NOT_VERIFIED",
        ),
        (
            outcome(status="ERROR", error_kind=ERROR_SANITIZATION_FAILED),
            m.UNIT_ERROR,
            "ARTIFACT_UNSAFE",
            "NOT_VERIFIED",
        ),
        (outcome(artifacts=False), m.UNIT_ERROR, "ARTIFACT_INVALID", "NOT_VERIFIED"),
        (outcome(run_id=f"leak-{TOKEN}"), m.UNIT_ERROR, "ARTIFACT_UNSAFE", "NOT_VERIFIED"),
    ],
)
def test_newman_results_are_mapped_without_rewriting_the_engine_result(
    world, behaviour, status, classification, verification
):
    first, second = cases(world, item(path="a"), item(path="b"))
    engine = FakeEngine([behaviour])
    result = runner(world, engine).run(plan(world, [first, second]))
    assert (
        result.status == "COMPLETED" and len(engine.calls) == 2
    )  # یک unitِ ناموفق بقیه را نمی‌ایستاند
    (unit, case), _ = units(world, result.run_id)
    assert (unit.status, case.status) == (status, status)
    assert (
        unit.metadata["classification"] == case.result_metadata["classification"] == classification
    )
    assert case.result_metadata["verification"] == verification
    assert result.units[0].verification == verification
    assert result.verified_passes == (1 if verification == "VERIFIED" else 0) + 0
    assert TOKEN not in dump(world)


def test_artifacts_outside_the_unit_directory_are_rejected(world, tmp_path):
    (case,) = cases(world, item())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "result.json").write_text("{}")

    def outside(request, cancel):
        result = published(request)
        result.artifacts.run_dir, result.artifacts.result_json = (
            str(elsewhere),
            str(elsewhere / "result.json"),
        )
        return result

    result = runner(world, FakeEngine([outside])).run(plan(world, [case]))
    ((unit, _),) = units(world, result.run_id)
    assert (
        unit.metadata["classification"] == "ARTIFACT_INVALID" and unit.metadata["artifacts"] == {}
    )


def test_an_engine_environment_error_stops_the_run(world):
    first, second = cases(world, item(path="a"), item(path="b"))
    engine = FakeEngine(
        [
            outcome(
                status="ERROR", error_kind=ERROR_NEWMAN_NOT_FOUND, exit_code=None, artifacts=False
            )
        ]
    )
    engine.behaviours[0] = lambda request, cancel: dataclasses.replace(
        published(request, status="ERROR", error_kind=ERROR_NEWMAN_NOT_FOUND),
        process_id=None,
        exit_code=None,
    )
    result = runner(world, engine).run(plan(world, [first, second]))
    assert (result.status, result.stop_reason) == ("INTERRUPTED", "ENGINE_ENVIRONMENT_ERROR")
    assert len(engine.calls) == 1
    (u1, _), (u2, c2) = units(world, result.run_id)
    assert u1.metadata["classification"] == "ENGINE_ENVIRONMENT_ERROR"
    assert (u2.status, c2.status) == (m.UNIT_NOT_RUN, m.CASE_RUN_NOT_RUN)
    run = world["store"].get_run(result.run_id)
    assert (
        run.status == m.RUN_INTERRUPTED
        and run.metadata["stop_reason"] == "ENGINE_ENVIRONMENT_ERROR"
    )


# ── بررسی‌های پیش از Newman (همه fail-closed) ─────────────────────────────────


def _tamper_version(world, version_id, payload):
    db = world["store"]._db
    db.execute("DROP TRIGGER IF EXISTS tr_box_case_versions_no_update")  # فقط پایگاهِ موقت
    db.execute(
        "UPDATE box_case_versions SET payload_json = ?, fingerprint = ?, script_fingerprint = ? WHERE id = ?",
        (m.to_json(payload), m.sha256_json(payload), m.script_fingerprint(payload), version_id),
    )


class TestPreExecutionIntegrity:
    def test_a_new_version_after_planning_is_refused_before_any_run(self, world):
        (case,) = cases(world, item())
        planned = plan(world, [case])
        world["store"].add_version(case.id, NewVersion(payload=item(path="changed")))
        engine = FakeEngine()
        result = runner(world, engine).run(planned)
        assert (result.status, result.refusal) == ("REFUSED", "PLAN_CHANGED")
        assert engine.calls == [] and world["store"].list_runs() == [] and not gate_busy(world)

    def test_a_changed_plan_fingerprint_is_refused(self, world):
        (case,) = cases(world, item())
        planned = plan(world, [case])
        forged = dataclasses.replace(
            planned, entries=(dataclasses.replace(planned.entries[0], case_status="ARCHIVED"),)
        )
        result = runner(world, FakeEngine()).run(forged)
        assert result.refusal == "PLAN_CHANGED"

    def test_a_forged_collection_payload_is_refused(self, world):
        (case,) = cases(world, item())
        planned = plan(world, [case])
        other = item(path="forged")
        forged_entry = dataclasses.replace(
            planned.entries[0], prepared_payload=other, prepared_fingerprint=m.sha256_json(other)
        )
        engine = FakeEngine()
        result = runner(world, engine).run(dataclasses.replace(planned, entries=(forged_entry,)))
        assert result.refusal == "PLAN_CHANGED" and engine.calls == []

    @pytest.mark.parametrize(
        "payload",
        [
            item(event(listen="prerequest"), path="b"),
            item(event(), path="b"),
            item({"listen": "test", "script": {"exec": [""]}}, path="b"),
            item(event=["malformed"], path="b"),
        ],
        ids=["prerequest-script", "test-script", "empty-event-entry", "malformed-event"],
    )
    def test_a_script_or_event_introduced_mid_run_blocks_the_unit(self, world, payload):
        first, second = cases(world, item(path="a"), item(path="b"))
        planned = plan(world, [first, second])
        engine = FakeEngine(
            on_call=lambda request: _tamper_version(world, second.current_version_id, payload)
        )
        result = runner(world, engine).run(planned)
        assert (result.status, result.stop_reason) == ("INTERRUPTED", "INTEGRITY_FAILED")
        assert len(engine.calls) == 1  # unitِ دوم هرگز به موتور نرسید
        (_, _), (u2, c2) = units(world, result.run_id)
        assert u2.metadata["classification"] == "PRE_EXECUTION_BLOCKED"
        assert (u2.status, c2.status) == (m.UNIT_NOT_RUN, m.CASE_RUN_NOT_RUN)

    def test_a_changed_collection_mid_run_blocks_the_unit(self, world, monkeypatch):
        first, second = cases(world, item(path="a"), item(path="b"))
        planned = plan(world, [first, second])
        real, built = runner_module.build_unit_collection, []

        def changing(entry):
            collection = real(entry)
            built.append(entry.position)
            if entry.position == 2:
                collection["info"]["name"] = "changed"
            return collection

        monkeypatch.setattr(runner_module, "build_unit_collection", changing)
        engine = FakeEngine()
        result = runner(world, engine).run(planned)
        assert result.stop_reason == "INTEGRITY_FAILED" and len(engine.calls) == 1

    def test_an_environment_secret_in_the_collection_is_never_sent(self, world):
        leaky = item(path="a")
        leaky["request"]["url"]["query"] = [{"key": "note", "value": ENV_SECRET}]
        leaky["request"]["url"]["raw"] += "?note=" + ENV_SECRET
        (case,) = cases(world, leaky)
        planned = plan(world, [case])
        assert planned.executable  # M3 مقدار را نمی‌شناسد؛ runner آن را با رازهای Environment می‌سنجد
        engine = FakeEngine()
        result = runner(world, engine).run(planned)
        assert result.stop_reason == "INTEGRITY_FAILED" and engine.calls == []
        # خودِ case (دادهٔ کتابخانه‌ی کاربر) مقدار را دارد؛ هیچ چیزی که runner نوشته آن را ندارد
        with closing(sqlite3.connect(world["db"])) as c:
            for table in ("box_runs", "box_run_units", "box_run_cases"):
                assert ENV_SECRET not in repr(c.execute(f"SELECT * FROM {table}").fetchall())
        written = [p for p in world["runs"].rglob("*") if p.is_file()]
        assert all(ENV_SECRET not in p.read_text(encoding="utf-8") for p in written)

    def test_an_invalid_profile_is_refused(self, world):
        (case,) = cases(world, item())
        own = RunProfile(HostIntent.NORMAL)  # نمونه‌ی خودِ همین تست — PROFILE ِ مشترک دست نمی‌خورد
        planned = plan(world, [case], profile=own)
        object.__setattr__(own, "insecure", True)  # دست‌کاری پس از برنامه‌ریزی
        engine = FakeEngine()
        result = runner(world, engine).run(planned)
        assert (result.status, result.refusal) == ("REFUSED", "PLAN_INVALID")
        assert engine.calls == [] and world["store"].list_runs() == []

    def test_missing_host_configuration_is_refused(self, world):
        (case,) = cases(world, item())
        planned = plan(world, [case])
        write_configuration(world["tmp"] / "config", values=[{"key": "userId", "value": "42"}])
        result = runner(world).run(planned)
        assert result.refusal == "VARIABLES_CHANGED" and world["store"].list_runs() == []

    @pytest.mark.parametrize("content", ["{not json", '{"values": "x"}', '["list"]'])
    def test_unusable_variable_configuration_is_refused(self, world, content):
        (case,) = cases(world, item())
        planned = plan(world, [case])
        world["environment"].write_text(content, encoding="utf-8")
        result = runner(world).run(planned)
        assert result.refusal == "CONFIGURATION_INVALID"

    def test_an_unsafe_artifact_root_is_refused(self, world):
        (case,) = cases(world, item())
        world["runs"].write_text("not a directory")
        result = runner(world).run(plan(world, [case]))
        assert result.refusal == "ARTIFACT_ROOT_UNSAFE" and not gate_busy(world)

    def test_a_colliding_run_directory_is_never_overwritten(self, world, monkeypatch):
        (case,) = cases(world, item())
        monkeypatch.setattr(runner_module, "_now_stamp", lambda: "20260101T000000Z")
        monkeypatch.setattr(
            runner_module.uuid, "uuid4", lambda: type("U", (), {"hex": "deadbeef" * 4})()
        )
        existing = world["runs"] / "run_000001_20260101T000000Z_deadbeef"
        existing.mkdir(parents=True)
        (existing / "keep.txt").write_text("old artifact")
        engine = FakeEngine()
        result = runner(world, engine).run(plan(world, [case]))
        assert (result.status, result.stop_reason) == ("INTERRUPTED", "ARTIFACT_PUBLICATION_FAILED")
        assert engine.calls == [] and (existing / "keep.txt").read_text() == "old artifact"

    def test_a_collection_write_failure_stops_before_newman(self, world, monkeypatch):
        (case,) = cases(world, item())

        def broken(value):
            raise OSError("disk full")

        monkeypatch.setattr(runner_module, "to_json", broken)
        engine = FakeEngine()
        result = runner(world, engine).run(plan(world, [case]))
        assert result.stop_reason == "ARTIFACT_PUBLICATION_FAILED" and engine.calls == []
        assert world["store"].get_run(result.run_id).status == m.RUN_INTERRUPTED

    def test_a_configuration_change_during_the_run_stops_it(self, world):
        first, second = cases(world, item(path="a"), item(path="b"))
        planned = plan(world, [first, second])
        engine = FakeEngine(
            on_call=lambda request: world["globals"].write_text(
                '{"name": "G", "_postman_variable_scope": "globals", "values": [{"key": "x", "value": "1"}]}'
            )
        )
        result = runner(world, engine).run(planned)
        assert result.stop_reason == "CONFIGURATION_CHANGED" and len(engine.calls) == 1


# ── سیاستِ بی‌اسکریپت ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        item(event(listen="prerequest"), path="s"),
        item(event(), path="s"),
        {"info": {"name": "c"}, "item": [item(path="s")], "event": [event()]},  # سطحِ کالکشن
        {"name": "folder", "item": [item(path="s")], "event": [event()]},  # سطحِ folder
        item({"listen": "test", "script": {"exec": [""]}}, path="s"),  # entry ِ خالی
        item(event="pm.test()", path="s"),  # شکلِ نامعتبر
    ],
    ids=[
        "prerequest",
        "test",
        "collection-event",
        "folder-event",
        "empty-event-entry",
        "malformed",
    ],
)
def test_every_scripted_case_stays_blocked_and_never_reaches_the_engine(world, payload):
    good, scripted = cases(world, item(path="g"), payload)
    engine = FakeEngine()
    result = runner(world, engine).run(plan(world, [good, scripted]))
    assert len(engine.calls) == 1
    assert engine.calls[0].collection["item"][0]["name"] == f"{m.case_key(good.id)} v1"
    (_, _), (unit, case) = units(world, result.run_id)
    assert unit.metadata["classification"] == "BLOCKED" and case.status == m.CASE_RUN_NOT_EXECUTABLE


def test_an_approved_scripted_case_is_still_blocked(world):
    good, scripted = cases(world, item(path="g"), item(event(), path="s"))
    version = world["store"].current_version(scripted.id)
    world["store"].record_script_decision(
        version.id, version.script_fingerprint, m.APPROVAL_APPROVED
    )
    engine = FakeEngine()
    runner(world, engine).run(plan(world, [good, scripted]))
    assert len(engine.calls) == 1


def test_an_empty_event_array_is_blocked_and_never_reaches_the_engine(world):
    """سیاستِ تأییدشده: event: [] مسدود است — نه حذف می‌شود و نه اجرا."""
    good, empty = cases(world, item(path="g"), item(path="e", event=[]))
    engine = FakeEngine()
    planned = plan(world, [good, empty])
    assert {f.code for f in planned.entries[1].blocking} == {"EXECUTION_EVENTS_PRESENT"}
    result = runner(world, engine).run(planned)
    assert len(engine.calls) == 1  # فقط caseِ بی‌event
    assert engine.calls[0].collection["item"][0]["name"] == f"{m.case_key(good.id)} v1"
    (_, _), (unit, case) = units(world, result.run_id)
    assert unit.metadata["classification"] == "BLOCKED" and case.status == m.CASE_RUN_NOT_EXECUTABLE


def test_a_plan_of_only_empty_event_arrays_runs_nothing(world):
    (empty,) = cases(world, item(event=[]))
    engine = FakeEngine()
    result = runner(world, engine).run(plan(world, [empty]))
    assert (result.status, result.refusal) == ("REFUSED", "NOTHING_TO_EXECUTE")
    assert engine.calls == [] and world["store"].list_runs() == []


def test_an_empty_event_array_introduced_mid_run_is_blocked_before_newman(world):
    first, second = cases(world, item(path="a"), item(path="b"))
    planned = plan(world, [first, second])
    engine = FakeEngine(
        on_call=lambda request: _tamper_version(
            world, second.current_version_id, item(path="b", event=[])
        )
    )
    result = runner(world, engine).run(planned)
    assert (result.status, result.stop_reason) == ("INTERRUPTED", "INTEGRITY_FAILED")
    assert len(engine.calls) == 1


def test_a_stored_and_recomputed_fingerprint_disagreement_is_blocked(world):
    good, odd = cases(world, item(path="g"), item(path="o"))
    db = world["store"]._db
    db.execute("DROP TRIGGER tr_box_case_versions_no_update")
    db.execute(
        "UPDATE box_case_versions SET script_fingerprint = ? WHERE id = ?",
        ("c" * 64, odd.current_version_id),
    )
    engine = FakeEngine()
    runner(world, engine).run(plan(world, [good, odd]))
    assert len(engine.calls) == 1


# ── لغو، توکن و پاک‌سازی ─────────────────────────────────────────────────────


class TestCancellationAndCleanup:
    def test_cancellation_before_the_first_unit(self, world):
        selected = cases(world)
        box = None

        def cancel_now():
            run = world["store"].active_run()
            world["store"].request_run_cancel(run.id)
            wait_for(lambda: box._monitor is not None and box._monitor.cancel_requested.is_set())

        engine = FakeEngine()
        box = runner(world, engine, FakeTokens(on_fetch=cancel_now))
        result = box.run(plan(world, selected))
        assert (result.status, result.stop_reason) == ("CANCELLED", "USER_CANCELLED")
        assert engine.calls == []
        run = world["store"].get_run(result.run_id)
        assert run.status == m.RUN_CANCELLED
        assert {u.status for u, _ in units(world, run.id)} == {m.UNIT_CANCELLED}

    def test_cancellation_between_units_keeps_finished_results(self, world):
        first, second = cases(world, item(path="a"), item(path="b"))
        box = None

        def first_then_cancel(request, cancel):
            world["store"].request_run_cancel(world["store"].active_run().id)
            wait_for(lambda: box._monitor.cancel_requested.is_set())
            return published(request)

        engine = FakeEngine([first_then_cancel])
        box = runner(world, engine)
        result = box.run(plan(world, [first, second]))
        assert result.status == "CANCELLED" and len(engine.calls) == 1
        (u1, _), (u2, c2) = units(world, result.run_id)
        assert u1.status == m.UNIT_PASS and (u2.status, c2.status) == (
            m.UNIT_CANCELLED,
            m.CASE_RUN_CANCELLED,
        )

    def test_cancellation_during_newman_stops_the_process_before_releasing_the_gate(self, world):
        (case,) = cases(world, item())
        started, release = threading.Event(), threading.Event()
        engine = FakeEngine([blocking(started, release)])
        planned = plan(world, [case])  # در threadِ اصلی (اتصالِ SQLite مالِ همین thread است)
        job = Background(lambda: runner(world, engine).run(planned))
        started.wait(20)
        assert gate_busy(world)
        world["store"].request_run_cancel(world["store"].active_run().id)
        result = job.join()
        assert result.status == "CANCELLED"
        assert engine.calls[0].cancel_event.is_set()  # لغو از راهِ مکانیزمِ خودِ موتور رسید
        ((unit, _),) = units(world, result.run_id)
        assert (unit.status, unit.metadata["classification"]) == (m.UNIT_CANCELLED, "CANCELLED")
        assert not gate_busy(world)

    def test_unconfirmed_termination_keeps_the_gate_and_does_not_finalize(self, world):
        (case,) = cases(world, item())
        started, release = threading.Event(), threading.Event()
        engine = FakeEngine([blocking(started, release, confirmed=False)])
        planned = plan(world, [case])  # در threadِ اصلی (اتصالِ SQLite مالِ همین thread است)
        job = Background(lambda: runner(world, engine).run(planned))
        started.wait(20)
        world["store"].request_run_cancel(world["store"].active_run().id)
        result = job.join()
        assert (result.status, result.stop_reason) == (
            "TERMINATION_UNCONFIRMED",
            "TERMINATION_UNCONFIRMED",
        )
        assert gate_busy(world)  # Gate عمداً آزاد نشد
        assert len(runner_module._UNRELEASED_GATES) == 1
        assert world["store"].get_run(result.run_id).status == m.RUN_RUNNING  # نهایی نشد
        assert any("termination" in note for note in result.cleanup_errors)

    def test_an_engine_interrupted_by_an_exception_keeps_the_gate(self, world):
        (case,) = cases(world, item())

        def explode(request, cancel):
            raise KeyboardInterrupt

        result = runner(world, FakeEngine([explode])).run(plan(world, [case]))
        assert result.status == "TERMINATION_UNCONFIRMED" and gate_busy(world)

    def test_a_lease_heartbeat_failure_stops_safely(self, world, monkeypatch):
        (case,) = cases(world, item())
        started, release = threading.Event(), threading.Event()
        real = BoxStore.heartbeat_run

        def failing(self, lease):
            if started.is_set():
                raise sqlite3.OperationalError("disk I/O error")
            return real(self, lease)

        monkeypatch.setattr(BoxStore, "heartbeat_run", failing)
        held_during: list[bool] = []

        def watched(request, cancel):
            result = blocking(started, release)(request, cancel)
            held_during.append(gate_busy(world))
            return result

        result = runner(world, FakeEngine([watched]), stale_after=0.2).run(plan(world, [case]))
        assert (result.status, result.stop_reason) == ("INTERRUPTED", "HEARTBEAT_FAILED")
        assert held_during == [True]  # خطای پایگاه Gate را آزاد نکرد
        ((unit, _),) = units(world, result.run_id)
        assert unit.metadata["classification"] == "STOPPED_BY_RUNNER"
        assert world["store"].get_run(result.run_id).status == m.RUN_INTERRUPTED
        assert not gate_busy(world)

    def test_a_lost_lease_stops_without_any_further_mutation(self, world):
        (case,) = cases(world, item())
        started, release = threading.Event(), threading.Event()
        engine = FakeEngine([blocking(started, release)])
        planned = plan(world, [case])  # در threadِ اصلی (اتصالِ SQLite مالِ همین thread است)
        job = Background(lambda: runner(world, engine).run(planned))
        started.wait(20)
        run = world["store"].active_run()
        new = world["store"].take_over_run(
            run.id, expected_generation=1, stale_before="9999-12-31T23:59:59.999Z", pid=9, host="x"
        )
        result = job.join()
        assert (result.status, result.stop_reason) == ("INTERRUPTED", "LEASE_LOST")
        ((unit, case_row),) = units(world, run.id)
        assert unit.status == m.UNIT_RUNNING and case_row.status == m.CASE_RUN_QUEUED  # بی‌تغییر
        assert world["store"].owns_run(new)  # مالکِ تازه دست‌نخورده
        assert not gate_busy(world)

    def test_a_lost_gate_stops_the_session(self, world):
        (case,) = cases(world, item())
        started, release = threading.Event(), threading.Event()
        engine = FakeEngine([blocking(started, release)])
        planned = plan(world, [case])  # در threadِ اصلی (اتصالِ SQLite مالِ همین thread است)
        job = Background(lambda: runner(world, engine).run(planned))
        started.wait(20)
        with closing(
            sqlite3.connect(world["gate_dir"] / "gate.sqlite3", isolation_level=None)
        ) as c:
            c.execute("UPDATE gate_owner SET owner_token = ?", ("f" * 32,))
        result = job.join()
        assert (result.status, result.stop_reason) == ("INTERRUPTED", "GATE_LOST")
        ((unit, _),) = units(world, result.run_id)
        assert unit.metadata["classification"] == "STOPPED_BY_RUNNER"
        assert any("owner record" in note for note in result.cleanup_errors)
        assert not gate_busy(world)

    def test_an_authentication_failure_runs_nothing(self, world):
        selected = cases(world)
        engine = FakeEngine()
        result = runner(world, engine, FakeTokens(failure=AUTH_FAILURE)).run(plan(world, selected))
        assert (result.status, result.stop_reason) == ("INTERRUPTED", "AUTHENTICATION_FAILED")
        assert engine.calls == []
        assert {u.status for u, _ in units(world, result.run_id)} == {m.UNIT_NOT_RUN}

    def test_a_gate_release_failure_is_reported(self, world, monkeypatch):
        (case,) = cases(world, item())

        real = GateLease.release

        def broken(self):
            real(self)  # قفل واقعاً آزاد می‌شود؛ فقط گزارشِ پس از آن شکست می‌خورد
            raise RuntimeError("release failed")

        monkeypatch.setattr(GateLease, "release", broken)
        result = runner(world).run(plan(world, [case]))
        monkeypatch.undo()
        assert result.status == "COMPLETED"
        assert any("release failed" in note for note in result.cleanup_errors)  # پنهان نشد
        assert not gate_busy(world)


# ── artifactها و رازها ───────────────────────────────────────────────────────


class TestArtifactsAndSecrets:
    def test_unique_directories_no_overwrite_no_deletion(self, world):
        (case,) = cases(world, item())
        first = runner(world).run(plan(world, [case]))
        second = runner(world).run(plan(world, [case]))
        assert first.artifact_dir != second.artifact_dir
        assert {p.name for p in world["runs"].iterdir()} == {
            first.artifact_dir,
            second.artifact_dir,
        }
        unit_dir = world["runs"] / first.artifact_dir / "unit_001"
        entries = list(unit_dir.iterdir())
        assert len(entries) == 2 and (unit_dir / "collection.json").is_file()
        assert [p.is_dir() for p in entries if p.name != "collection.json"] == [True]  # پوشه‌ی موتور

    def test_no_token_or_secret_anywhere(self, world, caplog):
        selected = cases(world)
        observed: list[str] = []
        engine = FakeEngine(
            on_call=lambda request: observed.append(
                world["store"]
                ._db.execute("SELECT owner_token FROM box_runs WHERE owner_token IS NOT NULL")
                .fetchone()[0]
            )
        )
        result = runner(world, engine).run(plan(world, selected))
        owner_token = observed[0]
        texts = [dump(world), repr(result), caplog.text]
        for path in world["runs"].rglob("*"):
            texts.append(path.name)
            if path.is_file():
                texts.append(path.read_text(encoding="utf-8"))
        for secret in (TOKEN, f"Bearer {TOKEN}", ENV_SECRET, owner_token):
            assert all(secret not in text for text in texts), secret
        collection = (
            world["runs"] / result.artifact_dir / "unit_001" / "collection.json"
        ).read_text()
        assert "{{token}}" in collection  # فقط ارجاع؛ runtime ِ موتور تزریق می‌کند

    def test_only_safe_relative_artifact_references_are_stored(self, world):
        (case,) = cases(world, item())
        result = runner(world).run(plan(world, [case]))
        ((unit, _),) = units(world, result.run_id)
        artifacts = unit.metadata["artifacts"]
        assert set(artifacts) == {"run_dir", "result_json"}
        assert all(
            not Path(v).is_absolute() and v.startswith("unit_001/") for v in artifacts.values()
        )
        assert world["store"].get_run(result.run_id).metadata["artifact_dir"] == result.artifact_dir


# ── اصلاحِ ممیزی ۱: پایانِ مبهمِ پروسه هرگز Gate را آزاد نمی‌کند ─────────────────


class _ProcessRunner:
    """جای run_process در موتورِ واقعی (بی‌Node): raise پس از «شروعِ» پروسه، یا outcome ِ داده‌شده."""

    def __init__(self, raise_after_start: BaseException | None = None, outcome=None) -> None:
        self.raise_after_start, self.outcome, self.calls = raise_after_start, outcome, 0

    def __call__(self, argv, timeout_s, cwd, **kwargs):
        self.calls += 1
        if self.raise_after_start is not None:
            raise self.raise_after_start
        return self.outcome


def _real_engine(world, process_runner):
    from src.test_execution.newman import NewmanExecutionEngine
    from tests.unit.test_execution.newman_fakes import INSTALLATION

    return NewmanExecutionEngine(
        process_runner=process_runner,
        installation=INSTALLATION,
        scratch_root=world["tmp"] / "scratch",
    )


_STUCK = [(m.UNIT_RUNNING, m.CASE_RUN_QUEUED), (m.UNIT_QUEUED, m.CASE_RUN_QUEUED)]


def _assert_quarantined(world, result, *, units_expected):
    assert (result.status, result.stop_reason) == (
        "TERMINATION_UNCONFIRMED",
        "TERMINATION_UNCONFIRMED",
    )
    assert gate_busy(world)  # Gate قرنطینه و آزادنشده
    assert len(runner_module._UNRELEASED_GATES) == 1
    run = world["store"].get_run(result.run_id)
    assert run.status == m.RUN_RUNNING and "summary" not in run.metadata  # نهایی نشد
    assert [(u.status, c.status) for u, c in units(world, run.id)] == units_expected


class TestAmbiguousTermination:
    def test_a_process_tree_kill_timeout_never_releases_the_gate(self, world):
        import subprocess

        first, second = cases(world, item(path="a"), item(path="b"))
        process = _ProcessRunner(raise_after_start=subprocess.TimeoutExpired(["taskkill"], 30))
        result = runner(world, _real_engine(world, process)).run(plan(world, [first, second]))
        assert process.calls == 1  # unitِ دوم هرگز شروع نشد
        _assert_quarantined(world, result, units_expected=_STUCK)

    def test_an_exception_after_start_before_the_pid_is_returned(self, world):
        first, second = cases(world, item(path="a"), item(path="b"))
        process = _ProcessRunner(raise_after_start=OSError("pipe broke after Popen"))
        result = runner(world, _real_engine(world, process)).run(plan(world, [first, second]))
        assert process.calls == 1
        _assert_quarantined(world, result, units_expected=_STUCK)

    def test_internal_error_without_a_pid_is_unconfirmed(self, world):
        first, second = cases(world, item(path="a"), item(path="b"))

        def internal(request, cancel):
            return dataclasses.replace(
                published(request, status="ERROR", error_kind="INTERNAL_ERROR", artifacts=False),
                process_id=None,
                exit_code=None,
            )

        engine = FakeEngine([internal])
        result = runner(world, engine).run(plan(world, [first, second]))
        assert len(engine.calls) == 1
        _assert_quarantined(world, result, units_expected=_STUCK)

    def test_a_pass_without_a_pid_is_not_proof_either(self, world):
        (case,) = cases(world, item())

        def odd(request, cancel):
            return dataclasses.replace(published(request), process_id=None)

        result = runner(world, FakeEngine([odd])).run(plan(world, [case]))
        assert result.status == "TERMINATION_UNCONFIRMED" and gate_busy(world)

    def test_a_real_engine_start_failure_is_a_confirmed_pre_start_failure(self, world):
        from src.test_execution.newman.process import ProcessOutcome

        first, second = cases(world, item(path="a"), item(path="b"))
        process = _ProcessRunner(outcome=ProcessOutcome(started=False, error="FileNotFoundError"))
        result = runner(world, _real_engine(world, process)).run(plan(world, [first, second]))
        assert result.status == "COMPLETED" and process.calls == 2  # unitِ بعدی هم اجرا شد
        assert [u.status for u, _ in units(world, result.run_id)] == [m.UNIT_ERROR, m.UNIT_ERROR]
        assert not gate_busy(world) and runner_module._UNRELEASED_GATES == []

    def test_a_pre_start_engine_error_follows_the_safe_cleanup_path(self, world):
        first, second = cases(world, item(path="a"), item(path="b"))

        def missing_node(request, cancel):
            return dataclasses.replace(
                published(request, status="ERROR", error_kind="NODE_NOT_FOUND"),
                process_id=None,
                exit_code=None,
            )

        result = runner(world, FakeEngine([missing_node])).run(plan(world, [first, second]))
        assert (result.status, result.stop_reason) == ("INTERRUPTED", "ENGINE_ENVIRONMENT_ERROR")
        assert world["store"].get_run(result.run_id).status == m.RUN_INTERRUPTED
        assert not gate_busy(world)

    def test_a_cancellation_before_the_process_started_is_confirmed(self, world):
        (case,) = cases(world, item())

        def cancelled_before_start(request, cancel):
            return dataclasses.replace(
                published(request, status="ERROR", error_kind="PROCESS_CANCELLED", cancelled=True),
                process_id=None,
                exit_code=None,
            )

        result = runner(world, FakeEngine([cancelled_before_start])).run(plan(world, [case]))
        assert result.status != "TERMINATION_UNCONFIRMED" and not gate_busy(world)


_NO_PID = {"process_id": None, "exit_code": None, "status": "ERROR"}


@pytest.mark.parametrize(
    ("changes", "confirmed"),
    [
        ({"process_id": 1, "exit_code": 0}, True),
        ({"process_id": 1, "exit_code": None}, False),
        ({**_NO_PID, "error_kind": "INTERNAL_ERROR"}, False),
        ({**_NO_PID, "error_kind": "SANITIZATION_FAILED"}, False),
        ({**_NO_PID, "error_kind": ""}, False),
        ({**_NO_PID, "status": "PASS"}, False),
        ({**_NO_PID, "error_kind": "PROCESS_START_FAILED"}, True),
        ({**_NO_PID, "error_kind": "PRIVATE_DIRECTORY"}, True),
        ({**_NO_PID, "error_kind": "PROCESS_CANCELLED", "cancelled": True}, True),
        ({**_NO_PID, "error_kind": "PROCESS_CANCELLED", "cancelled": False}, False),
    ],
)
def test_termination_confirmation_requires_positive_proof(changes, confirmed):
    from src.test_execution.newman.models import NewmanRunResult

    result = dataclasses.replace(NewmanRunResult(run_id="r", status="ERROR"), **changes)
    assert runner_module.termination_confirmed(result) is confirmed


# ── اصلاحِ ممیزی ۳: runِ کامل با درخواستِ دیرهنگام برچسبِ نادرست نمی‌گیرد ──────────


class TestLateRequestsAfterTheFinalUnit:
    def test_a_cancellation_after_the_final_unit_keeps_the_run_completed(self, world):
        first, second = cases(world, item(path="a"), item(path="b"))
        box = None

        def last_then_cancel(request, cancel):
            world["store"].request_run_cancel(world["store"].active_run().id)
            wait_for(lambda: box._monitor.cancel_requested.is_set())
            return published(request)

        engine = FakeEngine([outcome(), last_then_cancel])
        box = runner(world, engine)
        result = box.run(plan(world, [first, second]))
        assert (result.status, result.stop_reason) == ("COMPLETED", "")
        run = world["store"].get_run(result.run_id)
        assert run.status == m.RUN_COMPLETED and "stop_reason" not in run.metadata
        assert [u.status for u, _ in units(world, run.id)] == [m.UNIT_PASS, m.UNIT_PASS]

    def test_a_configuration_change_after_the_final_unit_keeps_the_run_completed(self, world):
        first, second = cases(world, item(path="a"), item(path="b"))
        calls = []

        def change_on_last(request):
            calls.append(1)
            if len(calls) == 2:
                world["globals"].write_text(
                    '{"name": "G", "_postman_variable_scope": "globals", "values": []} '
                )

        result = runner(world, FakeEngine(on_call=change_on_last)).run(plan(world, [first, second]))
        assert result.status == "COMPLETED"
        assert world["store"].get_run(result.run_id).status == m.RUN_COMPLETED

    def test_a_cancellation_before_the_final_unit_is_still_a_cancellation(self, world):
        first, second, third = cases(world)
        box = None

        def cancel_after_first(request, cancel):
            world["store"].request_run_cancel(world["store"].active_run().id)
            wait_for(lambda: box._monitor.cancel_requested.is_set())
            return published(request)

        engine = FakeEngine([cancel_after_first])
        box = runner(world, engine)
        result = box.run(plan(world, [first, second, third]))
        assert result.status == "CANCELLED" and len(engine.calls) == 1
        assert [u.status for u, _ in units(world, result.run_id)] == [
            m.UNIT_PASS,
            m.UNIT_CANCELLED,
            m.UNIT_CANCELLED,
        ]
