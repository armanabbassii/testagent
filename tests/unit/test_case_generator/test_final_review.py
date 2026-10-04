"""
تست‌های واحدِ قدم پنجم — بازبینیِ نهایی و صادرات

تمرکزِ این تست‌ها روی چیزهایی است که قدم پنجم واقعاً تصمیم می‌گیرد: وضعیت
(ready / needs_review / blocked)، پوششِ تست‌کیس‌ها با درخواست‌های ساخته‌شده،
سازگاریِ بین‌قدمی، اعتبارِ خودِ کالکشن، متریک‌ها، مواردِ حل‌نشده و صادراتِ
دست‌نخورده‌ی کالکشنِ قدم چهارم.

هیچ تستی اینجا LLM، شبکه یا API را صدا نمی‌زند. زنجیره‌ی ورودی با اجرای واقعیِ
Step4PostmanGenerator ساخته می‌شود تا بازبینی روی همان artifact واقعی انجام شود.
"""

from __future__ import annotations

import copy
import json

from src.agents.test_case_generator.final_review import (
    STATUS_BLOCKED,
    STATUS_NEEDS_REVIEW,
    STATUS_READY,
    FinalReviewGenerator,
)
from src.agents.test_case_generator.postman_generation import Step4PostmanGenerator

# ── fixtureها ────────────────────────────────────────────────────────────────

_STEP1_CLARIFICATION = (
    "The exact response fields expected in the success payload are not defined."
)
_STEP2_CLARIFICATION = (
    "The expected HTTP status code and error message format for a non-existent "
    "voucher are not defined."
)
_STEP3_CLARIFICATION = (
    "TC-004 requires a valid voucher ID, but no test case in the current scope "
    "creates or provisions a voucher."
)


def _step1_result(clarifications: list[str] | None = None) -> dict:
    return {
        "task_summary": "Voucher management for the admin service.",
        "identified_requirements": ["Create a voucher.", "Read a voucher."],
        "test_cases": [
            {
                "id": "TC-001",
                "title": "Create a valid voucher",
                "type": "positive",
                "priority": "high",
                "preconditions": ["An admin token is available"],
                "steps": ["Send POST /admin/voucher with a valid body"],
                "expected_result": "The voucher is created and its id is returned.",
                "related_service": {
                    "method": "POST",
                    "path": "/admin/voucher",
                    "service": "Admin",
                },
            },
            {
                "id": "TC-002",
                "title": "Create a voucher with an invalid discount",
                "type": "negative",
                "priority": "medium",
                "preconditions": [],
                "steps": ["Send POST /admin/voucher with discount above 100"],
                "expected_result": "The request is rejected with a validation error.",
                "related_service": {
                    "method": "POST",
                    "path": "/admin/voucher",
                    "service": "Admin",
                },
            },
            {
                "id": "TC-003",
                "title": "Retrieve the created voucher",
                "type": "positive",
                "priority": "high",
                "preconditions": ["A voucher already exists"],
                "steps": ["Send GET /admin/voucher/{id}"],
                "expected_result": "The voucher details are returned.",
                "related_service": {
                    "method": "GET",
                    "path": "/admin/voucher/{id}",
                    "service": "Admin",
                },
            },
            {
                "id": "TC-004",
                "title": "Read a non-existent voucher",
                "type": "negative",
                "priority": "medium",
                "preconditions": [],
                "steps": ["Send GET /admin/voucher/{id} for an unknown id"],
                "expected_result": "The request is rejected because the voucher is unknown.",
                "related_service": {
                    "method": "GET",
                    "path": "/admin/voucher/{id}",
                    "service": "Admin",
                },
            },
        ],
        "clarifications": list(clarifications or []),
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


def _step2_result(
    unmap: tuple[str, ...] = (), clarifications: list[str] | None = None
) -> dict:
    """نگاشت‌های قدم دوم.

    نامِ پارامترِ مسیرِ TC-003 عمداً `{voucherId}` است در حالی که سند
    `{id}` می‌گوید — همان چیزی که قدم دوم واقعاً تولید می‌کند.
    """
    mappings = [
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
            "confidence": "low",
            "reason": "The error behaviour is not documented.",
            "clarification": "",
        },
    ]
    for mapping in mappings:
        if mapping["test_case_id"] in unmap:
            mapping["api"] = None
    return {
        "services": [_admin_service()],
        "mappings": mappings,
        "clarifications": list(clarifications or []),
    }


def _step3_result(clarifications: list[dict] | None = None) -> dict:
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
                    {"test_case_id": "TC-003", "location": "path", "parameter": "id"},
                    {
                        "test_case_id": "TC-003",
                        "location": "header",
                        "parameter": "X-Voucher-Id",
                    },
                ],
                "confidence": "high",
                "reason": "The id returned by the creation is used to read it back.",
            }
        ],
        "clarifications": list(clarifications or []),
    }


def _build(step1: dict, step2: dict, step3: dict) -> dict:
    """قدم چهارم را واقعاً اجرا می‌کند — artifact ورودیِ بازبینی همین است."""
    return Step4PostmanGenerator(collection_name="Voucher API Tests").generate(
        step1_result=step1,
        step2_result=step2,
        step3_result=step3,
    )


def _review(
    step1: dict, step2: dict, step3: dict, step4: dict | None = None
) -> dict:
    return FinalReviewGenerator().generate(
        step1_result=step1,
        step2_result=step2,
        step3_result=step3,
        step4_result=step4 if step4 is not None else _build(step1, step2, step3),
    )


def _clean_chain() -> dict:
    """زنجیره‌ی کامل و بی‌ابهام: هر چهار تست‌کیس نگاشت و درخواست دارند."""
    step1 = _step1_result()
    step2 = _step2_result()
    step3 = _step3_result()
    return _review(step1, step2, step3)


def _open_chain() -> dict:
    """زنجیره‌ی واقعی: سه ابهامِ کسب‌وکاری + یک تست‌کیسِ حل‌نشده."""
    step1 = _step1_result([_STEP1_CLARIFICATION])
    step2 = _step2_result(unmap=("TC-004",), clarifications=[_STEP2_CLARIFICATION])
    step3 = _step3_result(
        [
            {
                "type": "missing_data",
                "test_case_id": "TC-004",
                "message": _STEP3_CLARIFICATION,
            }
        ]
    )
    return _review(step1, step2, step3)


def _codes(result: dict) -> list[str]:
    return [issue["code"] for issue in result["review"]["issues"]]


def _tampered(step4: dict) -> dict:
    """کپیِ مستقلِ نتیجه‌ی قدم چهارم، برای دست‌کاریِ عمدی در تست‌ها."""
    return copy.deepcopy(step4)


def _find_request(collection: dict, case_id: str) -> dict:
    """آیتمِ کالکشنِ یک تست‌کیس — با همان قراردادِ نام‌گذاریِ قدم چهارم."""
    for folder in collection["item"]:
        for item in folder["item"]:
            if item["name"].split(" — ", 1)[0] == case_id:
                return item
    raise AssertionError(f"no request was generated for {case_id}")


def _drop_request(collection: dict, case_id: str) -> None:
    """یک درخواست را از کالکشن حذف می‌کند تا صادراتِ دست‌کاری‌شده شبیه‌سازی شود."""
    for folder in collection["item"]:
        for index, item in enumerate(folder["item"]):
            if item["name"].split(" — ", 1)[0] == case_id:
                del folder["item"][index]
                return
    raise AssertionError(f"no request was generated for {case_id}")


def _append_request(collection: dict, case_id: str, item: dict) -> None:
    """درخواستِ تازه‌ای کنارِ درخواستِ همان تست‌کیس اضافه می‌کند."""
    for folder in collection["item"]:
        for existing in folder["item"]:
            if existing["name"].split(" — ", 1)[0] == case_id:
                folder["item"].append(item)
                return
    raise AssertionError(f"no request was generated for {case_id}")


# ── وضعیت ────────────────────────────────────────────────────────────────────

class TestReviewStatus:
    def test_a_fully_resolved_chain_is_ready(self):
        result = _clean_chain()
        assert result["review"]["status"] == STATUS_READY
        assert result["review"]["issues"] == []
        assert result["review"]["warnings"] == []
        assert result["review"]["unresolved_clarifications"] == []

    def test_open_clarifications_need_review_not_blocking(self):
        result = _open_chain()
        assert result["review"]["status"] == STATUS_NEEDS_REVIEW
        assert result["review"]["issues"] == []

    def test_an_unresolved_test_case_needs_review_not_blocking(self):
        result = _open_chain()
        assert result["review"]["status"] != STATUS_BLOCKED
        assert result["metrics"]["generated_requests"] == 3

    def test_a_broken_collection_is_blocked(self):
        step1, step2, step3 = _step1_result(), _step2_result(), _step3_result()
        step4 = _tampered(_build(step1, step2, step3))
        _drop_request(step4["collection"], "TC-001")
        result = _review(step1, step2, step3, step4)
        assert result["review"]["status"] == STATUS_BLOCKED
        assert result["review"]["issues"]

    def test_a_warning_alone_does_not_block(self):
        # جزئیاتِ بیشتر در TestMetrics.test_a_step4_warning_is_surfaced_...
        step1, step2 = _step1_result(), _step2_result()
        step3 = _step3_result()
        step3["execution_order"] = [
            {"test_case_id": "TC-003", "order": 1, "depends_on": ["TC-001"]},
            {"test_case_id": "TC-001", "order": 2, "depends_on": []},
            {"test_case_id": "TC-004", "order": 3, "depends_on": []},
            {"test_case_id": "TC-002", "order": 4, "depends_on": []},
        ]
        result = _review(step1, step2, step3)

        assert result["review"]["warnings"]
        assert result["review"]["status"] == STATUS_NEEDS_REVIEW


# ── پوششِ تست‌کیس‌ها ─────────────────────────────────────────────────────────

class TestCoverage:
    def test_every_test_case_gets_exactly_one_request(self):
        result = _clean_chain()
        assert _codes(result) == []
        assert result["metrics"]["test_cases"] == 4
        assert result["metrics"]["generated_requests"] == 4

    def test_a_test_case_reported_unresolved_is_not_an_issue(self):
        result = _open_chain()
        assert "missing_request" not in _codes(result)
        assert result["metrics"]["test_cases"] == 4
        assert result["metrics"]["generated_requests"] == 3

    def test_a_missing_request_is_an_issue(self):
        step1, step2, step3 = _step1_result(), _step2_result(), _step3_result()
        step4 = _tampered(_build(step1, step2, step3))
        _drop_request(step4["collection"], "TC-003")
        result = _review(step1, step2, step3, step4)

        assert "missing_request" in _codes(result)
        issue = next(
            i for i in result["review"]["issues"] if i["code"] == "missing_request"
        )
        assert issue["test_case_id"] == "TC-003"
        assert issue["step"] == 4

    def test_a_duplicate_request_is_an_issue(self):
        step1, step2, step3 = _step1_result(), _step2_result(), _step3_result()
        step4 = _tampered(_build(step1, step2, step3))
        original = _find_request(step4["collection"], "TC-002")

        duplicate = copy.deepcopy(original)
        duplicate["name"] = f"{original['name']} (2)"
        _append_request(step4["collection"], "TC-002", duplicate)
        step4["requests"].append(
            {
                "test_case_id": "TC-002",
                "title": "Create a voucher with an invalid discount",
                "name": duplicate["name"],
                "method": "POST",
                "path": "/admin/voucher",
                "scenario": "Voucher validation",
            }
        )

        result = _review(step1, step2, step3, step4)
        assert "duplicate_request" in _codes(result)

    def test_an_untraceable_request_is_an_issue(self):
        step1, step2, step3 = _step1_result(), _step2_result(), _step3_result()
        step4 = _tampered(_build(step1, step2, step3))
        _append_request(
            step4["collection"],
            "TC-001",
            {
                "name": "TC-999 — Ghost request",
                "request": {
                    "method": "GET",
                    "url": {
                        "raw": "{{baseUrl}}/admin/ghost",
                        "host": ["{{baseUrl}}"],
                        "path": ["admin", "ghost"],
                    },
                },
            },
        )

        result = _review(step1, step2, step3, step4)
        assert "extra_request" in _codes(result)


# ── سازگاریِ قدم دوم ────────────────────────────────────────────────────────

class TestStep2Consistency:
    def test_matching_mappings_produce_no_issue(self):
        assert _codes(_clean_chain()) == []

    def test_a_path_parameter_name_difference_is_not_a_mismatch(self):
        # TC-003 در قدم دوم به /admin/voucher/{voucherId} نگاشته شده و کالکشن
        # :id دارد — این دو یکی هستند و نباید issue بسازند.
        result = _clean_chain()
        assert "inconsistent_mapping" not in _codes(result)
        assert "undiscovered_api" not in _codes(result)

    def test_a_mapping_that_contradicts_the_request_is_an_issue(self):
        step1, step3 = _step1_result(), _step3_result()
        step2 = _step2_result()
        step4 = _build(step1, step2, step3)

        broken = copy.deepcopy(step2)
        broken["mappings"][1]["api"]["path"] = "/admin/voucher/v2"

        result = _review(step1, broken, step3, step4)
        assert "inconsistent_mapping" in _codes(result)

    def test_a_mapping_to_an_undiscovered_operation_is_an_issue(self):
        step1, step3 = _step1_result(), _step3_result()
        step2 = _step2_result()
        step4 = _build(step1, step2, step3)

        broken = copy.deepcopy(step2)
        broken["mappings"][1]["api"]["path"] = "/admin/voucher/v2"
        broken["services"] = [
            {**service, "apis": service["apis"][:1]} for service in broken["services"]
        ]

        result = _review(step1, broken, step3, step4)
        assert "undiscovered_api" in _codes(result)

    def test_a_missing_mapping_is_an_issue(self):
        step1, step3 = _step1_result(), _step3_result()
        step2 = _step2_result()
        step4 = _build(step1, step2, step3)

        broken = copy.deepcopy(step2)
        broken["mappings"] = [
            mapping
            for mapping in broken["mappings"]
            if mapping["test_case_id"] != "TC-002"
        ]

        result = _review(step1, broken, step3, step4)
        assert "missing_mapping" in _codes(result)

    def test_an_unresolved_mapping_is_an_issue(self):
        step1, step3 = _step1_result(), _step3_result()
        step2 = _step2_result()
        step4 = _build(step1, step2, step3)

        broken = copy.deepcopy(step2)
        broken["mappings"][1]["api"] = None

        result = _review(step1, broken, step3, step4)
        assert "unresolved_mapping" in _codes(result)


# ── سازگاریِ قدم سوم ────────────────────────────────────────────────────────

class TestStep3Consistency:
    def test_a_valid_step3_result_produces_no_issue(self):
        assert "inconsistent_step3" not in _codes(_clean_chain())

    def test_a_scenario_referencing_an_unknown_case_is_an_issue(self):
        step1, step2 = _step1_result(), _step2_result()
        step3 = _step3_result()
        step4 = _build(step1, step2, step3)

        broken = copy.deepcopy(step3)
        broken["scenarios"][0]["test_case_ids"] = ["TC-001", "TC-003", "TC-999"]

        result = _review(step1, step2, broken, step4)
        assert "inconsistent_step3" in _codes(result)

    def test_a_dependency_source_without_a_mapping_is_an_issue(self):
        step1, step2 = _step1_result(), _step2_result()
        step3 = _step3_result()
        step4 = _build(step1, step2, step3)

        broken = copy.deepcopy(step2)
        broken["mappings"][0]["api"] = None

        result = _review(step1, broken, step3, step4)
        assert "inconsistent_step3" in _codes(result)


# ── اعتبارِ کالکشن ──────────────────────────────────────────────────────────

class TestCollectionValidation:
    def test_a_collection_missing_its_schema_is_an_issue(self):
        step1, step2, step3 = _step1_result(), _step2_result(), _step3_result()
        step4 = _tampered(_build(step1, step2, step3))
        step4["collection"]["info"].pop("schema")

        result = _review(step1, step2, step3, step4)
        assert "invalid_collection" in _codes(result)
        assert result["review"]["status"] == STATUS_BLOCKED

    def test_an_undeclared_variable_is_an_issue(self):
        step1, step2, step3 = _step1_result(), _step2_result(), _step3_result()
        step4 = _tampered(_build(step1, step2, step3))
        _find_request(step4["collection"], "TC-001")["request"]["url"]["raw"] = (
            "{{baseUrl}}/admin/voucher?token={{mysteryToken}}"
        )

        result = _review(step1, step2, step3, step4)
        assert "undeclared_variable" in _codes(result)

    def test_step4_metadata_that_contradicts_the_collection_is_an_issue(self):
        step1, step2, step3 = _step1_result(), _step2_result(), _step3_result()
        step4 = _tampered(_build(step1, step2, step3))
        _drop_request(step4["collection"], "TC-001")

        result = _review(step1, step2, step3, step4)
        assert "inconsistent_step4" in _codes(result)
        assert "missing_request" in _codes(result)


# ── ابهام‌ها و تست‌کیس‌های حل‌نشده ──────────────────────────────────────────

class TestUnresolvedAndClarifications:
    def test_clarifications_from_every_step_stay_visible(self):
        items = _open_chain()["review"]["unresolved_clarifications"]

        messages = [item["message"] for item in items]
        assert _STEP1_CLARIFICATION in messages
        assert _STEP2_CLARIFICATION in messages
        assert _STEP3_CLARIFICATION in messages

        by_step = {item["source_step"] for item in items}
        assert by_step == {1, 2, 3, 4}

    def test_the_unresolved_test_case_is_carried_with_its_reason(self):
        step1 = _step1_result()
        step2 = _step2_result(unmap=("TC-004",))
        step3 = _step3_result()
        step4 = _build(step1, step2, step3)
        result = _review(step1, step2, step3, step4)

        item = next(
            i
            for i in result["review"]["unresolved_clarifications"]
            if i["test_case_id"] == "TC-004"
        )
        assert item["source_step"] == 4
        assert item["message"] == step4["unresolved"][0]["reason"]

    def test_nothing_is_invented_for_a_clarification(self):
        # هیچ status code و هیچ فیلدِ پاسخی برای ابهام‌ها ساخته نمی‌شود: فقط
        # خودِ متنِ ابهام گزارش می‌شود.
        result = _open_chain()
        for item in result["review"]["unresolved_clarifications"]:
            assert item["message"]
            assert "200" not in item["message"]
            assert "404" not in item["message"]

    def test_a_blank_clarification_is_skipped(self):
        step1 = _step1_result(["", "   "])
        step2 = _step2_result()
        step3 = _step3_result()
        result = _review(step1, step2, step3)
        assert result["review"]["unresolved_clarifications"] == []


# ── متریک‌ها و خلاصه ────────────────────────────────────────────────────────

class TestMetrics:
    def test_metrics_count_every_step(self):
        metrics = _open_chain()["metrics"]
        assert metrics == {
            "test_cases": 4,
            "mapped_test_cases": 3,
            "generated_requests": 3,
            "scenarios": 2,
            "data_dependencies": 1,
            "clarifications": 4,
            "issues": 0,
            "warnings": 0,
        }

    def test_the_clarification_count_matches_the_review_items(self):
        result = _open_chain()
        assert result["metrics"]["clarifications"] == len(
            result["review"]["unresolved_clarifications"]
        )

    def test_a_clean_chain_has_no_open_items(self):
        metrics = _clean_chain()["metrics"]
        assert metrics["clarifications"] == 0
        assert metrics["issues"] == 0
        assert metrics["mapped_test_cases"] == 4

    def test_a_step4_warning_is_surfaced_but_is_not_an_issue(self):
        step1, step2 = _step1_result(), _step2_result()
        step3 = _step3_result()
        # ترتیبِ اجرا با وابستگیِ داده نمی‌خواند: TC-003 پیش از منبعش می‌آید.
        step3["execution_order"] = [
            {"test_case_id": "TC-003", "order": 1, "depends_on": ["TC-001"]},
            {"test_case_id": "TC-001", "order": 2, "depends_on": []},
            {"test_case_id": "TC-004", "order": 3, "depends_on": []},
            {"test_case_id": "TC-002", "order": 4, "depends_on": []},
        ]
        step4 = _build(step1, step2, step3)

        result = _review(step1, step2, step3, step4)
        assert result["review"]["warnings"] == step4["warnings"]
        assert result["review"]["warnings"]
        assert result["review"]["issues"] == []
        assert result["review"]["status"] == STATUS_NEEDS_REVIEW

    def test_metrics_are_deterministic(self):
        assert _open_chain()["metrics"] == _open_chain()["metrics"]


class TestSummary:
    def test_the_summary_is_deterministic(self):
        assert _open_chain()["review"]["summary"] == _open_chain()["review"]["summary"]

    def test_the_summary_reports_the_counts(self):
        summary = _open_chain()["review"]["summary"]
        assert "4 test cases analyzed." in summary
        assert "3 Postman requests generated." in summary
        assert "2 scenarios identified." in summary
        assert "1 data dependency applied." in summary
        assert "4 unresolved clarifications remain." in summary

    def test_the_summary_matches_the_status(self):
        assert "requires human review" in _open_chain()["review"]["summary"]
        assert "structurally valid" in _clean_chain()["review"]["summary"]

    def test_a_blocked_review_says_so(self):
        result = FinalReviewGenerator().generate(
            step1_result=None,
            step2_result=None,
            step3_result=None,
            step4_result=None,
        )
        assert "cannot be exported" in result["review"]["summary"]


# ── صادرات ───────────────────────────────────────────────────────────────────

class TestExport:
    def test_the_exported_collection_is_the_step4_collection(self):
        step1, step2, step3 = _step1_result(), _step2_result(), _step3_result()
        step4 = _build(step1, step2, step3)
        result = _review(step1, step2, step3, step4)

        exported = result["artifacts"]["postman_collection"]
        assert json.dumps(exported, sort_keys=True, ensure_ascii=False) == json.dumps(
            step4["collection"], sort_keys=True, ensure_ascii=False
        )

    def test_no_step5_metadata_leaks_into_the_collection(self):
        collection = _open_chain()["artifacts"]["postman_collection"]
        assert set(collection) == {"info", "item", "variable"}
        for key in ("review", "metrics", "artifacts", "status", "summary"):
            assert key not in collection

    def test_the_exported_collection_is_still_a_valid_v21_collection(self):
        collection = _clean_chain()["artifacts"]["postman_collection"]
        assert collection["info"]["schema"].endswith("/collection.json")
        assert collection["item"]


# ── ورودی ────────────────────────────────────────────────────────────────────

class TestInputHandling:
    def test_only_the_collection_is_accepted_with_a_warning(self):
        step1, step2, step3 = _step1_result(), _step2_result(), _step3_result()
        step4 = _build(step1, step2, step3)

        result = _review(step1, step2, step3, step4["collection"])
        assert result["review"]["issues"] == []
        assert result["review"]["status"] == STATUS_NEEDS_REVIEW
        assert any(
            "Only the Postman collection was provided" in warning
            for warning in result["review"]["warnings"]
        )

    def test_a_bare_collection_cannot_confirm_unresolved_cases(self):
        step1 = _step1_result()
        step2 = _step2_result(unmap=("TC-004",))
        step3 = _step3_result()
        step4 = _build(step1, step2, step3)

        result = _review(step1, step2, step3, step4["collection"])
        assert "missing_request" in _codes(result)
        assert result["review"]["status"] == STATUS_BLOCKED

    def test_invalid_inputs_become_issues_instead_of_exceptions(self):
        result = FinalReviewGenerator().generate(
            step1_result=None,
            step2_result=[],
            step3_result={},
            step4_result="nope",
        )
        assert _codes(result) == [
            "invalid_step1_input",
            "invalid_step2_input",
            "invalid_step3_input",
            "invalid_step4_input",
        ]
        assert result["review"]["status"] == STATUS_BLOCKED
        assert result["artifacts"]["postman_collection"] == {}
        assert result["metrics"]["test_cases"] == 0

    def test_an_invalid_step2_does_not_flood_the_review_with_issues(self):
        step1 = _step1_result()
        step4 = _build(step1, _step2_result(), _step3_result())
        result = _review(step1, {"mappings": "nope"}, _step3_result(), step4)
        assert _codes(result) == ["invalid_step2_input"]


# ── بدونِ عوارضِ جانبی ──────────────────────────────────────────────────────

class TestNoSideEffects:
    def test_earlier_results_are_not_modified(self):
        step1, step2, step3 = _step1_result(), _step2_result(), _step3_result()
        step4 = _build(step1, step2, step3)
        before = copy.deepcopy((step1, step2, step3, step4))

        _review(step1, step2, step3, step4)

        assert (step1, step2, step3, step4) == before

    def test_the_same_inputs_give_the_same_result(self):
        step1, step2, step3 = _step1_result(), _step2_result(), _step3_result()
        step4 = _build(step1, step2, step3)
        assert _review(step1, step2, step3, step4) == _review(step1, step2, step3, step4)
