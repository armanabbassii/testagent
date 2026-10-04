"""
تست‌های واحدِ قدم چهارم — ساختِ قطعیِ Postman Collection

تمرکزِ این تست‌ها روی تصمیم‌هایی است که قدم چهارم واقعاً می‌گیرد:
احراز هویت، کدهای وضعیت، وابستگی‌های داده، ترتیبِ اجرا، سناریوها، ابهام‌ها و
اعتبارسنجی. هیچ تستی اینجا LLM یا شبکه را صدا نمی‌زند.
"""

from __future__ import annotations

import pytest

from src.agents.test_case_generator import postman_generation
from src.agents.test_case_generator.postman_generation import (
    PostmanGenerationError,
    Step4PostmanGenerator,
    case_expects_no_authentication,
    check_variables_are_available,
    extract_step3_sections,
    extract_test_cases,
    status_assertions,
    unfilled_required_parameters,
    validate_collection,
)


# ── fixtureها ────────────────────────────────────────────────────────────────

def _step1_result() -> dict:
    return {
        "task_summary": "Voucher management for the admin service.",
        "test_cases": [
            {
                "id": "TC-001",
                "title": "Create a valid voucher",
                "type": "positive",
                "priority": "high",
                "preconditions": ["An admin token is available"],
                "steps": ["Send POST /admin/voucher with a valid body"],
                "expected_result": "The voucher is created and its id is returned.",
            },
            {
                "id": "TC-002",
                "title": "Create a voucher with an invalid discount",
                "type": "negative",
                "priority": "medium",
                "preconditions": [],
                "steps": ["Send POST /admin/voucher with discount above 100"],
                "expected_result": "The request is rejected with a validation error.",
            },
            {
                "id": "TC-003",
                "title": "Retrieve the created voucher",
                "type": "positive",
                "priority": "high",
                "preconditions": ["A voucher already exists"],
                "steps": ["Send GET /admin/voucher/{id}"],
                "expected_result": "The voucher details are returned.",
            },
            {
                "id": "TC-004",
                "title": "Read a voucher without authentication",
                "type": "negative",
                "priority": "medium",
                "preconditions": [],
                "steps": ["Send GET /admin/voucher/{id} without authentication"],
                "expected_result": "The request is rejected because no token is sent.",
            },
        ],
    }


def _admin_service() -> dict:
    return {
        "name": "Admin",
        "source_url": "http://swagger.local/admin.json",
        "base_url": "https://admin.example.com/api/",
        "authorization": {"header": "Authorization", "value": "Bearer {{token}}"},
        "apis": [
            {
                "method": "POST",
                "path": "/admin/voucher",
                "operation_id": "createVoucher",
                "summary": "Create a voucher",
                "parameters": [{"in": "query", "name": "dryRun"}],
                "request_body": {"code": "SUMMER", "discount": 10},
                "responses": {"201": {"fields": ["id"]}, "400": {"fields": []}},
            },
            {
                "method": "GET",
                "path": "/admin/voucher/{id}",
                "operation_id": "getVoucher",
                "summary": "Get a voucher",
                "parameters": [],
                "request_body": None,
                "responses": {"200": {"fields": ["id", "code"]}, "404": {"fields": []}},
            },
        ],
        "unresolved": [],
    }


def _report_service() -> dict:
    return {
        "name": "Report",
        "source_url": "http://swagger.local/report.json",
        "base_url": "https://report.example.com",
        "authorization": None,
        "apis": [
            {
                "method": "GET",
                "path": "/report/voucher/{id}",
                "operation_id": "voucherReport",
                "summary": "Voucher report",
                "parameters": [],
                "request_body": None,
                "responses": {"200": {"fields": ["id"]}},
            }
        ],
        "unresolved": [],
    }


def _step2_result() -> dict:
    return {
        "services": [_admin_service(), _report_service()],
        "mappings": [
            {
                "test_case_id": "TC-001",
                "api": {
                    "method": "POST",
                    "path": "/admin/voucher",
                    "operation_id": "createVoucher",
                },
                "confidence": "high",
                "reason": "The test case creates a voucher.",
                "clarification": "",
            },
            {
                "test_case_id": "TC-002",
                "api": {
                    "method": "POST",
                    "path": "/admin/voucher",
                    "operation_id": "createVoucher",
                },
                "confidence": "high",
                "reason": "The test case validates the request body.",
                "clarification": "",
            },
            {
                # نامِ پارامترِ مسیر عمداً با سندِ کشف‌شده فرق دارد
                "test_case_id": "TC-003",
                "api": {
                    "method": "GET",
                    "path": "/admin/voucher/{voucherId}",
                    "operation_id": "getVoucher",
                },
                "confidence": "high",
                "reason": "The test case reads a voucher.",
                "clarification": "",
            },
            {
                "test_case_id": "TC-004",
                "api": {
                    "method": "GET",
                    "path": "/admin/voucher/{id}",
                    "operation_id": "getVoucher",
                },
                "confidence": "medium",
                "reason": "The test case reads a voucher without a token.",
                "clarification": "",
            },
        ],
        "clarifications": [],
    }


def _step3_result() -> dict:
    return {
        "scenarios": [
            {
                "id": "SC-001",
                "title": "Voucher lifecycle",
                "test_case_ids": ["TC-001", "TC-003", "TC-004"],
                "reason": "These test cases describe one voucher flow.",
            },
            {
                "id": "SC-002",
                "title": "Voucher validation",
                "test_case_ids": ["TC-002"],
                "reason": "These test cases only validate the request.",
            },
        ],
        "execution_order": [
            {"test_case_id": "TC-001", "order": 1, "depends_on": []},
            {"test_case_id": "TC-003", "order": 2, "depends_on": ["TC-001"]},
            {"test_case_id": "TC-004", "order": 3, "depends_on": []},
            {"test_case_id": "TC-002", "order": 4, "depends_on": []},
        ],
        "data_dependencies": [
            {
                "variable_name": "voucherId",
                "source": {
                    "test_case_id": "TC-001",
                    "location": "response.body",
                    "path": "$.id",
                },
                "targets": [
                    # نامِ متغیر (voucherId) با نامِ پارامترِ سند (id) فرق دارد —
                    # دقیقاً همان چیزی که قدم سوم تولید می‌کند.
                    {"test_case_id": "TC-003", "location": "path", "parameter": "id"},
                    {"test_case_id": "TC-003", "location": "query", "parameter": "dryRun"},
                    {
                        "test_case_id": "TC-003",
                        "location": "header",
                        "parameter": "X-Voucher-Id",
                    },
                    {"test_case_id": "TC-003", "location": "body", "parameter": "code"},
                ],
                "confidence": "high",
                "reason": "The id returned by the creation is used to read it back.",
            }
        ],
        "clarifications": [],
    }


def _generate(step1=None, step2=None, step3=None, **kwargs) -> dict:
    return Step4PostmanGenerator(**kwargs).generate(
        step1_result=step1 if step1 is not None else _step1_result(),
        step2_result=step2 if step2 is not None else _step2_result(),
        step3_result=step3 if step3 is not None else _step3_result(),
    )


def _iter_requests(collection: dict):
    """(نامِ پوشه، آیتم) برای هر درخواست."""
    for folder in collection["item"]:
        for item in folder["item"]:
            yield folder["name"], item


def _find(collection: dict, case_id: str) -> tuple[str, dict]:
    for folder_name, item in _iter_requests(collection):
        if item["name"].split(" — ", 1)[0] == case_id:
            return folder_name, item
    raise AssertionError(f"no request was generated for {case_id}")


def _headers(item: dict) -> dict[str, str]:
    return {h["key"]: h["value"] for h in item["request"].get("header") or []}


def _script(item: dict) -> str:
    events = item.get("event") or []
    if not events:
        return ""
    return "\n".join(events[0]["script"]["exec"])


def _folder_names(collection: dict) -> list[str]:
    return [folder["name"] for folder in collection["item"]]


# ── ساختِ پایه ───────────────────────────────────────────────────────────────

class TestBasicGeneration:
    def test_collection_is_postman_v21(self):
        result = _generate()
        assert result["collection"]["info"]["schema"] == postman_generation.SCHEMA_V21

    def test_collection_name_comes_from_the_task_summary(self):
        result = _generate()
        assert result["collection_name"] == (
            "Voucher management for the admin service API Tests"
        )
        assert result["collection"]["info"]["name"] == result["collection_name"]

    def test_explicit_collection_name_wins(self):
        result = _generate(collection_name="Voucher smoke tests")
        assert result["collection_name"] == "Voucher smoke tests"

    def test_missing_task_summary_falls_back_to_a_default_name(self):
        step1 = _step1_result()
        step1.pop("task_summary")
        result = _generate(step1=step1)
        assert result["collection_name"] == "Generated API Test Collection"

    def test_every_test_case_becomes_exactly_one_request(self):
        result = _generate()
        names = [item["name"] for _, item in _iter_requests(result["collection"])]
        assert len(names) == 4
        assert [name.split(" — ", 1)[0] for name in names] == [
            "TC-001", "TC-003", "TC-004", "TC-002"
        ]
        assert result["unresolved"] == []

    def test_request_names_keep_the_test_case_id_and_title(self):
        result = _generate()
        _, item = _find(result["collection"], "TC-001")
        assert item["name"] == "TC-001 — Create a valid voucher"

    def test_request_name_is_just_the_id_when_there_is_no_title(self):
        step1 = _step1_result()
        step1["test_cases"][0].pop("title")
        result = _generate(step1=step1)
        _, item = _find(result["collection"], "TC-001")
        assert item["name"] == "TC-001"

    def test_collection_declares_the_base_url_and_token_variables(self):
        result = _generate()
        variables = {v["key"]: v["value"] for v in result["collection"]["variable"]}
        assert variables["baseUrl"] == "https://admin.example.com/api"
        assert variables["token"] == ""

    def test_module_pulls_in_no_llm_and_no_http_client(self):
        """قدم چهارم قطعی است: نه LLM، نه درخواستِ شبکه‌ای."""
        assert not hasattr(postman_generation, "LLMClient")
        assert not hasattr(postman_generation, "requests")
        assert not hasattr(postman_generation, "httpx")
        assert not hasattr(postman_generation, "urllib")

    def test_generator_needs_no_user_id(self):
        """برخلافِ قدم‌های ۱ تا ۳، این قدم هیچ فراخوانیِ LLM ندارد."""
        parameters = Step4PostmanGenerator().generate.__code__.co_varnames
        assert "user_id" not in parameters

# ── احراز هویت ───────────────────────────────────────────────────────────────

class TestAuthentication:
    def test_authorized_requests_carry_the_bearer_variable(self):
        collection = _generate()["collection"]
        for case_id in ("TC-001", "TC-002", "TC-003"):
            _, item = _find(collection, case_id)
            assert _headers(item)["Authorization"] == "Bearer {{token}}"

    def test_a_negative_business_case_is_still_authenticated(self):
        """کیسِ منفیِ اعتبارسنجی یک درخواستِ مجاز است، نه یک درخواستِ بی‌هویت."""
        collection = _generate()["collection"]
        _, item = _find(collection, "TC-002")
        assert "Authorization" in _headers(item)

    def test_a_case_that_describes_a_request_without_a_token_omits_the_header(self):
        collection = _generate()["collection"]
        _, item = _find(collection, "TC-004")
        assert "Authorization" not in _headers(item)
        assert "intentionally not sent" in item["request"]["description"]

    def test_the_word_unauthorized_alone_does_not_drop_the_header(self):
        """«unauthorized» یعنی انتظارِ رد شدن، نه اینکه هدر فرستاده نشود."""
        case = {
            "title": "An unauthorized token is rejected",
            "expected_result": "The API returns 401 for an unauthorized token.",
        }
        assert case_expects_no_authentication(case) is False

    def test_a_negative_case_without_a_token_still_counts_as_unauthenticated(self):
        case = {
            "title": "Missing token",
            "preconditions": ["The request is sent without credentials"],
        }
        assert case_expects_no_authentication(case) is True

    def test_no_real_token_is_ever_written(self):
        collection = _generate()["collection"]
        for _, item in _iter_requests(collection):
            value = _headers(item).get("Authorization", "")
            assert value in ("", "Bearer {{token}}")

    def test_a_service_that_needs_no_auth_gets_no_authorization_header(self):
        step2 = _step2_result()
        step2["mappings"][2]["api"] = {
            "method": "GET",
            "path": "/report/voucher/{id}",
            "operation_id": "voucherReport",
        }
        result = _generate(step2=step2)
        _, item = _find(result["collection"], "TC-003")
        assert "Authorization" not in _headers(item)


# ── کدهای وضعیت ──────────────────────────────────────────────────────────────

class TestStatusAssertions:
    def test_a_positive_case_asserts_the_only_documented_success_status(self):
        collection = _generate()["collection"]
        _, item = _find(collection, "TC-001")
        assert "pm.response.to.have.status(201);" in _script(item)

    def test_a_negative_case_gets_no_status_assertion(self):
        collection = _generate()["collection"]
        _, item = _find(collection, "TC-002")
        assert "status" not in _script(item)
        assert "no status assertion was generated" in item["request"]["description"]

    def test_several_documented_success_statuses_are_not_guessed(self):
        operation = {"responses": {"200": {"fields": []}, "202": {"fields": []}}}
        assertions, notes = status_assertions("positive", operation)
        assert assertions == []
        assert "several success statuses" in notes[0]
        assert "200, 202" in notes[0]

    def test_an_operation_without_a_success_status_is_not_guessed(self):
        assertions, notes = status_assertions("positive", {"responses": {"404": {}}})
        assert assertions == []
        assert "no success status" in notes[0]

    def test_only_the_documented_status_is_used(self):
        operation = {"responses": {"201": {"fields": []}, "400": {"fields": []}}}
        assertions, notes = status_assertions("positive", operation)
        assert assertions == [{"type": "status_code", "expected": 201}]
        assert notes == []

    def test_a_positive_case_without_a_documented_status_gets_no_assertion(self):
        step2 = _step2_result()
        step2["services"][0]["apis"][1]["responses"] = {}
        collection = _generate(step2=step2)["collection"]
        _, item = _find(collection, "TC-003")
        assert "Status code is" not in _script(item)
        assert "no success status" in item["request"]["description"]


# ── وابستگی‌های داده ─────────────────────────────────────────────────────────

class TestDataDependencies:
    def test_the_source_request_extracts_the_variable_from_the_body(self):
        collection = _generate()["collection"]
        _, item = _find(collection, "TC-001")
        script = _script(item)
        assert 'pm.collectionVariables.set("voucherId", jsonData.id);' in script

    def test_a_header_source_is_read_from_the_response_headers(self):
        step3 = _step3_result()
        step3["data_dependencies"][0]["source"] = {
            "test_case_id": "TC-001",
            "location": "response.header",
            "path": "$.X-Trace-Id",
        }
        collection = _generate(step3=step3)["collection"]
        _, item = _find(collection, "TC-001")
        script = _script(item)
        assert 'pm.response.headers.get("X-Trace-Id")' in script
        # منبعِ هدری نباید بدنه را بی‌دلیل parse کند
        assert "jsonData" not in script

    def test_the_target_request_uses_the_variable_in_every_location(self):
        collection = _generate()["collection"]
        _, item = _find(collection, "TC-003")
        request = item["request"]
        assert request["url"]["path"] == ["admin", "voucher", ":id"]
        assert request["url"]["variable"] == [{"key": "id", "value": "{{voucherId}}"}]
        assert request["url"]["raw"].startswith("{{baseUrl}}/admin/voucher/:id")
        assert "dryRun={{voucherId}}" in request["url"]["raw"]
        assert _headers(item)["X-Voucher-Id"] == "{{voucherId}}"
        assert '"code": "{{voucherId}}"' in request["body"]["raw"]

    def test_the_path_parameter_name_comes_from_the_document_not_the_variable(self):
        """متغیرِ قدم سوم روی پارامترِ مستندشده‌ی سند می‌نشیند، نه جای آن."""
        collection = _generate()["collection"]
        _, item = _find(collection, "TC-003")
        assert ":voucherId" not in item["request"]["url"]["raw"]

    def test_the_target_keeps_the_documented_body_fields(self):
        """بدنه‌ی مستندشده بازنویسی نمی‌شود؛ فقط فیلدِ وابستگی جایگزین می‌شود."""
        collection = _generate()["collection"]
        _, item = _find(collection, "TC-001")
        assert '"code": "SUMMER"' in item["request"]["body"]["raw"]
        assert '"discount": 10' in item["request"]["body"]["raw"]

    def test_a_case_without_a_dependency_extracts_nothing(self):
        collection = _generate()["collection"]
        _, item = _find(collection, "TC-002")
        assert "collectionVariables.set" not in _script(item)

    def test_a_body_target_on_an_operation_without_a_body_is_explained(self):
        collection = _generate()["collection"]
        _, item = _find(collection, "TC-003")
        assert "Swagger documents no request body" in item["request"]["description"]
        assert item["request"]["body"]["raw"].strip() == '{\n  "code": "{{voucherId}}"\n}'

    def test_a_path_target_that_is_not_in_the_path_is_reported(self):
        step3 = _step3_result()
        step3["data_dependencies"][0]["targets"] = [
            {"test_case_id": "TC-003", "location": "path", "parameter": "wrongName"}
        ]
        result = _generate(step3=step3)
        _, item = _find(result["collection"], "TC-003")
        assert "has no such placeholder" in item["request"]["description"]
        assert any("wrongName" in warning for warning in result["warnings"])

    def test_an_unknown_variable_is_not_accepted_silently(self):
        collection = {
            "variable": [{"key": "baseUrl", "value": "https://x"}],
            "item": [
                {
                    "name": "folder",
                    "item": [
                        {
                            "name": "request",
                            "request": {
                                "method": "GET",
                                "url": {
                                    "raw": "{{baseUrl}}/a/{{nobodySetsThis}}",
                                    "host": ["{{baseUrl}}"],
                                },
                            },
                        }
                    ],
                }
            ],
        }
        problems = check_variables_are_available(collection, set())
        assert len(problems) == 1
        assert "nobodySetsThis" in problems[0]

    def test_an_extracted_variable_counts_as_available(self):
        collection = {
            "variable": [{"key": "baseUrl", "value": "https://x"}],
            "item": [
                {
                    "name": "folder",
                    "item": [
                        {
                            "name": "request",
                            "request": {
                                "method": "GET",
                                "url": {
                                    "raw": "{{baseUrl}}/a/{{voucherId}}",
                                    "host": ["{{baseUrl}}"],
                                },
                            },
                        }
                    ],
                }
            ],
        }
        assert check_variables_are_available(collection, {"voucherId"}) == []


# ── پارامترهای مستندشده ─────────────────────────────────────────────────────

class TestDocumentedParameters:
    def test_a_required_unfilled_parameter_is_reported_not_invented(self):
        step2 = _step2_result()
        step2["services"][0]["apis"][0]["parameters"] = [
            {"in": "query", "name": "tenantId", "required": True}
        ]
        result = _generate(step2=step2)
        _, item = _find(result["collection"], "TC-001")
        assert "required parameter(s) query 'tenantId'" in item["request"]["description"]
        assert "tenantId" not in item["request"]["url"]["raw"]

    def test_a_required_parameter_filled_by_a_dependency_is_not_reported(self):
        step2 = _step2_result()
        step2["services"][0]["apis"][1]["parameters"] = [
            {"in": "query", "name": "dryRun", "required": True}
        ]
        result = _generate(step2=step2)
        _, item = _find(result["collection"], "TC-003")
        assert "required parameter(s)" not in item["request"]["description"]

    def test_an_optional_parameter_is_not_reported(self):
        step2 = _step2_result()
        step2["services"][0]["apis"][0]["parameters"] = [
            {"in": "query", "name": "trace", "required": False}
        ]
        result = _generate(step2=step2)
        _, item = _find(result["collection"], "TC-001")
        assert "required parameter(s)" not in item["request"]["description"]

    def test_a_required_path_parameter_without_a_dependency_is_reported(self):
        step2 = _step2_result()
        step2["services"][0]["apis"][1]["parameters"] = [
            {"in": "path", "name": "id", "required": True}
        ]
        step3 = _step3_result()
        step3["data_dependencies"] = []
        result = _generate(step2=step2, step3=step3)
        _, item = _find(result["collection"], "TC-003")
        assert "path 'id'" in item["request"]["description"]

    def test_a_required_path_parameter_filled_by_a_dependency_is_not_reported(self):
        step2 = _step2_result()
        step2["services"][0]["apis"][1]["parameters"] = [
            {"in": "path", "name": "id", "required": True}
        ]
        result = _generate(step2=step2)
        _, item = _find(result["collection"], "TC-003")
        assert "required parameter(s)" not in item["request"]["description"]

    def test_the_helper_compares_against_every_filled_location(self):
        operation = {
            "parameters": [
                {"in": "path", "name": "id", "required": True},
                {"in": "header", "name": "X-Tenant", "required": True},
                {"in": "query", "name": "q", "required": True},
                {"in": "body", "name": "ignored", "required": True},
            ]
        }
        assert (
            unfilled_required_parameters(
                operation, {"id": "x"}, {"q": "1"}, {"x-tenant": "t"}
            )
            == []
        )

    def test_the_helper_reports_header_names_case_insensitively(self):
        operation = {"parameters": [{"in": "header", "name": "x-tenant", "required": True}]}
        assert unfilled_required_parameters(operation, {}, {}, {"X-Tenant": "t"}) == []


# ── ترتیبِ اجرا و سناریوها ───────────────────────────────────────────────────

class TestOrderingAndScenarios:
    def test_folders_follow_the_scenario_order(self):
        collection = _generate()["collection"]
        assert _folder_names(collection) == ["Voucher lifecycle", "Voucher validation"]

    def test_requests_follow_the_execution_order_inside_a_folder(self):
        collection = _generate()["collection"]
        folder = collection["item"][0]
        assert [i["name"].split(" — ")[0] for i in folder["item"]] == [
            "TC-001", "TC-003", "TC-004"
        ]

    def test_a_scenario_folder_is_ordered_by_its_first_member(self):
        step3 = _step3_result()
        step3["scenarios"] = [step3["scenarios"][1], step3["scenarios"][0]]
        collection = _generate(step3=step3)["collection"]
        # SC-002 (order 4) پیش از SC-001 (order 1) می‌آید چون خودِ قدم سوم
        # سناریوی دوم را اول فهرست کرده است
        assert _folder_names(collection) == ["Voucher validation", "Voucher lifecycle"]

    def test_a_case_outside_every_scenario_lands_in_the_unassigned_folder(self):
        step3 = _step3_result()
        step3["scenarios"][0]["test_case_ids"] = ["TC-001", "TC-003"]
        result = _generate(step3=step3)
        folder_name, _ = _find(result["collection"], "TC-004")
        assert folder_name == "Unassigned test cases"

    def test_a_case_listed_in_two_scenarios_is_not_duplicated(self):
        step3 = _step3_result()
        step3["scenarios"][1]["test_case_ids"] = ["TC-002", "TC-001"]
        result = _generate(step3=step3)
        names = [i["name"] for _, i in _iter_requests(result["collection"])]
        assert len(names) == len(set(names)) == 4
        assert _find(result["collection"], "TC-001")[0] == "Voucher lifecycle"

    def test_depends_on_alone_does_not_extract_a_variable(self):
        """فقط data_dependencies باعثِ استخراج/انتشارِ متغیر می‌شود، نه ترتیب."""
        step3 = _step3_result()
        step3["data_dependencies"] = []
        collection = _generate(step3=step3)["collection"]
        for _, item in _iter_requests(collection):
            assert "collectionVariables.set" not in _script(item)
        # اما ترتیبِ اجرا همچنان رعایت می‌شود
        folder = collection["item"][0]
        assert [i["name"].split(" — ")[0] for i in folder["item"]] == [
            "TC-001", "TC-003", "TC-004"
        ]

    def test_two_scenarios_with_the_same_title_get_distinct_folder_names(self):
        step3 = _step3_result()
        step3["scenarios"][1]["title"] = step3["scenarios"][0]["title"]
        collection = _generate(step3=step3)["collection"]
        assert len(set(_folder_names(collection))) == 2

    def test_an_order_that_contradicts_a_dependency_is_reported_not_reordered(self):
        step3 = _step3_result()
        step3["scenarios"][0]["test_case_ids"] = ["TC-001", "TC-003"]
        step3["execution_order"] = [
            {"test_case_id": "TC-003", "order": 1, "depends_on": []},
            {"test_case_id": "TC-001", "order": 2, "depends_on": []},
        ]
        result = _generate(step3=step3)
        assert any("is not set when the request runs" in w for w in result["warnings"])
        _, item = _find(result["collection"], "TC-003")
        assert "is not set when the request runs" in item["request"]["description"]
        # ترتیبِ خودِ قدم سوم دست‌نخورده می‌ماند — بازچینی نمی‌شود
        folder = result["collection"]["item"][0]
        assert [i["name"].split(" — ")[0] for i in folder["item"]] == ["TC-003", "TC-001"]

    def test_scenario_rows_report_the_request_count(self):
        result = _generate()
        assert result["scenarios"] == [
            {"id": "SC-001", "title": "Voucher lifecycle", "request_count": 3},
            {"id": "SC-002", "title": "Voucher validation", "request_count": 1},
        ]

    def test_an_empty_scenario_is_not_emitted_as_a_folder(self):
        step3 = _step3_result()
        step3["scenarios"][1]["test_case_ids"] = []
        collection = _generate(step3=step3)["collection"]
        assert _folder_names(collection) == ["Voucher lifecycle", "Unassigned test cases"]


# ── ابهام‌ها ─────────────────────────────────────────────────────────────────

class TestClarifications:
    def _step3_with_clarification(self) -> dict:
        step3 = _step3_result()
        step3["clarifications"] = [
            {
                "type": "ambiguous_expected_result",
                "test_case_id": "TC-002",
                "message": "The document does not say which error code a bad discount returns.",
            }
        ]
        return step3

    def test_a_clarification_becomes_a_note_not_an_assertion(self):
        collection = _generate(step3=self._step3_with_clarification())["collection"]
        _, item = _find(collection, "TC-002")
        assert "Unresolved clarification" in item["request"]["description"]
        assert "which error code" in item["request"]["description"]
        assert "pm.response.to.have.status(400)" not in _script(item)
        assert "pm.test" not in _script(item)

    def test_a_clarification_never_produces_a_new_request(self):
        result = _generate(step3=self._step3_with_clarification())
        assert len(result["requests"]) == 4

    def test_the_collection_description_lists_the_clarifications(self):
        result = _generate(step3=self._step3_with_clarification())
        description = result["collection"]["info"]["description"]
        assert "no assertion was guessed" in description
        assert "ambiguous_expected_result (TC-002)" in description

    def test_a_clarification_without_a_test_case_still_appears(self):
        step3 = _step3_result()
        step3["clarifications"] = [
            {"type": "missing_base_url", "test_case_id": "", "message": "No base URL."}
        ]
        result = _generate(step3=step3)
        assert "missing_base_url" in result["collection"]["info"]["description"]

    def test_no_description_is_added_when_there_is_nothing_to_report(self):
        """توضیحات همیشه منبعِ کالکشن را می‌گوید، حتی وقتی هشداری نیست."""
        info = _generate()["collection"]["info"]
        assert "Generated deterministically" in info["description"]
        assert "Test cases without a generated request" not in info["description"]
        assert "Warnings:" not in info["description"]


# ── ورودیِ حل‌نشده ───────────────────────────────────────────────────────────

class TestUnresolvedInput:
    def test_an_unmapped_test_case_is_not_invented(self):
        step2 = _step2_result()
        step2["mappings"][2]["api"] = None
        result = _generate(step2=step2)
        assert [u["test_case_id"] for u in result["unresolved"]] == ["TC-003"]
        assert "did not resolve an API" in result["unresolved"][0]["reason"]
        with pytest.raises(AssertionError):
            _find(result["collection"], "TC-003")

    def test_an_operation_that_was_never_discovered_is_not_invented(self):
        step2 = _step2_result()
        step2["mappings"][0]["api"] = {
            "method": "POST",
            "path": "/admin/not-in-the-document",
            "operation_id": "",
        }
        result = _generate(step2=step2)
        assert [u["test_case_id"] for u in result["unresolved"]] == ["TC-001"]
        assert "was not found among the APIs" in result["unresolved"][0]["reason"]
        # وابستگیِ داده‌ای که مبدأش ساخته نشده، بی‌صدا رد نمی‌شود
        assert any("is never extracted" in warning for warning in result["warnings"])

    def test_a_service_without_a_base_url_is_reported_not_guessed(self):
        step2 = _step2_result()
        step2["mappings"][2]["api"] = {
            "method": "GET",
            "path": "/report/voucher/{id}",
            "operation_id": "voucherReport",
        }
        step2["services"][1]["base_url"] = ""
        result = _generate(step2=step2)
        assert [u["test_case_id"] for u in result["unresolved"]] == ["TC-003"]
        assert "no base URL" in result["unresolved"][0]["reason"]

    def test_a_mapping_without_a_method_or_path_is_reported(self):
        step2 = _step2_result()
        step2["mappings"][1]["api"] = {"method": "", "path": "", "operation_id": ""}
        result = _generate(step2=step2)
        assert [u["test_case_id"] for u in result["unresolved"]] == ["TC-002"]
        assert "no method or path" in result["unresolved"][0]["reason"]

    def test_a_collection_with_no_buildable_request_is_an_error(self):
        step2 = _step2_result()
        for mapping in step2["mappings"]:
            mapping["api"] = None
        with pytest.raises(PostmanGenerationError) as excinfo:
            _generate(step2=step2)
        assert "No test case could be turned" in str(excinfo.value)
        assert "TC-001" in str(excinfo.value)

    def test_unresolved_cases_do_not_become_placeholders(self):
        step2 = _step2_result()
        step2["mappings"][3]["api"] = None
        result = _generate(step2=step2)
        names = [i["name"] for _, i in _iter_requests(result["collection"])]
        assert not any(name.startswith("TC-004") for name in names)


# ── اعتبارسنجیِ ورودی ────────────────────────────────────────────────────────

class TestInputValidation:
    def test_a_non_object_step1_result_is_rejected(self):
        with pytest.raises(PostmanGenerationError, match="Step 1 result"):
            _generate(step1=[])

    def test_an_empty_test_case_list_is_rejected(self):
        step1 = _step1_result()
        step1["test_cases"] = []
        with pytest.raises(PostmanGenerationError, match="any test case"):
            _generate(step1=step1)

    def test_a_test_case_without_an_id_is_rejected(self):
        step1 = _step1_result()
        step1["test_cases"][0].pop("id")
        with pytest.raises(PostmanGenerationError, match="has no 'id'"):
            _generate(step1=step1)

    def test_duplicate_test_case_ids_are_rejected(self):
        step1 = _step1_result()
        step1["test_cases"][1]["id"] = "TC-001"
        with pytest.raises(PostmanGenerationError, match="duplicate test case id"):
            _generate(step1=step1)

    def test_a_step2_result_without_services_is_rejected(self):
        step2 = _step2_result()
        step2["services"] = []
        with pytest.raises(PostmanGenerationError, match="any service"):
            _generate(step2=step2)

    def test_a_step3_result_missing_a_section_is_rejected(self):
        step3 = _step3_result()
        step3.pop("data_dependencies")
        with pytest.raises(PostmanGenerationError, match="'data_dependencies'"):
            _generate(step3=step3)

    def test_a_scenario_referencing_an_unknown_case_is_rejected(self):
        step3 = _step3_result()
        step3["scenarios"][0]["test_case_ids"].append("TC-999")
        with pytest.raises(PostmanGenerationError, match="TC-999"):
            _generate(step3=step3)

    def test_duplicate_scenario_ids_are_rejected(self):
        step3 = _step3_result()
        step3["scenarios"][1]["id"] = "SC-001"
        with pytest.raises(PostmanGenerationError, match="duplicate scenario id"):
            _generate(step3=step3)

    def test_a_scenario_without_an_id_is_rejected(self):
        step3 = _step3_result()
        step3["scenarios"][0]["id"] = ""
        with pytest.raises(PostmanGenerationError, match="'id' is missing"):
            _generate(step3=step3)

    def test_a_scenario_without_a_test_case_array_is_rejected(self):
        step3 = _step3_result()
        step3["scenarios"][0]["test_case_ids"] = "TC-001"
        with pytest.raises(PostmanGenerationError, match="must be an array"):
            _generate(step3=step3)

    def test_a_dependency_target_referencing_an_unknown_case_is_rejected(self):
        step3 = _step3_result()
        step3["data_dependencies"][0]["targets"][0]["test_case_id"] = "TC-404"
        with pytest.raises(PostmanGenerationError, match="TC-404"):
            _generate(step3=step3)

    def test_a_dependency_source_without_a_mapping_is_rejected(self):
        step2 = _step2_result()
        step2["mappings"][0]["api"] = None
        with pytest.raises(PostmanGenerationError, match="cannot produce a variable"):
            _generate(step2=step2)

    def test_an_execution_step_referencing_an_unknown_case_is_rejected(self):
        step3 = _step3_result()
        step3["execution_order"][0]["test_case_id"] = "TC-777"
        with pytest.raises(PostmanGenerationError, match="TC-777"):
            _generate(step3=step3)

    def test_extract_test_cases_normalises_the_ids(self):
        step1 = _step1_result()
        step1["test_cases"][0]["id"] = "  TC-001  "
        assert extract_test_cases(step1)[0]["id"] == "TC-001"

    def test_extract_step3_sections_keeps_the_documented_order(self):
        scenarios, order, dependencies, clarifications = extract_step3_sections(
            _step3_result()
        )
        assert len(scenarios) == 2
        assert len(order) == 4
        assert len(dependencies) == 1
        assert clarifications == []


# ── اعتبارسنجیِ خروجی ───────────────────────────────────────────────────────

class TestArtifactValidation:
    def test_the_generated_collection_is_valid(self):
        assert validate_collection(_generate()["collection"]) == []

    def test_a_collection_that_is_not_an_object_is_reported(self):
        assert validate_collection([]) == [
            "the generated collection is not a JSON object."
        ]

    def test_a_wrong_schema_url_is_reported(self):
        collection = _generate()["collection"]
        collection["info"]["schema"] = "https://example.com/collection.json"
        problems = validate_collection(collection)
        assert any("info.schema" in problem for problem in problems)

    def test_a_duplicate_request_name_is_reported(self):
        collection = _generate()["collection"]
        first = collection["item"][0]["item"][0]
        collection["item"][0]["item"][1]["name"] = first["name"]
        problems = validate_collection(collection)
        assert any("duplicate request name" in problem for problem in problems)

    def test_an_invalid_method_is_reported(self):
        collection = _generate()["collection"]
        collection["item"][0]["item"][0]["request"]["method"] = "FETCH"
        problems = validate_collection(collection)
        assert any("invalid method" in problem for problem in problems)

    def test_an_empty_url_is_reported(self):
        collection = _generate()["collection"]
        collection["item"][0]["item"][0]["request"]["url"]["raw"] = ""
        problems = validate_collection(collection)
        assert any("empty URL" in problem for problem in problems)

    def test_a_missing_host_is_reported(self):
        collection = _generate()["collection"]
        collection["item"][0]["item"][0]["request"]["url"]["host"] = []
        problems = validate_collection(collection)
        assert any("no URL host" in problem for problem in problems)

    def test_a_duplicate_variable_is_reported(self):
        collection = _generate()["collection"]
        collection["variable"].append({"key": "baseUrl", "value": "https://other"})
        problems = validate_collection(collection)
        assert any("duplicate collection variable" in problem for problem in problems)

    def test_a_missing_base_url_variable_is_reported(self):
        collection = _generate()["collection"]
        collection["variable"] = [{"key": "token", "value": ""}]
        problems = validate_collection(collection)
        assert any("baseUrl" in problem for problem in problems)

    def test_an_empty_collection_is_reported(self):
        collection = _generate()["collection"]
        collection["item"] = []
        problems = validate_collection(collection)
        assert any("'item' must be a non-empty array" in problem for problem in problems)

    def test_a_request_without_a_name_is_reported(self):
        collection = _generate()["collection"]
        collection["item"][0]["item"][0]["name"] = ""
        problems = validate_collection(collection)
        assert any("has no name" in problem for problem in problems)

    def test_the_description_field_is_not_required(self):
        """Postman اجازه می‌دهد info.description نباشد — این خطا نیست."""
        collection = _generate()["collection"]
        collection["info"].pop("description")
        assert validate_collection(collection) == []

    def test_a_non_serialisable_collection_is_reported(self):
        collection = _generate()["collection"]
        collection["item"][0]["item"][0]["request"]["url"]["raw"] = {1, 2}
        problems = validate_collection(collection)
        assert any("cannot be serialized" in problem for problem in problems)


# ── چند سرویس ───────────────────────────────────────────────────────────────

class TestMultipleServices:
    def _with_report_service(self) -> dict:
        step2 = _step2_result()
        step2["mappings"][2]["api"] = {
            "method": "GET",
            "path": "/report/voucher/{id}",
            "operation_id": "voucherReport",
        }
        return step2

    def test_the_first_service_keeps_the_plain_base_url_variable(self):
        collection = _generate(step2=self._with_report_service())["collection"]
        _, item = _find(collection, "TC-001")
        assert item["request"]["url"]["host"] == ["{{baseUrl}}"]

    def test_the_second_service_gets_its_own_variable(self):
        collection = _generate(step2=self._with_report_service())["collection"]
        _, item = _find(collection, "TC-003")
        assert item["request"]["url"]["host"] == ["{{baseUrl_report}}"]
        variables = {v["key"]: v["value"] for v in collection["variable"]}
        assert variables["baseUrl_report"] == "https://report.example.com"

    def test_a_single_service_does_not_add_extra_variables(self):
        collection = _generate()["collection"]
        assert {v["key"] for v in collection["variable"]} == {"baseUrl", "token"}

    def test_two_services_with_the_same_name_get_distinct_variables(self):
        step2 = _step2_result()
        step2["services"][1]["name"] = "Admin"
        step2["mappings"][2]["api"] = {
            "method": "GET",
            "path": "/report/voucher/{id}",
            "operation_id": "voucherReport",
        }
        step2["services"].append(
            {
                "name": "Admin",
                "base_url": "https://audit.example.com",
                "authorization": None,
                "apis": [
                    {
                        "method": "GET",
                        "path": "/audit/voucher/{id}",
                        "operation_id": "auditVoucher",
                        "parameters": [],
                        "request_body": None,
                        "responses": {"200": {"fields": ["id"]}},
                    }
                ],
                "unresolved": [],
            }
        )
        step2["mappings"][3]["api"] = {
            "method": "GET",
            "path": "/audit/voucher/{id}",
            "operation_id": "auditVoucher",
        }
        result = _generate(step2=step2)
        variables = {v["key"]: v["value"] for v in result["collection"]["variable"]}
        assert variables["baseUrl"] == "https://admin.example.com/api"
        assert variables["baseUrl_admin"] == "https://report.example.com"
        assert variables["baseUrl_admin_2"] == "https://audit.example.com"
        assert any("renamed" in warning for warning in result["warnings"])
