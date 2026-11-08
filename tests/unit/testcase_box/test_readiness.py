"""
تست‌های readiness: قاعده‌ی وضعیت با تأییدِ اسکریپت، متغیرهای لازم، قطعیت، خالص بودن
(بی‌شبکه، بی‌فایل، بی‌تغییرِ payload) و مسیرِ فقط‌خواندنیِ پایگاه.
"""

from __future__ import annotations

import builtins
import json
import socket
import subprocess

import pytest

from src.testcase_box import contract as c
from src.testcase_box import models as m
from src.testcase_box.db import NotFoundError
from src.testcase_box.models import CaseVersion, NewCase, NewVersion, script_fingerprint
from src.testcase_box.readiness import assess, assess_payload, assess_version
from src.testcase_box.storage import BoxStore

_HEADER = [{"key": "Authorization", "value": "{{token}}"}]


def _item(*events, **request) -> dict:
    item = {
        "name": "TC-001",
        "request": {
            "method": "GET",
            "header": list(_HEADER),
            "url": {
                "raw": "{{base_url}}/api/users/{{userId}}",
                "host": ["{{base_url}}"],
                "path": ["api", "users", "{{userId}}"],
            },
            **request,
        },
    }
    if events:
        item["event"] = list(events)
    return item


def _event(text: str, listen: str = "test") -> dict:
    return {"listen": listen, "script": {"type": "text/javascript", "exec": [text]}}


def _assess(payload, approval_state=m.SCRIPTS_NOT_REQUIRED, stored="match"):
    stored_fingerprint = script_fingerprint(payload) if stored == "match" else stored
    return assess_payload(
        payload, stored_script_fingerprint=stored_fingerprint, approval_state=approval_state
    )


def _codes(readiness) -> set[str]:
    return {item.code for item in readiness.findings}


class TestStatus:
    def test_no_scripts_is_ready_without_any_approval_finding(self):
        readiness = _assess(_item())
        assert (readiness.status, readiness.ready) == (c.STATUS_READY, True)
        assert readiness.findings == ()
        assert readiness.script_review.required is False

    @pytest.mark.parametrize(
        ("state", "code"),
        [
            (m.SCRIPTS_PENDING, "SCRIPT_REVIEW_REQUIRED"),
            (m.APPROVAL_REJECTED, "SCRIPT_REJECTED"),
            (m.APPROVAL_REVOKED, "SCRIPT_APPROVAL_REVOKED"),
            (m.SCRIPTS_NOT_REQUIRED, "SCRIPT_REVIEW_REQUIRED"),  # ناسازگار → fail-closed
            ("SOMETHING_ELSE", "SCRIPT_REVIEW_REQUIRED"),
        ],
    )
    def test_scripts_without_approval_need_review(self, state, code):
        readiness = _assess(_item(_event("pm.test('a', () => {});")), state)
        assert readiness.status == c.STATUS_NEEDS_SCRIPT_REVIEW
        assert not readiness.ready
        assert code in _codes(readiness)

    def test_approved_scripts_are_ready_and_info_does_not_force_review(self):
        readiness = _assess(_item(_event("pm.test('a', () => {});")), m.APPROVAL_APPROVED)
        assert readiness.status == c.STATUS_READY
        assert [(f.code, f.severity) for f in readiness.findings] == [
            ("SCRIPT_APPROVED", c.SEVERITY_INFO)
        ]
        assert "not a runtime safety guarantee" in readiness.findings[0].message

    def test_approval_never_clears_send_request(self):
        readiness = _assess(_item(_event("pm.sendRequest('x');")), m.APPROVAL_APPROVED)
        assert readiness.status == c.STATUS_NOT_EXECUTABLE
        assert "SCRIPT_SEND_REQUEST" in readiness.script_review.hard_rejections

    def test_approval_never_clears_fingerprint_mismatch(self):
        readiness = _assess(_item(_event("x();")), m.APPROVAL_APPROVED, stored="0" * 64)
        assert readiness.status == c.STATUS_NOT_EXECUTABLE
        assert "SCRIPT_FINGERPRINT_MISMATCH" in _codes(readiness)

    def test_stored_fingerprint_without_scripts_still_requires_review(self):
        readiness = _assess(_item(), m.SCRIPTS_PENDING, stored="a" * 64)
        assert readiness.status == c.STATUS_NOT_EXECUTABLE
        assert {"SCRIPT_FINGERPRINT_MISMATCH", "SCRIPT_REVIEW_REQUIRED"} <= _codes(readiness)

    def test_structural_error_beats_pending_review(self):
        payload = _item(_event("x();"), method="TRACE")
        assert _assess(payload, m.SCRIPTS_PENDING).status == c.STATUS_NOT_EXECUTABLE

    def test_warnings_alone_keep_ready(self):
        readiness = _assess(_item(header=[]))
        assert readiness.status == c.STATUS_READY
        assert _codes(readiness) == {"AUTH_HEADER_MISSING"}


class TestVariables:
    def test_kinds_and_locations(self):
        readiness = _assess(_item())
        assert [(v.name, v.kind, v.locations) for v in readiness.required_variables] == [
            ("base_url", c.KIND_HOST, ("$.request.url.host[0]", "$.request.url.raw")),
            ("token", c.KIND_TOKEN, ("$.request.header[0].value",)),
            ("userId", c.KIND_EXTERNAL, ("$.request.url.path[2]", "$.request.url.raw")),
        ]
        assert readiness.host_variable == "base_url"

    def test_prerequest_setter_satisfies_the_request(self):
        payload = _item(_event("pm.collectionVariables.set('userId', 7);", listen="prerequest"))
        readiness = _assess(payload, m.APPROVAL_APPROVED)
        assert "userId" not in {v.name for v in readiness.required_variables}
        assert readiness.provided_variables == ("userId",)

    def test_test_script_setter_does_not_satisfy_the_request(self):
        payload = _item(_event("pm.collectionVariables.set('userId', 7);"))
        readiness = _assess(payload, m.APPROVAL_APPROVED)
        assert "userId" in {v.name for v in readiness.required_variables}
        assert readiness.provided_variables == ("userId",)

    def test_script_reads(self):
        payload = _item(
            _event("pm.collectionVariables.set('a', 1); pm.collectionVariables.get('a');"),
            _event("pm.environment.get('tenant');", listen="prerequest"),
        )
        names = {v.name: v.kind for v in _assess(payload, m.APPROVAL_APPROVED).required_variables}
        assert names["tenant"] == c.KIND_EXTERNAL
        assert "a" not in names

    def test_dynamic_variables_are_not_required(self):
        payload = _item(method="POST", body={"mode": "raw", "raw": '{"id": "{{$guid}}"}'})
        readiness = _assess(payload)
        assert "$guid" not in {v.name for v in readiness.required_variables}
        assert "VARIABLE_DYNAMIC" in _codes(readiness)

    def test_unresolved_external_variables_do_not_block(self):
        readiness = _assess(_item())
        assert readiness.ready
        assert any(v.kind == c.KIND_EXTERNAL for v in readiness.required_variables)


class TestDeterminism:
    def test_same_input_same_result_regardless_of_key_order(self):
        payload = _item(_event("pm.test('a');"), header=_HEADER + [{"key": "X-A", "value": "1"}])
        reordered = json.loads(json.dumps(payload, sort_keys=True))
        reordered = {key: reordered[key] for key in reversed(list(reordered))}
        assert _assess(payload, m.SCRIPTS_PENDING) == _assess(reordered, m.SCRIPTS_PENDING)

    def test_findings_are_sorted_and_unique(self):
        payload = _item(
            _event("pm.sendRequest('x'); pm.environment.get(k);"),
            method="TRACE",
            header=[
                {"key": "Authorization", "value": "Bearer {{token}}"},
                {"key": "Cookie", "value": "x"},
            ],
        )
        findings = _assess(payload, m.SCRIPTS_PENDING).findings
        assert findings == c.ordered(findings)
        assert len(set(findings)) == len(findings)


class TestPurity:
    def test_no_network_files_or_processes(self, monkeypatch):
        payload = _item(_event("pm.environment.get('x');"))
        fingerprint = script_fingerprint(payload)

        def forbidden(*args, **kwargs):
            raise AssertionError("readiness must not touch the network, files or processes")

        monkeypatch.setattr(socket, "socket", forbidden)
        monkeypatch.setattr(socket, "create_connection", forbidden)
        monkeypatch.setattr(builtins, "open", forbidden)
        monkeypatch.setattr(subprocess, "Popen", forbidden)
        readiness = assess_payload(
            payload, stored_script_fingerprint=fingerprint, approval_state=m.SCRIPTS_PENDING
        )
        assert readiness.status == c.STATUS_NEEDS_SCRIPT_REVIEW

    def test_payload_is_not_mutated(self):
        payload = _item(_event("x();"), method="POST", body={"mode": "raw", "raw": '{"a": 1}'})
        before = json.dumps(payload, sort_keys=True)
        _assess(payload, m.SCRIPTS_PENDING)
        assert json.dumps(payload, sort_keys=True) == before


class TestStoredVersions:
    def test_assess_uses_the_version_and_its_approval(self):
        payload = _item(_event("pm.test('a');"))
        version = CaseVersion(7, 1, 1, payload, "f" * 64, script_fingerprint(payload), "t", None)
        readiness = assess(version, m.APPROVAL_APPROVED)
        assert readiness.version_id == 7
        assert readiness.script_review.fingerprint == version.script_fingerprint
        assert readiness.ready

    def test_assess_version_reads_without_writing(self, tmp_path):
        with BoxStore.open(tmp_path / "box.sqlite3") as store:
            case = store.create_case(NewCase(title="a", payload=_item(_event("pm.test('a');"))))
            version = store.current_version(case.id)
            before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
            pending = assess_version(store, version.id)
            assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
            assert pending.status == c.STATUS_NEEDS_SCRIPT_REVIEW

            store.record_script_decision(
                version.id, version.script_fingerprint, m.APPROVAL_APPROVED
            )
            assert assess_version(store, version.id).ready

            changed = store.add_version(case.id, NewVersion(payload=_item(_event("pm.test('b');"))))
            assert assess_version(store, changed.id).status == c.STATUS_NEEDS_SCRIPT_REVIEW
            with pytest.raises(NotFoundError):
                assess_version(store, 999)


def test_postman_builder_item_meets_the_contract():
    """itemی که مولدِ پروژه می‌سازد ساختاراً سالم است؛ اسکریپت‌هایش فقط بازبینی می‌خواهند."""
    from src.agents.test_case_generator.postman_builder import PostmanBuilder

    case = {
        "name": "TC-001 — get voucher",
        "type": "positive",
        "method": "GET",
        "path": "/voucher/{id}",
        "description": "",
        "headers": {"X-Trace": "a"},
        "query_params": {"page": 1},
        "path_params": {"id": "{{voucherId}}"},
        "body": None,
        "expected_status": 200,
        "assertions": [{"type": "status_code", "expected": 200}],
        "save_variables": [{"variable": "newId", "json_path": "$.id"}],
        "assumptions": [],
        "base_url_var": "base_url_vdi",
        "base_path": "/api",
        "required_variables": [
            {"variable": "voucherId", "scope": "collection", "producer": "TC-0"}
        ],
    }
    post = {
        **case,
        "name": "TC-002",
        "method": "POST",
        "path": "/voucher",
        "path_params": {},
        "query_params": {},
        "body": {"name": "a"},
        "base_url_var": "base_url",
        "required_variables": [],
    }
    collection = PostmanBuilder().build(
        suite={"controllers": [{"name": "C", "test_cases": [case, post]}]}, collection_name="x"
    )
    items = [leaf for folder in collection["item"] for leaf in folder.get("item", [folder])]
    assert len(items) == 2
    for item in items:
        pending = _assess(item, m.SCRIPTS_PENDING)
        blocking = [f.code for f in pending.findings if f.severity == c.SEVERITY_ERROR]
        assert blocking == []
        assert pending.status == c.STATUS_NEEDS_SCRIPT_REVIEW
        assert _assess(item, m.APPROVAL_APPROVED).ready
    get_item = _assess(items[0], m.APPROVAL_APPROVED)
    assert get_item.host_variable == "base_url_vdi"
    assert "newId" in get_item.provided_variables
