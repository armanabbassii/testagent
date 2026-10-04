"""تست‌های تحلیل تسک و تولید تست‌کیس ساختاریافته (قدم اول جریان HITL)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage

from src.agents.test_case_generator.task_analysis import (
    REQUIRES_MAPPING,
    TaskAnalysisError,
    TaskAnalysisGenerator,
    build_prompt,
    parse_analysis,
    render_prompt,
    split_prompt,
    validate_analysis,
)
from src.llm_client import LLMClient

PROMPT_PATH = (
    Path(__file__).parents[3]
    / "src"
    / "agents"
    / "test_case_generator"
    / "prompts"
    / "step1_task_analysis.md"
)


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
        }
        text = json.dumps(result)
        assert "postman" not in text.lower()
        assert "swagger" not in text.lower()


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
