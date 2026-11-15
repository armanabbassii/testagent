"""
src/task_sources/postman.py — تشخیص و تجزیه‌ی امنِ Postman Collection

همه‌ی fixtureها ساختگی‌اند؛ هیچ فایل، شبکه، Environment یا Globalsی خوانده
نمی‌شود. مقدارهای محرمانه رشته‌های نگهبان (SENTINEL) هستند تا با یک جست‌وجو در
repr ِ کلِ نتیجه ثابت شود که در هیچ فیلد، یادداشت یا دلیلی نیامده‌اند.
"""

from __future__ import annotations

import json

import pytest

from src.task_sources.postman import (
    DEFAULT_METHOD,
    MAX_FOLDER_DEPTH,
    MAX_REQUESTS,
    STATUS_COLLECTION,
    STATUS_INVALID_JSON,
    STATUS_NOT_POSTMAN,
    STATUS_UNSUPPORTED,
    PostmanParseResult,
    detect_postman,
    parse_postman_collection,
    parse_postman_document,
)

V21 = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
V20 = "https://schema.getpostman.com/json/collection/v2.0.0/collection.json"
BASE = "http://podium.admin2.devpod.ir/api"

SECRET = "S3CR3T-SENTINEL-VALUE"
PASSWORD = "PW-SENTINEL-LEAK"
QUERY_SECRET = "QUERY-SENTINEL-LEAK"
BODY_SECRET = "BODY-SENTINEL-LEAK"
SENTINELS = (SECRET, PASSWORD, QUERY_SECRET, BODY_SECRET)


def _collection(items, variables=None, schema=V21, **info) -> dict:
    document = {"info": {"name": "Synthetic", "schema": schema, **info}, "item": items}
    if variables is not None:
        document["variable"] = variables
    return document


def _request(url, method="GET", name="req", **request) -> dict:
    body = {"url": url, **request}
    if method is not None:
        body["method"] = method
    return {"name": name, "request": body}


def _parse(items, variables=None, **kwargs) -> PostmanParseResult:
    return parse_postman_document(_collection(items, variables, **kwargs))


def _only(items, variables=None):
    result = _parse(items, variables)
    assert result.recognised, result.reason
    [request] = result.collection.requests
    return request


def _var(key, value, **extra) -> dict:
    return {"key": key, "value": value, **extra}


def _assert_no_sentinels(result: PostmanParseResult) -> None:
    text = repr(result)
    for sentinel in SENTINELS:
        assert sentinel not in text


# ── تشخیص ────────────────────────────────────────────────────────────────────


class TestDetection:
    @pytest.mark.parametrize(("schema", "version"), [(V21, "2.1.0"), (V20, "2.0.0")])
    def test_supported_versions_are_recognised(self, schema, version):
        result = _parse([_request("https://h.test/a")], schema=schema)

        assert result.status == STATUS_COLLECTION
        assert result.recognised
        assert result.version == version
        assert result.collection.version == version

    def test_the_schema_url_may_use_http_and_any_case_host(self):
        schema = "http://Schema.GetPostman.com/json/collection/v2.1.0/collection.json"
        assert _parse([], schema=schema).recognised

    def test_invalid_json(self):
        result = parse_postman_collection('{"info": ')

        assert result.status == STATUS_INVALID_JSON
        assert not result.recognised
        assert result.collection is None

    def test_parse_from_text(self):
        result = parse_postman_collection(json.dumps(_collection([_request("https://h.test/a")])))

        assert result.recognised
        assert result.collection.requests_extracted == 1

    @pytest.mark.parametrize(
        "document",
        [
            {"urls": ["https://h.test/api/admin/voucher", "http://x.test/v1"]},
            {"info": {"name": "x"}, "item": [_request("https://h.test/a")]},
            [1, 2, 3],
            "just a string",
        ],
    )
    def test_ordinary_json_is_not_postman(self, document):
        assert detect_postman(document).status == STATUS_NOT_POSTMAN

    def test_postman_id_alone_is_not_enough(self):
        document = {
            "info": {"_postman_id": "abc", "name": "x"},
            "item": [_request("https://h.test/a")],
        }

        assert detect_postman(document).status == STATUS_NOT_POSTMAN

    def test_a_non_postman_schema_host_is_not_postman(self):
        document = _collection(
            [], schema="https://json-schema.org/json/collection/v2.1.0/collection.json"
        )

        assert detect_postman(document).status == STATUS_NOT_POSTMAN

    @pytest.mark.parametrize(
        ("schema", "version"),
        [
            ("https://schema.getpostman.com/json/collection/v1.0.0/collection.json", "1.0.0"),
            ("https://schema.getpostman.com/json/collection/v3.0.0/collection.json", "3.0.0"),
            ("https://schema.getpostman.com/json/draft-07/collection.json", ""),
        ],
    )
    def test_other_postman_schema_versions_are_unsupported(self, schema, version):
        result = detect_postman(_collection([], schema=schema))

        assert result.status == STATUS_UNSUPPORTED
        assert result.version == version
        assert "not supported" in result.reason

    def test_the_v1_format_is_unsupported(self):
        v1 = {
            "id": "c1",
            "name": "old",
            "order": ["r1"],
            "requests": [{"id": "r1", "url": "https://h.test/a", "method": "GET"}],
        }
        result = detect_postman(v1)

        assert result.status == STATUS_UNSUPPORTED
        assert "v1" in result.reason

    def test_a_supported_schema_without_an_item_list_is_unsupported(self):
        document = {"info": {"schema": V21}, "item": {"not": "a list"}}

        assert detect_postman(document).status == STATUS_UNSUPPORTED

    def test_unsupported_and_ordinary_json_are_distinguishable(self):
        unsupported = detect_postman(_collection([], schema=V21.replace("v2.1.0", "v1.0.0")))
        ordinary = detect_postman({"a": "https://h.test"})

        assert unsupported.status != ordinary.status


# ── پیمایش ───────────────────────────────────────────────────────────────────


class TestWalk:
    def test_nested_folders_keep_their_hierarchy(self):
        items = [
            {
                "name": "Voucher",
                "item": [
                    {"name": "Create", "item": [_request("https://h.test/a", name="create")]},
                    _request("https://h.test/b", name="list"),
                ],
            },
            _request("https://h.test/c", name="root"),
        ]
        requests = _parse(items).collection.requests

        assert [(r.name, r.folder) for r in requests] == [
            ("create", ("Voucher", "Create")),
            ("list", ("Voucher",)),
            ("root", ()),
        ]

    def test_empty_folders(self):
        result = _parse([{"name": "Empty", "item": []}])

        assert result.collection.requests == ()
        assert result.collection.requests_discovered == 0
        assert result.collection.malformed_skipped == 0

    def test_an_unnamed_folder_gets_a_placeholder(self):
        [request] = _parse([{"item": [_request("https://h.test/a")]}]).collection.requests

        assert request.folder == ("<unnamed folder>",)

    def test_malformed_entries_are_skipped_and_counted(self):
        items = [
            "not an object",
            {"name": "neither folder nor request"},
            {"name": "bad request", "request": 42},
            {"name": "no url", "request": {"method": "GET"}},
            {"name": "bad folder", "item": "not a list"},
            _request("https://h.test/ok", name="ok"),
        ]
        collection = _parse(items).collection

        assert [r.name for r in collection.requests] == ["ok"]
        assert collection.malformed_skipped == 5
        assert collection.requests_discovered == 3
        assert any("5 malformed entries skipped" in note for note in collection.notes)

    def test_the_depth_limit_stops_descent_and_is_reported(self):
        node: dict = {"name": "leaf-folder", "item": [_request("https://h.test/deep", name="deep")]}
        for level in range(MAX_FOLDER_DEPTH + 2):
            node = {"name": f"f{level}", "item": [node]}
        collection = _parse([node, _request("https://h.test/top", name="top")]).collection

        assert [r.name for r in collection.requests] == ["top"]
        assert collection.folders_beyond_depth == 1
        assert collection.limited
        assert any(f"limit is {MAX_FOLDER_DEPTH}" in note for note in collection.notes)

    def test_requests_at_the_depth_limit_are_still_read(self):
        node: dict = {"name": "f", "item": [_request("https://h.test/ok", name="ok")]}
        for level in range(MAX_FOLDER_DEPTH - 1):
            node = {"name": f"f{level}", "item": [node]}
        collection = _parse([node]).collection

        assert [r.name for r in collection.requests] == ["ok"]
        assert len(collection.requests[0].folder) == MAX_FOLDER_DEPTH
        assert collection.folders_beyond_depth == 0

    def test_the_request_limit_is_counted_not_silently_dropped(self):
        items = [_request(f"https://h.test/r{i}", name=f"r{i}") for i in range(MAX_REQUESTS + 5)]
        collection = _parse(items).collection

        assert collection.requests_extracted == MAX_REQUESTS
        assert collection.requests_discovered == MAX_REQUESTS + 5
        assert collection.requests_omitted == 5
        assert collection.limited
        assert collection.requests[-1].name == f"r{MAX_REQUESTS - 1}"
        assert any(f"limit is {MAX_REQUESTS}" in note for note in collection.notes)

    def test_counts_add_up(self):
        items = [{"name": "bad", "request": None}] + [
            _request(f"https://h.test/{i}") for i in range(3)
        ]
        c = _parse(items).collection

        assert c.requests_extracted + c.requests_omitted + 1 == c.requests_discovered


# ── URL ──────────────────────────────────────────────────────────────────────


class TestUrlForms:
    def test_a_string_url(self):
        request = _only([_request("https://api.test/admin/voucher?page=1&size=2")])

        assert request.host == "api.test"
        assert request.path == "/admin/voucher"
        assert request.query_keys == ("page", "size")
        assert request.raw_url == "https://api.test/admin/voucher?page&size"

    def test_a_request_given_as_a_bare_string(self):
        request = _only([{"name": "short", "request": "https://api.test/a/b"}])

        assert request.path == "/a/b"
        assert request.method == DEFAULT_METHOD
        assert request.method_defaulted is True

    @pytest.mark.parametrize(
        ("host", "path"),
        [
            (["api", "test"], ["admin", "voucher"]),
            ("api.test", "admin/voucher"),
            ("api.test", "/admin/voucher/"),
            (["api", "test"], [{"type": "string", "value": "admin"}, "voucher"]),
        ],
    )
    def test_structured_host_and_path_as_strings_or_lists(self, host, path):
        url = {
            "raw": "https://api.test/admin/voucher",
            "protocol": "https",
            "host": host,
            "path": path,
        }
        request = _only([_request(url)])

        assert request.host == "api.test"
        assert request.path == "/admin/voucher"
        assert not any("disagrees" in note for note in request.notes)

    def test_disabled_query_entries_are_excluded(self):
        url = {
            "raw": "https://api.test/a?page=1&debug=1",
            "host": ["api", "test"],
            "path": ["a"],
            "query": [
                {"key": "page", "value": "1"},
                {"key": "debug", "value": "1", "disabled": True},
            ],
        }
        request = _only([_request(url)])

        assert request.query_keys == ("page",)

    def test_path_variables_are_named_without_values(self):
        url = {
            "raw": "https://api.test/admin/voucher/:id",
            "host": ["api", "test"],
            "path": ["admin", "voucher", ":id"],
            "variable": [{"key": "id", "value": "12345"}],
        }
        request = _only([_request(url)])

        assert request.path == "/admin/voucher/:id"
        assert request.path_variables == ("id",)
        assert "12345" not in repr(request)

    def test_a_url_without_a_host(self):
        request = _only([_request("/admin/voucher")])

        assert request.host is None
        assert request.path == "/admin/voucher"

    def test_raw_and_structured_disagreement_uses_the_structured_parts(self):
        url = {
            "raw": "https://other.test/admin/old?x=1",
            "host": ["api", "test"],
            "path": ["admin", "voucher"],
            "query": [{"key": "page", "value": "1"}],
        }
        request = _only([_request(url)])

        assert request.host == "api.test"
        assert request.path == "/admin/voucher"
        assert request.query_keys == ("page",)
        [note] = [n for n in request.notes if "disagrees" in n]
        assert "host, path, query" in note
        assert "other.test" not in note

    def test_a_url_object_with_only_raw(self):
        request = _only([_request({"raw": "{{baseUrl}}/admin/voucher"})])

        assert request.path == "/admin/voucher"
        assert request.host_variable == "baseUrl"

    @pytest.mark.parametrize("port", ["8443", 8443])
    def test_a_structured_port_is_part_of_the_host(self, port):
        url = {
            "raw": "https://api.test:8443/a",
            "host": ["api", "test"],
            "port": port,
            "path": ["a"],
        }
        request = _only([_request(url)])

        assert request.host == "api.test"
        assert request.raw_url == "https://api.test:8443/a"
        assert not any("disagrees" in note for note in request.notes)

    def test_the_scheme_falls_back_to_raw_when_protocol_is_missing(self):
        url = {"raw": "https://api.test/a", "host": ["api", "test"], "path": ["a"]}

        assert _only([_request(url)]).raw_url == "https://api.test/a"

    def test_query_from_raw_when_structured_has_none(self):
        url = {"raw": "https://api.test/a?page=1", "host": ["api", "test"], "path": ["a"]}

        assert _only([_request(url)]).query_keys == ("page",)


class TestMethod:
    def test_a_missing_method_defaults_to_get_and_is_marked(self):
        request = _only([_request("https://h.test/a", method=None)])

        assert request.method == "GET"
        assert request.method_defaulted is True

    def test_an_explicit_get_is_not_marked_as_defaulted(self):
        request = _only([_request("https://h.test/a", method="get")])

        assert request.method == "GET"
        assert request.method_defaulted is False

    def test_a_blank_method_counts_as_missing(self):
        assert _only([_request("https://h.test/a", method="  ")]).method_defaulted is True

    def test_an_invalid_method_is_defaulted_with_a_note(self):
        request = _only([_request("https://h.test/a", method=42)])

        assert request.method_defaulted is True
        assert any("method is invalid" in note for note in request.notes)

    def test_a_custom_method_is_kept(self):
        assert _only([_request("https://h.test/a", method="PROPFIND")]).method == "PROPFIND"


# ── متغیرها ──────────────────────────────────────────────────────────────────


class TestVariables:
    @pytest.mark.parametrize("name", ["baseUrl", "podiumHost", "admin_root", "x"])
    def test_any_variable_name_resolves_host_and_base_path(self, name):
        request = _only([_request(f"{{{{{name}}}}}/admin/voucher")], [_var(name, BASE)])

        assert request.host == "podium.admin2.devpod.ir"
        assert request.host_variable == name
        assert request.base_path == "/api"
        assert request.path == "/admin/voucher"
        assert request.unresolved == ()

    def test_the_structured_form_with_a_host_variable(self):
        url = {
            "raw": "{{baseUrl}}/admin/voucher",
            "host": ["{{baseUrl}}"],
            "path": ["admin", "voucher"],
        }
        request = _only([_request(url)], [_var("baseUrl", BASE)])

        assert (request.host, request.base_path, request.path) == (
            "podium.admin2.devpod.ir",
            "/api",
            "/admin/voucher",
        )

    def test_a_host_value_without_a_scheme(self):
        request = _only([_request("{{h}}/a")], [_var("h", "api.test:8080/gw/")])

        assert request.host == "api.test"
        assert request.base_path == "/gw"

    def test_nested_references_resolve(self):
        variables = [
            _var("scheme", "https"),
            _var("domain", "api.test"),
            _var("baseUrl", "{{scheme}}://{{domain}}/v2"),
        ]
        request = _only([_request("{{baseUrl}}/items")], variables)

        assert request.host == "api.test"
        assert request.base_path == "/v2"
        assert set(request.variables) == {"baseUrl", "scheme", "domain"}

    def test_a_cycle_is_detected_and_left_unresolved(self):
        variables = [_var("a", "{{b}}/x"), _var("b", "{{a}}")]
        request = _only([_request("{{a}}/items")], variables)

        assert request.host is None
        assert {"a", "b"} <= set(request.unresolved)
        assert any("cycle" in note for note in request.notes)

    def test_a_self_reference_is_a_cycle(self):
        request = _only([_request("{{a}}/items")], [_var("a", "{{a}}")])

        assert request.host is None
        assert "a" in request.unresolved

    def test_unresolved_variables_are_kept_by_name(self):
        request = _only([_request("{{baseUrl}}/admin/{{version}}/voucher")])

        assert request.host is None
        assert request.host_variable == "baseUrl"
        assert request.path == "/admin/{{version}}/voucher"
        assert request.unresolved == ("baseUrl", "version")

    def test_a_path_variable_with_a_collection_value_is_substituted(self):
        request = _only([_request("https://api.test/{{ver}}/items")], [_var("ver", "v2")])

        assert request.path == "/v2/items"

    @pytest.mark.parametrize("dynamic", ["$guid", "$timestamp", "$randomInt"])
    def test_dynamic_variables_stay_unresolved(self, dynamic):
        request = _only(
            [_request(f"https://api.test/items/{{{{{dynamic}}}}}")], [_var(dynamic, "fixed")]
        )

        assert dynamic in request.unresolved
        assert request.path == f"/items/{{{{{dynamic}}}}}"

    def test_the_last_enabled_entry_wins(self):
        variables = [
            _var("baseUrl", "http://first.test"),
            _var("baseUrl", "http://second.test"),
            _var("baseUrl", "http://disabled.test", disabled=True),
            _var("baseUrl", "http://off.test", enabled=False),
        ]
        request = _only([_request("{{baseUrl}}/a")], variables)

        assert request.host == "second.test"

    def test_a_disabled_only_variable_is_unresolved(self):
        request = _only(
            [_request("{{baseUrl}}/a")], [_var("baseUrl", "http://x.test", disabled=True)]
        )

        assert request.host is None
        assert "baseUrl" in request.unresolved

    def test_variable_names_are_not_trimmed(self):
        # همان قاعده‌ی Postman: «{{ baseUrl }}» متغیرِ دیگری است.
        request = _only([_request("{{ baseUrl }}/a")], [_var("baseUrl", BASE)])

        assert request.host is None

    def test_malformed_variable_blocks_are_reported(self):
        result = _parse([_request("https://h.test/a")], variables={"not": "a list"})

        assert any("collection variables are malformed" in note for note in result.collection.notes)

    def test_variables_defined_lists_names_only(self):
        result = _parse([], [_var("baseUrl", BASE), _var("apiKey", SECRET)])

        assert result.collection.variables_defined == ("baseUrl", "apiKey")
        _assert_no_sentinels(result)


# ── اسرار ────────────────────────────────────────────────────────────────────


class TestSecrets:
    @pytest.mark.parametrize(
        "variable",
        [
            _var("apiKey", SECRET),
            _var("accessToken", SECRET),
            _var("plainName", SECRET, type="secret"),
        ],
    )
    def test_secret_variables_are_never_substituted(self, variable):
        name = variable["key"]
        result = _parse([_request(f"https://api.test/items/{{{{{name}}}}}")], [variable])
        [request] = result.collection.requests

        assert request.secret_variables == (name,)
        assert name in request.unresolved
        assert request.path == f"/items/{{{{{name}}}}}"
        _assert_no_sentinels(result)

    def test_a_secret_host_variable_is_not_resolved(self):
        result = _parse([_request("{{hostToken}}/a")], [_var("hostToken", f"http://{SECRET}.test")])
        [request] = result.collection.requests

        assert request.host is None
        assert request.base_path == ""
        _assert_no_sentinels(result)

    def test_indirect_secret_references_taint_the_whole_value(self):
        variables = [
            _var("authToken", SECRET),
            _var("baseUrl", "https://{{authToken}}@api.test/v1"),
        ]
        result = _parse([_request("{{baseUrl}}/items")], variables)
        [request] = result.collection.requests

        assert request.host is None
        assert set(request.secret_variables) == {"authToken", "baseUrl"}
        assert {"authToken", "baseUrl"} <= set(request.unresolved)
        assert any("secret" in note for note in request.notes)
        _assert_no_sentinels(result)

    def test_indirect_secret_in_a_path_segment_keeps_the_template(self):
        variables = [_var("sessionId", SECRET), _var("segment", "prefix-{{sessionId}}")]
        result = _parse([_request("https://api.test/{{segment}}/items")], variables)
        [request] = result.collection.requests

        assert request.path == "/{{segment}}/items"
        assert "prefix" not in request.path
        _assert_no_sentinels(result)

    def test_multi_level_indirection_is_still_secret(self):
        variables = [
            _var("pwd", PASSWORD),
            _var("a", "{{pwd}}"),
            _var("b", "{{a}}"),
            _var("base", "http://{{b}}.test"),
        ]
        result = _parse([_request("{{base}}/x")], variables)

        assert result.collection.requests[0].host is None
        _assert_no_sentinels(result)

    def test_url_userinfo_credentials_are_removed(self):
        result = _parse([_request(f"https://admin:{PASSWORD}@api.test/admin/voucher")])
        [request] = result.collection.requests

        assert request.host == "api.test"
        assert request.raw_url == "https://api.test/admin/voucher"
        assert any("credentials in the URL were removed" in note for note in request.notes)
        _assert_no_sentinels(result)

    def test_userinfo_inside_a_host_variable_value_is_removed(self):
        result = _parse(
            [_request("{{baseUrl}}/a")], [_var("baseUrl", f"https://u:{PASSWORD}@api.test/gw")]
        )
        [request] = result.collection.requests

        assert request.host == "api.test"
        assert request.base_path == "/gw"
        assert any("credentials in the host value were removed" in note for note in request.notes)
        _assert_no_sentinels(result)

    def test_structured_url_auth_is_treated_as_credentials(self):
        url = {
            "raw": "https://api.test/a",
            "host": ["api", "test"],
            "path": ["a"],
            "auth": {"user": "u", "password": PASSWORD},
        }
        result = _parse([_request(url)])

        assert any("credentials" in note for note in result.collection.requests[0].notes)
        _assert_no_sentinels(result)

    def test_query_values_are_never_kept(self):
        url = {
            "raw": f"https://api.test/a?token={QUERY_SECRET}&q={{{{apiKey}}}}",
            "host": ["api", "test"],
            "path": ["a"],
            "query": [{"key": "token", "value": QUERY_SECRET}, {"key": "q", "value": "{{apiKey}}"}],
        }
        result = _parse([_request(url)], [_var("apiKey", SECRET)])
        [request] = result.collection.requests

        assert request.query_keys == ("token", "q")
        assert "apiKey" in request.secret_variables
        _assert_no_sentinels(result)

    def test_query_values_in_a_host_variable_are_ignored(self):
        result = _parse(
            [_request("{{baseUrl}}/a")],
            [_var("baseUrl", f"https://api.test/gw?key={QUERY_SECRET}#{SECRET}")],
        )
        [request] = result.collection.requests

        assert request.host == "api.test"
        assert request.base_path == "/gw"
        _assert_no_sentinels(result)

    def test_bodies_auth_headers_scripts_and_examples_are_not_retained(self):
        item = _request(
            "https://api.test/a",
            method="POST",
            body={"mode": "raw", "raw": json.dumps({"password": BODY_SECRET})},
            header=[{"key": "Authorization", "value": f"Bearer {SECRET}"}],
            auth={"type": "bearer", "bearer": [{"key": "token", "value": SECRET}]},
        )
        item["event"] = [
            {"listen": "prerequest", "script": {"exec": [f"pm.environment.set('t', '{SECRET}')"]}}
        ]
        item["response"] = [{"name": "ok", "body": BODY_SECRET}]
        document = _collection([item])
        document["auth"] = {"type": "basic", "basic": [{"key": "password", "value": PASSWORD}]}

        _assert_no_sentinels(parse_postman_document(document))

    def test_unsafe_names_are_replaced_in_output(self):
        weird = "na me<script>"
        request = _only([_request(f"https://api.test/{{{{{weird}}}}}")])

        assert "<script>" not in repr(request)
        assert "<unnamed>" in request.unresolved

    def test_failure_reasons_never_echo_the_input(self):
        for result in (
            parse_postman_collection(f'{{"broken": "{SECRET}"'),
            detect_postman({"secret": SECRET}),
            detect_postman(
                _collection([], schema=f"https://schema.getpostman.com/{SECRET}/collection.json")
            ),
        ):
            assert SECRET not in result.reason
            _assert_no_sentinels(result)


# ── سناریوی واقعیِ پیوستِ Jira (ساختگی) ───────────────────────────────────────


def test_the_jira_attachment_scenario_end_to_end():
    collection = _collection(
        [
            {
                "name": "Voucher",
                "item": [
                    {
                        "name": "Create voucher",
                        "request": {
                            "method": "POST",
                            "url": {
                                "raw": "{{baseUrl}}/admin/voucher",
                                "host": ["{{baseUrl}}"],
                                "path": ["admin", "voucher"],
                            },
                            "header": [{"key": "Authorization", "value": "{{token}}"}],
                            "body": {"mode": "raw", "raw": "{}"},
                        },
                    },
                    {
                        "name": "Get voucher",
                        "request": {
                            "method": "GET",
                            "url": {
                                "raw": "{{baseUrl}}/admin/voucher/{{voucherId}}?lang=fa",
                                "host": ["{{baseUrl}}"],
                                "path": ["admin", "voucher", "{{voucherId}}"],
                                "query": [{"key": "lang", "value": "fa"}],
                            },
                        },
                    },
                ],
            }
        ],
        [_var("baseUrl", BASE), _var("token", SECRET)],
    )
    result = parse_postman_collection(json.dumps(collection))

    assert result.recognised
    create, get = result.collection.requests
    assert (create.method, create.host, create.base_path, create.path) == (
        "POST",
        "podium.admin2.devpod.ir",
        "/api",
        "/admin/voucher",
    )
    assert create.folder == ("Voucher",)
    assert get.path == "/admin/voucher/{{voucherId}}"
    assert get.query_keys == ("lang",)
    assert get.unresolved == ("voucherId",)
    assert get.raw_url == "{{baseUrl}}/admin/voucher/{{voucherId}}?lang"
    _assert_no_sentinels(result)
