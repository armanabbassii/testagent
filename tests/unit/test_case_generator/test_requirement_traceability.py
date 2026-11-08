"""
ردیابیِ نیازمندی، دروازه‌ی ضرورت و پوشش در قدم اول — آفلاین

قواعدی که این‌جا قطعی سنجیده می‌شوند:
  * فیلدهای جدید اختیاری‌اند؛ خروجیِ قدیمی همان شکلِ قبلی را دارد؛
  * نیازمندیِ «implementation» (مثلاً ثبتِ endpoint در یک سیستمِ دسترسی) برای
    ردیابی نگه داشته می‌شود ولی به تنهایی تست‌کیس نمی‌سازد؛
  * کنارگذاشتنِ یک تست‌کیس هیچ پوششِ نیازمندی‌ای را بی‌صدا از بین نمی‌برد؛
  * حذفِ تکراری فقط همان حذفِ متنِ کاملاً یکسانِ قبلی است — نه مقایسه‌ی چند فیلد.
"""

from __future__ import annotations

import copy

import pytest

from src.agents.test_case_generator.task_analysis import (
    TaskAnalysisError,
    _assemble_result,
    validate_analysis,
    validate_details,
    validate_plan,
)


def _case(case_id: str, title: str, case_type: str = "positive", **extra) -> dict:
    return {
        "id": case_id,
        "title": title,
        "type": case_type,
        "priority": "high",
        "preconditions": [],
        "steps": [f"Send the request for: {title}"],
        "expected_result": f"{title} — as specified.",
        "related_service": {"method": "GET", "path": "/catalog/items"},
        "auth_intent": "inherit",
        **extra,
    }


def _requirements() -> list[dict]:
    return [
        {
            "id": "R1",
            "text": "The list returns the item link.",
            "kind": "behavior",
            "source": "description",
            "evidence": "Return the link.",
        },
        {
            "id": "R2",
            "text": "The list omits the internal note.",
            "kind": "behavior",
            "source": "comment",
            "source_reference": "Comment 2",
            "evidence": "Remove the note.",
        },
        {
            "id": "R3",
            "text": "Register the endpoint in the access-control list.",
            "kind": "implementation",
            "source": "comment",
            "evidence": "Add it to the ACL.",
        },
    ]


def _payload(cases: list[dict], **extra) -> dict:
    return {
        "task_summary": "Catalog list",
        "identified_requirements": [],
        "test_cases": cases,
        "clarifications": [],
        "variables": [],
        **extra,
    }


class TestBackwardCompatibility:
    def test_an_old_payload_keeps_its_exact_shape(self):
        result = validate_analysis(_payload([_case("TC-001", "List items")]))
        assert set(result) == {
            "task_summary",
            "identified_requirements",
            "test_cases",
            "clarifications",
            "variables",
        }
        assert set(result["test_cases"][0]) == {
            "id",
            "title",
            "type",
            "priority",
            "preconditions",
            "steps",
            "expected_result",
            "related_service",
            "auth_intent",
        }

    def test_exact_text_duplicates_are_still_the_only_automatic_removal(self):
        same = _case("TC-001", "List items")
        overlapping = _case("TC-003", "List items", expected_result="Different outcome.")
        result = validate_analysis(
            _payload([same, {**copy.deepcopy(same), "id": "TC-002"}, overlapping])
        )
        assert [case["id"] for case in result["test_cases"]] == ["TC-001", "TC-003"]


class TestTraceability:
    def test_requirements_refs_checks_and_intents_are_kept(self):
        case = _case(
            "TC-001",
            "List response omits the internal note",
            requirement_refs=["R2"],
            checks=[
                {
                    "kind": "field_absent",
                    "fields": ["internalNote"],
                    "description": "The note is not returned.",
                    "requirement_ref": "R2",
                }
            ],
            input_intent=[
                {
                    "name": "limit",
                    "intent": "the default page size",
                    "value": 20,
                    "evidence": "Default is 20.",
                }
            ],
        )
        result = validate_analysis(_payload([case], requirements=_requirements()))
        kept = result["test_cases"][0]
        assert kept["requirement_refs"] == ["R2"]
        assert kept["checks"][0]["fields"] == ["internalNote"]
        assert kept["input_intent"][0]["value"] == 20
        assert [item["kind"] for item in result["requirements"]] == [
            "behavior",
            "behavior",
            "implementation",
        ]
        # identified_requirements is derived when the model only sent the structured list
        assert result["identified_requirements"][2].startswith("Register the endpoint")
        # every case is traced, so no warning key is added
        assert "traceability_warnings" not in result

    def test_an_implementation_requirement_alone_does_not_justify_a_test_case(self):
        case = _case(
            "TC-001", "Caller without permission is rejected", "negative", requirement_refs=["R3"]
        )
        with pytest.raises(TaskAnalysisError, match="implementation requirement"):
            validate_analysis(_payload([case], requirements=_requirements()))

    def test_the_implementation_requirement_stays_traceable(self):
        case = _case("TC-001", "List items", requirement_refs=["R1", "R3"])
        result = validate_analysis(_payload([case], requirements=_requirements()))
        assert any(
            item["id"] == "R3" and item["kind"] == "implementation"
            for item in result["requirements"]
        )

    @pytest.mark.parametrize(
        ("extra", "message"),
        [
            ({"requirement_refs": ["R9"]}, "unknown requirement id 'R9'"),
            ({"checks": [{"kind": "magic", "description": "x"}]}, "'kind' must be one of"),
            ({"checks": [{"kind": "field_absent", "description": ""}]}, "'description' is missing"),
            ({"input_intent": [{"name": "limit"}]}, "'intent' is missing"),
        ],
    )
    def test_invalid_traceability_fields_are_reported_to_the_model(self, extra, message):
        with pytest.raises(TaskAnalysisError, match=message):
            validate_analysis(
                _payload([_case("TC-001", "List items", **extra)], requirements=_requirements())
            )

    def test_refs_without_requirements_are_rejected(self):
        with pytest.raises(TaskAnalysisError, match="'requirements' is missing"):
            validate_analysis(_payload([_case("TC-001", "List items", requirement_refs=["R1"])]))

    def test_an_untraced_case_is_a_warning_not_an_error(self):
        result = validate_analysis(
            _payload(
                [_case("TC-001", "List items", requirement_refs=["R1"]), _case("TC-002", "Other")],
                requirements=_requirements(),
            )
        )
        assert result["traceability_warnings"] == [
            "Test case 'TC-002' is not traced to any requirement."
        ]


class TestDroppedTestCases:
    def test_dropped_cases_are_reported_with_reason_and_coverage(self):
        dropped = [
            {
                "title": "List with explicit filters",
                "reason": "Same behaviour as TC-001.",
                "covered_by": ["TC-001"],
                "requirement_refs": ["R1"],
            },
            {"title": "Unauthenticated list", "reason": "Out of scope.", "covered_by": []},
        ]
        result = validate_analysis(
            _payload(
                [_case("TC-001", "List items", requirement_refs=["R1"])],
                requirements=_requirements(),
                dropped_test_cases=dropped,
            )
        )
        assert result["dropped_test_cases"] == [
            {
                "title": "List with explicit filters",
                "reason": "Same behaviour as TC-001.",
                "covered_by": ["TC-001"],
                "requirement_refs": ["R1"],
            },
            {"title": "Unauthenticated list", "reason": "Out of scope.", "covered_by": []},
        ]

    def test_dropping_a_case_may_not_lose_requirement_coverage(self):
        dropped = [
            {
                "title": "Note is omitted",
                "reason": "Looks similar.",
                "covered_by": ["TC-001"],
                "requirement_refs": ["R2"],
            }
        ]
        with pytest.raises(TaskAnalysisError, match="lose coverage of requirement\\(s\\) R2"):
            validate_analysis(
                _payload(
                    [_case("TC-001", "List items", requirement_refs=["R1"])],
                    requirements=_requirements(),
                    dropped_test_cases=dropped,
                )
            )

    def test_covered_by_must_name_a_kept_case(self):
        dropped = [{"title": "x", "reason": "y", "covered_by": ["TC-404"]}]
        with pytest.raises(TaskAnalysisError, match="unknown test case 'TC-404'"):
            validate_analysis(_payload([_case("TC-001", "List items")], dropped_test_cases=dropped))


class TestStagedGeneration:
    def test_plan_fields_survive_the_details_stage(self):
        planned = {
            "id": "TC-001",
            "title": "List response omits the internal note",
            "type": "positive",
            "priority": "high",
            "related_service": {"method": "GET", "path": "/catalog/items"},
            "auth_intent": "inherit",
            "requirement_refs": ["R2"],
            "checks": [
                {"kind": "field_absent", "fields": ["internalNote"], "description": "Not returned."}
            ],
        }
        plan = validate_plan(
            _payload(
                [planned],
                requirements=_requirements(),
                dropped_test_cases=[
                    {
                        "title": "Duplicate",
                        "reason": "Same as TC-001.",
                        "covered_by": ["TC-001"],
                        "requirement_refs": ["R2"],
                    }
                ],
            )
        )
        details = validate_details(
            {
                "test_cases": [
                    {
                        "id": "TC-001",
                        "preconditions": [],
                        "steps": ["Send it"],
                        "expected_result": "No note.",
                    }
                ]
            },
            plan["test_cases"],
        )
        result = _assemble_result(plan, details)
        assert result["test_cases"][0]["requirement_refs"] == ["R2"]
        assert result["test_cases"][0]["checks"][0]["kind"] == "field_absent"
        assert result["dropped_test_cases"][0]["covered_by"] == ["TC-001"]
        assert len(result["requirements"]) == 3

    def test_plan_validation_rejects_lost_coverage_before_expansion(self):
        planned = {
            "id": "TC-001",
            "title": "List",
            "type": "positive",
            "priority": "high",
            "related_service": {"method": "GET", "path": "/x"},
            "auth_intent": "inherit",
            "requirement_refs": ["R1"],
        }
        with pytest.raises(TaskAnalysisError, match="lose coverage"):
            validate_plan(
                _payload(
                    [planned],
                    requirements=_requirements(),
                    dropped_test_cases=[
                        {"title": "x", "reason": "y", "covered_by": [], "requirement_refs": ["R2"]}
                    ],
                )
            )

    def test_an_old_plan_assembles_to_the_old_shape(self):
        planned = {
            "id": "TC-001",
            "title": "List",
            "type": "positive",
            "priority": "high",
            "related_service": {"method": "GET", "path": "/x"},
            "auth_intent": "inherit",
        }
        plan = validate_plan(_payload([planned]))
        details = validate_details(
            {
                "test_cases": [
                    {
                        "id": "TC-001",
                        "preconditions": [],
                        "steps": ["Send it"],
                        "expected_result": "Listed.",
                    }
                ]
            },
            plan["test_cases"],
        )
        result = _assemble_result(plan, details)
        assert "requirements" not in result and "dropped_test_cases" not in result
        assert "requirement_refs" not in result["test_cases"][0]
