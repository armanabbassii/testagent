"""
تست‌های آماده‌سازیِ payloadِ اجرا (M4): نیتِ میزبان بدونِ بازنویسی، حفظِ دقیقِ URL،
احراز هویت بدونِ تعمیر، پروفایل و زمان‌بندیِ متغیرها، تأییدِ اسکریپت، هشدارِ تغییرِ
درخواست، اثرانگشت، استقلالِ خروجی و خالص بودن.
"""

from __future__ import annotations

import builtins
import copy
import json
import os
import socket
import sqlite3
import subprocess

import pytest

from src.testcase_box import contract as c
from src.testcase_box import models as m
from src.testcase_box import preparation as p
from src.testcase_box.models import CaseVersion, NewCase, script_fingerprint, sha256_json
from src.testcase_box.preparation import prepare_case
from src.testcase_box.profiles import ExecutionProfile, HostIntent
from src.testcase_box.storage import BoxStore

_AUTH = {"key": "Authorization", "value": "{{token}}"}
PROFILE = ExecutionProfile("staging", resolved_variables={"base_url", "base_url_vdi", "userId"})


def _item(*events, host: str = "base_url", **request) -> dict:
    item = {
        "name": "TC-001",
        "request": {
            "method": "GET",
            "header": [dict(_AUTH)],
            "url": {
                "raw": "{{" + host + "}}/api/users/{{userId}}",
                "host": ["{{" + host + "}}"],
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


def _version(payload, *, fingerprint="match", scripts="match", version_id=7) -> CaseVersion:
    return CaseVersion(
        version_id,
        1,
        1,
        payload,
        sha256_json(payload) if fingerprint == "match" else fingerprint,
        script_fingerprint(payload) if scripts == "match" else scripts,
        "t",
        None,
    )


def _prepare(
    payload,
    intent=HostIntent.NORMAL,
    profile=PROFILE,
    approval=m.SCRIPTS_NOT_REQUIRED,
    **version,
):
    return prepare_case(
        _version(payload, **version), host_intent=intent, profile=profile, approval_state=approval
    )


def _codes(result) -> set[str]:
    return {item.code for item in result.findings}


def _assert_blocked(result, status):
    assert result.status == status
    assert not result.prepared
    assert result.prepared_payload is None
    assert result.prepared_fingerprint is None
    assert result.transformations == ()


class TestHostIntent:
    def test_normal_uses_base_url(self):
        payload = _item()
        result = _prepare(payload)
        assert (result.status, result.prepared) == (p.STATUS_PREPARED, True)
        assert result.selected_host_variable == "base_url"
        assert result.host_intent == "NORMAL"
        assert result.prepared_payload == payload
        assert result.transformations == ()
        assert result.findings == ()

    def test_vdi_uses_base_url_vdi(self):
        payload = _item(host="base_url_vdi")
        result = _prepare(payload, HostIntent.VDI)
        assert result.status == p.STATUS_PREPARED
        assert result.selected_host_variable == "base_url_vdi"
        assert result.prepared_payload == payload

    def test_plain_string_intent_is_accepted(self):
        assert _prepare(_item(host="base_url_vdi"), "VDI").prepared

    @pytest.mark.parametrize(
        ("host", "intent"),
        [("base_url", HostIntent.VDI), ("base_url_vdi", HostIntent.NORMAL)],
    )
    def test_mismatch_is_blocking_and_never_rewritten(self, host, intent):
        payload = _item(host=host)
        before = copy.deepcopy(payload)
        result = _prepare(payload, intent)
        _assert_blocked(result, p.STATUS_NOT_PREPARABLE)
        assert "HOST_INTENT_MISMATCH" in _codes(result)
        assert result.selected_host_variable is None
        assert result.readiness_status == c.STATUS_READY
        assert payload == before

    @pytest.mark.parametrize("intent", ["normal", "vdi", "", None, "base_url", 1])
    def test_invalid_intent_is_blocking(self, intent):
        result = _prepare(_item(), intent)
        _assert_blocked(result, p.STATUS_NOT_PREPARABLE)
        assert "HOST_INTENT_INVALID" in _codes(result)
        assert result.host_intent is None

    def test_host_missing_from_profile_needs_configuration(self):
        profile = ExecutionProfile("p", resolved_variables={"userId", "base_url_vdi"})
        result = _prepare(_item(), profile=profile)
        _assert_blocked(result, p.STATUS_NEEDS_CONFIGURATION)
        assert [(f.code, f.location) for f in result.findings] == [
            ("PROFILE_HOST_MISSING", "$.request.url.host")
        ]

    def test_empty_host_needs_configuration(self):
        profile = ExecutionProfile("p", resolved_variables={"userId"}, empty_variables={"base_url"})
        result = _prepare(_item(), profile=profile)
        _assert_blocked(result, p.STATUS_NEEDS_CONFIGURATION)
        assert _codes(result) == {"PROFILE_HOST_EMPTY"}

    def test_opposite_host_need_not_be_configured(self):
        profile = ExecutionProfile("p", resolved_variables={"base_url", "userId"})
        assert _prepare(_item(), profile=profile).prepared

    @pytest.mark.parametrize(
        ("payload", "location"),
        [
            (
                _item(header=[_AUTH, {"key": "X-Other", "value": "{{base_url_vdi}}"}]),
                "$.request.header[1].value",
            ),
            (
                _item(
                    method="POST",
                    body={"mode": "raw", "raw": '{"next": "{{base_url_vdi}}/x"}'},
                ),
                "$.request.body.raw",
            ),
            (
                _item(
                    header=[
                        _AUTH,
                        {"key": "X-Other", "value": "{{base_url_vdi}}", "disabled": True},
                    ]
                ),
                "$.request.header[1].value",
            ),
            (_item(_event("pm.environment.get('base_url_vdi');")), "$.event[0]"),
            (_item(_event("pm.environment.set('base_url_vdi', 'x');")), "$.event[0]"),
        ],
        ids=["header", "body", "disabled-header", "script-read", "script-write"],
    )
    def test_opposite_host_reference_is_blocking(self, payload, location):
        before = copy.deepcopy(payload)
        result = _prepare(payload, approval=m.APPROVAL_APPROVED)
        _assert_blocked(result, p.STATUS_NOT_PREPARABLE)
        conflicts = [f for f in result.findings if f.code == "HOST_REFERENCE_CONFLICT"]
        assert [f.location for f in conflicts] == [location]
        assert payload == before
        variable = {v.name: v for v in result.required_variables}["base_url_vdi"]
        assert (variable.kind, variable.source) == (c.KIND_HOST, p.SOURCE_CONFLICT)

    def test_opposite_host_in_query_is_blocking(self):
        payload = _item()
        url = payload["request"]["url"]
        url["raw"] += "?next={{base_url_vdi}}"
        url["query"] = [{"key": "next", "value": "{{base_url_vdi}}"}]
        result = _prepare(payload)
        assert result.status == p.STATUS_NOT_PREPARABLE
        assert {f.location for f in result.findings if f.code == "HOST_REFERENCE_CONFLICT"} == {
            "$.request.url.query[0].value",
            "$.request.url.raw",
        }


class TestUrlPreservation:
    def test_url_is_copied_exactly(self):
        payload = _item()
        payload["request"]["url"] = {
            "raw": "{{base_url}}/api/a%2Fb/x%20y/:id/{{userId}}?q=1&q=2&e=&f={{userId}}&enc=%26",
            "host": ["{{base_url}}"],
            "path": ["api", "a%2Fb", "x%20y", ":id", "{{userId}}"],
            "query": [
                {"key": "q", "value": "1"},
                {"key": "q", "value": "2"},
                {"key": "e", "value": ""},
                {"key": "f", "value": "{{userId}}"},
                {"key": "enc", "value": "%26"},
                {"key": "off", "value": "{{notConfigured}}", "disabled": True},
            ],
            "variable": [{"key": "id", "value": "5"}],
        }
        result = _prepare(payload)
        assert result.status == p.STATUS_PREPARED, result.findings
        assert result.prepared_payload["request"]["url"] == payload["request"]["url"]
        assert json.dumps(result.prepared_payload, sort_keys=True) == json.dumps(
            payload, sort_keys=True
        )
        assert result.source_fingerprint == result.prepared_fingerprint

    @pytest.mark.parametrize(
        ("raw", "host", "code"),
        [
            ("api.example.test/api/users/{{userId}}", ["api", "example", "test"], "HOST_LITERAL"),
            ("https://{{base_url}}/api/users/{{userId}}", ["{{base_url}}"], None),
            ("{{base_url}}:8443/api/users/{{userId}}", ["{{base_url}}"], "URL_PORT_UNSUPPORTED"),
            ("{{base_url}}/api/users/{{userId}}#x", ["{{base_url}}"], "URL_HASH_UNSUPPORTED"),
            ("u:pw@{{base_url}}/api/users/{{userId}}", ["{{base_url}}"], "LITERAL_CREDENTIAL"),
            ("{{base_url}}/api/other/{{userId}}", ["{{base_url}}"], "URL_COMPONENTS_CONFLICT"),
            ("{{base_url_vdi}}/api/users/{{userId}}", ["{{base_url}}"], "HOST_CONFLICT"),
            ("{{host}}/api/users/{{userId}}", ["{{host}}"], "HOST_VARIABLE_UNSUPPORTED"),
        ],
    )
    def test_unsupported_urls_remain_blocking(self, raw, host, code):
        payload = _item()
        payload["request"]["url"].update(raw=raw, host=host)
        result = _prepare(payload)
        _assert_blocked(result, p.STATUS_NOT_PREPARABLE)
        if code:
            assert code in _codes(result)


class TestAuthentication:
    @pytest.mark.parametrize(
        ("key", "value"),
        [("Authorization", " {{token}} "), ("authorization", "{{token}}\t")],
    )
    def test_trimmed_value_is_canonicalized_and_recorded(self, key, value):
        payload = _item(header=[{"key": key, "value": value}])
        result = _prepare(payload)
        assert result.prepared
        assert result.transformations == (p.TRANSFORM_AUTH,)
        assert result.prepared_payload["request"]["header"] == [
            {"key": key, "value": c.TOKEN_REFERENCE}
        ]
        assert payload["request"]["header"][0]["value"] == value  # منبع دست نخورده
        assert result.prepared_fingerprint == sha256_json(result.prepared_payload)
        assert result.prepared_fingerprint != result.source_fingerprint

    def test_canonical_value_needs_no_transformation(self):
        assert _prepare(_item()).transformations == ()

    def test_missing_header_is_a_warning_and_nothing_is_added(self):
        result = _prepare(_item(header=[]))
        assert result.prepared
        assert _codes(result) == {"AUTH_HEADER_MISSING"}
        assert result.prepared_payload["request"]["header"] == []

    def test_disabled_authorization_is_copied_unchanged(self):
        disabled = {"key": "Authorization", "value": " {{token}} ", "disabled": True}
        result = _prepare(_item(header=[_AUTH, disabled]))
        assert result.prepared
        assert result.prepared_payload["request"]["header"][1] == disabled
        assert result.transformations == ()

    @pytest.mark.parametrize(
        ("request_fields", "code"),
        [
            ({"header": [{"key": "Authorization", "value": "Bearer {{token}}"}]}, None),
            ({"header": [{"key": "Authorization", "value": "bearer{{token}}"}]}, None),
            ({"header": [{"key": "Authorization", "value": "abc123"}]}, "LITERAL_CREDENTIAL"),
            (
                {"header": [{"key": "Authorization", "value": "{{other}}"}]},
                "AUTH_VARIABLE_UNSUPPORTED",
            ),
            ({"header": [_AUTH, dict(_AUTH)]}, "AUTH_AMBIGUOUS"),
            (
                {"header": [_AUTH], "auth": {"type": "bearer", "bearer": {"token": "{{token}}"}}},
                "AUTH_AMBIGUOUS",
            ),
            ({"header": [_AUTH, {"key": "X-T", "value": "{{token}}"}]}, "TOKEN_OUTSIDE_AUTH"),
        ],
        ids=["bearer", "bearer-no-space", "literal", "other-variable", "two", "block", "outside"],
    )
    def test_m3_authentication_errors_are_not_repaired(self, request_fields, code):
        result = _prepare(_item(**request_fields))
        _assert_blocked(result, p.STATUS_NOT_PREPARABLE)
        assert (code or "AUTH_DUPLICATE_BEARER") in _codes(result)

    def test_bearer_is_never_added(self):
        result = _prepare(_item(header=[{"key": "Authorization", "value": " {{token}}"}]))
        assert "bearer" not in json.dumps(result.prepared_payload).lower()


class TestMethod:
    @pytest.mark.parametrize(("method", "expected"), [("post", "POST"), (" get ", "GET")])
    def test_method_is_uppercased_and_recorded(self, method, expected):
        payload = _item(method=method)
        result = _prepare(payload)
        assert result.prepared_payload["request"]["method"] == expected
        assert result.transformations == (p.TRANSFORM_METHOD,)
        assert payload["request"]["method"] == method

    def test_both_transformations_are_sorted(self):
        payload = _item(method="get", header=[{"key": "Authorization", "value": "{{token}} "}])
        assert _prepare(payload).transformations == (p.TRANSFORM_AUTH, p.TRANSFORM_METHOD)


class TestProfileVariables:
    def test_missing_variable_needs_configuration(self):
        profile = ExecutionProfile("p", resolved_variables={"base_url"})
        result = _prepare(_item(), profile=profile)
        _assert_blocked(result, p.STATUS_NEEDS_CONFIGURATION)
        assert [(f.code, f.location) for f in result.findings] == [
            ("VARIABLE_UNAVAILABLE", "$.request.url.path[2]"),
            ("VARIABLE_UNAVAILABLE", "$.request.url.raw"),
        ]
        assert result.readiness_status == c.STATUS_READY

    def test_empty_variable_is_a_warning(self):
        profile = ExecutionProfile("p", resolved_variables={"base_url"}, empty_variables={"userId"})
        result = _prepare(_item(), profile=profile)
        assert result.prepared
        assert {(f.code, f.severity) for f in result.findings} == {
            ("VARIABLE_EMPTY", c.SEVERITY_WARNING)
        }

    @pytest.mark.parametrize(
        ("request_fields", "location"),
        [
            ({"header": [_AUTH, {"key": "X-T", "value": "{{tenant}}"}]}, "$.request.header[1]"),
            (
                {"method": "POST", "body": {"mode": "raw", "raw": '{"t": "{{tenant}}"}'}},
                "$.request.body.raw",
            ),
            (
                {
                    "method": "POST",
                    "body": {
                        "mode": "urlencoded",
                        "urlencoded": [{"key": "t", "value": "{{tenant}}"}],
                    },
                },
                "$.request.body.urlencoded[0]",
            ),
            (
                {
                    "method": "POST",
                    "body": {"mode": "formdata", "formdata": [{"key": "t", "value": "{{tenant}}"}]},
                },
                "$.request.body.formdata[0]",
            ),
        ],
        ids=["header", "raw-body", "urlencoded", "formdata"],
    )
    def test_request_parts_need_the_variable(self, request_fields, location):
        result = _prepare(_item(**request_fields))
        assert result.status == p.STATUS_NEEDS_CONFIGURATION
        unavailable = [f.location for f in result.findings if f.code == "VARIABLE_UNAVAILABLE"]
        assert len(unavailable) == 1 and unavailable[0].startswith(location)

    def test_path_variable_value_needs_the_variable(self):
        payload = _item()
        payload["request"]["url"].update(
            raw="{{base_url}}/api/users/:id",
            path=["api", "users", ":id"],
            variable=[{"key": "id", "value": "{{tenant}}"}],
        )
        result = _prepare(payload)
        assert result.status == p.STATUS_NEEDS_CONFIGURATION
        assert "$.request.url.variable[0].value" in {f.location for f in result.findings}

    @pytest.mark.parametrize(
        "request_fields",
        [
            {"header": [_AUTH, {"key": "X-T", "value": "{{tenant}}", "disabled": True}]},
            {
                "method": "POST",
                "body": {
                    "mode": "urlencoded",
                    "urlencoded": [{"key": "t", "value": "{{tenant}}", "disabled": True}],
                },
            },
        ],
        ids=["header", "urlencoded"],
    )
    def test_disabled_entries_need_nothing(self, request_fields):
        result = _prepare(_item(**request_fields))
        assert result.prepared
        assert "tenant" not in {v.name for v in result.required_variables}

    def test_token_in_profile_is_ignored(self):
        for profile in (
            ExecutionProfile("p", resolved_variables={"base_url", "userId", "token"}),
            ExecutionProfile(
                "p", resolved_variables={"base_url", "userId"}, empty_variables={"token"}
            ),
        ):
            result = _prepare(_item(), profile=profile)
            assert result.prepared
            assert [(f.code, f.severity) for f in result.findings] == [
                ("PROFILE_TOKEN_IGNORED", c.SEVERITY_INFO)
            ]

    def test_required_variables_report_their_source(self):
        result = _prepare(_item())
        assert [(v.name, v.kind, v.source) for v in result.required_variables] == [
            ("base_url", c.KIND_HOST, p.SOURCE_PROFILE),
            ("token", c.KIND_TOKEN, p.SOURCE_RUNTIME),
            ("userId", c.KIND_EXTERNAL, p.SOURCE_PROFILE),
        ]
        assert result.required_variables[0].locations == (
            "$.request.url.host[0]",
            "$.request.url.raw",
        )

    def test_other_cases_do_not_provide_variables(self):
        """تولیدکننده‌ی بیرونی (caseِ دیگر) فقط از راهِ پروفایل؛ M4 حلش نمی‌کند."""
        result = _prepare(_item(), profile=ExecutionProfile("p", resolved_variables={"base_url"}))
        assert {v.name: v.source for v in result.required_variables}["userId"] == (
            p.SOURCE_UNAVAILABLE
        )

    def test_wrong_argument_types(self):
        with pytest.raises(TypeError):
            prepare_case(
                _version(_item()),
                host_intent=HostIntent.NORMAL,
                profile={"resolved_variables": ["base_url"]},
                approval_state=m.SCRIPTS_NOT_REQUIRED,
            )
        with pytest.raises(TypeError):
            prepare_case(
                _item(),
                host_intent=HostIntent.NORMAL,
                profile=PROFILE,
                approval_state=m.SCRIPTS_NOT_REQUIRED,
            )


class TestTiming:
    _PROFILE = ExecutionProfile("p", resolved_variables={"base_url"})

    def test_prerequest_setter_provides_the_request_variable(self):
        payload = _item(_event("pm.collectionVariables.set('userId', 7);", listen="prerequest"))
        result = _prepare(payload, profile=self._PROFILE, approval=m.APPROVAL_APPROVED)
        assert result.prepared
        assert {v.name: v.source for v in result.required_variables}["userId"] == p.SOURCE_SCRIPT
        assert result.provided_variables == ("userId",)

    def test_test_script_setter_never_provides_the_request_variable(self):
        payload = _item(_event("pm.collectionVariables.set('userId', 7);"))
        result = _prepare(payload, profile=self._PROFILE, approval=m.APPROVAL_APPROVED)
        _assert_blocked(result, p.STATUS_NEEDS_CONFIGURATION)
        assert "VARIABLE_UNAVAILABLE" in _codes(result)
        assert result.provided_variables == ("userId",)

    def test_unavailable_script_read_is_a_warning(self):
        payload = _item(_event("pm.environment.get('tenant');"))
        result = _prepare(payload, approval=m.APPROVAL_APPROVED)
        assert result.prepared
        warning = [f for f in result.findings if f.code == "SCRIPT_VARIABLE_UNAVAILABLE"]
        assert [(f.location, f.severity) for f in warning] == [("$.event[0]", c.SEVERITY_WARNING)]

    def test_prerequest_read_is_not_satisfied_by_a_later_test_write(self):
        payload = _item(
            _event("pm.environment.get('a');", listen="prerequest"),
            _event("pm.environment.set('a', 1); pm.environment.get('b');"),
            _event("pm.environment.set('b', 1);", listen="prerequest"),
        )
        result = _prepare(payload, approval=m.APPROVAL_APPROVED)
        assert [f.location for f in result.findings if f.code == "SCRIPT_VARIABLE_UNAVAILABLE"] == [
            "$.event[0]"
        ]

    def test_known_dynamic_variable_is_generated(self):
        payload = _item(method="POST", body={"mode": "raw", "raw": '{"id": "{{$guid}}"}'})
        result = _prepare(payload)
        assert result.prepared
        assert _codes(result) == {"VARIABLE_DYNAMIC"}
        assert "$guid" not in {v.name for v in result.required_variables}

    def test_unknown_dynamic_variable_is_blocking(self):
        payload = _item(method="POST", body={"mode": "raw", "raw": '{"id": "{{$notReal}}"}'})
        result = _prepare(payload)
        _assert_blocked(result, p.STATUS_NOT_PREPARABLE)
        assert [f.location for f in result.findings if f.code == "DYNAMIC_VARIABLE_UNKNOWN"] == [
            "$.request.body.raw"
        ]

    def test_unknown_dynamic_variable_in_a_disabled_entry_is_not_sent(self):
        disabled = {"key": "X-Id", "value": "{{$notReal}}", "disabled": True}
        assert _prepare(_item(header=[_AUTH, disabled])).prepared


class TestScriptApproval:
    @pytest.mark.parametrize(
        "state",
        [m.SCRIPTS_PENDING, m.APPROVAL_REJECTED, m.APPROVAL_REVOKED, m.SCRIPTS_NOT_REQUIRED, "X"],
    )
    def test_unapproved_scripts_block_preparation(self, state):
        result = _prepare(_item(_event("pm.test('a');")), approval=state)
        _assert_blocked(result, p.STATUS_NEEDS_SCRIPT_REVIEW)

    def test_review_beats_configuration(self):
        profile = ExecutionProfile("p", resolved_variables={"base_url"})
        result = _prepare(
            _item(_event("pm.test('a');")), profile=profile, approval=m.SCRIPTS_PENDING
        )
        assert result.status == p.STATUS_NEEDS_SCRIPT_REVIEW
        assert "VARIABLE_UNAVAILABLE" in _codes(result)

    def test_approved_scripts_are_prepared(self):
        result = _prepare(_item(_event("pm.test('a');")), approval=m.APPROVAL_APPROVED)
        assert result.prepared
        assert result.prepared_payload["event"] == [_event("pm.test('a');")]

    def test_approval_never_clears_hard_rejections(self):
        payload = _item(_event("pm.sendRequest('x');"))
        _assert_blocked(_prepare(payload, approval=m.APPROVAL_APPROVED), p.STATUS_NOT_PREPARABLE)

    def test_approval_never_clears_fingerprint_mismatch(self):
        result = _prepare(_item(_event("x();")), approval=m.APPROVAL_APPROVED, scripts="0" * 64)
        _assert_blocked(result, p.STATUS_NOT_PREPARABLE)
        assert "SCRIPT_FINGERPRINT_MISMATCH" in _codes(result)

    def test_error_beats_review(self):
        result = _prepare(_item(_event("x();"), method="TRACE"), approval=m.SCRIPTS_PENDING)
        assert result.status == p.STATUS_NOT_PREPARABLE


class TestRequestMutation:
    @pytest.mark.parametrize(
        "text",
        [
            "pm.request.headers.add({key: 'X', value: '1'});",
            "pm.request.headers.upsert({key: 'X', value: '1'});",
            "pm.request.headers.remove('X');",
            "pm .request .headers .clear();",
            "pm.request.addHeader({key: 'X'});",
            "pm.request.removeHeader('X');",
            "pm.request.upsertHeader({key: 'X'});",
            "pm.request.url = 'x';",
            "pm.request.url.update('x');",
            "pm.request.url.query.add('a=1');",
            "pm.request.addQueryParams('a=1');",
            "pm.request.removeQueryParams('a');",
            "pm.request.method = 'POST';",
            "pm.request.body.raw = '{}';",
            "pm.request.body.update({mode: 'raw'});",
            "pm.request.auth = null;",
            "pm.request.authorizeUsing('noauth');",
        ],
    )
    def test_detected_prerequest_mutations_are_warnings(self, text):
        payload = _item(_event(text, listen="prerequest"))
        result = _prepare(payload, approval=m.APPROVAL_APPROVED)
        assert result.prepared
        mutations = [f for f in result.findings if f.code == "SCRIPT_MUTATES_REQUEST"]
        assert [(f.location, f.severity) for f in mutations] == [("$.event[0]", c.SEVERITY_WARNING)]
        assert "static detection only" in mutations[0].message

    @pytest.mark.parametrize(
        "text",
        [
            "pm.request.headers.get('X');",
            "if (pm.request.method === 'GET') {}",
            "if (pm.request.method == 'GET') {}",
            "console.log(pm.request.url.toString());",
            "pm.request.headers.has('X');",
        ],
    )
    def test_reads_are_not_mutations(self, text):
        payload = _item(_event(text, listen="prerequest"))
        assert "SCRIPT_MUTATES_REQUEST" not in _codes(
            _prepare(payload, approval=m.APPROVAL_APPROVED)
        )

    def test_test_script_mutations_are_after_the_request(self):
        payload = _item(_event("pm.request.headers.add({key: 'X'});"))
        assert "SCRIPT_MUTATES_REQUEST" not in _codes(
            _prepare(payload, approval=m.APPROVAL_APPROVED)
        )

    @pytest.mark.parametrize(
        "text",
        ["const r = pm.request; r.headers.add({key: 'X'});", "pm.request['headers'].add({});"],
    )
    def test_known_limits_of_static_detection(self, text):
        """alias و دسترسیِ کروشه‌ای دیده نمی‌شوند — نبودنِ هشدار ضمانتِ امنیتی نیست."""
        payload = _item(_event(text, listen="prerequest"))
        assert "SCRIPT_MUTATES_REQUEST" not in _codes(
            _prepare(payload, approval=m.APPROVAL_APPROVED)
        )

    @pytest.mark.parametrize(
        ("text", "target"),
        [
            ("pm.environment.set('base_url', 'x');", "host variable"),
            ("pm.variables.set('token', 'x');", "token"),
        ],
    )
    def test_prerequest_writes_to_host_or_token(self, text, target):
        result = _prepare(_item(_event(text, listen="prerequest")), approval=m.APPROVAL_APPROVED)
        assert result.prepared
        mutation = [f for f in result.findings if f.code == "SCRIPT_MUTATES_REQUEST"]
        assert len(mutation) == 1 and target in mutation[0].message

    def test_script_text_never_appears_in_findings(self):
        text = "const leaked = 's3cr3t-body'; pm.request.headers.add({key: 'X'});"
        result = _prepare(_item(_event(text, listen="prerequest")), approval=m.APPROVAL_APPROVED)
        for item in result.findings:
            assert "s3cr3t" not in item.message and "s3cr3t" not in item.location


class TestOutput:
    def test_deterministic_and_independent_of_key_order(self):
        payload = _item(_event("pm.test('a');"), method="post")
        reordered = json.loads(json.dumps(payload, sort_keys=True))
        reordered = {key: reordered[key] for key in reversed(list(reordered))}
        first = _prepare(payload, approval=m.APPROVAL_APPROVED)
        assert first == _prepare(payload, approval=m.APPROVAL_APPROVED)
        assert first == _prepare(reordered, approval=m.APPROVAL_APPROVED)

    def test_findings_are_sorted_and_unique(self):
        payload = _item(
            _event("pm.sendRequest('x'); pm.environment.get(k);", listen="prerequest"),
            method="TRACE",
            header=[{"key": "Authorization", "value": "Bearer {{token}}"}],
            body={"mode": "raw", "raw": "{{$nope}} {{base_url_vdi}} {{missing}}"},
        )
        findings = _prepare(payload, approval=m.SCRIPTS_PENDING).findings
        assert findings == c.ordered(findings)
        assert len(set(findings)) == len(findings)

    def test_returned_payload_is_independent(self):
        payload = _item(_event("pm.test('a');"))
        version = _version(payload)
        before = json.dumps(payload, sort_keys=True)
        first = prepare_case(
            version, host_intent="NORMAL", profile=PROFILE, approval_state=m.APPROVAL_APPROVED
        )
        first.prepared_payload["request"]["header"].append({"key": "X", "value": "y"})
        first.prepared_payload["event"][0]["script"]["exec"][0] = "changed"
        assert json.dumps(version.payload, sort_keys=True) == before
        second = prepare_case(
            version, host_intent="NORMAL", profile=PROFILE, approval_state=m.APPROVAL_APPROVED
        )
        assert second.prepared_payload == payload
        assert second.prepared_payload is not payload

    def test_source_is_never_mutated_even_when_transformed(self):
        payload = _item(method="get", header=[{"key": "Authorization", "value": " {{token}}"}])
        before = json.dumps(payload, sort_keys=True)
        _prepare(payload)
        assert json.dumps(payload, sort_keys=True) == before

    def test_fingerprints(self):
        payload = _item(method="get")
        result = _prepare(payload)
        assert result.source_fingerprint == sha256_json(payload)
        assert result.prepared_fingerprint == sha256_json(result.prepared_payload)
        assert result.prepared_fingerprint != result.source_fingerprint

    def test_explicit_stored_fingerprint_is_reported_but_not_used(self):
        payload = _item()
        result = _prepare(payload, fingerprint="f" * 64)
        assert result.prepared
        assert _codes(result) == {"SOURCE_FINGERPRINT_DIFFERS"}
        assert result.source_fingerprint == sha256_json(payload)

    def test_result_metadata(self):
        result = _prepare(_item(), version_id=42)
        assert (result.version_id, result.profile_id) == (42, "staging")
        assert result.readiness_status == c.STATUS_READY

    @pytest.mark.parametrize("payload", ["text", [], {"item": []}, {"info": {}}])
    def test_malformed_payloads_are_not_preparable(self, payload):
        _assert_blocked(_prepare(payload), p.STATUS_NOT_PREPARABLE)


class _ForbiddenEnviron(dict):
    def _forbidden(self, *args, **kwargs):
        raise AssertionError("preparation must not read the environment")

    __getitem__ = get = __contains__ = __iter__ = keys = items = copy = _forbidden


class TestPurity:
    def test_no_network_files_processes_database_or_environment(self, monkeypatch):
        payload = _item(_event("pm.environment.get('x');", listen="prerequest"))
        version = _version(payload)

        def forbidden(*args, **kwargs):
            raise AssertionError("preparation must not touch external systems")

        monkeypatch.setattr(socket, "socket", forbidden)
        monkeypatch.setattr(socket, "create_connection", forbidden)
        monkeypatch.setattr(builtins, "open", forbidden)
        monkeypatch.setattr(subprocess, "Popen", forbidden)
        monkeypatch.setattr(sqlite3, "connect", forbidden)
        monkeypatch.setattr(os, "getenv", forbidden)
        monkeypatch.setattr(os, "environ", _ForbiddenEnviron())
        result = prepare_case(
            version, host_intent=HostIntent.NORMAL, profile=PROFILE, approval_state="APPROVED"
        )
        monkeypatch.undo()
        assert result.prepared

    def test_stored_version_is_prepared_without_writing(self, tmp_path):
        with BoxStore.open(tmp_path / "box.sqlite3") as store:
            case = store.create_case(NewCase(title="a", payload=_item(_event("pm.test('a');"))))
            version = store.current_version(case.id)
            store.record_script_decision(
                version.id, version.script_fingerprint, m.APPROVAL_APPROVED
            )
            before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
            result = prepare_case(
                version,
                host_intent=HostIntent.NORMAL,
                profile=PROFILE,
                approval_state=store.script_approval_state(version.id),
            )
            assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
            assert result.prepared
            assert result.source_fingerprint == version.fingerprint
            assert store.get_version(version.id) == version


def test_postman_builder_items_are_prepared_for_their_own_host():
    """itemهای مولدِ پروژه: هر کدام فقط با نیتِ میزبانِ خودش آماده می‌شود؛ متغیرِ caseِ
    تولیدکننده (voucherId) فقط از راهِ پروفایل تأمین می‌شود."""
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
    get_item, post_item = [
        leaf for folder in collection["item"] for leaf in folder.get("item", [folder])
    ]
    full = ExecutionProfile("p", resolved_variables={"base_url", "base_url_vdi", "voucherId"})

    vdi = _prepare(get_item, HostIntent.VDI, full, m.APPROVAL_APPROVED)
    assert vdi.status == p.STATUS_PREPARED, vdi.findings
    assert vdi.prepared_payload["request"]["url"] == get_item["request"]["url"]
    assert _prepare(get_item, HostIntent.NORMAL, full, m.APPROVAL_APPROVED).status == (
        p.STATUS_NOT_PREPARABLE
    )
    without_producer = ExecutionProfile("p", resolved_variables={"base_url_vdi"})
    missing = _prepare(get_item, HostIntent.VDI, without_producer, m.APPROVAL_APPROVED)
    assert missing.status == p.STATUS_NEEDS_CONFIGURATION
    assert "voucherId" in {
        f.message.split("'")[1] for f in missing.findings if f.code == "VARIABLE_UNAVAILABLE"
    }

    normal = _prepare(post_item, HostIntent.NORMAL, full, m.APPROVAL_APPROVED)
    assert normal.status == p.STATUS_PREPARED, normal.findings
