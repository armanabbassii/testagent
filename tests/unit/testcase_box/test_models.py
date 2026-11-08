"""
تست‌های مدل‌ها، اعتبارسنجیِ ورودی، JSON و اثرانگشتِ TestCase Box.
"""

from __future__ import annotations

import math

import pytest

from src.testcase_box import models as m
from src.testcase_box.models import (
    InvalidTransitionError,
    NewCase,
    NewLink,
    NewRun,
    NewRunCase,
    NewRunUnit,
    NewSource,
    NewVersion,
)


def _item(script: str | None = None) -> dict:
    item = {"name": "TC-001", "request": {"method": "GET", "url": {"raw": "{{base_url}}/a"}}}
    if script is not None:
        item["event"] = [
            {"listen": "test", "script": {"type": "text/javascript", "exec": [script]}}
        ]
    return item


class TestJson:
    def test_round_trip_is_canonical(self):
        value = {"b": [1, 2.5, None, True], "a": {"ü": "متن"}}
        text = m.to_json(value)
        assert text.startswith('{"a"')
        assert m.from_json(text) == value

    @pytest.mark.parametrize(
        "value",
        [
            {"x": object()},
            {"x": math.nan},
            {"x": math.inf},
            {"x": (1, 2)},
            {1: "non-string key"},
            {"x": {1, 2}},
        ],
    )
    def test_values_that_do_not_round_trip_are_rejected(self, value):
        with pytest.raises(ValueError):
            m.to_json(value)

    def test_from_json_checks_root_type(self):
        assert m.from_json("[1]", list) == [1]
        with pytest.raises(ValueError):
            m.from_json("[1]", dict)
        with pytest.raises(ValueError):
            m.from_json("{not json")


class TestFingerprints:
    def test_payload_fingerprint_ignores_key_order(self):
        assert m.sha256_json({"a": 1, "b": 2}) == m.sha256_json({"b": 2, "a": 1})
        assert m.is_sha256(m.sha256_json({}))

    def test_no_scripts_means_no_script_fingerprint(self):
        assert m.script_fingerprint(_item()) is None
        blank = _item("   ")
        assert m.payload_scripts(blank) == []
        assert m.script_fingerprint(blank) is None

    def test_script_change_changes_fingerprint(self):
        first = m.script_fingerprint(_item("pm.test('a', () => {});"))
        second = m.script_fingerprint(_item("pm.test('b', () => {});"))
        assert m.is_sha256(first) and m.is_sha256(second)
        assert first != second

    def test_scripts_found_at_any_depth(self):
        payload = {
            "profile": {
                "folders": [{"event": [{"listen": "prerequest", "script": {"exec": "x()"}}]}]
            }
        }
        scripts = m.payload_scripts(payload)
        assert [s["path"] for s in scripts] == ["$.profile.folders[0].event[0]"]
        assert m.payload_scripts({"event": [{"script": {"src": "https://x/s.js"}}]})

    def test_case_key(self):
        assert m.case_key(123) == "BX-000123"


class TestInputValidation:
    def test_case_requires_title_and_object_payload(self):
        with pytest.raises(ValueError):
            NewCase(title="  ", payload={})
        with pytest.raises(ValueError):
            NewCase(title="x" * (m.TITLE_MAX_CHARS + 1), payload={})
        with pytest.raises(ValueError):
            NewCase(title="ok", payload=["not", "object"])
        with pytest.raises(ValueError):
            NewCase(title="ok", payload={"x": object()})
        with pytest.raises(ValueError):
            NewCase(title="ok", payload={}, fingerprint="ABC")

    def test_version_validates_payload_and_fingerprint(self):
        with pytest.raises(ValueError):
            NewVersion(payload={"x": math.nan})
        with pytest.raises(ValueError):
            NewVersion(payload={}, fingerprint="0" * 63)
        NewVersion(payload={}, fingerprint="0" * 64)

    def test_only_known_source_types(self):
        for source_type in m.KNOWN_SOURCE_TYPES:
            NewSource(source_type, "ref")
        with pytest.raises(ValueError):
            NewSource("JIRA", "ref")
        with pytest.raises(ValueError):
            NewSource(m.SOURCE_BATCH, " ")

    def test_link_validation(self):
        with pytest.raises(ValueError):
            NewLink(0)
        with pytest.raises(ValueError):
            NewLink(1, "BLOCKS")

    def test_run_shape_validation(self):
        case = NewRunCase(case_id=1, case_version_id=1, execution_order=1)
        with pytest.raises(ValueError):
            NewRun(units=())
        with pytest.raises(ValueError):
            NewRunUnit(unit_number=1, cases=())
        with pytest.raises(ValueError):
            NewRunUnit(unit_number=1, cases=(case, case))
        with pytest.raises(ValueError):
            NewRun(
                units=(
                    NewRunUnit(1, (case,)),
                    NewRunUnit(1, (NewRunCase(case_id=2, case_version_id=2, execution_order=1),)),
                )
            )
        with pytest.raises(ValueError):
            NewRun(
                units=(
                    NewRunUnit(1, (case,)),
                    NewRunUnit(2, (NewRunCase(case_id=1, case_version_id=1, execution_order=1),)),
                )
            )
        with pytest.raises(ValueError):
            NewRunCase(case_id=1, case_version_id=1, execution_order=1, status=m.CASE_RUN_PASS)


class TestTransitions:
    def test_final_states_have_no_exits(self):
        for final, check in (
            (m.RUN_FINAL, m.check_run_transition),
            (m.UNIT_FINAL, m.check_unit_transition),
            (m.RUN_CASE_FINAL, m.check_run_case_transition),
            (m.IMPORT_FINAL, m.check_import_transition),
        ):
            for state in final:
                with pytest.raises(InvalidTransitionError):
                    check(state, m.RUN_QUEUED)

    def test_allowed_transitions(self):
        m.check_run_transition(m.RUN_QUEUED, m.RUN_RUNNING)
        m.check_run_transition(m.RUN_RUNNING, m.RUN_COMPLETED)
        m.check_unit_transition(m.UNIT_RUNNING, m.UNIT_FAIL)
        m.check_run_case_transition(m.CASE_RUN_QUEUED, m.CASE_RUN_SKIPPED)
        with pytest.raises(InvalidTransitionError):
            m.check_run_transition(m.RUN_QUEUED, m.RUN_COMPLETED)
        with pytest.raises(InvalidTransitionError):
            m.check_unit_transition(m.UNIT_QUEUED, m.UNIT_PASS)
        with pytest.raises(InvalidTransitionError):
            m.check_run_case_transition(m.CASE_RUN_QUEUED, m.CASE_RUN_NOT_EXECUTABLE)


_EXEC = {"listen": "test", "script": {"type": "text/javascript", "exec": ["pm.test('x');"]}}

# شکل‌هایی که postman-collection اجرا می‌کند یا قابلِ دسته‌بندیِ مطمئن نیستند (fail-closed)
DETECTED = {
    "list event": {"event": [_EXEC]},
    "single-object event": {"event": _EXEC},
    "string script": {"event": [{"listen": "test", "script": "pm.test('x');"}]},
    "array script": {"event": [{"listen": "prerequest", "script": ["a();", "b();"]}]},
    "object script exec string": {"event": [{"listen": "test", "script": {"exec": "x()"}}]},
    "object script src": {"event": [{"script": {"src": "https://example.test/s.js"}}]},
    "object script packages": {"event": [{"script": {"packages": {"lib": {"id": "1"}}}}]},
    "object script unknown key": {"event": [{"script": {"future": "x"}}]},
    "nested folder event": {"profile": {"folders": [{"event": _EXEC}]}},
    "malformed event scalar": {"event": "pm.test('x');"},
    "malformed event number": {"event": 5},
    "malformed entry": {"event": [7]},
    "malformed script number": {"event": [{"script": 5}]},
    "malformed script bool": {"event": [{"script": True}]},
    "malformed exec number": {"event": [{"script": {"exec": 5}}]},
    "malformed exec object": {"event": [{"script": {"exec": {"a": 1}}}]},
    "malformed exec item": {"event": [{"script": {"exec": [None]}}]},
}

NOT_DETECTED = {
    "no event": {"request": {"method": "GET"}},
    "empty list": {"event": []},
    "empty object": {"event": {}},
    "null event": {"event": None},
    "blank string event": {"event": "  "},
    "null entry": {"event": [None]},
    "listen only": {"event": [{"listen": "test"}]},
    "null script": {"event": [{"listen": "test", "script": None}]},
    "blank string script": {"event": [{"listen": "test", "script": "   "}]},
    "blank array script": {"event": [{"listen": "test", "script": ["", "  "]}]},
    "empty object script": {"event": [{"listen": "test", "script": {}}]},
    "inert keys only": {
        "event": [{"script": {"id": "1", "type": "text/javascript", "name": "n", "exec": []}}]
    },
    "blank src": {"event": [{"script": {"exec": [" "], "src": ""}}]},
}


class TestScriptDetection:
    @pytest.mark.parametrize("payload", DETECTED.values(), ids=DETECTED.keys())
    def test_executable_or_uncertain_structures_get_a_fingerprint(self, payload):
        assert m.payload_scripts(payload)
        assert m.is_sha256(m.script_fingerprint(payload))

    @pytest.mark.parametrize("payload", NOT_DETECTED.values(), ids=NOT_DETECTED.keys())
    def test_empty_structures_have_no_scripts(self, payload):
        assert m.payload_scripts(payload) == []
        assert m.script_fingerprint(payload) is None

    def test_single_object_event_path(self):
        assert [s["path"] for s in m.payload_scripts({"event": _EXEC})] == ["$.event"]
        assert [s["path"] for s in m.payload_scripts({"event": [{}, _EXEC]})] == ["$.event[1]"]

    def test_only_the_running_entries_are_recorded_raw(self):
        payload = {"event": [{"listen": "test"}, {"listen": "test", "script": "x()"}]}
        assert m.payload_scripts(payload) == [
            {"path": "$.event[1]", "event": {"listen": "test", "script": "x()"}}
        ]

    def test_fingerprint_is_deterministic_and_bound_to_content_and_path(self):
        base = {"a": 1, "event": [_EXEC]}
        assert m.script_fingerprint(base) == m.script_fingerprint({"event": [_EXEC], "a": 1})
        assert m.script_fingerprint(base) == m.script_fingerprint({"a": 2, "event": [_EXEC]})
        moved = {"event": [{}, _EXEC]}
        nested = {"item": {"event": [_EXEC]}}
        edited = {"event": [{"listen": "test", "script": {"exec": ["pm.test('y');"]}}]}
        listen = {"event": [{**_EXEC, "listen": "prerequest"}]}
        fingerprints = {m.script_fingerprint(p) for p in (base, moved, nested, edited, listen)}
        assert len(fingerprints) == 5


# ── M2.2 / L-a: newer Postman empty-script export ─────────────────────────────


def _script_event(script) -> dict:
    return {"event": [{"listen": "test", "script": script}]}


class TestEmptyPackagesMetadata:
    def test_exact_empty_export_is_blank(self):
        payload = _script_event({"exec": [""], "type": "text/javascript", "packages": {}})
        assert m.payload_scripts(payload) == []
        assert m.script_fingerprint(payload) is None

    @pytest.mark.parametrize(
        "script",
        [
            {"exec": [""], "type": "text/javascript", "packages": {"lib": {"id": "1"}}},
            {"exec": ["pm.test('x');"], "type": "text/javascript", "packages": {}},
            {"exec": [""], "packages": {}, "future": "x"},
            {"exec": [""], "packages": {}, "src": "https://example.test/s.js"},
            {"exec": [""], "packages": "lib"},
            {"exec": 5, "packages": {}},
        ],
        ids=[
            "non-empty packages",
            "real exec",
            "unknown key",
            "src",
            "packages string",
            "malformed exec",
        ],
    )
    def test_anything_beyond_the_exact_shape_still_needs_review(self, script):
        payload = _script_event(script)
        assert m.payload_scripts(payload)
        assert m.is_sha256(m.script_fingerprint(payload))
