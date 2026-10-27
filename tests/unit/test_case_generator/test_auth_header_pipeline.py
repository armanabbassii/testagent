"""رگرسیونِ سرتاسریِ هدرِ احراز هویت — از سندِ Swagger تا کالکشنِ نهایی.

سه endpointِ گزارش‌شده در تسک (guildCode) در سندی قرار می‌گیرند که **هیچ**
``security`` ای اعلام نمی‌کند. توصیفِ تسک هم می‌گوید این APIهای admin با توکن
فراخوانی می‌شوند. این ترکیب قبلاً باعث می‌شد یا هدر خالی ساخته شود، یا یک
fallbackِ سطحِ تسک به همه‌ی درخواست‌ها هدر اضافه کند.

اینجا رفتارِ درست آزموده می‌شود: قصدِ هر تست‌کیس (``auth_intent``) از قدم اول
تا کالکشنِ نهایی می‌رسد و تصمیمِ اعتبارنامه را می‌سازد؛ قراردادِ API فقط
مکانیزم (نامِ هدر/کوکی و نامِ متغیر) را تعیین می‌کند.

مسیرِ آزمون همان مسیرِ واقعی است:
    Step 1 (متنِ تسک + auth_intent)
        → Step2ApiMappingGenerator (بدونِ هیچ fallbackِ متنی/مسیری)
        → Step4PostmanGenerator (ساختِ قطعیِ کالکشن)

هیچ LLM و هیچ درخواستِ شبکه‌ای در این تست‌ها اجرا نمی‌شود.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.agents.test_case_generator.api_mapping import Step2ApiMappingGenerator
from src.agents.test_case_generator.auth_intent import (
    AUTH_INTENT_AUTHENTICATED,
    AUTH_INTENT_INHERIT,
    AUTH_INTENT_INVALID,
    AUTH_INTENT_UNAUTHENTICATED,
)
from src.agents.test_case_generator.postman_generation import Step4PostmanGenerator

_GUILD_CODE_ENDPOINTS = (
    ("costium", "getCostiumGuildCode"),
    ("digitalService", "getDigitalServiceGuildCode"),
    ("plan", "getPlanGuildCode"),
)

_COSTIUM_PATH = "/api/admin/costium/{id}/guildCode"
_PUBLIC_PATH = "/api/public/status"

# (resource، قصد، عنوان) — چند تست برای یک endpoint عمداً قصدهای متفاوت دارند.
_CASES = (
    ("TC-001", "costium", AUTH_INTENT_AUTHENTICATED, "Read the costium guild code"),
    (
        "TC-002",
        "digitalService",
        AUTH_INTENT_AUTHENTICATED,
        "Read the digitalService guild code",
    ),
    ("TC-003", "plan", AUTH_INTENT_AUTHENTICATED, "Read the plan guild code"),
    (
        "TC-004",
        "costium",
        AUTH_INTENT_UNAUTHENTICATED,
        "Read the costium guild code without credentials",
    ),
    (
        "TC-005",
        "costium",
        AUTH_INTENT_INVALID,
        "Read the costium guild code with an expired token",
    ),
    ("TC-006", "costium", AUTH_INTENT_INHERIT, "Read the costium guild code"),
    ("TC-007", None, AUTH_INTENT_INHERIT, "Read the public status"),
    (
        "TC-008",
        None,
        AUTH_INTENT_AUTHENTICATED,
        "Read the public status with credentials",
    ),
)


def _step1_result() -> dict:
    return {
        "task_summary": (
            "Read guild codes from the admin service. The endpoints are admin APIs "
            "and are called with an Authorization Bearer token / PODIUM_ACCESS_TOKEN "
            "cookie."
        ),
        "identified_requirements": [
            "The endpoints return guild codes.",
            "Admin endpoints require authentication.",
        ],
        "test_cases": [
            {
                "id": case_id,
                "title": title,
                "type": "positive",
                "priority": "high",
                "preconditions": ["An admin token is available"],
                "steps": [
                    f"Send GET {_PUBLIC_PATH if resource is None else f'/api/admin/{resource}/{{id}}/guildCode'}"
                ],
                "expected_result": "The guild code is returned.",
                "auth_intent": intent,
            }
            for case_id, resource, intent, title in _CASES
        ],
    }


def _mapping(case_id: str, method: str, path: str, operation_id: str) -> dict:
    return {
        "test_case_id": case_id,
        "api": {"method": method, "path": path, "operation_id": operation_id},
        "confidence": "high",
        "reason": "The test case reads this operation.",
        "clarification": "",
    }


def _step3_result(*, case_ids: list[str]) -> dict:
    return {
        "scenarios": [],
        "execution_order": [
            {"test_case_id": case_id, "order": index, "depends_on": []}
            for index, case_id in enumerate(case_ids, start=1)
        ],
        "data_dependencies": [],
        "clarifications": [],
    }


def _swagger_doc() -> dict:
    """سندِ admin بدونِ هیچ ``security`` — دقیقاً شکلی که auth از دست می‌رفت."""
    paths: dict = {}
    for resource, operation_id in _GUILD_CODE_ENDPOINTS:
        paths[f"/api/admin/{resource}/{{id}}/guildCode"] = {
            "get": {
                "operationId": operation_id,
                "summary": f"Get the {resource} guild code",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "OK"}},
            }
        }
    paths[_PUBLIC_PATH] = {
        "get": {
            "operationId": "getPublicStatus",
            "summary": "Public status",
            "responses": {"200": {"description": "OK"}},
        }
    }
    return {
        "openapi": "3.0.1",
        "info": {"title": "Admin", "version": "1.0.0"},
        "servers": [{"url": "https://podium-admin.sandpod.ir"}],
        "paths": paths,
    }


def _step2_result(tmp_path, step1: dict) -> dict:
    """قدم دومِ واقعی؛ فقط نگاشت (LLM) ماک می‌شود."""
    swagger = tmp_path / "admin.json"
    swagger.write_text(json.dumps(_swagger_doc()), encoding="utf-8")

    mappings = []
    for case_id, resource, _intent, _title in _CASES:
        if resource is None:
            mappings.append(_mapping(case_id, "GET", _PUBLIC_PATH, "getPublicStatus"))
        else:
            operation_id = dict(_GUILD_CODE_ENDPOINTS)[resource]
            mappings.append(
                _mapping(
                    case_id,
                    "GET",
                    f"/api/admin/{resource}/{{id}}/guildCode",
                    operation_id,
                )
            )

    generator = Step2ApiMappingGenerator()
    generator._agent.map_test_cases = MagicMock(return_value=(mappings, []))
    return generator.generate(step1, [str(swagger)], user_id="user-1")


def _generate_collection(tmp_path, step1: dict | None = None) -> tuple[dict, dict]:
    step1 = step1 or _step1_result()
    step2 = _step2_result(tmp_path, step1)
    case_ids = [case["id"] for case in step1["test_cases"]]
    result = Step4PostmanGenerator().generate(
        step1_result=step1,
        step2_result=step2,
        step3_result=_step3_result(case_ids=case_ids),
    )
    return step2, result["collection"]


def _item(collection: dict, case_id: str) -> dict:
    for folder in collection["item"]:
        for item in folder["item"]:
            if item["name"].split(" — ", 1)[0] == case_id:
                return item
    raise AssertionError(f"no request was generated for {case_id}")


def _headers(item: dict) -> dict[str, str]:
    return {header["key"]: header["value"] for header in item["request"].get("header") or []}


def _auth_headers(item: dict) -> list[dict]:
    return [
        header
        for header in item["request"].get("header") or []
        if header["key"].lower() == "authorization"
    ]


class TestAuthSurvivesTheWholePipeline:
    @pytest.mark.parametrize(
        ("case_id", "resource"),
        [
            ("TC-001", "costium"),
            ("TC-002", "digitalService"),
            ("TC-003", "plan"),
        ],
    )
    def test_each_authenticated_request_carries_the_exact_header(
        self, tmp_path, case_id, resource
    ):
        step2, collection = _generate_collection(tmp_path)

        # سند هیچ security ای اعلام نکرده و هیچ fallbackِ مسیری/متنی هم اجرا
        # نمی‌شود؛ هدر فقط از auth_intentِ همان تست‌کیس آمده است.
        service = step2["services"][0]
        assert service["authorization"] is None
        assert all(api["requires_auth"] is False for api in service["apis"])

        item = _item(collection, case_id)
        assert _auth_headers(item) == [{"key": "authorization", "value": "{{token}}"}]

        # مسیرِ ثبت‌شده در Postman همان عملیات است، با :id به‌جای {id}.
        assert f"/api/admin/{resource}/:id/guildCode" in item["request"]["url"]["raw"]

    def test_an_explicitly_unauthenticated_case_omits_the_header(self, tmp_path):
        _, collection = _generate_collection(tmp_path)

        item = _item(collection, "TC-004")
        assert "authorization" not in _headers(item)
        assert "intentionally not sent" in item["request"]["description"]

    def test_an_invalid_token_case_keeps_the_header_with_the_invalid_variable(self, tmp_path):
        _, collection = _generate_collection(tmp_path)

        item = _item(collection, "TC-005")
        assert _auth_headers(item) == [
            {"key": "authorization", "value": "{{invalidToken}}"}
        ]

        variables = {v["key"]: v["value"] for v in collection["variable"]}
        assert variables["invalidToken"] == ""
        assert variables["token"] == ""

    def test_inherit_without_a_contract_adds_no_header(self, tmp_path):
        """«/admin/» و متنِ تسک به‌تنهایی مجوزِ افزودنِ هدر نیستند."""
        _, collection = _generate_collection(tmp_path)

        assert "authorization" not in _headers(_item(collection, "TC-006"))

    def test_a_public_endpoint_gets_no_header_by_default(self, tmp_path):
        _, collection = _generate_collection(tmp_path)

        assert "authorization" not in _headers(_item(collection, "TC-007"))

    def test_a_public_endpoint_gets_a_header_when_the_case_explicitly_asks(self, tmp_path):
        _, collection = _generate_collection(tmp_path)

        assert _auth_headers(_item(collection, "TC-008")) == [
            {"key": "authorization", "value": "{{token}}"}
        ]

    def test_one_endpoint_can_carry_authenticated_and_unauthenticated_cases(self, tmp_path):
        _, collection = _generate_collection(tmp_path)

        assert _headers(_item(collection, "TC-001"))["authorization"] == "{{token}}"
        assert "authorization" not in _headers(_item(collection, "TC-004"))
        assert _headers(_item(collection, "TC-005"))["authorization"] == "{{invalidToken}}"
        assert "authorization" not in _headers(_item(collection, "TC-006"))

    def test_every_request_has_at_most_one_authorization_header(self, tmp_path):
        _, collection = _generate_collection(tmp_path)

        for case_id, _resource, _intent, _title in _CASES:
            keys = [
                header["key"].lower()
                for header in _item(collection, case_id)["request"].get("header") or []
            ]
            assert keys.count("authorization") <= 1

    def test_no_real_credential_is_written_anywhere(self, tmp_path):
        _, collection = _generate_collection(tmp_path)

        for case_id, _resource, _intent, _title in _CASES:
            for header in _item(collection, case_id)["request"].get("header") or []:
                if header["key"].lower() == "authorization":
                    assert header["value"] in ("{{token}}", "{{invalidToken}}")

    def test_cookie_contract_is_preserved_end_to_end(self, tmp_path):
        step1 = _step1_result()
        step2 = _step2_result(tmp_path, step1)
        step2["services"][0]["authorization"] = {
            "header": "Cookie",
            "value": "PODIUM_ACCESS_TOKEN={{token}}",
        }
        result = Step4PostmanGenerator().generate(
            step1_result=step1,
            step2_result=step2,
            step3_result=_step3_result(
                case_ids=[case["id"] for case in step1["test_cases"]]
            ),
        )
        collection = result["collection"]

        assert _headers(_item(collection, "TC-001"))["Cookie"] == (
            "PODIUM_ACCESS_TOKEN={{token}}"
        )
        assert "authorization" not in _headers(_item(collection, "TC-001"))
        assert _headers(_item(collection, "TC-005"))["Cookie"] == (
            "PODIUM_ACCESS_TOKEN={{invalidToken}}"
        )
        assert "authorization" not in _headers(_item(collection, "TC-005"))

    def test_task_wording_and_admin_paths_do_not_add_a_header_on_their_own(self, tmp_path):
        """اگر قصدِ تست‌کیس ثبت نشده باشد، متنِ تسک fallback نیست."""
        step1 = _step1_result()
        for case in step1["test_cases"]:
            case.pop("auth_intent", None)
            case["title"] = "Read the guild code"
        step2, collection = _generate_collection(tmp_path, step1=step1)

        assert step2["services"][0]["authorization"] is None
        for case_id in ("TC-001", "TC-002", "TC-003", "TC-006"):
            assert "authorization" not in _headers(_item(collection, case_id))

    def test_the_collection_declares_the_token_variable_with_an_empty_runtime_value(self, tmp_path):
        _, collection = _generate_collection(tmp_path)

        variables = {v["key"]: v["value"] for v in collection["variable"]}
        assert variables["token"] == ""
        assert variables["baseUrl"] == "https://podium-admin.sandpod.ir"
