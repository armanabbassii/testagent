"""
تست‌های واحدِ PostmanBuilder — تبدیلِ TestCaseSuite به Postman Collection v2.1

این تست‌ها رفتارِ موجودِ builder را که جریانِ Swagger-first (agent.py) به آن
تکیه دارد قفل می‌کنند و امکاناتِ افزوده‌شده برای قدم چهارم (توضیحاتِ کالکشن و
استخراجِ متغیر از هدرِ پاسخ) را پوشش می‌دهند.
"""

from __future__ import annotations

import pytest

from src.agents.test_case_generator.postman_builder import (
    SCHEMA_V21,
    PostmanBuildError,
    PostmanBuilder,
)


def _case(**overrides) -> dict:
    case = {
        "name": "TC-001 — Create voucher",
        "type": "positive",
        "method": "post",
        "path": "/admin/voucher/{id}",
        "description": "Creates a voucher.",
        "headers": {"Authorization": "Bearer {{token}}"},
        "query_params": {"dryRun": False},
        "path_params": {"id": "{{voucherId}}"},
        "body": {"code": "SUMMER"},
        "expected_status": 201,
        "assertions": [{"type": "status_code", "expected": 201}],
        "save_variables": [],
        "assumptions": [],
    }
    case.update(overrides)
    return case


def _suite(*cases: dict) -> dict:
    return {
        "controllers": [{"name": "Admin", "test_cases": list(cases) or [_case()]}]
    }


def _build(**kwargs) -> dict:
    params = {
        "suite": _suite(),
        "base_url": "https://admin.example.com/api/",
        "collection_name": "Admin API Tests",
    }
    params.update(kwargs)
    return PostmanBuilder().build(**params)


def _item(collection: dict, index: int = 0) -> dict:
    return collection["item"][0]["item"][index]


def _script(item: dict) -> str:
    events = item.get("event") or []
    return "\n".join(events[0]["script"]["exec"]) if events else ""


def _headers(item: dict) -> dict[str, str]:
    return {h["key"]: h["value"] for h in item["request"].get("header") or []}


# ── ساختارِ کلی ──────────────────────────────────────────────────────────────

class TestCollectionShape:
    def test_collection_uses_the_v21_schema(self):
        assert _build()["info"]["schema"] == SCHEMA_V21

    def test_collection_keeps_the_given_name(self):
        assert _build()["info"]["name"] == "Admin API Tests"

    def test_base_url_has_its_trailing_slash_stripped(self):
        variables = {v["key"]: v["value"] for v in _build()["variable"]}
        assert variables["baseUrl"] == "https://admin.example.com/api"

    def test_a_token_variable_is_declared_empty(self):
        variables = {v["key"]: v["value"] for v in _build()["variable"]}
        assert variables["token"] == ""

    def test_folders_come_from_the_controller_names(self):
        collection = _build()
        assert [folder["name"] for folder in collection["item"]] == ["Admin"]

    def test_every_test_case_becomes_one_item(self):
        collection = _build(suite=_suite(_case(), _case(name="TC-002 — Second")))
        assert len(collection["item"][0]["item"]) == 2

    def test_description_is_absent_when_not_given(self):
        assert "description" not in _build()["info"]

    def test_description_is_added_when_given(self):
        info = _build(description="Built from steps 1-3.")["info"]
        assert info["description"] == "Built from steps 1-3."

    def test_a_blank_description_is_not_added(self):
        assert "description" not in _build(description="   ")["info"]


# ── URL ──────────────────────────────────────────────────────────────────────

class TestUrl:
    def test_the_host_is_the_base_url_variable(self):
        url = _item(_build())["request"]["url"]
        assert url["host"] == ["{{baseUrl}}"]

    def test_path_placeholders_become_postman_path_variables(self):
        url = _item(_build())["request"]["url"]
        assert url["path"] == ["admin", "voucher", ":id"]
        assert url["raw"].startswith("{{baseUrl}}/admin/voucher/:id")

    def test_path_variable_values_come_from_path_params(self):
        url = _item(_build())["request"]["url"]
        assert url["variable"] == [{"key": "id", "value": "{{voucherId}}"}]

    def test_query_params_are_serialised_with_booleans(self):
        url = _item(_build())["request"]["url"]
        assert url["query"] == [{"key": "dryRun", "value": "false"}]
        assert url["raw"].endswith("?dryRun=false")

    def test_no_query_entry_is_added_when_nothing_is_documented(self):
        collection = _build(suite=_suite(_case(query_params={})))
        assert "query" not in _item(collection)["request"]["url"]

    def test_no_path_variable_entry_without_a_placeholder(self):
        collection = _build(
            suite=_suite(_case(path="/admin/voucher", path_params={}))
        )
        assert "variable" not in _item(collection)["request"]["url"]

    def test_a_custom_base_url_variable_is_used(self):
        collection = _build(suite=_suite(_case(base_url_var="baseUrl_report")))
        assert _item(collection)["request"]["url"]["host"] == ["{{baseUrl_report}}"]

    def test_the_method_is_uppercased(self):
        assert _item(_build())["request"]["method"] == "POST"


# ── هدرها و بدنه ─────────────────────────────────────────────────────────────

class TestHeadersAndBody:
    def test_the_authorization_contract_is_preserved_verbatim(self):
        assert _headers(_item(_build()))["Authorization"] == "Bearer {{token}}"

    def test_content_type_is_added_when_a_body_exists(self):
        assert _headers(_item(_build()))["Content-Type"] == "application/json"

    def test_content_type_is_not_added_without_a_body(self):
        collection = _build(suite=_suite(_case(body=None)))
        assert "Content-Type" not in _headers(_item(collection))

    def test_a_documented_content_type_is_not_duplicated(self):
        collection = _build(
            suite=_suite(_case(headers={"Content-Type": "application/vnd.api+json"}))
        )
        headers = _headers(_item(collection))
        assert headers["Content-Type"] == "application/vnd.api+json"

    def test_duplicate_headers_are_dropped_case_insensitively(self):
        collection = _build(
            suite=_suite(_case(headers={"X-A": "1", "x-a": "2"}))
        )
        keys = [h["key"] for h in _item(collection)["request"]["header"]]
        assert keys.count("X-A") == 1

    def test_the_body_is_raw_json(self):
        body = _item(_build())["request"]["body"]
        assert body["mode"] == "raw"
        assert body["options"] == {"raw": {"language": "json"}}
        assert '"code": "SUMMER"' in body["raw"]

    def test_no_body_object_is_emitted_when_there_is_none(self):
        collection = _build(suite=_suite(_case(body=None)))
        assert "body" not in _item(collection)["request"]


# ── اسکریپت‌ها ───────────────────────────────────────────────────────────────

class TestScripts:
    def test_a_status_assertion_becomes_a_pm_test(self):
        script = _script(_item(_build()))
        assert 'pm.test("Status code is 201", function () {' in script
        assert "pm.response.to.have.status(201);" in script

    def test_a_field_assertion_reads_the_json_body(self):
        collection = _build(
            suite=_suite(
                _case(
                    assertions=[
                        {"type": "json_path", "json_path": "$.id", "expected": "V-1"}
                    ]
                )
            )
        )
        script = _script(_item(collection))
        assert "let jsonData;" in script
        assert 'pm.expect(jsonData.id).to.eql("V-1");' in script

    def test_a_presence_assertion_does_not_compare_a_value(self):
        collection = _build(
            suite=_suite(
                _case(assertions=[{"type": "json_path", "json_path": "$.data.id"}])
            )
        )
        script = _script(_item(collection))
        assert "pm.expect(jsonData.data.id).to.not.be.undefined;" in script

    def test_a_body_save_variable_is_read_from_the_json_body(self):
        collection = _build(
            suite=_suite(
                _case(
                    assertions=[],
                    save_variables=[{"json_path": "$.id", "variable": "voucherId"}],
                )
            )
        )
        script = _script(_item(collection))
        assert "let jsonData;" in script
        assert 'pm.collectionVariables.set("voucherId", jsonData.id);' in script

    def test_a_global_scope_save_uses_pm_globals(self):
        collection = _build(
            suite=_suite(
                _case(
                    assertions=[],
                    save_variables=[
                        {
                            "json_path": "$.id",
                            "variable": "voucherId",
                            "scope": "global",
                        }
                    ],
                )
            )
        )
        assert "pm.globals.set(" in _script(_item(collection))

    def test_a_header_save_reads_the_response_headers(self):
        collection = _build(
            suite=_suite(
                _case(
                    assertions=[],
                    save_variables=[
                        {
                            "json_path": "$.X-Trace-Id",
                            "variable": "traceId",
                            "source": "header",
                        }
                    ],
                )
            )
        )
        script = _script(_item(collection))
        assert (
            'pm.collectionVariables.set("traceId", '
            'pm.response.headers.get("X-Trace-Id"));' in script
        )
        # نیازی به parse کردنِ بدنه نیست
        assert "jsonData" not in script

    def test_a_header_save_without_a_header_name_is_skipped(self):
        collection = _build(
            suite=_suite(
                _case(
                    assertions=[],
                    save_variables=[
                        {"json_path": "", "variable": "traceId", "source": "header"}
                    ],
                )
            )
        )
        assert "event" not in _item(collection)

    def test_an_unsupported_json_path_is_skipped(self):
        collection = _build(
            suite=_suite(
                _case(
                    assertions=[],
                    save_variables=[{"json_path": "$.a[0].b", "variable": "first"}],
                )
            )
        )
        assert "event" not in _item(collection)

    def test_no_event_is_emitted_without_assertions_or_saves(self):
        collection = _build(suite=_suite(_case(assertions=[], save_variables=[])))
        assert "event" not in _item(collection)

    def test_a_non_integer_expected_status_is_skipped(self):
        collection = _build(
            suite=_suite(_case(assertions=[{"type": "status_code", "expected": "201"}]))
        )
        assert "event" not in _item(collection)


# ── توضیحاتِ request ─────────────────────────────────────────────────────────

class TestRequestDescription:
    def test_a_positive_case_is_labelled(self):
        assert "**Positive test case**" in _item(_build())["request"]["description"]

    def test_a_negative_case_is_labelled(self):
        collection = _build(suite=_suite(_case(type="negative")))
        assert "**Negative test case**" in _item(collection)["request"]["description"]

    def test_a_boundary_case_is_labelled(self):
        collection = _build(suite=_suite(_case(type="boundary")))
        assert "**Boundary test case**" in _item(collection)["request"]["description"]

    def test_an_unknown_type_falls_back_to_the_neutral_label(self):
        collection = _build(suite=_suite(_case(type="exploratory")))
        assert "**Test test case**" in _item(collection)["request"]["description"]

    def test_the_scenario_description_is_included(self):
        text = _item(_build())["request"]["description"]
        assert "Creates a voucher." in text

    def test_assumptions_are_listed(self):
        collection = _build(suite=_suite(_case(assumptions=["No status documented."])))
        text = _item(collection)["request"]["description"]
        assert "Assumptions:" in text
        assert "- No status documented." in text


# ── متغیرهای اضافی ───────────────────────────────────────────────────────────

class TestExtraVariables:
    def test_extra_variables_are_appended(self):
        variables = {
            v["key"]: v["value"]
            for v in _build(
                extra_variables=[{"key": "baseUrl_report", "value": "https://r.example"}]
            )["variable"]
        }
        assert variables["baseUrl_report"] == "https://r.example"

    def test_a_reserved_key_is_not_redefined(self):
        variables = _build(
            extra_variables=[{"key": "baseUrl", "value": "https://evil.example"}]
        )["variable"]
        assert [v for v in variables if v["key"] == "baseUrl"][0]["value"] == (
            "https://admin.example.com/api"
        )

    def test_duplicate_extra_keys_are_dropped(self):
        variables = _build(
            extra_variables=[
                {"key": "X", "value": "1"},
                {"key": "X", "value": "2"},
            ]
        )["variable"]
        assert len([v for v in variables if v["key"] == "X"]) == 1

    def test_invalid_entries_are_ignored(self):
        keys = [
            v["key"]
            for v in _build(extra_variables=["nope", {"value": "no key"}])["variable"]
        ]
        assert keys == ["baseUrl", "token"]

    def test_missing_extra_variables_is_fine(self):
        assert len(_build()["variable"]) == 2


# ── خطاها ────────────────────────────────────────────────────────────────────

class TestErrors:
    def test_a_suite_without_controllers_is_rejected(self):
        with pytest.raises(PostmanBuildError, match="no controllers"):
            _build(suite={"controllers": []})

    def test_a_non_object_suite_is_rejected(self):
        with pytest.raises(PostmanBuildError, match="no controllers"):
            _build(suite=[])

    def test_an_empty_base_url_is_rejected(self):
        with pytest.raises(PostmanBuildError, match="base_url is empty"):
            _build(base_url="   ")

    def test_a_case_without_a_method_is_rejected(self):
        with pytest.raises(PostmanBuildError, match="missing its HTTP 'method'"):
            _build(suite=_suite(_case(method="")))

    def test_a_case_without_a_path_is_rejected(self):
        with pytest.raises(PostmanBuildError, match="missing its 'path'"):
            _build(suite=_suite(_case(path="")))

    def test_a_case_without_a_name_is_rejected(self):
        with pytest.raises(PostmanBuildError, match="missing its 'name'"):
            _build(suite=_suite(_case(name="")))

    def test_a_non_object_controller_is_rejected(self):
        with pytest.raises(PostmanBuildError, match="expected a controller object"):
            _build(suite={"controllers": ["nope"]})

    def test_a_non_object_case_is_rejected(self):
        with pytest.raises(PostmanBuildError, match="expected a test case object"):
            _build(suite={"controllers": [{"name": "Admin", "test_cases": ["nope"]}]})

    def test_a_controller_without_test_cases_is_rejected(self):
        with pytest.raises(PostmanBuildError, match="has no test_cases array"):
            _build(suite={"controllers": [{"name": "Admin"}]})
