"""
تست‌های طراحیِ تست (test_design) — داده‌ی تستِ هدفمند و assertionِ متناسب با نیازمندی

همه آفلاین‌اند: Swagger یک dictِ ساختگیِ عمومی است (یک فهرستِ صفحه‌بندی‌شده و یک
جزئیات با شناسه‌ی int64)، LLM با mock جایگزین می‌شود و اسکریپت‌های تولیدشده با
Node و یک شبیه‌سازِ حداقلیِ pm/chai واقعاً اجرا می‌شوند. هیچ‌چیز به تسک، endpoint
یا نامِ فیلدِ خاصی از محیطِ واقعی وابسته نیست.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.agents.test_case_generator.api_discovery import discover_from_spec
from src.agents.test_case_generator.api_mapping import (
    ApiMapping,
    MappedApi,
    build_result,
    compact_step2_result,
)
from src.agents.test_case_generator.final_review import (
    CHECKS_NOT_ASSERTED,
    NO_REQUIREMENT_REF,
    PENDING_TEST_CASE,
    POSSIBLY_EQUIVALENT,
    UNFILLED_RUNTIME_VARIABLE,
    FinalReviewGenerator,
)
from src.agents.test_case_generator.postman_generation import Step4PostmanGenerator
from src.agents.test_case_generator.scenario_analysis import (
    Step3ScenarioAnalysisGenerator,
    build_result as build_step3_result,
)
from src.agents.test_case_generator.test_design import (
    normalize_test_design,
    to_builder_assertion,
)

_LLM = "src.agents.test_case_generator.scenario_analysis.LLMClient"
INT64_MAX = 2**63 - 1


# ── fixtureها ────────────────────────────────────────────────────────────────


def _swagger() -> dict:
    item = {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "format": "int64"},
            "name": {"type": "string"},
            "link": {"type": "string"},
            "internalNote": {"type": "string"},
        },
    }
    return {
        "openapi": "3.0.1",
        "info": {"title": "Catalog", "version": "1.0"},
        "servers": [{"url": "https://catalog.example.com/api"}],
        "components": {"schemas": {"Item": item}},
        "paths": {
            "/catalog/items": {
                "get": {
                    "operationId": "listItems",
                    "tags": ["catalog"],
                    "parameters": [
                        {
                            "name": "offset",
                            "in": "query",
                            "schema": {"type": "string", "minimum": 0, "example": 0},
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {
                                "type": "string",
                                "minimum": 1,
                                "maximum": 50,
                                "example": 10,
                            },
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "result": {
                                                "type": "object",
                                                "properties": {
                                                    "items": {
                                                        "type": "array",
                                                        "items": {
                                                            "$ref": "#/components/schemas/Item"
                                                        },
                                                    },
                                                    "limit": {"type": "integer"},
                                                    "offset": {"type": "integer"},
                                                },
                                            }
                                        },
                                    }
                                }
                            },
                        },
                        "400": {"description": "Bad request"},
                    },
                }
            },
            "/catalog/items/{id}": {
                "get": {
                    "operationId": "getItem",
                    "tags": ["catalog"],
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "format": "int64"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {"*/*": {"schema": {"type": "object"}}},
                        },
                        "404": {"description": "Not found"},
                    },
                }
            },
        },
    }


_LIST = MappedApi(method="GET", path="/catalog/items", operation_id="listItems")
_GET = MappedApi(method="GET", path="/catalog/items/{id}", operation_id="getItem")

_CASES = [
    ("TC-001", "List items with the default page size", "positive", _LIST),
    ("TC-002", "List response omits the internal note", "positive", _LIST),
    ("TC-003", "List with the largest page size returns every item", "positive", _LIST),
    ("TC-004", "List with a page size above the maximum", "positive", _LIST),
    ("TC-005", "Get an existing item", "positive", _GET),
    ("TC-006", "Get a non-existent item is rejected", "negative", _GET),
]


def _step1(**extra) -> dict:
    result = {
        "task_summary": "Catalog read API",
        "identified_requirements": ["The list omits internal notes."],
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
        "clarifications": [],
    }
    result.update(extra)
    return result


def _step2() -> dict:
    service = discover_from_spec(_swagger(), source_url="http://swagger.local/catalog")
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


def _operations(step2: dict) -> dict:
    apis = {
        (api["method"], api["path"]): api
        for service in step2["services"]
        for api in service["apis"]
    }
    return {case_id: apis[(api["method"], api["path"])] for case_id, _, _, api in _CASES}


def _normalize(entries, step1=None, step2=None, dependencies=None):
    step1 = step1 or _step1()
    step2 = step2 or _step2()
    return normalize_test_design(
        entries,
        test_cases=step1["test_cases"],
        operations=_operations(step2),
        dependencies=dependencies or [],
    )


def _value(name, value, source, location="query", **extra):
    return {"location": location, "name": name, "value": value, "value_source": source, **extra}


def _step3(test_design=None, dependencies=None, clarifications=None) -> dict:
    ids = [case_id for case_id, *_ in _CASES]
    result = {
        "scenarios": [
            {"id": "SC-001", "title": "Catalog", "test_case_ids": ids, "reason": "One API."}
        ],
        "execution_order": [
            {"test_case_id": case_id, "order": order, "depends_on": []}
            for order, case_id in enumerate(ids, start=1)
        ],
        "data_dependencies": dependencies or [],
        "clarifications": clarifications or [],
    }
    if test_design is not None:
        result["test_design"] = test_design
    return result


def _design_fixture() -> list[dict]:
    """طراحیِ عمومیِ کامل — همان شکلی که مدل در قدم سوم برمی‌گرداند."""
    return [
        {
            "test_case_id": "TC-001",
            "request_values": [
                _value("limit", 20, "user_or_requirement", evidence="The default page size is 20."),
                _value("offset", 0, "schema_constraint"),
            ],
            "assertions": [
                {
                    "kind": "array_length",
                    "json_path": "result.items",
                    "path_source": "swagger",
                    "max": 20,
                },
                {
                    "kind": "field_equals",
                    "json_path": "result.limit",
                    "path_source": "swagger",
                    "expected": 20,
                },
            ],
        },
        {
            "test_case_id": "TC-002",
            "assertions": [
                {
                    "kind": "field_absent",
                    "json_path": "result.items[].internalNote",
                    "path_source": "swagger",
                },
                {
                    "kind": "field_present",
                    "json_path": "result.items[].link",
                    "path_source": "swagger",
                },
            ],
        },
        {
            "test_case_id": "TC-003",
            "request_values": [
                _value("limit", 50, "schema_constraint"),
                _value("offset", 0, "schema_constraint"),
            ],
            "assertions": [
                {
                    "kind": "array_length",
                    "json_path": "result.items",
                    "path_source": "swagger",
                    "max": 50,
                }
            ],
            "unverified_expectations": [
                {
                    "expectation": "Every existing item is returned.",
                    "reason": "Only verifiable when the total number of items is 50 or fewer; no documented source gives the total.",
                }
            ],
        },
        {
            "test_case_id": "TC-004",
            "request_values": [_value("limit", 51, "schema_constraint")],
            "assertions": [],
        },
        {
            "test_case_id": "TC-005",
            "request_values": [
                _value("id", "{{existingItemId}}", "runtime_variable", location="path")
            ],
        },
        {
            "test_case_id": "TC-006",
            "request_values": [_value("id", INT64_MAX, "nonexistent_id", location="path")],
            "assertions": [{"kind": "status_class", "expected": "4xx"}],
        },
    ]


def _pipeline(test_design=None, step1=None):
    step1 = step1 or _step1()
    step2 = _step2()
    step3 = _step3(test_design)
    step4 = Step4PostmanGenerator().generate(step1, step2, step3)
    review = FinalReviewGenerator().generate(step1, step2, step3, step4)
    return step1, step2, step3, step4, review


def _items(collection: dict) -> dict[str, dict]:
    return {
        item["name"].split(" — ", 1)[0]: item
        for folder in collection["item"]
        for item in folder["item"]
    }


def _script(item: dict, listen: str = "test") -> list[str]:
    for event in item.get("event") or []:
        if event["listen"] == listen:
            return event["script"]["exec"]
    return []


# ── analyzer و کشف: قیدها و مسیرهای تودرتو ───────────────────────────────────


class TestSwaggerConstraints:
    def test_parameter_constraints_are_published(self):
        api = _operations(_step2())["TC-001"]
        limit = next(p for p in api["parameters"] if p["name"] == "limit")
        assert limit["constraints"] == {"minimum": 1, "maximum": 50}
        path_id = next(
            p for p in _operations(_step2())["TC-005"]["parameters"] if p["name"] == "id"
        )
        assert path_id["constraints"] == {"format": "int64"}

    def test_nested_response_paths_are_published(self):
        responses = _operations(_step2())["TC-001"]["responses"]["200"]
        assert responses["fields"] == ["result"]
        assert "result.items[].internalNote" in responses["field_paths"]
        assert "result.limit" in responses["field_paths"]

    def test_undocumented_response_gets_no_field_paths(self):
        responses = _operations(_step2())["TC-005"]["responses"]["200"]
        assert "field_paths" not in responses
        assert responses["fields"] == []

    def test_parameters_without_constraints_keep_the_old_shape(self):
        swagger = _swagger()
        swagger["paths"]["/catalog/items"]["get"]["parameters"][0]["schema"] = {"type": "string"}
        service = discover_from_spec(swagger, source_url="http://swagger.local/catalog")
        offset = next(p for api in service.apis for p in api.parameters if p["name"] == "offset")
        assert set(offset) == {"name", "in", "required", "type", "description"}

    def test_compact_step2_carries_the_new_keys_to_step3(self):
        compact = compact_step2_result(_step2())
        operation = next(m for m in compact["mappings"] if m["test_case_id"] == "TC-001")[
            "operation"
        ]
        assert "field_paths" in operation["responses"]["200"]
        assert any("constraints" in p for p in operation["parameters"])


# ── اعتبارسنجیِ مقدارها ─────────────────────────────────────────────────────


class TestRequestValues:
    def test_a_default_needs_a_documented_default_or_a_quoted_statement(self):
        designs, notes = _normalize(
            [{"test_case_id": "TC-001", "request_values": [_value("limit", 10, "schema_default")]}]
        )
        assert designs == []
        assert "documents no default" in notes[0]["message"]

    def test_a_user_stated_default_is_accepted_with_evidence(self):
        designs, notes = _normalize(
            [
                {
                    "test_case_id": "TC-001",
                    "request_values": [
                        _value("limit", 20, "user_or_requirement", evidence="Default size is 20.")
                    ],
                }
            ]
        )
        assert notes == []
        assert designs[0]["request_values"][0]["value"] == 20

    def test_a_user_value_without_evidence_is_rejected(self):
        designs, notes = _normalize(
            [
                {
                    "test_case_id": "TC-001",
                    "request_values": [_value("limit", 20, "user_or_requirement")],
                }
            ]
        )
        assert designs == [] and "quoted evidence" in notes[0]["message"]

    @pytest.mark.parametrize("value", [1, 50, 0, 51])
    def test_boundary_values_must_sit_on_or_beyond_a_limit(self, value):
        designs, _ = _normalize(
            [
                {
                    "test_case_id": "TC-001",
                    "request_values": [_value("limit", value, "schema_constraint")],
                }
            ]
        )
        assert designs and designs[0]["request_values"][0]["value"] == value

    def test_a_value_inside_the_range_is_not_a_boundary(self):
        designs, notes = _normalize(
            [
                {
                    "test_case_id": "TC-001",
                    "request_values": [_value("limit", 25, "schema_constraint")],
                }
            ]
        )
        assert designs == [] and "not on or beyond a documented limit" in notes[0]["message"]

    def test_a_non_existent_id_must_be_well_formed(self):
        ok, _ = _normalize(
            [
                {
                    "test_case_id": "TC-006",
                    "request_values": [_value("id", INT64_MAX, "nonexistent_id", location="path")],
                }
            ]
        )
        assert ok[0]["request_values"][0]["value"] == INT64_MAX
        for bad in ("", "abc", 2**63):
            designs, notes = _normalize(
                [
                    {
                        "test_case_id": "TC-006",
                        "request_values": [_value("id", bad, "nonexistent_id", location="path")],
                    }
                ]
            )
            assert designs == [], bad
            assert notes

    def test_unknown_parameters_and_the_authorization_header_are_rejected(self):
        designs, notes = _normalize(
            [
                {
                    "test_case_id": "TC-001",
                    "request_values": [
                        _value("page", 1, "user_or_requirement", evidence="x"),
                        _value(
                            "Authorization",
                            "x",
                            "user_or_requirement",
                            location="header",
                            evidence="x",
                        ),
                    ],
                }
            ]
        )
        assert designs == []
        assert "no such parameter" in notes[0]["message"]
        assert "authentication contract" in notes[1]["message"]

    def test_a_runtime_variable_must_be_a_template(self):
        designs, notes = _normalize(
            [
                {
                    "test_case_id": "TC-005",
                    "request_values": [
                        _value("id", "existingId", "runtime_variable", location="path")
                    ],
                }
            ]
        )
        assert designs == [] and "{{name}}" in notes[0]["message"]

    def test_a_dependency_value_needs_a_matching_dependency(self):
        entry = [
            {
                "test_case_id": "TC-005",
                "request_values": [_value("id", None, "dependency", location="path")],
            }
        ]
        designs, notes = _normalize(entry)
        assert designs == [] and "no Step 3 data dependency" in notes[0]["message"]
        dependency = {
            "variable_name": "itemId",
            "source": {
                "test_case_id": "TC-001",
                "location": "response.body",
                "path": "$.result.items[0].id",
            },
            "targets": [{"test_case_id": "TC-005", "location": "path", "parameter": "id"}],
        }
        designs, notes = _normalize(entry, dependencies=[dependency])
        assert notes == [] and designs[0]["request_values"][0]["value_source"] == "dependency"


# ── اعتبارسنجیِ assertionها ─────────────────────────────────────────────────


class TestAssertions:
    def test_an_exact_status_must_be_documented(self):
        designs, notes = _normalize(
            [{"test_case_id": "TC-006", "assertions": [{"kind": "status", "expected": 403}]}]
        )
        assert designs == [] and "never invented" in notes[0]["message"]
        designs, notes = _normalize(
            [{"test_case_id": "TC-006", "assertions": [{"kind": "status", "expected": 404}]}]
        )
        assert notes == [] and designs[0]["assertions"] == [{"kind": "status", "expected": 404}]

    def test_a_positive_case_cannot_expect_a_client_error(self):
        designs, notes = _normalize(
            [
                {
                    "test_case_id": "TC-005",
                    "assertions": [{"kind": "status_class", "expected": "4xx"}],
                }
            ]
        )
        assert designs == [] and "positive" in notes[0]["message"]

    def test_only_2xx_and_4xx_classes_exist(self):
        designs, _ = _normalize(
            [
                {
                    "test_case_id": "TC-006",
                    "assertions": [{"kind": "status_class", "expected": "5xx"}],
                }
            ]
        )
        assert designs == []

    def test_a_swagger_path_must_be_documented(self):
        designs, notes = _normalize(
            [
                {
                    "test_case_id": "TC-002",
                    "assertions": [
                        {
                            "kind": "field_absent",
                            "json_path": "data.items[].internalNote",
                            "path_source": "swagger",
                        }
                    ],
                }
            ]
        )
        assert designs == [] and "not documented" in notes[0]["message"]

    def test_a_path_from_the_requirement_needs_evidence(self):
        entry = {"kind": "field_present", "json_path": "body.link", "path_source": "requirement"}
        designs, notes = _normalize([{"test_case_id": "TC-005", "assertions": [entry]}])
        assert designs == [] and "quoted evidence" in notes[0]["message"]
        entry["evidence"] = "The response returns body.link."
        designs, notes = _normalize([{"test_case_id": "TC-005", "assertions": [entry]}])
        assert notes == [] and designs[0]["assertions"][0]["json_path"] == "body.link"

    def test_anywhere_is_limited_to_presence_and_absence_of_one_key(self):
        cases = [
            (
                {
                    "kind": "field_equals",
                    "json_path": "link",
                    "scope": "anywhere",
                    "expected": 1,
                    "evidence": "x",
                },
                "only allowed",
            ),
            (
                {
                    "kind": "field_absent",
                    "json_path": "a.link",
                    "scope": "anywhere",
                    "evidence": "x",
                },
                "single key",
            ),
            (
                {"kind": "field_absent", "json_path": "link", "scope": "anywhere"},
                "anywhere in the response",
            ),
        ]
        for entry, message in cases:
            designs, notes = _normalize([{"test_case_id": "TC-005", "assertions": [entry]}])
            assert designs == [], entry
            assert message in notes[0]["message"]
        designs, notes = _normalize(
            [
                {
                    "test_case_id": "TC-005",
                    "assertions": [
                        {
                            "kind": "field_absent",
                            "json_path": "link",
                            "scope": "anywhere",
                            "evidence": "The link is no longer returned.",
                        }
                    ],
                }
            ]
        )
        assert notes == [] and designs[0]["assertions"][0]["scope"] == "anywhere"

    def test_array_length_rules(self):
        for entry, message in [
            (
                {
                    "kind": "array_length",
                    "json_path": "result.items[].id",
                    "path_source": "swagger",
                    "max": 1,
                },
                "not on its items",
            ),
            (
                {"kind": "array_length", "json_path": "result.items", "path_source": "swagger"},
                "needs expected, min or max",
            ),
            (
                {
                    "kind": "array_length",
                    "json_path": "result.items",
                    "path_source": "swagger",
                    "min": 5,
                    "max": 1,
                },
                "min is greater",
            ),
        ]:
            designs, notes = _normalize([{"test_case_id": "TC-001", "assertions": [entry]}])
            assert designs == [] and message in notes[0]["message"]

    def test_builder_conversion(self):
        assert to_builder_assertion({"kind": "status", "expected": 404}) == {
            "type": "status_code",
            "expected": 404,
        }
        assert to_builder_assertion({"kind": "status_class", "expected": "4xx"}) == {
            "type": "status_class",
            "expected": "4xx",
        }
        assert to_builder_assertion(
            {"kind": "field_absent", "json_path": "k", "scope": "anywhere"}
        ) == {"type": "field_absent", "json_path": "k", "scope": "anywhere"}


# ── تعارض با قرارداد، معلق‌بودن و محدودیت ───────────────────────────────────


class TestConflictsAndLimitations:
    def test_a_value_beyond_the_documented_maximum_makes_a_positive_case_pending(self):
        designs, notes = _normalize([_design_fixture()[3]])
        assert designs[0]["pending"] is True
        assert "maximum 50" in designs[0]["pending_reason"]
        assert "no requirement defines" in designs[0]["pending_reason"]
        assert notes[0]["type"] == "pending_expectation"

    def test_a_deliberate_violation_with_a_rejection_assertion_is_allowed(self):
        step1 = _step1()
        step1["test_cases"][3]["type"] = "negative"
        designs, _ = _normalize(
            [
                {
                    "test_case_id": "TC-004",
                    "request_values": [_value("limit", 51, "schema_constraint")],
                    "assertions": [{"kind": "status", "expected": 400}],
                }
            ],
            step1=step1,
        )
        assert designs[0]["pending"] is False

    def test_largest_page_reports_the_completeness_limitation(self):
        designs, notes = _normalize([_design_fixture()[2]])
        design = designs[0]
        assert design["pending"] is False
        assert [v["value"] for v in design["request_values"]] == [50, 0]
        assert design["assertions"] == [
            {
                "kind": "array_length",
                "json_path": "result.items",
                "scope": "path",
                "path_source": "swagger",
                "max": 50,
            }
        ]
        assert "Unverified expectation: Every existing item is returned." in notes[0]["message"]

    def test_normalization_is_idempotent(self):
        first, _ = _normalize(_design_fixture())
        second, notes = _normalize(copy.deepcopy(first))
        assert second == first
        assert all(note["type"] == "pending_expectation" for note in notes)

    @pytest.mark.parametrize(
        "raw",
        [
            "x",
            [1],
            [{"test_case_id": "TC-999"}],
            [{"test_case_id": "TC-001"}, {"test_case_id": "TC-001"}],
        ],
    )
    def test_malformed_input_never_raises(self, raw):
        designs, notes = _normalize(raw)
        assert designs == []
        assert isinstance(notes, list)


# ── قدم سوم ──────────────────────────────────────────────────────────────────


def _llm_payload(test_design=None) -> dict:
    payload = _step3()
    if test_design is not None:
        payload["test_design"] = test_design
    return payload


def _run_step3(payload: dict) -> dict:
    with patch(_LLM) as client:
        llm = MagicMock()
        llm.chat.return_value = json.dumps(payload)
        client.return_value = llm
        return Step3ScenarioAnalysisGenerator().generate(
            _step1(), compact_step2_result(_step2()), user_id="test"
        )


class TestStep3:
    def test_without_test_design_the_result_keeps_its_shape(self):
        result = _run_step3(_llm_payload())
        assert set(result) == {
            "scenarios",
            "execution_order",
            "data_dependencies",
            "clarifications",
        }
        assert set(build_step3_result([], [], [], [])) == set(result)

    def test_valid_designs_are_kept_and_problems_become_clarifications(self):
        design = _design_fixture()
        design[0]["request_values"].append(_value("page", 1, "user_or_requirement", evidence="x"))
        result = _run_step3(_llm_payload(design))
        ids = [entry["test_case_id"] for entry in result["test_design"]]
        assert ids == ["TC-001", "TC-002", "TC-003", "TC-004", "TC-005", "TC-006"]
        messages = [c["message"] for c in result["clarifications"]]
        assert any("'page'" in message for message in messages)
        assert any(
            c["type"] == "pending_expectation" and c["test_case_id"] == "TC-004"
            for c in result["clarifications"]
        )


# ── قدم چهارم و builder ─────────────────────────────────────────────────────


class TestStep4:
    def test_values_and_assertions_reach_the_request(self):
        _, _, _, step4, _ = _pipeline(_design_fixture())
        items = _items(step4["collection"])
        url = items["TC-001"]["request"]["url"]
        assert url["query"] == [{"key": "limit", "value": "20"}, {"key": "offset", "value": "0"}]
        script = "\n".join(_script(items["TC-001"]))
        assert 'jsonData["result"]["items"]' in script
        assert "to.be.at.most(20)" in script
        assert 'pm.expect(jsonData["result"]["limit"]).to.eql(20);' in script

    def test_a_pending_case_gets_no_request(self):
        _, _, _, step4, review = _pipeline(_design_fixture())
        assert "TC-004" not in _items(step4["collection"])
        row = next(r for r in step4["unresolved"] if r["test_case_id"] == "TC-004")
        assert row["pending"] is True and "Pending clarification" in row["reason"]
        kinds = {
            (i["kind"], i["test_case_id"]) for i in review["review"]["unresolved_clarifications"]
        }
        assert (PENDING_TEST_CASE, "TC-004") in kinds

    def test_a_non_existent_id_is_sent_and_rejection_uses_the_4xx_class(self):
        _, _, _, step4, _ = _pipeline(_design_fixture())
        item = _items(step4["collection"])["TC-006"]
        assert item["request"]["url"]["variable"] == [{"key": "id", "value": str(INT64_MAX)}]
        assert "pm.expect(pm.response.code).to.be.within(400, 499);" in "\n".join(_script(item))
        assert not any(
            "cannot be derived" in line for line in [item["request"].get("description", "")]
        )

    def test_a_runtime_variable_is_declared_empty_and_reported(self):
        _, _, _, step4, review = _pipeline(_design_fixture())
        collection = step4["collection"]
        assert {"key": "existingItemId", "value": ""} in collection["variable"]
        item = _items(collection)["TC-005"]
        assert "{{existingItemId}}" in item["request"]["url"]["raw"]
        kinds = [i["kind"] for i in review["review"]["unresolved_clarifications"]]
        assert UNFILLED_RUNTIME_VARIABLE in kinds

    def test_no_pre_request_script_is_added(self):
        _, _, _, step4, _ = _pipeline(_design_fixture())
        for item in _items(step4["collection"]).values():
            assert all(event["listen"] != "prerequest" for event in item.get("event") or [])

    def test_a_dependency_wins_over_a_design_value(self):
        step1, step2 = _step1(), _step2()
        dependency = {
            "variable_name": "itemId",
            "source": {
                "test_case_id": "TC-001",
                "location": "response.body",
                "path": "$.result.items[0].id",
            },
            "targets": [{"test_case_id": "TC-005", "location": "path", "parameter": "id"}],
            "confidence": "high",
            "reason": "The listed id is read back.",
        }
        design = [
            {
                "test_case_id": "TC-005",
                "request_values": [
                    _value("id", 7, "user_or_requirement", location="path", evidence="x")
                ],
            }
        ]
        step3 = _step3(design, dependencies=[dependency])
        step4 = Step4PostmanGenerator().generate(step1, step2, step3)
        item = _items(step4["collection"])["TC-005"]
        assert "{{itemId}}" in item["request"]["url"]["raw"]

    def test_edited_step3_designs_are_validated_again(self):
        design = [{"test_case_id": "TC-006", "assertions": [{"kind": "status", "expected": 418}]}]
        _, _, _, step4, review = _pipeline(design)
        assert any("418" in warning for warning in step4["warnings"])
        item = _items(step4["collection"])["TC-006"]
        assert not any("418" in line for line in _script(item))


# ── اجرای واقعیِ assertionها با Node ─────────────────────────────────────────

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(_NODE is None, reason="Node.js is not installed")

_HARNESS = r"""
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const results = [];
function typeOf(v) { return v === null ? "null" : Array.isArray(v) ? "array" : typeof v; }
function expect(value) {
  const fail = (t) => { throw new Error(t); };
  const assertions = {
    within(lo, hi) { if (!(value >= lo && value <= hi)) fail(`${value} not within ${lo}..${hi}`); },
    a(t) { if (typeOf(value) !== t) fail(`expected ${t}, got ${typeOf(value)}`); },
    an(t) { if (typeOf(value) !== t) fail(`expected ${t}, got ${typeOf(value)}`); },
    lengthOf(n) { if (!value || value.length !== n) fail(`length ${value && value.length} != ${n}`); },
    eql(x) { if (JSON.stringify(value) !== JSON.stringify(x)) fail("not equal"); },
    property(k) { if (value === null || typeof value !== "object" || !(k in value)) fail(`missing ${k}`); },
  };
  const negated = {
    property(k) { if (value !== null && typeof value === "object" && k in value) fail(`has ${k}`); },
  };
  const be = {
    within: assertions.within, a: assertions.a, an: assertions.an,
    at: {
      most(n) { if (!(value <= n)) fail(`${value} > ${n}`); },
      least(n) { if (!(value >= n)) fail(`${value} < ${n}`); },
    },
    get true() { if (value !== true) fail(`${value} is not true`); return true; },
    get false() { if (value !== false) fail(`${value} is not false`); return true; },
    get null() { if (value !== null) fail(`${value} is not null`); return true; },
  };
  return {
    to: {
      be, eql: assertions.eql,
      have: { lengthOf: assertions.lengthOf, property: assertions.property },
      not: {
        have: { property: negated.property },
        be: { get undefined() { if (value === undefined) fail("undefined"); return true; } },
      },
    },
  };
}
const pm = {
  test(name, fn) {
    try { fn(); results.push({ name, passed: true }); }
    catch (e) { results.push({ name, passed: false, error: String(e.message) }); }
  },
  expect,
  response: {
    code: input.code,
    json() { return JSON.parse(JSON.stringify(input.body)); },
    to: { have: { status(c) { if (input.code !== c) throw new Error(`status ${input.code}`); } } },
  },
  globals: { set() {}, get() {} },
  collectionVariables: { set() {}, get() {} },
};
new Function("pm", input.script)(pm);
process.stdout.write(JSON.stringify(results));
"""


def _execute(lines: list[str], *, code=200, body=None) -> list[dict]:
    completed = subprocess.run(
        [_NODE, "-e", _HARNESS],
        input=json.dumps({"script": "\n".join(lines), "code": code, "body": body}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)


def _body(items, limit=20):
    return {"result": {"items": items, "limit": limit, "offset": 0}}


@needs_node
class TestGeneratedAssertionsRun:
    def _scripts(self):
        _, _, _, step4, _ = _pipeline(_design_fixture())
        return {case_id: _script(item) for case_id, item in _items(step4["collection"]).items()}

    def test_list_size_and_echoed_values(self):
        script = self._scripts()["TC-001"]
        ok = _execute(script, body=_body([{"id": 1, "link": "a"}] * 3))
        assert all(r["passed"] for r in ok), ok
        too_many = _execute(script, body=_body([{"id": 1}] * 21))
        assert not all(r["passed"] for r in too_many)
        wrong_echo = _execute(script, body=_body([], limit=10))
        assert not all(r["passed"] for r in wrong_echo)

    def test_absence_and_presence_on_every_item(self):
        script = self._scripts()["TC-002"]
        assert all(
            r["passed"]
            for r in _execute(script, body=_body([{"id": 1, "link": "x"}, {"id": 2, "link": "y"}]))
        )
        # [Status code is 200, every item has no internalNote, every item has link]
        leaked = _execute(
            script,
            body=_body([{"id": 1, "link": "x"}, {"id": 2, "link": "y", "internalNote": "secret"}]),
        )
        assert [r["passed"] for r in leaked] == [True, False, True]
        missing = _execute(script, body=_body([{"id": 1}]))
        assert [r["passed"] for r in missing] == [True, True, False]

    def test_the_4xx_class_rejects_server_errors(self):
        script = self._scripts()["TC-006"]
        assert all(r["passed"] for r in _execute(script, code=404, body={}))
        assert not all(r["passed"] for r in _execute(script, code=500, body={}))
        assert not all(r["passed"] for r in _execute(script, code=200, body={}))

    def test_anywhere_absence_searches_nested_objects(self):
        design = [
            {
                "test_case_id": "TC-005",
                "assertions": [
                    {
                        "kind": "field_absent",
                        "json_path": "internalNote",
                        "scope": "anywhere",
                        "evidence": "Internal notes are never returned.",
                    },
                    {
                        "kind": "field_type",
                        "json_path": "result.id",
                        "path_source": "user",
                        "value_type": "integer",
                        "evidence": "The id is an integer.",
                    },
                ],
            }
        ]
        _, _, _, step4, _ = _pipeline(design)
        script = _script(_items(step4["collection"])["TC-005"])
        clean = _execute(script, body={"result": {"id": 3, "nested": [{"a": 1}]}})
        assert all(r["passed"] for r in clean), clean
        leaked = _execute(script, body={"result": {"id": 3.5, "nested": [{"internalNote": 1}]}})
        assert [r["passed"] for r in leaked] == [True, False, False]


# ── قدم پنجم ────────────────────────────────────────────────────────────────


class TestStep5:
    def test_checks_without_response_assertions_are_reported(self):
        step1 = _step1()
        step1["test_cases"][4]["checks"] = [
            {
                "kind": "field_absent",
                "fields": ["internalNote"],
                "description": "The note is not returned.",
            }
        ]
        _, _, _, _, review = _pipeline(
            [{"test_case_id": "TC-006", "assertions": [{"kind": "status", "expected": 404}]}],
            step1=step1,
        )
        assert any(
            w.startswith(CHECKS_NOT_ASSERTED) and "'TC-005'" in w
            for w in review["review"]["warnings"]
        )

    def test_possibly_equivalent_requests_are_only_reported(self):
        design = [
            {"test_case_id": case_id, "assertions": [{"kind": "status", "expected": 200}]}
            for case_id in ("TC-001", "TC-002")
        ]
        _, _, _, step4, review = _pipeline(design)
        assert {"TC-001", "TC-002"} <= set(_items(step4["collection"]))
        assert any(w.startswith(POSSIBLY_EQUIVALENT) for w in review["review"]["warnings"])

    def test_untraced_cases_are_reported_when_requirements_exist(self):
        step1 = _step1(requirements=[{"id": "R1", "text": "x", "kind": "behavior"}])
        step1["test_cases"][0]["requirement_refs"] = ["R1"]
        _, _, _, _, review = _pipeline(None, step1=step1)
        untraced = [w for w in review["review"]["warnings"] if w.startswith(NO_REQUIREMENT_REF)]
        assert len(untraced) == len(_CASES) - 1

    def test_results_without_the_new_fields_get_no_new_findings(self):
        _, _, _, _, review = _pipeline(None)
        text = json.dumps(review["review"])
        for code in (
            CHECKS_NOT_ASSERTED,
            NO_REQUIREMENT_REF,
            PENDING_TEST_CASE,
            POSSIBLY_EQUIVALENT,
            UNFILLED_RUNTIME_VARIABLE,
        ):
            assert code not in text
