"""
تست‌های قراردادِ payload (inspect_payload) — ساختار، URL و میزبان، هدر، بدنه، احراز هویت،
متغیرها و امن بودنِ پیام‌ها. همه خالص؛ هیچ شبکه، فایل یا پایگاهی.
"""

from __future__ import annotations

import copy
import json

import pytest

from src.testcase_box.contract import SEVERITY_ERROR, SEVERITY_WARNING
from src.testcase_box.payload import inspect_payload

SECRET = "s3cr3t-Value-9f8e7d"


def _item(**request_overrides) -> dict:
    request = {
        "method": "GET",
        "header": [{"key": "Authorization", "value": "{{token}}"}],
        "url": {
            "raw": "{{base_url}}/api/users?page=1",
            "host": ["{{base_url}}"],
            "path": ["api", "users"],
            "query": [{"key": "page", "value": "1"}],
        },
    }
    request.update(request_overrides)
    return {"name": "TC-001", "request": request}


def _url(**overrides) -> dict:
    url = copy.deepcopy(_item()["request"]["url"])
    url.update(overrides)
    return url


def _codes(payload) -> set[str]:
    return {item.code for item in inspect_payload(payload).findings}


def _errors(payload) -> set[str]:
    return {
        item.code for item in inspect_payload(payload).findings if item.severity == SEVERITY_ERROR
    }


class TestValidMinimal:
    def test_minimal_get_is_clean(self):
        report = inspect_payload(_item())
        assert report.findings == ()
        assert report.host_variable == "base_url"

    def test_vdi_host(self):
        url = _url(raw="{{base_url_vdi}}/api/users?page=1", host=["{{base_url_vdi}}"])
        report = inspect_payload(_item(url=url))
        assert report.findings == ()
        assert report.host_variable == "base_url_vdi"

    @pytest.mark.parametrize("method", ["GET", "post", "Put", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    def test_supported_methods(self, method):
        assert _errors(_item(method=method)) == set()

    def test_string_host_and_path_forms(self):
        url = _url(host="{{base_url}}", path="/api/users")
        assert _errors(_item(url=url)) == set()

    def test_inert_item_fields_are_accepted(self):
        payload = {**_item(), "id": "abc", "description": {"content": "x"}, "response": [{}]}
        assert _errors(payload) == set()


class TestStructure:
    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            ([], "PAYLOAD_NOT_OBJECT"),
            ("x", "PAYLOAD_NOT_OBJECT"),
            ({"info": {}, "item": []}, "PAYLOAD_IS_COLLECTION"),
            ({"name": "folder", "item": []}, "PAYLOAD_IS_FOLDER"),
            ({"name": "x"}, "REQUEST_MISSING"),
            ({"request": "{{base_url}}/a"}, "REQUEST_NOT_OBJECT"),
        ],
    )
    def test_not_a_single_request_item(self, payload, code):
        assert _codes(payload) == {code}

    def test_unsupported_item_and_request_fields(self):
        payload = {**_item(), "protocolProfileBehavior": {"strictSSL": False}, "variable": []}
        payload["request"]["proxy"] = {}
        findings = inspect_payload(payload).findings
        assert {(f.code, f.location) for f in findings} == {
            ("FIELD_UNSUPPORTED", "$.protocolProfileBehavior"),
            ("FIELD_UNSUPPORTED", "$.variable"),
            ("FIELD_UNSUPPORTED", "$.request.proxy"),
        }

    def test_malformed_item_fields(self):
        assert _codes({**_item(), "name": 5}) == {"FIELD_MALFORMED"}

    @pytest.mark.parametrize(
        ("method", "code"),
        [
            (None, "METHOD_MISSING"),
            ("", "METHOD_MISSING"),
            ("TRACE", "METHOD_UNSUPPORTED"),
            (5, "METHOD_UNSUPPORTED"),
        ],
    )
    def test_method(self, method, code):
        payload = _item()
        if method is None:
            del payload["request"]["method"]
        else:
            payload["request"]["method"] = method
        assert _codes(payload) == {code}


class TestUrlAndHost:
    @pytest.mark.parametrize(
        ("url", "code"),
        [
            (None, "URL_MISSING"),
            ("{{base_url}}/api/users", "URL_NOT_STRUCTURED"),
            (5, "URL_MALFORMED"),
        ],
    )
    def test_url_shape(self, url, code):
        assert _codes(_item(url=url)) == {code}

    def test_raw_missing(self):
        url = _url()
        del url["raw"]
        assert _codes(_item(url=url)) == {"URL_RAW_MISSING"}

    @pytest.mark.parametrize(
        ("host", "raw", "code"),
        [
            (["api", "example", "com"], "api.example.com/api/users?page=1", "HOST_LITERAL"),
            (["10", "0", "0", "5"], "10.0.0.5/api/users?page=1", "HOST_LITERAL"),
            (["{{base_url}}", "x"], "{{base_url}}.x/api/users?page=1", "HOST_LITERAL"),
            (["{{other_host}}"], "{{other_host}}/api/users?page=1", "HOST_VARIABLE_UNSUPPORTED"),
            ([], "/api/users?page=1", "HOST_MISSING"),
        ],
    )
    def test_unsupported_hosts(self, host, raw, code):
        report = inspect_payload(_item(url=_url(host=host, raw=raw)))
        assert {f.code for f in report.findings} == {code}
        assert report.host_variable is None

    def test_vdi_is_never_inferred_from_a_hostname(self):
        url = _url(host=["vdi", "example", "com"], raw="vdi.example.com/api/users?page=1")
        report = inspect_payload(_item(url=url))
        assert report.host_variable is None
        assert "HOST_LITERAL" in {f.code for f in report.findings}

    def test_raw_and_structured_host_conflict(self):
        url = _url(raw="{{base_url_vdi}}/api/users?page=1")
        assert "HOST_CONFLICT" in _codes(_item(url=url))

    def test_relative_raw_with_structured_host_conflicts(self):
        assert "HOST_CONFLICT" in _codes(_item(url=_url(raw="/api/users?page=1")))

    @pytest.mark.parametrize(
        ("raw", "code"),
        [
            ("https://{{base_url}}/api/users?page=1", "URL_PROTOCOL_UNSUPPORTED"),
            ("{{base_url}}:8443/api/users?page=1", "URL_PORT_UNSUPPORTED"),
            ("{{base_url}}/api/users?page=1#top", "URL_HASH_UNSUPPORTED"),
            ("user:pw@{{base_url}}/api/users?page=1", "LITERAL_CREDENTIAL"),
        ],
    )
    def test_raw_components_are_reported_not_dropped(self, raw, code):
        payload = _item(url=_url(raw=raw))
        before = copy.deepcopy(payload)
        findings = inspect_payload(payload).findings
        assert code in {f.code for f in findings}
        assert "HOST_CONFLICT" not in {f.code for f in findings}
        assert payload == before

    @pytest.mark.parametrize(
        ("key", "value", "code"),
        [
            ("protocol", "https", "URL_PROTOCOL_UNSUPPORTED"),
            ("port", "8443", "URL_PORT_UNSUPPORTED"),
            ("hash", "top", "URL_HASH_UNSUPPORTED"),
            ("auth", {"user": "u"}, "LITERAL_CREDENTIAL"),
            ("unknown", "x", "FIELD_UNSUPPORTED"),
        ],
    )
    def test_structured_components_are_reported(self, key, value, code):
        assert code in _codes(_item(url=_url(**{key: value})))

    def test_blank_optional_components_are_ignored(self):
        assert _codes(_item(url=_url(port="", protocol=None, hash=""))) == set()

    def test_path_conflict(self):
        url = _url(path=["api", "accounts"])
        findings = inspect_payload(_item(url=url)).findings
        assert [(f.code, f.message) for f in findings] == [
            ("URL_COMPONENTS_CONFLICT", "'url.raw' and the structured URL disagree on the path.")
        ]

    def test_query_conflict_and_disabled_query_is_ignored(self):
        assert "URL_COMPONENTS_CONFLICT" in _codes(
            _item(url=_url(query=[{"key": "page", "value": "2"}]))
        )
        disabled = [{"key": "page", "value": "1"}, {"key": "debug", "value": "1", "disabled": True}]
        assert _codes(_item(url=_url(query=disabled))) == set()

    def test_query_without_value_matches_raw(self):
        url = _url(raw="{{base_url}}/api/users?flag", query=[{"key": "flag", "value": None}])
        assert _codes(_item(url=url)) == set()

    @pytest.mark.parametrize(
        "query", [{"key": "a"}, [{"value": "x"}], [{"key": "a", "value": 1}], ["a=b"]]
    )
    def test_malformed_query(self, query):
        assert "QUERY_MALFORMED" in _codes(_item(url=_url(query=query)))

    @pytest.mark.parametrize(("field", "value"), [("host", 5), ("path", [1]), ("variable", "x")])
    def test_malformed_structured_parts(self, field, value):
        assert "URL_MALFORMED" in _codes(_item(url=_url(**{field: value})))

    def test_path_variables(self):
        url = _url(
            raw="{{base_url}}/api/users/:id/:kind?page=1",
            path=["api", "users", ":id", ":kind"],
            variable=[{"key": "id", "value": "7"}, {"key": "kind", "value": ""}],
        )
        findings = inspect_payload(_item(url=url)).findings
        assert [(f.code, f.message) for f in findings] == [
            ("PATH_VARIABLE_EMPTY", "Path variable ':kind' has no value.")
        ]

    def test_mixed_host_references(self):
        headers = [
            {"key": "Authorization", "value": "{{token}}"},
            {"key": "X-Origin", "value": "{{base_url_vdi}}"},
        ]
        findings = inspect_payload(_item(header=headers)).findings
        assert [(f.code, f.severity) for f in findings] == [
            ("HOST_MIXED_REFERENCES", SEVERITY_WARNING)
        ]


class TestHeadersAndBody:
    def test_duplicate_headers_case_insensitive(self):
        headers = [
            {"key": "Authorization", "value": "{{token}}"},
            {"key": "X-A", "value": "1"},
            {"key": "x-a", "value": "2"},
            {"key": "X-A", "value": "3", "disabled": True},
        ]
        findings = inspect_payload(_item(header=headers)).findings
        assert [(f.code, f.message) for f in findings] == [
            ("HEADER_DUPLICATE", "Header 'x-a' is set 2 times.")
        ]

    @pytest.mark.parametrize(
        "header",
        [
            "Authorization: {{token}}",
            [{"key": 1, "value": "x"}],
            [{"key": "a", "value": 1}],
            [{"key": "a", "extra": 1}],
            ["x"],
        ],
    )
    def test_malformed_headers(self, header):
        assert "HEADER_MALFORMED" in _codes(_item(header=header))

    @pytest.mark.parametrize(
        "body",
        [
            {"mode": "raw", "raw": '{"name": "a"}', "options": {"raw": {"language": "json"}}},
            {"mode": "raw", "raw": '{"id": {{id}}}'},
            {"mode": "urlencoded", "urlencoded": [{"key": "a", "value": "1", "type": "text"}]},
            {"mode": "formdata", "formdata": [{"key": "a", "value": "1", "type": "text"}]},
            {},
            None,
        ],
    )
    def test_supported_bodies(self, body):
        assert _errors(_item(method="POST", body=body)) == set()

    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"mode": "file", "file": {"src": "/etc/passwd"}}, "BODY_MODE_UNSUPPORTED"),
            ({"mode": "graphql", "graphql": {"query": "{a}"}}, "BODY_MODE_UNSUPPORTED"),
            (
                {"mode": "formdata", "formdata": [{"key": "f", "type": "file", "src": "a"}]},
                "BODY_FILE_UPLOAD",
            ),
            ({"mode": "raw", "raw": 5}, "BODY_MALFORMED"),
            ({"raw": "x"}, "BODY_MALFORMED"),
            ("text", "BODY_MALFORMED"),
            (
                {"mode": "urlencoded", "urlencoded": [{"key": "a", "type": "file"}]},
                "BODY_MALFORMED",
            ),
            ({"mode": "raw", "raw": "", "extra": 1}, "FIELD_UNSUPPORTED"),
        ],
    )
    def test_unsupported_bodies(self, body, code):
        assert code in _codes(_item(method="POST", body=body))


class TestAuthentication:
    def _auth(self, value: str, **extra) -> set[str]:
        return _codes(_item(header=[{"key": "Authorization", "value": value}], **extra))

    def test_token_header_is_accepted(self):
        assert self._auth("{{token}}") == set()
        assert self._auth("  {{token}} ") == set()

    @pytest.mark.parametrize(
        "value", ["Bearer {{token}}", "bearer {{token}}", "BEARER   {{token}}", "Bearer{{token}}"]
    )
    def test_duplicate_bearer_prefix(self, value):
        assert self._auth(value) == {"AUTH_DUPLICATE_BEARER"}

    @pytest.mark.parametrize("value", ["{{other}}", "Bearer {{other}}", "Token {{token}}"])
    def test_other_variables(self, value):
        assert "AUTH_VARIABLE_UNSUPPORTED" in self._auth(value)

    def test_literal_authorization(self):
        findings = inspect_payload(
            _item(header=[{"key": "Authorization", "value": f"Bearer {SECRET}"}])
        ).findings
        assert [(f.code, f.severity) for f in findings] == [("LITERAL_CREDENTIAL", SEVERITY_ERROR)]

    def test_missing_header_is_only_a_warning(self):
        findings = inspect_payload(_item(header=[])).findings
        assert [(f.code, f.severity) for f in findings] == [
            ("AUTH_HEADER_MISSING", SEVERITY_WARNING)
        ]

    def test_two_headers_are_ambiguous(self):
        headers = [
            {"key": "Authorization", "value": "{{token}}"},
            {"key": "authorization", "value": "{{token}}"},
        ]
        assert _codes(_item(header=headers)) == {"AUTH_AMBIGUOUS"}

    def test_header_and_auth_block_are_ambiguous(self):
        auth = {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{token}}", "type": "string"}],
        }
        assert {"AUTH_AMBIGUOUS", "AUTH_BLOCK_UNSUPPORTED"} <= _codes(_item(auth=auth))

    @pytest.mark.parametrize(
        "auth",
        [
            {"type": "bearer", "bearer": [{"key": "token", "value": SECRET}]},
            {"type": "bearer", "bearer": {"token": SECRET}},
            {"type": "basic", "basic": [{"key": "password", "value": SECRET}]},
        ],
    )
    def test_auth_blocks_with_literal_credentials(self, auth):
        codes = _codes(_item(header=[], auth=auth))
        assert {"AUTH_BLOCK_UNSUPPORTED", "LITERAL_CREDENTIAL"} <= codes

    def test_noauth_and_inherit(self):
        assert _codes(_item(auth={"type": "noauth"})) == set()
        assert _codes(_item(auth={"type": "inherit"})) == {"AUTH_INHERIT"}
        assert _codes(_item(auth="bearer")) == {"FIELD_MALFORMED"}

    def test_disabled_literal_authorization_is_still_flagged(self):
        headers = [
            {"key": "Authorization", "value": "{{token}}"},
            {"key": "Authorization", "value": SECRET, "disabled": True},
        ]
        assert _codes(_item(header=headers)) == {"LITERAL_CREDENTIAL"}

    @pytest.mark.parametrize("name", ["Cookie", "X-Api-Key", "X-Session-Id", "Proxy-Authorization"])
    def test_literal_sensitive_headers_block(self, name):
        headers = [{"key": "Authorization", "value": "{{token}}"}, {"key": name, "value": SECRET}]
        assert _errors(_item(header=headers)) == {"LITERAL_CREDENTIAL"}

    def test_sensitive_header_with_variable_is_fine(self):
        headers = [
            {"key": "Authorization", "value": "{{token}}"},
            {"key": "Cookie", "value": "{{sid}}"},
        ]
        assert _codes(_item(header=headers)) == set()

    def test_cors_header_is_not_sensitive(self):
        headers = [
            {"key": "Authorization", "value": "{{token}}"},
            {"key": "Access-Control-Allow-Credentials", "value": "true"},
        ]
        assert _codes(_item(header=headers)) == set()

    @pytest.mark.parametrize(
        "change",
        [
            {
                "url": _url(
                    raw="{{base_url}}/api/users?page=1&t={{token}}",
                    query=[{"key": "page", "value": "1"}, {"key": "t", "value": "{{token}}"}],
                )
            },
            {"method": "POST", "body": {"mode": "raw", "raw": '{"t": "{{token}}"}'}},
            {
                "header": [
                    {"key": "Authorization", "value": "{{token}}"},
                    {"key": "X-T", "value": "{{token}}"},
                ]
            },
        ],
        ids=["query", "body", "other-header"],
    )
    def test_token_outside_the_authorization_header(self, change):
        assert "TOKEN_OUTSIDE_AUTH" in _errors(_item(**change))

    def test_credential_like_query_and_body_are_warnings(self):
        url = _url(
            raw="{{base_url}}/api/users?page=1&password=wrong",
            query=[{"key": "page", "value": "1"}, {"key": "password", "value": "wrong"}],
        )
        body = {
            "mode": "raw",
            "raw": json.dumps({"user": "a", "password": "wrong", "nested": {"api_key": "k"}}),
        }
        findings = inspect_payload(_item(method="POST", url=url, body=body)).findings
        assert {(f.code, f.severity) for f in findings} == {
            ("CREDENTIAL_LIKE_VALUE", SEVERITY_WARNING)
        }
        assert {f.message for f in findings} == {
            "Field 'password' looks like a credential and holds a literal value.",
            "Field 'api_key' looks like a credential and holds a literal value.",
        }

    def test_credential_like_with_variable_is_silent(self):
        body = {"mode": "urlencoded", "urlencoded": [{"key": "password", "value": "{{pw}}"}]}
        assert _codes(_item(method="POST", body=body)) == set()


class TestVariables:
    def test_references_with_locations(self):
        url = _url(
            raw="{{base_url}}/api/users/{{userId}}?page={{page}}",
            path=["api", "users", "{{userId}}"],
            query=[{"key": "page", "value": "{{page}}"}],
        )
        body = {"mode": "raw", "raw": '{"ref": "{{ref}}", "id": "{{$guid}}"}'}
        report = inspect_payload(_item(method="POST", url=url, body=body))
        names = {(r.name, r.location) for r in report.references}
        assert ("userId", "$.request.url.path[2]") in names
        assert ("page", "$.request.url.query[0].value") in names
        assert ("ref", "$.request.body.raw") in names
        assert ("token", "$.request.header[0].value") in names
        assert [(f.code, f.message) for f in report.findings] == [
            ("VARIABLE_DYNAMIC", "Postman dynamic variable '$guid' is generated at runtime.")
        ]

    def test_nested_json_closing_braces_are_not_malformed(self):
        body = {"mode": "raw", "raw": '{"a": {"b": {"c": "{{id}}"}}}'}
        assert _codes(_item(method="POST", body=body)) == set()

    @pytest.mark.parametrize("value", ["{{unclosed", "a {{ b", "{{}}"])
    def test_malformed_variables(self, value):
        headers = [{"key": "Authorization", "value": "{{token}}"}, {"key": "X-A", "value": value}]
        assert _codes(_item(header=headers)) == {"VARIABLE_MALFORMED"}


class TestSafetyAndPurity:
    def _secret_everywhere(self) -> dict:
        url = _url(
            raw=f"user:{SECRET}@{{{{base_url}}}}/api/users?page=1&password={SECRET}",
            query=[{"key": "page", "value": "1"}, {"key": "password", "value": SECRET}],
        )
        return _item(
            method="POST",
            url=url,
            header=[
                {"key": "Authorization", "value": SECRET},
                {"key": "Cookie", "value": f"sid={SECRET}"},
            ],
            body={"mode": "raw", "raw": json.dumps({"password": SECRET})},
            auth={"type": "basic", "basic": [{"key": "password", "value": SECRET}]},
        )

    def test_no_value_ever_appears_in_findings(self):
        findings = inspect_payload(self._secret_everywhere()).findings
        assert findings
        for item in findings:
            assert SECRET not in item.message
            assert SECRET not in item.location
            assert "sid=" not in item.message

    def test_payload_is_never_mutated(self):
        payload = self._secret_everywhere()
        before = json.dumps(payload, sort_keys=True)
        inspect_payload(payload)
        assert json.dumps(payload, sort_keys=True) == before

    @pytest.mark.parametrize(
        "payload",
        [
            {"request": {"url": {"raw": None, "host": None, "path": None, "query": None}}},
            {
                "request": {
                    "method": [],
                    "url": {"raw": "{{", "host": [None]},
                    "header": {},
                    "body": [],
                }
            },
            {"request": {"url": {"raw": "?&=&", "query": [None]}, "auth": {"type": "x", "x": 5}}},
            {"request": {"url": {"raw": "@:#", "variable": [5]}, "body": {"mode": 5}}},
        ],
    )
    def test_arbitrary_malformed_input_never_raises(self, payload):
        assert inspect_payload(payload).findings
