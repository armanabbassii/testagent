"""
تست‌های رگرسیونِ قاعده‌ی سراسریِ «Authorization: {{token}}»

مشکلِ اصلی: snapshotهای Swagger (data/swagger/*.json) هیچ `security` ای ندارند،
پس فیلدِ `authorization` در نتیجه‌ی قدم دوم همیشه null بود و قدم چهارم — که
هدر را فقط از همان فیلد می‌ساخت — درخواست‌ها را بدونِ Authorization تولید می‌کرد.

قاعده‌ی جدید بی‌قید و شرط است: هر درخواستِ Postman دقیقاً یک هدرِ
{"key": "Authorization", "value": "{{token}}", "type": "text"} دارد. این تست‌ها
خودِ JSONِ سریال‌شده‌ی نهایی را می‌سنجند — همان رشته‌ای که قدم پنجم برای دانلود
می‌دهد — نه فقط dictِ میانی.
"""

from __future__ import annotations

import copy
import json

import pytest

from src.agents.test_case_generator.api_discovery import discover_from_spec
from src.agents.test_case_generator.api_mapping import ApiMapping, MappedApi, build_result
from src.agents.test_case_generator.final_review import FinalReviewGenerator
from src.agents.test_case_generator.postman_builder import PostmanBuilder
from src.agents.test_case_generator.postman_generation import (
    Step4PostmanGenerator,
    has_required_authorization,
    validate_collection,
)
from ui.formatting import collection_json, postman_collection

_REQUIRED = {"key": "Authorization", "value": "{{token}}", "type": "text"}


# ── fixtureها: Swagger بدونِ هیچ اطلاعاتِ احراز هویت ────────────────────────

def _swagger() -> dict:
    """سندِ OpenAPI بدونِ security، بدونِ securitySchemes و بدونِ پارامترِ هدر."""
    return {
        "openapi": "3.0.1",
        "info": {"title": "Admin", "version": "1.0"},
        "servers": [{"url": "https://admin.example.com/api"}],
        "paths": {
            "/admin/voucher/{code}": {
                "get": {
                    "operationId": "getVoucherDetails",
                    "tags": ["voucher-admin-controller"],
                    "parameters": [
                        {
                            "name": "code",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {"description": "OK"},
                        "404": {"description": "Not found"},
                    },
                }
            },
            "/admin/voucher": {
                "post": {
                    "operationId": "defineVoucher",
                    "tags": ["voucher-admin-controller"],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "code": {"type": "string"},
                                        "discount": {"type": "integer"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {
                        "201": {"description": "Created"},
                        "400": {"description": "Bad request"},
                    },
                }
            },
        },
    }


_GET = MappedApi(method="GET", path="/admin/voucher/{code}", operation_id="getVoucherDetails")
_POST = MappedApi(method="POST", path="/admin/voucher", operation_id="defineVoucher")

# چند تست‌کیس روی یک API — مثبت و منفی‌های کسب‌وکاری
_CASES = [
    ("TC-001", "Read an existing voucher", "positive", _GET),
    ("TC-002", "Read a voucher that does not exist", "negative", _GET),
    ("TC-003", "Read a voucher with an invalid business identifier", "negative", _GET),
    ("TC-004", "Define a voucher with a discount above 100", "negative", _POST),
    ("TC-005", "Read a voucher without authentication", "negative", _GET),
]


def _step1() -> dict:
    return {
        "task_summary": "Voucher administration",
        "test_cases": [
            {
                "id": case_id,
                "title": title,
                "type": case_type,
                "priority": "high",
                "preconditions": [],
                "steps": [title],
                "expected_result": f"{title} behaves as specified.",
            }
            for case_id, title, case_type, _ in _CASES
        ],
    }


def _step2() -> dict:
    """نتیجه‌ی واقعیِ قدم دوم — از همان مسیرِ کشفِ Swagger و build_result."""
    service = discover_from_spec(_swagger(), source_url="http://swagger.local/admin")
    mappings = [
        ApiMapping(
            test_case_id=case_id,
            api=api,
            confidence="high",
            reason="Mapped by the test fixture.",
            clarification="",
        )
        for case_id, _, _, api in _CASES
    ]
    return build_result([service], mappings, [])


def _step3(dependencies: list[dict] | None = None) -> dict:
    ids = [case_id for case_id, *_ in _CASES]
    return {
        "scenarios": [
            {"id": "SC-001", "title": "Vouchers", "test_case_ids": ids, "reason": "One flow."}
        ],
        "execution_order": [
            {"test_case_id": case_id, "order": order, "depends_on": []}
            for order, case_id in enumerate(ids, start=1)
        ],
        "data_dependencies": dependencies or [],
        "clarifications": [],
    }


def _exported(step2: dict | None = None, step3: dict | None = None) -> dict:
    """کلِ جریانِ قطعی: قدم ۴ → قدم ۵ → رشته‌ی JSONِ دانلود → parse دوباره."""
    step1 = _step1()
    step2 = step2 if step2 is not None else _step2()
    step3 = step3 if step3 is not None else _step3()
    step4 = Step4PostmanGenerator().generate(
        step1_result=step1, step2_result=step2, step3_result=step3
    )
    review = FinalReviewGenerator().generate(
        step1_result=step1,
        step2_result=step2,
        step3_result=step3,
        step4_result=step4,
    )
    return json.loads(collection_json(postman_collection(review)))


def _requests(collection: dict) -> list[dict]:
    return [item for folder in collection["item"] for item in folder["item"]]


def _auth_headers(item: dict) -> list[dict]:
    return [
        header
        for header in item["request"]["header"]
        if header["key"].strip().lower() == "authorization"
    ]


def _assert_every_request_is_authorized(collection: dict) -> None:
    requests = _requests(collection)
    assert requests, "the collection has no request"
    for item in requests:
        assert _auth_headers(item) == [_REQUIRED], item["name"]


def _built(headers: dict | None, body: dict | None = None) -> dict:
    """یک درخواستِ PostmanBuilder، سریال‌شده و دوباره parse شده."""
    case = {
        "name": "TC-001",
        "type": "positive",
        "method": "POST",
        "path": "/admin/voucher",
        "description": "",
        "query_params": {},
        "path_params": {},
        "body": body,
        "expected_status": 0,
        "assertions": [],
        "save_variables": [],
        "assumptions": [],
    }
    if headers is not None:
        case["headers"] = headers
    collection = PostmanBuilder().build(
        suite={"controllers": [{"name": "Admin", "test_cases": [case]}]},
        base_url="https://admin.example.com",
    )
    return json.loads(json.dumps(collection))["item"][0]["item"][0]


# ── A و B: نبودِ authorization در قدم دوم و در Swagger ──────────────────────

class TestNoAuthorizationUpstream:
    def test_swagger_without_security_yields_no_authorization_in_step2(self):
        """پیش‌شرطِ مشکل: قدم دوم واقعاً هیچ authorization ای ندارد."""
        service = _step2()["services"][0]
        assert service["authorization"] is None
        assert all(
            parameter["in"] != "header"
            for api in service["apis"]
            for parameter in api["parameters"]
        )

    def test_a_step2_mapping_without_authorization_still_produces_the_header(self):
        _assert_every_request_is_authorized(_exported())

    def test_an_older_step2_artifact_without_the_field_at_all_still_works(self):
        """نتیجه‌ی قدیمیِ قدم دوم ممکن است اصلاً کلیدِ authorization نداشته باشد."""
        step2 = _step2()
        del step2["services"][0]["authorization"]
        _assert_every_request_is_authorized(_exported(step2=step2))


# ── C: همه‌ی تست‌کیس‌های یک API ─────────────────────────────────────────────

class TestEveryGeneratedTestCase:
    def test_every_test_case_becomes_an_authorized_request(self):
        collection = _exported()
        names = [item["name"] for item in _requests(collection)]
        assert len(names) == len(_CASES)
        _assert_every_request_is_authorized(collection)

    @pytest.mark.parametrize(
        "case_id",
        ["TC-001", "TC-002", "TC-003", "TC-004", "TC-005"],
        ids=[
            "existing-resource",
            "non-existent-resource",
            "invalid-business-identifier",
            "invalid-discount",
            "described-without-authentication",
        ],
    )
    def test_each_scenario_sends_exactly_one_token_header(self, case_id):
        item = next(
            item for item in _requests(_exported()) if item["name"].startswith(case_id)
        )
        assert _auth_headers(item) == [_REQUIRED]


# ── D، E و F: نرمال‌سازی، حروفِ مختلف، حفظِ بقیه‌ی هدرها ────────────────────

class TestHeaderNormalization:
    def test_an_existing_authorization_value_is_replaced_not_duplicated(self):
        item = _built({"Authorization": "Bearer real-looking-token"})
        assert _auth_headers(item) == [_REQUIRED]

    @pytest.mark.parametrize(
        "name", ["authorization", "AUTHORIZATION", "AuThOrIzAtIoN", " authorization "]
    )
    def test_header_names_are_matched_case_insensitively(self, name):
        item = _built({name: "Bearer {{token}}"})
        assert _auth_headers(item) == [_REQUIRED]

    def test_several_authorization_variants_collapse_into_one(self):
        item = _built({"authorization": "a", "AUTHORIZATION": "b", "Authorization": "c"})
        assert _auth_headers(item) == [_REQUIRED]

    def test_missing_headers_field_still_gets_the_header(self):
        """جریانِ agent.py برای کیسِ unauthorized هدرها را خالی می‌فرستد."""
        assert _auth_headers(_built(None)) == [_REQUIRED]
        assert _auth_headers(_built({})) == [_REQUIRED]

    def test_unrelated_headers_are_preserved_unchanged(self):
        item = _built(
            {
                "authorization": "old",
                "Content-Type": "application/vnd.api+json",
                "X-Trace-Id": "{{traceId}}",
            },
            body={"code": "SUMMER"},
        )
        others = [
            header for header in item["request"]["header"] if header not in _auth_headers(item)
        ]
        assert others == [
            {"key": "Content-Type", "value": "application/vnd.api+json"},
            {"key": "X-Trace-Id", "value": "{{traceId}}"},
        ]

    def test_the_automatic_content_type_is_still_added_for_a_json_body(self):
        item = _built({}, body={"code": "SUMMER"})
        assert {"key": "Content-Type", "value": "application/json"} in item["request"]["header"]

    def test_method_url_and_body_are_unchanged(self):
        item = _built({"Authorization": "x"}, body={"code": "SUMMER"})
        request = item["request"]
        assert request["method"] == "POST"
        assert request["url"]["raw"] == "{{baseUrl}}/admin/voucher"
        assert json.loads(request["body"]["raw"]) == {"code": "SUMMER"}


# ── G و H: JSONِ نهایی و پایداری در طولِ همه‌ی قدم‌ها ───────────────────────

class TestAcrossPipelineStages:
    def test_the_token_variable_is_declared_by_the_collection(self):
        variables = {v["key"] for v in _exported()["variable"]}
        assert "token" in variables

    def test_a_step3_dependency_targeting_the_authorization_header_cannot_override_it(self):
        dependencies = [
            {
                "variable_name": "issuedToken",
                "source": {
                    "test_case_id": "TC-001",
                    "location": "response.header",
                    "path": "$.X-Issued-Token",
                },
                "targets": [
                    {"test_case_id": "TC-002", "location": "header", "parameter": "authorization"}
                ],
                "confidence": "low",
                "reason": "Fixture: a dependency that targets the auth header.",
            }
        ]
        _assert_every_request_is_authorized(_exported(step3=_step3(dependencies)))

    def test_a_fresh_collection_passes_step4_and_step5_validation(self):
        step1, step2, step3 = _step1(), _step2(), _step3()
        step4 = Step4PostmanGenerator().generate(
            step1_result=step1, step2_result=step2, step3_result=step3
        )
        assert validate_collection(step4["collection"]) == []
        review = FinalReviewGenerator().generate(
            step1_result=step1, step2_result=step2, step3_result=step3, step4_result=step4
        )
        assert review["review"]["issues"] == []

    def test_a_stale_step4_collection_without_the_header_is_blocked_in_step5(self):
        """نتیجه‌ی قدیمیِ قدم چهارم (پیش از این قاعده) بی‌صدا صادر نمی‌شود."""
        step1, step2, step3 = _step1(), _step2(), _step3()
        step4 = Step4PostmanGenerator().generate(
            step1_result=step1, step2_result=step2, step3_result=step3
        )
        stale = copy.deepcopy(step4)
        for item in _requests(stale["collection"]):
            item["request"]["header"] = [
                header for header in item["request"]["header"]
                if header["key"].lower() != "authorization"
            ]

        review = FinalReviewGenerator().generate(
            step1_result=step1, step2_result=step2, step3_result=step3, step4_result=stale
        )
        assert review["review"]["status"] == "blocked"
        messages = [issue["message"] for issue in review["review"]["issues"]]
        assert any("Authorization: {{token}}" in message for message in messages)


class TestHasRequiredAuthorization:
    @pytest.mark.parametrize(
        "headers, expected",
        [
            ([_REQUIRED], True),
            ([{"key": "authorization", "value": "{{token}}"}], True),
            ([], False),
            ([{"key": "Authorization", "value": "Bearer {{token}}"}], False),
            ([_REQUIRED, {"key": "AUTHORIZATION", "value": "{{token}}"}], False),
        ],
        ids=["required", "lowercase-name", "missing", "wrong-value", "duplicate"],
    )
    def test_exactly_one_token_header_is_required(self, headers, expected):
        assert has_required_authorization({"header": headers}) is expected
