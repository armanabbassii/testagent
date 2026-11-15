"""
identified_requirements — از requirements[].text ساخته می‌شود، نه از خروجیِ مدل

* پرامپت‌های Direct و برنامه دیگر این فهرست را از مدل نمی‌خواهند؛
* نتیجه‌ی نهایی (مسیرِ یک‌مرحله‌ای و مرحله‌ای) همیشه آن را دارد: همان متن‌ها به
  همان ترتیبِ requirements؛
* اگر مدل از روی عادت فهرستِ جداگانه‌ای بفرستد، نادیده گرفته می‌شود؛
* خروجیِ قالبِ قدیمی (بدونِ requirements) فهرستِ خودِ مدل را نگه می‌دارد؛
* قدم ۳ و batch executor همان ساختارِ قبلی (فهرستِ رشته) را می‌گیرند.

هیچ فراخوانیِ واقعیِ LLM انجام نمی‌شود.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from src.agents.test_case_generator import scenario_analysis
from src.agents.test_case_generator import task_analysis as ta
from src.agents.test_case_generator.task_analysis import (
    TaskAnalysisGenerator,
    validate_analysis,
)
from src.batch_testing.executor import _project_step1
from src.llm_client import FINISH_REASON_LENGTH, LLMResponse

_PATCH = "src.agents.test_case_generator.task_analysis.LLMClient"
TEXTS = ["A voucher can be created with a percentage.", "Percent 0 is rejected."]


def _requirements() -> list[dict]:
    return [
        {
            "id": f"R{i}",
            "text": text,
            "kind": "behavior",
            "source": "description",
            "source_reference": "",
            "evidence": text,
        }
        for i, text in enumerate(TEXTS, start=1)
    ]


def _case(number: int, ref: str) -> dict:
    return {
        "id": f"TC-{number:03d}",
        "title": f"Scenario {number}",
        "type": "positive",
        "priority": "high",
        "related_service": {"method": "POST", "path": "/admin/voucher/percent"},
        "auth_intent": "inherit",
        "requirement_refs": [ref],
        "checks": [],
        "input_intent": [],
    }


def _direct_output(**extra) -> dict:
    return {
        "task_summary": "Add vouchers.",
        "requirements": _requirements(),
        "test_cases": [
            dict(_case(1, "R1"), preconditions=[], steps=["Submit"], expected_result="Created"),
            dict(_case(2, "R2"), preconditions=[], steps=["Submit 0"], expected_result="Rejected"),
        ],
        "dropped_test_cases": [],
        "clarifications": [],
        "variables": [],
        "environment_decisions": [],
        **extra,
    }


def _plan_output() -> dict:
    data = _direct_output()
    data["test_cases"] = [_case(1, "R1"), _case(2, "R2")]
    return data


def _details(user_message: str) -> dict:
    ids = [i for i in ("TC-001", "TC-002") if f'"id": "{i}"' in user_message]
    return {
        "test_cases": [
            {"id": i, "preconditions": [], "steps": [f"Run {i}"], "expected_result": "OK"}
            for i in ids
        ]
    }


class TestThePromptsNoLongerAskForIt:
    def test_direct_and_plan_skeletons_omit_it_and_say_why(self):
        for path, split in (
            (ta._PROMPT_PATH, ta.split_prompt),
            (ta._PLAN_PROMPT_PATH, ta.split_plan_prompt),
        ):
            system, _ = split(path.read_text(encoding="utf-8"))
            assert '"identified_requirements": [' not in system
            assert 'Keep "identified_requirements"' not in system
            assert 'Do not return a separate "identified_requirements" list' in system


class TestDerivation:
    def test_the_direct_path_derives_it(self):
        with patch(_PATCH) as cls:
            cls.return_value.chat.return_value = json.dumps(_direct_output())
            result = TaskAnalysisGenerator().generate("Add vouchers", "u")
        assert result["identified_requirements"] == TEXTS

    def test_the_staged_path_derives_it(self):
        def respond(**kwargs):
            system = kwargs["system_prompt"]
            if "TEST CASE PLAN (COMPACT)" in system:
                return json.dumps(_plan_output())
            if "TEST CASE DETAILS" in system:
                return json.dumps(_details(kwargs["user_message"]))
            return LLMResponse('{"task_summary": "Add', finish_reason=FINISH_REASON_LENGTH)

        with patch(_PATCH) as cls:
            cls.return_value.chat.side_effect = respond
            result = TaskAnalysisGenerator().generate("Add vouchers", "u")

        assert [c["id"] for c in result["test_cases"]] == ["TC-001", "TC-002"]
        assert result["identified_requirements"] == TEXTS
        assert [c["steps"] for c in result["test_cases"]] == [["Run TC-001"], ["Run TC-002"]]

    def test_a_list_still_sent_by_the_model_is_replaced_by_the_derived_one(self):
        result = validate_analysis(_direct_output(identified_requirements=["stale", "other"]))
        assert result["identified_requirements"] == TEXTS

    def test_an_output_without_structured_requirements_keeps_the_model_list(self):
        data = _direct_output(identified_requirements=["Legacy requirement"])
        del data["requirements"]
        for case in data["test_cases"]:
            del case["requirement_refs"]
        data.pop("dropped_test_cases")
        result = validate_analysis(data)
        assert result["identified_requirements"] == ["Legacy requirement"]

    def test_nothing_at_all_gives_an_empty_list(self):
        data = _direct_output()
        del data["requirements"]
        for case in data["test_cases"]:
            del case["requirement_refs"]
        data.pop("dropped_test_cases")
        assert validate_analysis(data)["identified_requirements"] == []


class TestDownstreamStructureIsUnchanged:
    def _result(self) -> dict:
        with patch(_PATCH) as cls:
            cls.return_value.chat.return_value = json.dumps(_direct_output())
            return TaskAnalysisGenerator().generate("Add vouchers", "u")

    def test_the_field_is_a_list_of_strings_in_the_final_object(self):
        result = self._result()
        assert isinstance(result["identified_requirements"], list)
        assert all(isinstance(item, str) for item in result["identified_requirements"])
        # بقیه‌ی کلیدهای اصلی هم همان‌اند.
        for key in ("task_summary", "test_cases", "clarifications", "variables", "requirements"):
            assert key in result

    def test_step3_receives_it_in_its_prompt(self):
        result = self._result()
        template = "STEP1:\n{{step1_result}}\nSTEP2:\n{{step2_result}}"
        message = scenario_analysis.render_prompt(template, result, {"mappings": []})
        rendered = json.loads(message.split("STEP1:\n", 1)[1].split("\nSTEP2:", 1)[0])
        assert rendered["identified_requirements"] == TEXTS

    def test_the_batch_executor_projection_counts_it(self):
        assert _project_step1(self._result())["identified_requirements"] == len(TEXTS)
