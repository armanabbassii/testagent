"""
tests/unit/testcase_box/test_run_planning.py — برنامه‌ریزیِ run (M7.4) روی پایگاهِ موقت

فقط خواندن: هر تست بررسی می‌کند که برنامه‌ریزی هیچ چیزی در پایگاه ننوشته است. بی‌شبکه، بی‌Newman.
"""

from __future__ import annotations

import pytest

from src.testcase_box import models as m
from src.testcase_box.db import NotFoundError
from src.testcase_box.models import NewCase, NewRun, NewRunCase, NewRunUnit, NewSource, NewVersion
from src.testcase_box.profiles import (
    ExecutionProfile,
    HostIntent,
    variable_profile_from_documents,
)
from src.testcase_box.run_planning import (
    ENTRY_BLOCKED,
    ENTRY_ELIGIBLE,
    PLAN_NEW,
    PLAN_RERUN,
    RunPlanningError,
    plan_new_run,
    plan_rerun,
    script_free_findings,
)
from src.testcase_box.run_profile import RunProfile
from src.testcase_box.storage import BoxStore

PROFILE = RunProfile(HostIntent.NORMAL)
VARIABLES = ExecutionProfile("dev", resolved_variables={"base_url", "base_url_vdi", "userId"})


def item(*events, path: str = "users", **extra) -> dict:
    payload = {
        "name": "TC",
        "request": {
            "method": "GET",
            "header": [{"key": "Authorization", "value": "{{token}}"}],
            "url": {
                "raw": "{{base_url}}/api/" + path + "/{{userId}}",
                "host": ["{{base_url}}"],
                "path": ["api", path, "{{userId}}"],
            },
        },
        **extra,
    }
    if events:
        payload["event"] = list(events)
    return payload


def event(text: str = "pm.test('x', () => {})", listen: str = "test") -> dict:
    return {"listen": listen, "script": {"type": "text/javascript", "exec": [text]}}


@pytest.fixture
def store(tmp_path):
    with BoxStore.open(tmp_path / "box.sqlite3") as opened:
        yield opened


def _case(store, title="Case", payload=None, sources=()) -> m.BoxCase:
    return store.create_case(NewCase(title=title, payload=payload or item(), sources=sources))


def _snapshot(store) -> dict:
    tables = ("box_cases", "box_case_versions", "box_case_sources", "box_runs", "box_run_units")
    return {t: store._db.execute(f"SELECT * FROM {t} ORDER BY id").fetchall() for t in tables}


def _codes(entry) -> set[str]:
    return {f.code for f in entry.blocking}


def _plan(store, ids, **kwargs):
    before = _snapshot(store)
    plan = plan_new_run(store, ids, profile=PROFILE, variables=VARIABLES, **kwargs)
    assert [tuple(r) for rows in _snapshot(store).values() for r in rows] == [
        tuple(r) for rows in before.values() for r in rows
    ]  # برنامه‌ریزی هیچ چیزی ننوشت
    return plan


class TestEligibility:
    def test_a_valid_script_free_case_is_eligible(self, store):
        case = _case(store)
        (entry,) = _plan(store, [case.id]).entries
        assert entry.eligibility == ENTRY_ELIGIBLE and entry.blocking == ()
        assert entry.version_id == case.current_version_id and entry.version_number == 1
        assert entry.preparation_status == "PREPARED"
        assert entry.prepared_fingerprint == m.sha256_json(entry.prepared_payload)
        assert entry.case_key == m.case_key(case.id) and entry.unit_number == 1

    def test_a_case_with_scripts_is_blocked_even_when_approved(self, store):
        case = _case(store, payload=item(event()))
        version = store.current_version(case.id)
        store.record_script_decision(version.id, version.script_fingerprint, m.APPROVAL_APPROVED)
        (entry,) = _plan(store, [case.id]).entries
        assert entry.eligibility == ENTRY_BLOCKED and entry.prepared_payload is None
        assert "EXECUTION_SCRIPTS_PRESENT" in _codes(entry)

    def test_d7_warnings_stay_visible_and_never_make_a_case_eligible(self, store):
        token_reader = event("const t = pm.environment.get('token');", listen="prerequest")
        mutator = event("pm.request.headers.add({key: 'X', value: '1'});", listen="prerequest")
        case = _case(store, payload=item(token_reader, mutator))
        version = store.current_version(case.id)
        store.record_script_decision(version.id, version.script_fingerprint, m.APPROVAL_APPROVED)
        (entry,) = _plan(store, [case.id]).entries
        warnings = {f.code for f in entry.warnings}
        assert {"SCRIPT_TOKEN_ACCESS", "SCRIPT_MUTATES_REQUEST"} <= warnings
        assert not entry.eligible and "EXECUTION_SCRIPTS_PRESENT" in _codes(entry)

    def test_an_empty_event_entry_blocks_and_is_never_removed(self, store):
        empty = {"listen": "test", "script": {"exec": [""]}}
        case = _case(store, payload=item(empty))
        (entry,) = _plan(store, [case.id]).entries
        assert _codes(entry) == {"EXECUTION_EVENTS_PRESENT"}
        assert store.current_version(case.id).payload["event"] == [empty]  # دست‌نخورده

    def test_an_empty_event_array_blocks_and_is_never_removed(self, store):
        case = _case(store, payload=item(event=[]))
        (entry,) = _plan(store, [case.id]).entries
        assert not entry.eligible and entry.prepared_payload is None
        assert _codes(entry) == {"EXECUTION_EVENTS_PRESENT"}
        assert store.current_version(case.id).payload["event"] == []  # حذف نشد

    def test_an_absent_or_null_event_is_allowed(self, store):
        absent, null = (
            _case(store, payload=item(path="a")),
            _case(store, payload=item(path="n", event=None)),
        )
        plan = _plan(store, [absent.id, null.id])
        assert [e.eligible for e in plan.entries] == [True, True]

    def test_an_archived_case_is_blocked_in_a_new_run(self, store):
        case = _case(store)
        store.archive_case(case.id)
        (entry,) = _plan(store, [case.id]).entries
        assert _codes(entry) == {"CASE_ARCHIVED"} and entry.case_status == m.CASE_ARCHIVED

    def test_a_missing_case_is_reported_not_dropped(self, store):
        case = _case(store)
        plan = _plan(store, [case.id, 999])
        assert [e.case_id for e in plan.entries] == [case.id, 999]
        assert _codes(plan.entries[1]) == {"CASE_NOT_FOUND"}

    def test_an_invalid_test_case_is_blocked_with_its_preparation_findings(self, store):
        case = _case(store, payload={"name": "broken", "request": {"method": "GET"}})
        (entry,) = _plan(store, [case.id]).entries
        codes = _codes(entry)
        assert "EXECUTION_NOT_PREPARED" in codes and "URL_MISSING" in codes
        assert entry.preparation_status == "NOT_PREPARABLE"

    def test_an_unresolved_variable_needs_configuration(self, store):
        case = _case(store)
        variables = ExecutionProfile("dev", resolved_variables={"base_url"})
        (entry,) = plan_new_run(store, [case.id], profile=PROFILE, variables=variables).entries
        assert entry.preparation_status == "NEEDS_CONFIGURATION"
        assert {"EXECUTION_NOT_PREPARED", "VARIABLE_UNAVAILABLE"} <= _codes(entry)

    def test_the_host_intent_of_the_profile_is_enforced(self, store):
        case = _case(store)
        vdi = RunProfile(HostIntent.VDI)
        (entry,) = plan_new_run(store, [case.id], profile=vdi, variables=VARIABLES).entries
        assert "HOST_INTENT_MISMATCH" in _codes(entry)

    def test_a_tampered_version_fingerprint_is_not_trusted(self, store):
        case = _case(store)
        store._db.execute("DROP TRIGGER tr_box_case_versions_no_update")  # فقط پایگاهِ موقت
        store._db.execute("UPDATE box_case_versions SET fingerprint = ?", ("a" * 64,))
        (entry,) = plan_new_run(store, [case.id], profile=PROFILE, variables=VARIABLES).entries
        assert _codes(entry) == {"VERSION_INTEGRITY_FAILED"}


class TestSelection:
    def test_mixed_selections_keep_every_entry_in_order(self, store):
        good, scripted, archived = _case(store), _case(store, payload=item(event())), _case(store)
        store.archive_case(archived.id)
        plan = _plan(store, [scripted.id, good.id, archived.id, 0, good.id])
        assert [e.position for e in plan.entries] == [1, 2, 3, 4, 5]
        assert [e.eligibility for e in plan.entries] == [
            ENTRY_BLOCKED,
            ENTRY_ELIGIBLE,
            ENTRY_BLOCKED,
            ENTRY_BLOCKED,
            ENTRY_BLOCKED,
        ]
        assert _codes(plan.entries[3]) == {"SELECTION_INVALID"}
        assert _codes(plan.entries[4]) == {"SELECTION_DUPLICATE"}
        assert [e.position for e in plan.eligible_entries] == [2]
        assert len(plan.blocked_entries) == 4 and plan.executable

    def test_a_fully_blocked_plan_is_represented_not_raised(self, store):
        scripted = _case(store, payload=item(event()))
        plan = _plan(store, [scripted.id])
        assert not plan.executable and len(plan.blocked_entries) == 1

    @pytest.mark.parametrize("selection", [[], (), "12", None])
    def test_an_empty_or_malformed_selection_is_refused(self, store, selection):
        with pytest.raises(RunPlanningError):
            plan_new_run(store, selection, profile=PROFILE, variables=VARIABLES)

    def test_the_profile_types_are_enforced(self, store):
        with pytest.raises(TypeError):
            plan_new_run(store, [1], profile={"schema": 1}, variables=VARIABLES)
        with pytest.raises(TypeError):
            plan_new_run(store, [1], profile=PROFILE, variables={"base_url"})


class TestDeterminismAndProvenance:
    def test_identical_inputs_produce_identical_plans(self, store):
        cases = [_case(store, title=f"C{n}") for n in range(3)]
        ids = [cases[2].id, cases[0].id, cases[1].id]
        first, second = _plan(store, ids), _plan(store, list(ids))
        assert first.to_dict() == second.to_dict() and first.fingerprint == second.fingerprint
        assert [e.case_id for e in first.entries] == ids  # ترتیبِ انتخاب
        other = plan_new_run(
            store,
            ids,
            profile=RunProfile(HostIntent.NORMAL, request_timeout_ms=5_000),
            variables=VARIABLES,
        )
        assert other.fingerprint != first.fingerprint  # snapshot ِ پروفایل بخشی از برنامه است

    def test_provenance_is_preserved(self, store):
        payload = item()
        source = NewSource(
            m.SOURCE_BATCH,
            "PODIUM-1/attempt_1/TC-001",
            {"item_fingerprint": m.sha256_json(payload), "execution_id": "e1"},
        )
        case = store.create_case_for_source(
            NewCase(title="Imported", payload=payload, sources=(source,))
        ).case
        (entry,) = _plan(store, [case.id]).entries
        assert [(s.source_type, s.source_reference, s.case_version_id) for s in entry.sources] == [
            (m.SOURCE_BATCH, "PODIUM-1/attempt_1/TC-001", case.current_version_id)
        ]
        assert entry.to_dict()["sources"][0]["source_reference"] == "PODIUM-1/attempt_1/TC-001"

    def test_the_snapshot_has_names_but_no_values_or_payloads(self, store):
        case = _case(store)
        data = _plan(store, [case.id]).to_dict()
        assert data["variables"] == {
            "profile_id": "dev",
            "resolved": ["base_url", "base_url_vdi", "userId"],
            "empty": [],
        }
        assert "prepared_payload" not in data["entries"][0]
        assert data["kind"] == PLAN_NEW and data["profile"] == PROFILE.to_dict()


def _finished_run(store, cases) -> int:
    run = store.create_run(
        NewRun(
            units=tuple(
                NewRunUnit(n, (NewRunCase(c.id, c.current_version_id, 1),))
                for n, c in enumerate(cases, 1)
            )
        )
    )
    for run_case in store.run_cases(run.id):
        store.set_run_case_status(run_case.id, m.CASE_RUN_CANCELLED)
    for unit in store.run_units(run.id):
        store.set_unit_status(unit.id, m.UNIT_CANCELLED)
    store.set_run_status(run.id, m.RUN_CANCELLED)
    return run.id


class TestRerun:
    def test_a_rerun_uses_the_pinned_versions_and_original_order(self, store):
        first, second = _case(store, title="A"), _case(store, title="B")
        run_id = _finished_run(store, [second, first])
        pinned = store.current_version(first.id).id
        store.add_version(first.id, NewVersion(payload=item(path="changed")))  # نسخه‌ی تازه
        plan = plan_rerun(store, run_id, profile=PROFILE, variables=VARIABLES)
        assert plan.kind == PLAN_RERUN and plan.rerun_of_run_id == run_id
        assert [e.case_id for e in plan.entries] == [second.id, first.id]
        assert plan.entries[1].version_id == pinned  # هرگز نسخه‌ی جاری
        assert plan.entries[1].version_number == 1
        assert store.current_version(first.id).version_number == 2

    def test_an_archived_case_runs_in_a_rerun_with_its_pinned_version(self, store):
        case = _case(store)
        run_id = _finished_run(store, [case])
        store.archive_case(case.id)
        (entry,) = plan_rerun(store, run_id, profile=PROFILE, variables=VARIABLES).entries
        assert entry.eligible and entry.case_status == m.CASE_ARCHIVED

    def test_a_missing_pinned_version_blocks_the_entry(self, store, monkeypatch):
        case = _case(store)
        run_id = _finished_run(store, [case])
        real = store.get_version

        def missing(version_id):
            if version_id == case.current_version_id:
                raise NotFoundError(f"Version {version_id} does not exist.")
            return real(version_id)

        monkeypatch.setattr(store, "get_version", missing)
        (entry,) = plan_rerun(store, run_id, profile=PROFILE, variables=VARIABLES).entries
        assert _codes(entry) == {"PINNED_VERSION_MISSING"} and not entry.eligible

    def test_a_pinned_version_of_another_case_is_invalid(self, store, monkeypatch):
        case, other = _case(store, title="A"), _case(store, title="B")
        run_id = _finished_run(store, [case])
        foreign = store.current_version(other.id)
        monkeypatch.setattr(store, "get_version", lambda version_id: foreign)
        (entry,) = plan_rerun(store, run_id, profile=PROFILE, variables=VARIABLES).entries
        assert _codes(entry) == {"PINNED_VERSION_INVALID"}

    def test_only_finished_existing_runs_can_be_rerun(self, store):
        case = _case(store)
        open_run = store.create_run(
            NewRun(units=(NewRunUnit(1, (NewRunCase(case.id, case.current_version_id, 1),)),))
        )
        with pytest.raises(RunPlanningError, match="finished"):
            plan_rerun(store, open_run.id, profile=PROFILE, variables=VARIABLES)
        with pytest.raises(RunPlanningError, match="does not exist"):
            plan_rerun(store, 999, profile=PROFILE, variables=VARIABLES)


class TestScriptFreePolicy:
    @pytest.mark.parametrize(
        ("payload", "codes"),
        [
            (item(), set()),
            (item(event=[]), {"EXECUTION_EVENTS_PRESENT"}),  # آرایه‌ی خالی هم مسدود است
            (item(response=[{"event": []}]), {"EXECUTION_EVENTS_PRESENT"}),  # در هر عمقی
            (item(event=None), set()),
            (item(event()), {"EXECUTION_SCRIPTS_PRESENT"}),
            (item(event(listen="prerequest")), {"EXECUTION_SCRIPTS_PRESENT"}),
            (item({"listen": "test", "script": {"exec": [""]}}), {"EXECUTION_EVENTS_PRESENT"}),
            (
                item(event="pm.test()"),
                {"EXECUTION_SCRIPTS_PRESENT", "EXECUTION_SCRIPT_STRUCTURE_INVALID"},
            ),
            (
                item({"listen": "unknown", "script": {"exec": [""]}}),
                {"EXECUTION_SCRIPT_STRUCTURE_INVALID"},
            ),
            (
                item(["not-a-dict"]),
                {"EXECUTION_SCRIPTS_PRESENT", "EXECUTION_SCRIPT_STRUCTURE_INVALID"},
            ),
            (
                item(response=[{"event": [event()]}]),  # اسکریپت در عمقِ دیگر
                {"EXECUTION_SCRIPTS_PRESENT"},
            ),
            ("not-a-dict", {"EXECUTION_SCRIPT_STRUCTURE_INVALID"}),
        ],
    )
    def test_scripts_are_detected_at_every_location(self, payload, codes):
        stored = m.script_fingerprint(payload) if isinstance(payload, dict) else None
        assert {f.code for f in script_free_findings(payload, stored)} >= codes
        if not codes:
            assert script_free_findings(payload, stored) == ()

    def test_a_fingerprint_disagreement_is_not_script_free(self):
        assert {f.code for f in script_free_findings(item(), "b" * 64)} == {
            "EXECUTION_SCRIPTS_PRESENT",
            "EXECUTION_SCRIPT_STRUCTURE_INVALID",
        }


class TestVariableProfile:
    def test_environment_overrides_globals_and_values_never_leave(self):
        environment = {
            "values": [
                {"key": "base_url", "value": "https://env.example", "enabled": True},
                {"key": "userId", "value": "  "},
                {"key": "token", "value": "raw-token-value"},
                {"key": "disabled", "value": "x", "enabled": False},
                {"key": "bad{name}", "value": "x"},
            ]
        }
        globals_document = {
            "values": [
                {"key": "userId", "value": "42"},
                {"key": "base_url_vdi", "value": "https://vdi.example"},
                {"key": "count", "value": 3},
                {"key": "nothing", "value": None},
            ]
        }
        profile = variable_profile_from_documents("dev", environment, globals_document)
        assert profile.resolved_variables == {"base_url", "base_url_vdi", "count"}
        assert profile.empty_variables == {"userId", "nothing"}  # مقدارِ خالیِ environment برنده است
        text = repr(profile)
        assert "env.example" not in text and "raw-token-value" not in text and "42" not in text

    @pytest.mark.parametrize("document", [None, [], {"values": "x"}, {"other": []}])
    def test_malformed_documents_are_rejected(self, document):
        with pytest.raises(ValueError):
            variable_profile_from_documents("dev", document, {"values": []})
