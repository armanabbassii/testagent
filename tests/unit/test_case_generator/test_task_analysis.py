"""تست‌های تحلیل تسک و تولید تست‌کیس ساختاریافته (قدم اول جریان HITL)."""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage

from src.agents.test_case_generator.auth_intent import (
    AUTH_INTENT_AUTHENTICATED,
    AUTH_INTENT_INHERIT,
    AUTH_INTENT_INVALID,
    AUTH_INTENT_UNAUTHENTICATED,
)
from src.agents.test_case_generator.task_analysis import (
    REQUIRES_MAPPING,
    TaskAnalysisError,
    TaskAnalysisGenerator,
    build_details_prompt,
    build_plan_prompt,
    build_prompt,
    parse_analysis,
    render_prompt,
    split_details_prompt,
    split_plan_prompt,
    split_prompt,
    validate_analysis,
    validate_details,
    validate_plan,
)
from src.config import LLM_MAX_OUTPUT_TOKENS
from src.llm_client import FINISH_REASON_LENGTH, LLMClient, LLMResponse
from src.token_usage import TokenUsage, collect_usage

PROMPT_PATH = (
    Path(__file__).parents[3]
    / "src"
    / "agents"
    / "test_case_generator"
    / "prompts"
    / "step1_task_analysis.md"
)

PLAN_PROMPT_PATH = PROMPT_PATH.with_name("step1_task_plan.md")
DETAILS_PROMPT_PATH = PROMPT_PATH.with_name("step1_test_case_details.md")


def _case(**overrides) -> dict:
    case = {
        "id": "TC-001",
        "title": "Create a valid percentage voucher",
        "type": "positive",
        "priority": "high",
        "preconditions": ["User has permission to create a voucher"],
        "steps": ["Enter valid voucher information", "Submit the request"],
        "expected_result": "The voucher is created successfully.",
        "related_service": {"method": "POST", "path": "/admin/voucher/percent"},
    }
    case.update(overrides)
    return case


def _payload(**overrides) -> dict:
    payload = {
        "task_summary": "Add percentage voucher creation.",
        "identified_requirements": ["A voucher can be created with a percentage value."],
        "test_cases": [_case()],
        "clarifications": [],
    }
    payload.update(overrides)
    return payload


def _planned_case(index: int, **overrides) -> dict:
    """یک تست‌کیس در «برنامه» — بدونِ جزئیاتِ اجرایی."""
    case = {
        "id": f"TC-{index:03d}",
        "title": f"Scenario {index}",
        "type": "positive" if index % 2 else "negative",
        "priority": "high" if index <= 10 else "medium",
        "related_service": {
            "method": "POST",
            "path": f"/admin/voucher/{index}",
        },
    }
    case.update(overrides)
    return case


def _plan_payload(count: int, **overrides) -> dict:
    payload = {
        "task_summary": "Add percentage voucher creation.",
        "identified_requirements": ["A voucher can be created with a percentage value."],
        "test_cases": [_planned_case(index) for index in range(1, count + 1)],
        "clarifications": ["Requires clarification: maximum percentage is not stated."],
    }
    payload.update(overrides)
    return payload


def _details_payload(case_ids: list[str], **overrides) -> dict:
    payload = {
        "test_cases": [
            {
                "id": case_id,
                "preconditions": [f"Precondition for {case_id}"],
                "steps": [f"Step one for {case_id}", f"Step two for {case_id}"],
                "expected_result": f"Expected result for {case_id}",
            }
            for case_id in case_ids
        ]
    }
    payload.update(overrides)
    return payload


def _ids_in(message: str) -> list[str]:
    """شناسه‌های تست‌کیس را از متنِ پیامِ کاربر بیرون می‌کشد."""
    return re.findall(r'"id":\s*"(TC-\d+)"', message)


# ── prompt ───────────────────────────────────────────────────────────────────

class TestSplitPrompt:
    def test_shipped_prompt_splits(self):
        text = PROMPT_PATH.read_text(encoding="utf-8")
        system_prompt, user_template = split_prompt(text)

        assert "DO NOT INVENT BUSINESS RULES" in system_prompt
        assert "{{task_description}}" in user_template
        assert "{{developed_services}}" in user_template
        # بخش دستورها نباید داخل پیام کاربر تکرار شود
        assert "DO NOT INVENT BUSINESS RULES" not in user_template

    def test_missing_input_section_raises(self):
        with pytest.raises(TaskAnalysisError, match="no INPUT section"):
            split_prompt("Just some instructions without an input section.")

    def test_missing_placeholder_raises(self):
        text = (
            "Rules here.\n"
            "==================================================\n"
            "INPUT\n"
            "==================================================\n"
            "TASK DESCRIPTION:\n{{task_description}}\n"
        )
        with pytest.raises(TaskAnalysisError, match="developed_services"):
            split_prompt(text)

    def test_empty_system_section_raises(self):
        text = (
            "==================================================\n"
            "INPUT\n"
            "==================================================\n"
            "{{task_description}} {{developed_services}}\n"
        )
        with pytest.raises(TaskAnalysisError, match="empty system prompt"):
            split_prompt(text)


class TestRenderPrompt:
    def test_placeholders_are_replaced(self):
        template = "TASK: {{task_description}}\nSERVICES: {{developed_services}}"
        rendered = render_prompt(template, "Add voucher creation", "POST /admin/voucher")

        assert "Add voucher creation" in rendered
        assert "POST /admin/voucher" in rendered
        assert "{{" not in rendered

    def test_missing_services_render_as_none_provided(self):
        rendered = render_prompt(
            "SERVICES: {{developed_services}}", "Add voucher creation", ""
        )
        assert "(none provided)" in rendered

    def test_user_text_is_not_reinterpreted_as_placeholder(self):
        rendered = render_prompt(
            "TASK: {{task_description}}\nSERVICES: {{developed_services}}",
            "Handle a literal {{developed_services}} token",
            "",
        )
        assert "Handle a literal {{developed_services}} token" in rendered
        assert "(none provided)" in rendered

    def test_values_are_trimmed(self):
        rendered = render_prompt(
            "TASK: {{task_description}}|", "  spaced task  ", "  "
        )
        assert "TASK: spaced task|" in rendered


class TestBuildPrompt:
    def test_returns_system_prompt_and_rendered_user_message(self):
        system_prompt, user_message = build_prompt(
            PROMPT_PATH, "Add percentage voucher creation", "POST /admin/voucher/percent"
        )

        assert "test cases" in system_prompt
        assert "Add percentage voucher creation" in user_message
        assert "POST /admin/voucher/percent" in user_message


class TestStagedPrompts:
    def test_the_plan_prompt_splits_and_excludes_details_fields(self):
        text = PLAN_PROMPT_PATH.read_text(encoding="utf-8")
        system_prompt, user_template = split_plan_prompt(text)

        assert "TEST CASE PLAN" in system_prompt
        assert "{{task_description}}" in user_template
        assert "{{developed_services}}" in user_template
        # مرحله‌ی برنامه نباید جزئیاتِ اجرایی بخواهد
        assert '"steps"' not in system_prompt
        assert '"expected_result"' not in system_prompt

    def test_the_details_prompt_splits_with_its_own_placeholders(self):
        text = DETAILS_PROMPT_PATH.read_text(encoding="utf-8")
        system_prompt, user_template = split_details_prompt(text)

        assert "TEST CASE DETAILS" in system_prompt
        assert "{{task_description}}" in user_template
        assert "{{test_case_plan}}" in user_template
        # این مرحله سرویس‌ها را نمی‌گیرد؛ فقط برنامه را گسترش می‌دهد
        assert "{{developed_services}}" not in user_template

    def test_details_split_requires_the_plan_placeholder(self):
        text = (
            "Rules here.\n"
            "==================================================\n"
            "INPUT\n"
            "==================================================\n"
            "TASK DESCRIPTION:\n{{task_description}}\n"
        )
        with pytest.raises(TaskAnalysisError, match="test_case_plan"):
            split_details_prompt(text)

    def test_build_plan_prompt_renders_task_and_services(self):
        system_prompt, user_message = build_plan_prompt(
            PLAN_PROMPT_PATH, "Add a voucher", "POST /admin/voucher"
        )

        assert "Add a voucher" in user_message
        assert "POST /admin/voucher" in user_message
        assert "Add a voucher" not in system_prompt
        assert "{{" not in user_message

    def test_build_details_prompt_renders_the_planned_cases_as_json(self):
        planned = [_planned_case(1), _planned_case(2)]
        system_prompt, user_message = build_details_prompt(
            DETAILS_PROMPT_PATH, "Add a voucher", planned
        )

        assert "Add a voucher" in user_message
        assert "TC-001" in user_message and "TC-002" in user_message
        assert '"title": "Scenario 1"' in user_message
        assert "{{" not in user_message
        assert "Add a voucher" not in system_prompt


# ── اعتبارسنجی ───────────────────────────────────────────────────────────────

class TestValidateAnalysis:
    def test_valid_payload_passes_through(self):
        result = validate_analysis(_payload())

        assert result["task_summary"] == "Add percentage voucher creation."
        assert len(result["test_cases"]) == 1
        assert result["test_cases"][0]["id"] == "TC-001"
        assert result["clarifications"] == []

    def test_type_and_priority_are_lowercased(self):
        result = validate_analysis(_payload(test_cases=[_case(type="Positive", priority="HIGH")]))
        assert result["test_cases"][0]["type"] == "positive"
        assert result["test_cases"][0]["priority"] == "high"

    def test_method_is_uppercased(self):
        case = _case(related_service={"method": "post", "path": "/admin/voucher"})
        result = validate_analysis(_payload(test_cases=[case]))
        assert result["test_cases"][0]["related_service"]["method"] == "POST"

    def test_preconditions_default_to_empty_list(self):
        case = _case()
        del case["preconditions"]
        result = validate_analysis(_payload(test_cases=[case]))
        assert result["test_cases"][0]["preconditions"] == []

    def test_missing_clarifications_defaults_to_empty(self):
        payload = _payload()
        del payload["clarifications"]
        assert validate_analysis(payload)["clarifications"] == []

    def test_explicit_null_clarifications_defaults_to_empty(self):
        assert validate_analysis(_payload(clarifications=None))["clarifications"] == []

    def test_all_four_types_are_accepted(self):
        cases = [
            _case(id=f"TC-00{i}", title=f"Case {i}", type=kind)
            for i, kind in enumerate(
                ["positive", "negative", "boundary", "state_transition"], start=1
            )
        ]
        result = validate_analysis(_payload(test_cases=cases))
        assert [c["type"] for c in result["test_cases"]] == [
            "positive",
            "negative",
            "boundary",
            "state_transition",
        ]

    def test_requirements_accepts_a_bare_string(self):
        result = validate_analysis(_payload(identified_requirements="A single requirement"))
        assert result["identified_requirements"] == ["A single requirement"]

    def test_steps_accept_a_bare_string(self):
        result = validate_analysis(_payload(test_cases=[_case(steps="Submit the request")]))
        assert result["test_cases"][0]["steps"] == ["Submit the request"]

    def test_empty_string_entries_are_dropped_from_lists(self):
        case = _case(preconditions=["", "  ", "Real precondition"])
        result = validate_analysis(_payload(test_cases=[case]))
        assert result["test_cases"][0]["preconditions"] == ["Real precondition"]

    def test_missing_task_summary_raises(self):
        with pytest.raises(TaskAnalysisError, match="'task_summary' is missing"):
            validate_analysis(_payload(task_summary=""))

    def test_empty_test_cases_raises(self):
        with pytest.raises(TaskAnalysisError, match="'test_cases' must be a non-empty array"):
            validate_analysis(_payload(test_cases=[]))

    def test_invalid_type_raises(self):
        with pytest.raises(TaskAnalysisError, match="'type' must be one of"):
            validate_analysis(_payload(test_cases=[_case(type="exploratory")]))

    def test_invalid_priority_raises(self):
        with pytest.raises(TaskAnalysisError, match="'priority' must be one of"):
            validate_analysis(_payload(test_cases=[_case(priority="urgent")]))

    def test_missing_steps_raises(self):
        case = _case(steps=[])
        with pytest.raises(TaskAnalysisError, match=r"steps: must contain at least one entry"):
            validate_analysis(_payload(test_cases=[case]))

    def test_missing_expected_result_raises(self):
        with pytest.raises(TaskAnalysisError, match="'expected_result' is missing"):
            validate_analysis(_payload(test_cases=[_case(expected_result="   ")]))

    def test_missing_id_raises(self):
        with pytest.raises(TaskAnalysisError, match="'id' is missing"):
            validate_analysis(_payload(test_cases=[_case(id="")]))

    def test_duplicate_ids_raise(self):
        cases = [_case(id="TC-001"), _case(id="TC-001", title="Another case")]
        with pytest.raises(TaskAnalysisError, match="duplicate test case id 'TC-001'"):
            validate_analysis(_payload(test_cases=cases))

    def test_non_string_step_raises(self):
        with pytest.raises(TaskAnalysisError, match=r"steps\[1\]: must be a string"):
            validate_analysis(_payload(test_cases=[_case(steps=["Submit the request", 42])]))

    def test_non_object_test_case_raises(self):
        with pytest.raises(TaskAnalysisError, match="expected an object"):
            validate_analysis(_payload(test_cases=["TC-001"]))

    def test_all_errors_are_reported_together(self):
        payload = _payload(
            task_summary="",
            test_cases=[_case(type="exploratory", priority="urgent")],
        )
        with pytest.raises(TaskAnalysisError) as exc:
            validate_analysis(payload)

        message = str(exc.value)
        assert "'task_summary' is missing" in message
        assert "'type' must be one of" in message
        assert "'priority' must be one of" in message


class TestRelatedService:
    def test_missing_related_service_becomes_requires_mapping(self):
        case = _case()
        del case["related_service"]
        result = validate_analysis(_payload(test_cases=[case]))

        service = result["test_cases"][0]["related_service"]
        assert service["method"] == REQUIRES_MAPPING
        assert service["path"] == REQUIRES_MAPPING
        assert service["service"] == ""

    def test_sentinel_string_becomes_requires_mapping(self):
        case = _case(related_service="Unknown - requires API mapping")
        result = validate_analysis(_payload(test_cases=[case]))
        assert result["test_cases"][0]["related_service"]["method"] == REQUIRES_MAPPING

    def test_null_becomes_requires_mapping(self):
        result = validate_analysis(_payload(test_cases=[_case(related_service=None)]))
        assert result["test_cases"][0]["related_service"]["path"] == REQUIRES_MAPPING

    def test_empty_object_becomes_requires_mapping(self):
        result = validate_analysis(_payload(test_cases=[_case(related_service={})]))
        assert result["test_cases"][0]["related_service"]["method"] == REQUIRES_MAPPING

    def test_service_name_is_kept(self):
        case = _case(
            related_service={
                "method": "POST",
                "path": "/admin/voucher/percent",
                "service": "Admin API",
            }
        )
        result = validate_analysis(_payload(test_cases=[case]))
        assert result["test_cases"][0]["related_service"]["service"] == "Admin API"

    def test_unknown_method_marker_is_normalized(self):
        case = _case(related_service={"method": "unknown", "path": "/admin/voucher"})
        result = validate_analysis(_payload(test_cases=[case]))
        assert result["test_cases"][0]["related_service"]["method"] == REQUIRES_MAPPING

    def test_bare_path_string_raises(self):
        case = _case(related_service="/admin/voucher/percent")
        with pytest.raises(TaskAnalysisError, match="bare string"):
            validate_analysis(_payload(test_cases=[case]))

    def test_object_without_path_raises(self):
        case = _case(related_service={"method": "POST"})
        with pytest.raises(TaskAnalysisError, match="'path' is missing"):
            validate_analysis(_payload(test_cases=[case]))

    def test_object_without_method_raises(self):
        case = _case(related_service={"path": "/admin/voucher"})
        with pytest.raises(TaskAnalysisError, match="'method' is missing"):
            validate_analysis(_payload(test_cases=[case]))

    def test_non_string_service_raises(self):
        case = _case(
            related_service={"method": "POST", "path": "/v", "service": {"name": "Admin"}}
        )
        with pytest.raises(TaskAnalysisError, match="'service' must be a string"):
            validate_analysis(_payload(test_cases=[case]))


class TestDuplicates:
    def test_identical_cases_are_collapsed(self):
        cases = [
            _case(id="TC-001"),
            _case(id="TC-002"),  # همان رفتار، شناسه‌ی متفاوت
        ]
        result = validate_analysis(_payload(test_cases=cases))

        assert [c["id"] for c in result["test_cases"]] == ["TC-001"]

    def test_materially_different_cases_are_kept(self):
        cases = [
            _case(id="TC-001"),
            _case(id="TC-002", expected_result="The voucher is rejected."),
        ]
        result = validate_analysis(_payload(test_cases=cases))

        assert len(result["test_cases"]) == 2

    def test_same_title_with_different_type_is_kept(self):
        cases = [
            _case(id="TC-001", type="positive"),
            _case(id="TC-002", type="boundary"),
        ]
        result = validate_analysis(_payload(test_cases=cases))

        assert len(result["test_cases"]) == 2


class TestAuthIntent:
    def test_missing_auth_intent_defaults_to_inherit(self):
        result = validate_analysis(_payload(test_cases=[_case()]))

        assert result["test_cases"][0]["auth_intent"] == AUTH_INTENT_INHERIT

    @pytest.mark.parametrize(
        "intent",
        [
            AUTH_INTENT_INHERIT,
            AUTH_INTENT_AUTHENTICATED,
            AUTH_INTENT_UNAUTHENTICATED,
            AUTH_INTENT_INVALID,
        ],
    )
    def test_canonical_auth_intents_are_preserved(self, intent):
        result = validate_analysis(_payload(test_cases=[_case(auth_intent=intent)]))

        assert result["test_cases"][0]["auth_intent"] == intent

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("valid_token", AUTH_INTENT_AUTHENTICATED),
            ("authorized", AUTH_INTENT_AUTHENTICATED),
            ("no_auth", AUTH_INTENT_UNAUTHENTICATED),
            ("missing credentials", AUTH_INTENT_UNAUTHENTICATED),
            ("expired_token", AUTH_INTENT_INVALID),
            ("invalid token", AUTH_INTENT_INVALID),
            ("default", AUTH_INTENT_INHERIT),
            ("not_required", AUTH_INTENT_INHERIT),
        ],
    )
    def test_aliases_are_normalized(self, raw, expected):
        result = validate_analysis(_payload(test_cases=[_case(auth_intent=raw)]))

        assert result["test_cases"][0]["auth_intent"] == expected

    def test_an_unknown_auth_intent_is_rejected(self):
        with pytest.raises(TaskAnalysisError, match="auth_intent must be one of"):
            validate_analysis(_payload(test_cases=[_case(auth_intent="sometimes")]))

    def test_a_non_string_auth_intent_is_rejected(self):
        with pytest.raises(TaskAnalysisError, match="auth_intent must be a string"):
            validate_analysis(_payload(test_cases=[_case(auth_intent=1)]))

    def test_the_plan_carries_auth_intent_to_the_final_result(self):
        planned = _planned_case(1, auth_intent=AUTH_INTENT_INVALID)
        plan = validate_plan(_plan_payload(1, test_cases=[planned]))

        assert plan["test_cases"][0]["auth_intent"] == AUTH_INTENT_INVALID

    def test_cases_for_the_same_endpoint_with_different_intents_are_kept(self):
        cases = [
            _case(id="TC-001", auth_intent=AUTH_INTENT_AUTHENTICATED),
            _case(id="TC-002", auth_intent=AUTH_INTENT_UNAUTHENTICATED),
        ]

        result = validate_analysis(_payload(test_cases=cases))

        assert [case["id"] for case in result["test_cases"]] == ["TC-001", "TC-002"]


class TestParseAnalysis:
    def test_parses_plain_json(self):
        result = parse_analysis(json.dumps(_payload()))
        assert result["test_cases"][0]["id"] == "TC-001"

    def test_parses_fenced_json(self):
        raw = f"```json\n{json.dumps(_payload())}\n```"
        assert parse_analysis(raw)["task_summary"] == "Add percentage voucher creation."

    def test_empty_response_raises(self):
        with pytest.raises(TaskAnalysisError, match="empty response"):
            parse_analysis("")

    def test_non_json_response_raises(self):
        with pytest.raises(TaskAnalysisError, match="did not return JSON"):
            parse_analysis("I cannot do that.")

    def test_contract_violation_raises(self):
        with pytest.raises(TaskAnalysisError, match="does not match the required test case format"):
            parse_analysis(json.dumps(_payload(test_cases=[])))


class TestValidatePlan:
    def test_a_complete_plan_passes(self):
        plan = validate_plan(_plan_payload(3))

        assert plan["task_summary"] == "Add percentage voucher creation."
        assert [case["id"] for case in plan["test_cases"]] == [
            "TC-001",
            "TC-002",
            "TC-003",
        ]
        assert plan["clarifications"]

    def test_details_fields_are_not_required(self):
        plan = validate_plan(_plan_payload(1))
        assert set(plan["test_cases"][0]) == {
            "id",
            "title",
            "type",
            "priority",
            "related_service",
            "auth_intent",
        }

    def test_ids_must_be_unique(self):
        payload = _plan_payload(
            2, test_cases=[_planned_case(1), _planned_case(1, title="Another")]
        )
        with pytest.raises(TaskAnalysisError, match="duplicate test case id 'TC-001'"):
            validate_plan(payload)

    def test_an_empty_plan_is_rejected(self):
        with pytest.raises(TaskAnalysisError, match="'test_cases' must be a non-empty array"):
            validate_plan(_plan_payload(0))

    def test_invalid_type_is_rejected(self):
        with pytest.raises(TaskAnalysisError, match="'type' must be one of"):
            validate_plan(_plan_payload(1, test_cases=[_planned_case(1, type="smoke")]))

    def test_missing_related_service_becomes_requires_mapping(self):
        case = _planned_case(1)
        del case["related_service"]
        plan = validate_plan(_plan_payload(1, test_cases=[case]))

        service = plan["test_cases"][0]["related_service"]
        assert service["method"] == REQUIRES_MAPPING
        assert service["path"] == REQUIRES_MAPPING

    def test_error_names_the_plan_contract(self):
        with pytest.raises(TaskAnalysisError, match="test case plan format"):
            validate_plan(_plan_payload(1, task_summary=""))


class TestValidateDetails:
    def _planned(self, count: int):
        return validate_plan(_plan_payload(count))["test_cases"]

    def test_details_are_returned_in_plan_order(self):
        planned = self._planned(3)
        details = validate_details(
            _details_payload(["TC-003", "TC-001", "TC-002"]), planned
        )

        assert [detail["id"] for detail in details] == ["TC-001", "TC-002", "TC-003"]
        assert details[0]["steps"] == [
            "Step one for TC-001",
            "Step two for TC-001",
        ]

    def test_a_missing_planned_case_is_rejected(self):
        planned = self._planned(3)
        with pytest.raises(TaskAnalysisError, match="missing details"):
            validate_details(_details_payload(["TC-001", "TC-003"]), planned)

    def test_an_unknown_case_is_rejected(self):
        planned = self._planned(2)
        with pytest.raises(TaskAnalysisError, match="not in the planned test cases"):
            validate_details(_details_payload(["TC-001", "TC-002", "TC-999"]), planned)

    def test_a_duplicate_case_is_rejected(self):
        planned = self._planned(2)
        with pytest.raises(TaskAnalysisError, match="duplicate detail"):
            validate_details(_details_payload(["TC-001", "TC-001", "TC-002"]), planned)

    def test_a_missing_id_is_rejected(self):
        planned = self._planned(1)
        payload = _details_payload(["TC-001"])
        payload["test_cases"][0]["id"] = ""
        with pytest.raises(TaskAnalysisError, match="'id' is missing"):
            validate_details(payload, planned)

    def test_empty_steps_are_rejected(self):
        planned = self._planned(1)
        payload = _details_payload(["TC-001"])
        payload["test_cases"][0]["steps"] = []
        with pytest.raises(TaskAnalysisError, match=r"steps: must contain at least one"):
            validate_details(payload, planned)

    def test_missing_expected_result_is_rejected(self):
        planned = self._planned(1)
        payload = _details_payload(["TC-001"])
        payload["test_cases"][0]["expected_result"] = "  "
        with pytest.raises(TaskAnalysisError, match="'expected_result' is missing"):
            validate_details(payload, planned)

    def test_preconditions_are_optional(self):
        planned = self._planned(1)
        payload = _details_payload(["TC-001"])
        del payload["test_cases"][0]["preconditions"]

        details = validate_details(payload, planned)
        assert details[0]["preconditions"] == []


# ── تولیدکننده ───────────────────────────────────────────────────────────────

class TestTaskAnalysisGenerator:
    def _generate(self, payload: dict, **kwargs):
        """generator را با LLMClient ماک‌شده اجرا می‌کند و (نتیجه، llm ماک) می‌دهد."""
        with patch(
            "src.agents.test_case_generator.task_analysis.LLMClient"
        ) as mock_cls:
            llm = MagicMock()
            llm.chat.return_value = json.dumps(payload)
            mock_cls.return_value = llm
            result = TaskAnalysisGenerator(**kwargs).generate(
                task_description="Add percentage voucher creation.",
                user_id="user-1",
                developed_services="POST /admin/voucher/percent",
            )
        return result, mock_cls, llm

    def test_returns_validated_result(self):
        result, _, _ = self._generate(_payload())

        assert result["task_summary"] == "Add percentage voucher creation."
        assert result["test_cases"][0]["type"] == "positive"

    def test_llm_client_is_constructed_with_user_and_agent(self):
        _, mock_cls, _ = self._generate(_payload())

        mock_cls.assert_called_once_with(
            user_id="user-1", agent_name=TaskAnalysisGenerator.name
        )

    def test_system_prompt_is_sent_separately_from_input(self):
        _, _, llm = self._generate(_payload())
        kwargs = llm.chat.call_args.kwargs

        # دستورها در system prompt می‌مانند و ورودی کاربر در پیام کاربر
        assert "DO NOT INVENT BUSINESS RULES" in kwargs["system_prompt"]
        assert "Add percentage voucher creation." in kwargs["user_message"]
        assert "POST /admin/voucher/percent" in kwargs["user_message"]
        assert "Add percentage voucher creation." not in kwargs["system_prompt"]

    def test_temperature_and_max_tokens_are_configurable(self):
        _, _, llm = self._generate(_payload(), temperature=0.3, max_tokens=2048)
        kwargs = llm.chat.call_args.kwargs

        assert kwargs["temperature"] == 0.3
        assert kwargs["max_tokens"] == 2048

    def test_empty_task_description_raises_value_error(self):
        with pytest.raises(ValueError, match="task_description is empty"):
            TaskAnalysisGenerator().generate("   ", user_id="user-1")

    def test_invalid_llm_output_raises(self):
        with patch(
            "src.agents.test_case_generator.task_analysis.LLMClient"
        ) as mock_cls:
            mock_cls.return_value.chat.return_value = "not json at all"
            with pytest.raises(TaskAnalysisError, match="did not return JSON"):
                TaskAnalysisGenerator().generate("Add something", user_id="user-1")

    def test_missing_services_are_marked_none_provided(self):
        with patch(
            "src.agents.test_case_generator.task_analysis.LLMClient"
        ) as mock_cls:
            llm = MagicMock()
            llm.chat.return_value = json.dumps(_payload())
            mock_cls.return_value = llm

            TaskAnalysisGenerator().generate("Add something", user_id="user-1")

        assert "(none provided)" in llm.chat.call_args.kwargs["user_message"]

    def test_custom_prompt_path_is_used(self, tmp_path):
        prompt_file = tmp_path / "custom.md"
        prompt_file.write_text(
            "Custom rules.\n"
            "====\nINPUT\n====\n"
            "TASK:\n{{task_description}}\n"
            "SERVICES:\n{{developed_services}}\n",
            encoding="utf-8",
        )

        with patch(
            "src.agents.test_case_generator.task_analysis.LLMClient"
        ) as mock_cls:
            llm = MagicMock()
            llm.chat.return_value = json.dumps(_payload())
            mock_cls.return_value = llm

            TaskAnalysisGenerator(prompt_path=prompt_file).generate(
                "Add something", user_id="user-1"
            )

        assert llm.chat.call_args.kwargs["system_prompt"] == "Custom rules."

    def test_no_postman_or_swagger_keys_leak_into_output(self):
        result, _, _ = self._generate(_payload())

        assert set(result) == {
            "task_summary",
            "identified_requirements",
            "test_cases",
            "clarifications",
            "variables",
        }
        # تسکی که متغیری نخواسته، فهرستِ خالی می‌گیرد — نه اسکریپت یا {{...}}
        assert result["variables"] == []
        text = json.dumps(result)
        assert "postman" not in text.lower()
        assert "swagger" not in text.lower()


class TestStagedGeneration:
    """مسیرِ جایگزین وقتی پاسخِ یک‌مرحله‌ای به سقفِ توکن می‌خورد."""

    _PATCH = "src.agents.test_case_generator.task_analysis.LLMClient"

    def _truncated(self, text: str = '{"task_summary": "Add percent') -> LLMResponse:
        return LLMResponse(
            text,
            finish_reason=FINISH_REASON_LENGTH,
            prompt_tokens=1000,
            completion_tokens=LLM_MAX_OUTPUT_TOKENS,
            max_tokens=LLM_MAX_OUTPUT_TOKENS,
        )

    def _run(self, respond, *, batch_size=None, task_description="Add vouchers"):
        with patch(self._PATCH) as mock_cls:
            mock_cls.return_value.chat.side_effect = respond
            result = TaskAnalysisGenerator(batch_size=batch_size).generate(
                task_description, "user-1"
            )
        return result, mock_cls.return_value.chat

    def test_a_truncated_response_is_expanded_in_ordered_batches(self):
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return self._truncated()
            if len(calls) == 2:
                return json.dumps(_plan_payload(20))
            return json.dumps(_details_payload(_ids_in(kwargs["user_message"])))

        result, chat = self._run(respond)

        # ۱ پاسخِ بریده + ۱ برنامه + ۳ دسته‌ی گسترش (۸، ۸، ۴)
        assert chat.call_count == 5
        assert [len(_ids_in(call["user_message"])) for call in calls[2:]] == [8, 8, 4]
        assert [case["id"] for case in result["test_cases"]] == [
            f"TC-{index:03d}" for index in range(1, 21)
        ]
        assert all(
            call["max_tokens"] == LLM_MAX_OUTPUT_TOKENS
            and call["temperature"] == 0.1
            for call in calls
        )

    def test_plan_fields_and_details_are_merged_without_loss(self):
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return self._truncated()
            if len(calls) == 2:
                return json.dumps(_plan_payload(3))
            return json.dumps(_details_payload(_ids_in(kwargs["user_message"])))

        result, _ = self._run(respond)

        assert result["task_summary"] == "Add percentage voucher creation."
        assert result["identified_requirements"] == [
            "A voucher can be created with a percentage value."
        ]
        assert result["clarifications"] == [
            "Requires clarification: maximum percentage is not stated."
        ]

        first = result["test_cases"][0]
        # از برنامه
        assert first["id"] == "TC-001"
        assert first["title"] == "Scenario 1"
        assert first["type"] == "positive"
        assert first["priority"] == "high"
        assert first["related_service"] == {
            "method": "POST",
            "path": "/admin/voucher/1",
            "service": "",
        }
        # قصدِ احراز هویت هم از برنامه می‌آید و در ادغام گم نمی‌شود.
        assert first["auth_intent"] == AUTH_INTENT_INHERIT
        # از مرحله‌ی گسترش
        assert first["preconditions"] == ["Precondition for TC-001"]
        assert first["steps"] == [
            "Step one for TC-001",
            "Step two for TC-001",
        ]
        assert first["expected_result"] == "Expected result for TC-001"

    def test_the_configured_batch_size_is_used(self):
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return self._truncated()
            if len(calls) == 2:
                return json.dumps(_plan_payload(7))
            return json.dumps(_details_payload(_ids_in(kwargs["user_message"])))

        result, _ = self._run(respond, batch_size=3)

        assert [len(_ids_in(call["user_message"])) for call in calls[2:]] == [3, 3, 1]
        assert [case["id"] for case in result["test_cases"]] == [
            f"TC-{index:03d}" for index in range(1, 8)
        ]

    def test_batch_size_must_be_positive(self):
        with pytest.raises(ValueError, match="batch_size must be at least 1"):
            TaskAnalysisGenerator(batch_size=0)

    def test_a_truncated_expansion_batch_reports_its_range(self):
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return self._truncated()
            if len(calls) == 2:
                return json.dumps(_plan_payload(20))
            if len(calls) == 3:
                return json.dumps(_details_payload(_ids_in(kwargs["user_message"])))
            return self._truncated('{"test_cases": [{"id": "TC-009"')

        with pytest.raises(TaskAnalysisError) as excinfo:
            self._run(respond, batch_size=8)

        message = str(excinfo.value)
        assert "output token limit" in message
        # بریدگی نباید به‌عنوان «JSON نامعتبر» گزارش شود
        assert "invalid JSON" not in message
        assert "planned test cases 9-16 of 20" in message
        assert "batch 2 of 3" in message

    def test_a_truncated_plan_is_reported_explicitly(self):
        def respond(**kwargs):
            return self._truncated()

        with pytest.raises(TaskAnalysisError) as excinfo:
            self._run(respond)

        message = str(excinfo.value)
        assert "truncated because it reached the output token limit" in message
        assert "A staged generation was attempted" in message
        assert "compact test case plan also reached the output token limit" in message
        assert "No partial test case list was used" in message

    def test_a_truncated_response_is_never_accepted_even_if_it_is_valid_json(self):
        leaked = json.dumps(
            _payload(test_cases=[_case(id="LEAK-1", title="Leaked case")])
        )
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return self._truncated(leaked)
            if len(calls) == 2:
                return json.dumps(_plan_payload(2))
            return json.dumps(_details_payload(_ids_in(kwargs["user_message"])))

        result, _ = self._run(respond)

        assert [case["id"] for case in result["test_cases"]] == ["TC-001", "TC-002"]

    def test_a_malformed_plan_with_a_normal_finish_reason_is_a_json_error(self):
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return self._truncated()
            return "I cannot produce the plan."

        with pytest.raises(TaskAnalysisError, match="did not return JSON"):
            self._run(respond)

    def test_a_complete_response_still_uses_a_single_call(self):
        with patch(self._PATCH) as mock_cls:
            mock_cls.return_value.chat.return_value = json.dumps(_payload())
            TaskAnalysisGenerator().generate("Add something", "user-1")

        assert mock_cls.return_value.chat.call_count == 1


def _generate_with(respond, *, batch_size=None):
    """اجرای ژنراتور با LLMِ ماک‌شده؛ پاسخ‌ها از ``respond`` می‌آیند."""
    with patch(
        "src.agents.test_case_generator.task_analysis.LLMClient"
    ) as mock_cls:
        mock_cls.return_value.chat.side_effect = respond
        result = TaskAnalysisGenerator(batch_size=batch_size).generate(
            "Add vouchers", "user-1"
        )
    return result, mock_cls.return_value.chat


class TestEmptySingleCallFallsBackToStagedGeneration:
    """پاسخِ کاملِ یک‌فراخوانی بدونِ هیچ تست‌کیس نباید پذیرفته یا ترمیم شود.

    این همان حالتی است که خطای «'test_cases' must be a non-empty array» را در
    مسیرِ عادی می‌داد: JSON معتبر بود ولی تست‌کیسی نداشت. حالا همان تولیدِ
    مرحله‌ای اجرا می‌شود و اگر آن هم نتواند لیستِ کامل بسازد، خطا بالا می‌رود.
    """

    def test_an_empty_single_call_is_recovered_by_staged_generation(self):
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return json.dumps(_payload(test_cases=[]))
            if len(calls) == 2:
                return json.dumps(_plan_payload(20))
            return json.dumps(_details_payload(_ids_in(kwargs["user_message"])))

        result, chat = _generate_with(respond)

        # ۱ پاسخِ خالی + ۱ برنامه + ۳ دسته‌ی گسترش (۸، ۸، ۴)
        assert chat.call_count == 5
        assert [len(_ids_in(call["user_message"])) for call in calls[2:]] == [8, 8, 4]
        assert [case["id"] for case in result["test_cases"]] == [
            f"TC-{index:03d}" for index in range(1, 21)
        ]
        first = result["test_cases"][0]
        assert first["title"] == "Scenario 1"
        assert first["steps"] == ["Step one for TC-001", "Step two for TC-001"]
        assert first["expected_result"] == "Expected result for TC-001"

    def test_a_missing_test_cases_key_is_also_treated_as_empty(self):
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return json.dumps(
                    {
                        "task_summary": "Add percentage voucher creation.",
                        "identified_requirements": [],
                        "clarifications": [],
                    }
                )
            if len(calls) == 2:
                return json.dumps(_plan_payload(2))
            return json.dumps(_details_payload(_ids_in(kwargs["user_message"])))

        result, chat = _generate_with(respond)

        assert chat.call_count == 3
        assert [case["id"] for case in result["test_cases"]] == ["TC-001", "TC-002"]

    def test_an_empty_plan_after_an_empty_single_call_is_rejected(self):
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return json.dumps(_payload(test_cases=[]))
            return json.dumps(_plan_payload(0))

        with pytest.raises(TaskAnalysisError) as excinfo:
            _generate_with(respond)

        message = str(excinfo.value)
        assert "required test case plan format" in message
        assert "'test_cases' must be a non-empty array." in message

    def test_a_malformed_plan_after_an_empty_single_call_is_rejected(self):
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return json.dumps(_payload(test_cases=[]))
            return "I cannot produce the plan."

        with pytest.raises(TaskAnalysisError, match="did not return JSON"):
            _generate_with(respond)


class TestStagedValidationStaysStrict:
    """هر مرحله فقط با قراردادِ خودش سنجیده می‌شود؛ هیچ‌چیز حدس زده نمی‌شود."""

    def test_a_plan_shaped_payload_is_not_a_valid_final_result(self):
        with pytest.raises(TaskAnalysisError) as excinfo:
            validate_analysis(_plan_payload(2))

        message = str(excinfo.value)
        assert "required test case format" in message
        assert "required test case plan format" not in message
        assert "test_cases[0] ('TC-001').steps: is required" in message
        assert "'expected_result' is missing or empty." in message

    def test_an_empty_final_test_case_list_is_never_accepted(self):
        with pytest.raises(TaskAnalysisError) as excinfo:
            validate_analysis(_payload(test_cases=[]))

        assert "'test_cases' must be a non-empty array." in str(excinfo.value)

    def test_an_empty_details_response_is_rejected_without_partial_result(self):
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return json.dumps(_payload(test_cases=[]))
            if len(calls) == 2:
                return json.dumps(_plan_payload(2))
            return json.dumps({"test_cases": []})

        with pytest.raises(TaskAnalysisError) as excinfo:
            _generate_with(respond)

        message = str(excinfo.value)
        assert "'test_cases' must be a non-empty array." in message
        assert "missing details for planned test case id(s): 'TC-001', 'TC-002'" in message

    def test_a_malformed_details_response_is_rejected_without_partial_result(self):
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return json.dumps(_payload(test_cases=[]))
            if len(calls) == 2:
                return json.dumps(_plan_payload(2))
            return "I cannot expand these test cases."

        with pytest.raises(TaskAnalysisError, match="did not return JSON"):
            _generate_with(respond)

    def test_a_failed_batch_reports_its_range_and_yields_no_partial_result(self):
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return json.dumps(_payload(test_cases=[]))
            if len(calls) == 2:
                return json.dumps(_plan_payload(20))
            if len(calls) == 3:
                return json.dumps(_details_payload(_ids_in(kwargs["user_message"])))
            return json.dumps({"test_cases": []})

        with pytest.raises(TaskAnalysisError) as excinfo:
            _generate_with(respond)

        message = str(excinfo.value)
        assert "planned test cases 9-16 of 20" in message
        assert "batch 2 of 3" in message
        assert "No partial result can be used" in message


def _provider_response(content: str, prompt: int, completion: int) -> SimpleNamespace:
    """پاسخِ ارائه‌دهنده با فراداده‌ی مصرف — شکلِ همان چیزی که SDK می‌دهد."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            prompt_tokens_details=None,
        ),
    )


@pytest.fixture
def provider():
    """OpenAI ماک‌شده با کدِ واقعیِ LLMClient؛ خروجی صفِ پاسخ‌هاست."""
    with patch("src.llm_client.OpenAI") as mock_openai, patch(
        "src.llm_client._make_http_client"
    ), patch("src.llm_client.obs", MagicMock(enabled=False)):
        yield mock_openai.return_value.chat.completions.create


class TestStep1UsageSurvivesFailure:
    """مصرفِ فراخوانی‌هایی که به شکست می‌رسند نباید گم شود."""

    def test_usage_of_the_failed_single_call_and_plan_is_kept(self, provider):
        provider.side_effect = [
            _provider_response(json.dumps(_payload(test_cases=[])), 100, 10),
            _provider_response(json.dumps(_plan_payload(0)), 200, 20),
        ]
        records: list[TokenUsage] = []

        with pytest.raises(TaskAnalysisError):
            with collect_usage(records.append):
                TaskAnalysisGenerator().generate("Add something", "user-1")

        assert [record.calls for record in records] == [1, 1]
        aggregate = TokenUsage()
        for record in records:
            aggregate = aggregate.combine(record)
        assert aggregate == TokenUsage(2, 300, 30, 330)



# ── قراردادِ بازگشتیِ LLMClient.chat() ────────────────────────────────────────
# LLMClient.chat() متنِ خامِ مدل را به‌صورت یک رشته (str) برمی‌گرداند، نه شیءِ
# ChatCompletion. تست‌های زیر کلاینتِ واقعی را — با transport ماک‌شده تا شبکه
# لازم نباشد ولی کدِ خودِ LLMClient اجرا شود — به TaskAnalysisGenerator وصل
# می‌کنند. اگر روزی کسی `.choices[0].message.content` را به مسیرِ تحلیلِ تسک
# برگرداند، همان‌جا شکست می‌خورد.

def _completion(content: str) -> ChatCompletion:
    """یک ChatCompletion واقعی می‌سازد — همان چیزی که SDK برمی‌گرداند."""
    return ChatCompletion(
        id="chatcmpl-test",
        object="chat.completion",
        created=0,
        model="test-model",
        choices=[
            Choice(
                index=0,
                finish_reason="stop",
                message=ChatCompletionMessage(role="assistant", content=content),
            )
        ],
    )


@pytest.fixture
def patched_openai():
    """OpenAI را در ماژولِ llm_client ماک می‌کند: بدون شبکه، با کدِ واقعیِ LLMClient."""
    with patch("src.llm_client.OpenAI") as mock_openai, patch(
        "src.llm_client._make_http_client"
    ):
        mock_openai.return_value.chat.completions.create.return_value = _completion(
            json.dumps(_payload())
        )
        yield mock_openai


class TestLLMClientChatReturnContract:
    def test_chat_returns_a_plain_string(self, patched_openai):
        client = LLMClient(user_id="user-1", agent_name="task_analysis")

        raw = client.chat(user_message="hi", system_prompt="sys")

        assert isinstance(raw, str)
        assert json.loads(raw)["task_summary"] == "Add percentage voucher creation."

    def test_generate_consumes_the_real_clients_string_output(self, patched_openai):
        """مسیرِ کامل با LLMClient واقعی — نه ماکِ ماژولِ task_analysis."""
        result = TaskAnalysisGenerator().generate(
            task_description="Add percentage voucher creation.",
            user_id="user-1",
            developed_services="POST /admin/voucher/percent",
        )

        assert result["task_summary"] == "Add percentage voucher creation."
        assert result["test_cases"][0]["id"] == "TC-001"

    def test_generate_sends_the_llm_client_a_string_it_can_parse(self, patched_openai):
        """تأییدِ اینکه مسیرِ واقعی همان فراخوانیِ OpenAI را می‌زند.

        اگر generate خروجیِ chat() را به‌عنوان شیءِ ChatCompletion مصرف کند،
        تستِ بالا با AttributeError شکست می‌خورد؛ این‌جا فقط درخواستِ ارسالی
        بررسی می‌شود.
        """
        TaskAnalysisGenerator().generate(
            task_description="Add percentage voucher creation.",
            user_id="user-1",
        )

        call_kwargs = patched_openai.return_value.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"]
        assert call_kwargs["messages"][0]["role"] == "system"
