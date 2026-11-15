"""
پرامپت‌های قدم اول — حذفِ متنِ تکراری بدونِ از دست دادنِ هیچ قاعده یا قرارداد

* مقدمه‌ی خطاب به توسعه‌دهنده («You are working on an existing Python-based …») از
  هر سه پرامپت حذف شده است؛
* بخش‌های HUMAN-IN-THE-LOOP و SCOPE OF THIS STEPِ پرامپتِ اصلی در GOAL و
  IMPORTANT ادغام شده‌اند و همه‌ی ممنوعیت‌هایشان مانده است؛
* همه‌ی بخش‌های قاعده، قراردادِ خروجی و قالبِ پیامِ کاربر دست‌نخورده‌اند؛
* ساختِ ورودیِ Direct / Plan / Expansion همان است.
"""

from __future__ import annotations

import json
import re

import pytest

from src.agents.test_case_generator import task_analysis as ta

PROMPTS = {
    "direct": (ta._PROMPT_PATH, ta.split_prompt),
    "plan": (ta._PLAN_PROMPT_PATH, ta.split_plan_prompt),
    "details": (ta._DETAILS_PROMPT_PATH, ta.split_details_prompt),
}


def _system(name: str) -> str:
    path, split = PROMPTS[name]
    return split(path.read_text(encoding="utf-8"))[0]


def _section(text: str, header: str) -> str:
    """متنِ یک بخش از سرعنوانش تا سرعنوانِ بعدی."""
    parts = re.split(r"\n=+\n([A-Z][^\n]*)\n=+\n", "\n" + text)
    for index in range(1, len(parts), 2):
        if parts[index].strip() == header:
            return parts[index + 1]
    raise AssertionError(f"section {header!r} not found")


class TestRemovedBoilerplate:
    @pytest.mark.parametrize("name", list(PROMPTS))
    def test_the_developer_preamble_is_gone(self, name):
        system = _system(name)
        assert "Python-based Test Automation Agent" not in system
        assert "We are now implementing STEP 1" not in system
        assert system.startswith("You analyze a software development task")

    def test_merged_sections_are_gone_from_direct(self):
        system = _system("direct")
        assert "HUMAN-IN-THE-LOOP" not in system
        assert "SCOPE OF THIS STEP" not in system


class TestNoRuleWasLost:
    DIRECT_SECTIONS = [
        "CURRENT STEP: TASK ANALYSIS & TEST CASE GENERATION",
        "GOAL",
        "TEST CASE GENERATION",
        "IMPORTANT RULE: DO NOT INVENT BUSINESS RULES",
        "DEVELOPED SERVICES",
        "TEST CASE STRUCTURE",
        "AUTHENTICATION INTENT",
        "TEST CASE QUALITY",
        "DEPENDENCIES",
        "REQUESTED VARIABLES",
        "REQUIREMENTS AND TRACEABILITY",
        "NECESSITY GATE (APPLY TO EVERY TEST CASE)",
        "SCOPE",
        "RESPONSE CHECKS AND INPUT INTENT",
        "DUPLICATES AND DROPPED TEST CASES",
        "ENVIRONMENT DECISIONS",
        "OUTPUT FORMAT",
        "IMPORTED ISSUES",
        "POSTMAN COLLECTION SUMMARY",
    ]

    @pytest.mark.parametrize("header", DIRECT_SECTIONS)
    def test_every_rule_section_is_still_in_direct(self, header):
        assert _section(_system("direct"), header).strip()

    @pytest.mark.parametrize(
        "rule",
        [
            "generate a Postman Collection",
            "Postman variables, scripts or {{...}}",
            'only record the variables the task explicitly requests in the\n  "variables" array',
            "generate Authorization headers, {{token}} or credentials of any kind",
            "execute APIs or tests, or call application services",
            "analyze, fetch, generate or invent Swagger/OpenAPI specifications",
            "Record only the auth_intent value",
            "documented authentication contract",
            "It is not final",
            "approve, edit, remove, add or regenerate test cases",
        ],
    )
    def test_every_prohibition_and_review_rule_is_kept(self, rule):
        assert rule in _system("direct")

    def test_plan_and_details_keep_their_stage_contracts(self):
        plan, details = _system("plan"), _system("details")
        assert "CURRENT STEP: TEST CASE PLAN (COMPACT)" in plan
        assert "Do NOT produce preconditions, steps, or expected_result" in plan
        assert "This step must NOT generate a Postman Collection." in plan
        assert "CURRENT STEP: TEST CASE DETAILS" in details
        assert "Return exactly ONE detail object for EVERY planned test case." in details


class TestOutputContractIsUnchanged:
    def test_direct_output_skeleton_keys(self):
        block = _section(_system("direct"), "OUTPUT FORMAT")
        skeleton = block[block.index("{") : block.index("When the task explicitly")].strip()
        data = json.loads(skeleton.replace('"...",\n        "..."', '"..."'))
        assert list(data) == [
            "task_summary",
            "requirements",
            "test_cases",
            "dropped_test_cases",
            "clarifications",
            "variables",
            "environment_decisions",
        ]
        assert list(data["test_cases"][0]) == [
            "id",
            "title",
            "type",
            "priority",
            "preconditions",
            "steps",
            "expected_result",
            "related_service",
            "auth_intent",
            "requirement_refs",
            "checks",
            "input_intent",
        ]


class TestInputConstruction:
    def test_direct_and_plan_get_the_same_user_message_parts(self):
        _, direct = ta.build_prompt(ta._PROMPT_PATH, "TASK-TEXT", "SERVICES", "SUMMARY-TEXT")
        _, plan = ta.build_plan_prompt(
            ta._PLAN_PROMPT_PATH, "TASK-TEXT", "SERVICES", "SUMMARY-TEXT"
        )
        for message in (direct, plan):
            assert "TASK DESCRIPTION:\nTASK-TEXT" in message
            assert "DEVELOPED SERVICES:\nSERVICES" in message
            assert message.count("SUMMARY-TEXT") == 1

    def test_without_postman_the_summary_slot_disappears(self):
        _, direct = ta.build_prompt(ta._PROMPT_PATH, "TASK-TEXT", "", "")
        assert "{{postman_summary}}" not in direct
        assert "\n\n\n" not in direct

    def test_expansion_gets_the_task_and_only_its_batch(self):
        planned = [{"id": "TC-001", "title": "t", "type": "positive", "priority": "high"}]
        system, user = ta.build_details_prompt(ta._DETAILS_PROMPT_PATH, "TASK-TEXT", planned)
        assert "TASK DESCRIPTION:\nTASK-TEXT" in user
        assert json.dumps(planned, ensure_ascii=False, indent=2) in user
        assert "SUMMARY" not in user and "DEVELOPED SERVICES" not in user
        assert "CURRENT STEP: TEST CASE DETAILS" in system
