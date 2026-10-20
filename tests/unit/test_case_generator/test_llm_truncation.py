"""
تست‌های بریده‌شدگیِ پاسخِ LLM، سقفِ توکنِ خروجی و قراردادِ «فقط JSON»

سه چیزی که این‌جا قفل می‌شود:

  1. ``finish_reason == "length"`` یعنی پاسخ بریده شده — و برنامه این را
     می‌فهمد و صریح گزارش می‌کند (نه اینکه JSONِ نیمه‌کاره را ترمیم کند).
  2. سقفِ توکنِ خروجی یک مقدارِ مشترک و قابلِ تنظیم است، نه عددِ سرخودِ هر
     generator.
  3. خروجیِ LLM باید فقط JSON باشد و پرامپت هم همین را می‌خواهد.

هیچ‌کدام از این تست‌ها به شبکه دست نمی‌زنند: یا OpenAI در llm_client ماک شده
(با کدِ واقعیِ LLMClient) یا کلِ LLMClient در ماژولِ قدم ماک شده است.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage

import src.config as config_module
import src.llm_client as llm_client
from src.agents.test_case_generator.api_discovery import (
    DiscoveredApi,
    DiscoveredService,
)
from src.agents.test_case_generator.api_mapping import (
    DEFAULT_MAPPING_BATCH_SIZE,
    ApiMappingAgent,
    ApiMappingError,
    Step2ApiMappingGenerator,
    split_prompt,
)
from src.agents.test_case_generator.task_analysis import (
    DEFAULT_TEST_CASE_BATCH_SIZE,
    TaskAnalysisError,
    TaskAnalysisGenerator,
)
from src.config import LLM_MAX_OUTPUT_TOKENS
from src.llm_client import (
    FINISH_REASON_LENGTH,
    LLMClient,
    LLMResponse,
    truncation_message,
)

STEP2_PROMPT_PATH = (
    Path(__file__).parents[3]
    / "src"
    / "agents"
    / "test_case_generator"
    / "prompts"
    / "step2_api_mapping.md"
)

# همان سقفی که پاسخِ واقعیِ قدم دوم روی آن تمام شد
_OBSERVED_PROMPT_TOKENS = 55265
_OBSERVED_COMPLETION_TOKENS = 4096


# ── ساختِ پاسخِ ارائه‌دهنده ─────────────────────────────────────────────────

def _completion(
    content: str,
    finish_reason: str = "stop",
    usage: CompletionUsage | None = None,
) -> ChatCompletion:
    """یک ChatCompletion واقعی می‌سازد — همان چیزی که SDK برمی‌گرداند."""
    return ChatCompletion(
        id="chatcmpl-test",
        object="chat.completion",
        created=0,
        model="test-model",
        choices=[
            Choice(
                index=0,
                finish_reason=finish_reason,
                message=ChatCompletionMessage(role="assistant", content=content),
            )
        ],
        usage=usage,
    )


@pytest.fixture
def provider():
    """OpenAI را در llm_client ماک می‌کند و اجازه می‌دهد پاسخ را تعیین کنیم."""
    with patch("src.llm_client.OpenAI") as mock_openai, patch(
        "src.llm_client._make_http_client"
    ):
        create = mock_openai.return_value.chat.completions.create

        def respond(
            content: str,
            finish_reason: str = "stop",
            prompt_tokens: int | None = None,
            completion_tokens: int | None = None,
        ):
            usage = None
            if prompt_tokens is not None or completion_tokens is not None:
                usage = CompletionUsage(
                    prompt_tokens=prompt_tokens or 0,
                    completion_tokens=completion_tokens or 0,
                    total_tokens=(prompt_tokens or 0) + (completion_tokens or 0),
                )
            create.return_value = _completion(content, finish_reason, usage)
            return create

        yield respond


@pytest.fixture
def raw_path(monkeypatch):
    """مسیرِ بدونِ observability را قطعی می‌کند (مستقل از .env)."""
    monkeypatch.setattr(llm_client, "obs", MagicMock(enabled=False))


def _client() -> LLMClient:
    return LLMClient(user_id="user-1", agent_name="task_analysis")


# ── فراداده‌ی پاسخ و بریده‌شدگی ─────────────────────────────────────────────

class TestLLMResponse:
    def test_a_length_finish_reason_means_truncated(self):
        response = LLMResponse('{"a": 1', finish_reason=FINISH_REASON_LENGTH)

        assert response.truncated is True

    def test_a_stop_finish_reason_means_complete(self):
        response = LLMResponse('{"a": 1}', finish_reason="stop")

        assert response.truncated is False
        assert truncation_message(response) == ""

    def test_the_truncation_message_names_the_output_token_limit(self):
        response = LLMResponse(
            '{"mappings": [',
            finish_reason=FINISH_REASON_LENGTH,
            prompt_tokens=_OBSERVED_PROMPT_TOKENS,
            completion_tokens=_OBSERVED_COMPLETION_TOKENS,
            max_tokens=LLM_MAX_OUTPUT_TOKENS,
        )

        message = response.truncation_message()

        assert "truncated because it reached the output token limit" in message
        assert "finish_reason='length'" in message
        assert f"max_tokens={LLM_MAX_OUTPUT_TOKENS}" in message
        assert f"completion_tokens={_OBSERVED_COMPLETION_TOKENS}" in message
        assert "nothing was repaired or guessed" in message

    def test_an_llm_response_is_still_a_string(self):
        """قراردادِ قبلیِ chat() («برگرداندنِ رشته») نباید عوض شود."""
        response = LLMResponse('{"a": 1}', finish_reason="stop")

        assert isinstance(response, str)
        assert json.loads(response) == {"a": 1}
        assert response.strip() == response

    def test_an_empty_response_is_still_falsy(self):
        assert LLMResponse() == ""
        assert not LLMResponse()


class TestTruncationMessageHelper:
    def test_a_plain_string_is_never_reported_as_truncated(self):
        """کلاینتِ تزریق‌شده در تست‌ها رشته‌ی ساده برمی‌گرداند — نباید ادعایی شود."""
        assert truncation_message("not an LLMResponse") == ""

    def test_other_types_are_ignored(self):
        assert truncation_message(None) == ""
        assert truncation_message(MagicMock()) == ""

    def test_a_truncated_response_is_reported(self):
        message = truncation_message(
            LLMResponse("x", finish_reason=FINISH_REASON_LENGTH, max_tokens=8192)
        )

        assert "output token limit" in message


# ── کلاینتِ واقعی: فراداده از خودِ پاسخ خوانده می‌شود ─────────────────────────

class TestLLMClientMetadata:
    def test_chat_keeps_the_string_contract(self, provider, raw_path):
        provider(json.dumps({"task_summary": "ok"}))

        raw = _client().chat(user_message="hi")

        assert isinstance(raw, str)
        assert json.loads(raw)["task_summary"] == "ok"

    def test_a_complete_response_is_not_flagged(self, provider, raw_path):
        provider(json.dumps({"a": 1}), finish_reason="stop", completion_tokens=10)

        raw = _client().chat(user_message="hi")

        assert raw.finish_reason == "stop"
        assert raw.truncated is False
        assert truncation_message(raw) == ""

    def test_a_response_cut_by_the_output_limit_is_flagged(self, provider, raw_path):
        provider(
            '{"mappings": [{"test_case_id": "TC-1"',
            finish_reason="length",
            prompt_tokens=_OBSERVED_PROMPT_TOKENS,
            completion_tokens=_OBSERVED_COMPLETION_TOKENS,
        )

        raw = _client().chat(user_message="hi", max_tokens=LLM_MAX_OUTPUT_TOKENS)

        assert raw.finish_reason == FINISH_REASON_LENGTH
        assert raw.truncated is True
        assert raw.prompt_tokens == _OBSERVED_PROMPT_TOKENS
        assert raw.completion_tokens == _OBSERVED_COMPLETION_TOKENS
        assert raw.max_tokens == LLM_MAX_OUTPUT_TOKENS
        assert "output token limit" in truncation_message(raw)

    def test_the_requested_limit_is_recorded_on_the_response(self, provider, raw_path):
        provider("{}")

        raw = _client().chat(user_message="hi", max_tokens=2048)

        assert raw.max_tokens == 2048

    def test_a_response_without_usage_still_works(self, provider, raw_path):
        provider("{}")

        raw = _client().chat(user_message="hi")

        assert raw.truncated is False
        assert raw.prompt_tokens is None

    def test_the_observed_path_carries_the_same_metadata(self, provider, monkeypatch):
        provider(
            '{"a": 1', finish_reason="length", completion_tokens=_OBSERVED_COMPLETION_TOKENS
        )
        observation = MagicMock()
        observation.__enter__ = MagicMock(return_value=MagicMock())
        observation.__exit__ = MagicMock(return_value=False)
        obs = MagicMock(enabled=True)
        obs.observation.return_value = observation
        monkeypatch.setattr(llm_client, "obs", obs)

        raw = _client().chat(user_message="hi", max_tokens=8192)

        assert raw.finish_reason == FINISH_REASON_LENGTH
        assert raw.truncated is True


# ── سقفِ توکنِ خروجی: یک مقدارِ مشترک و قابلِ تنظیم ──────────────────────────

def _limit_with_env(value: str | None) -> int:
    """تنظیمات را با یک مقدارِ محیطیِ مشخص می‌خواند و بعد به حالتِ اول برمی‌گرداند."""
    original = os.environ.get("LLM_MAX_OUTPUT_TOKENS")
    try:
        if value is None:
            os.environ.pop("LLM_MAX_OUTPUT_TOKENS", None)
        else:
            os.environ["LLM_MAX_OUTPUT_TOKENS"] = value
        importlib.reload(config_module)
        return config_module.LLM_MAX_OUTPUT_TOKENS
    finally:
        if original is None:
            os.environ.pop("LLM_MAX_OUTPUT_TOKENS", None)
        else:
            os.environ["LLM_MAX_OUTPUT_TOKENS"] = original
        importlib.reload(config_module)


def _step1_payload() -> dict:
    return {
        "task_summary": "Add voucher creation.",
        "identified_requirements": ["A voucher can be created."],
        "test_cases": [
            {
                "id": "TC-001",
                "title": "Create a valid voucher",
                "type": "positive",
                "priority": "high",
                "preconditions": ["The user has permission"],
                "steps": ["Submit a valid voucher"],
                "expected_result": "The voucher is created.",
                "related_service": {"method": "POST", "path": "/admin/voucher"},
            }
        ],
        "clarifications": [],
    }


class TestOutputLimitConfiguration:
    def test_the_documented_default_is_8192(self):
        assert _limit_with_env(None) == 8192

    def test_the_limit_is_configurable_through_the_environment(self):
        assert _limit_with_env("1234") == 1234

    def test_the_value_is_restored_after_reading(self):
        assert config_module.LLM_MAX_OUTPUT_TOKENS == LLM_MAX_OUTPUT_TOKENS

    def test_step1_uses_the_shared_limit_by_default(self):
        with patch(
            "src.agents.test_case_generator.task_analysis.LLMClient"
        ) as mock_cls:
            mock_cls.return_value.chat.return_value = json.dumps(_step1_payload())

            TaskAnalysisGenerator().generate("Add a voucher", "user-1")

        assert (
            mock_cls.return_value.chat.call_args.kwargs["max_tokens"]
            == LLM_MAX_OUTPUT_TOKENS
        )

    def test_step1_explicit_max_tokens_still_wins(self):
        with patch(
            "src.agents.test_case_generator.task_analysis.LLMClient"
        ) as mock_cls:
            mock_cls.return_value.chat.return_value = json.dumps(_step1_payload())

            TaskAnalysisGenerator(max_tokens=2048).generate("Add a voucher", "user-1")

        assert mock_cls.return_value.chat.call_args.kwargs["max_tokens"] == 2048

    def test_step2_uses_the_shared_limit_by_default(self):
        with patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            mock_cls.return_value.chat.return_value = json.dumps(
                _mapping_payload(
                    [
                        _mapping("TC-001", _voucher_api()),
                        _mapping("TC-002", _audit_api()),
                    ]
                )
            )

            ApiMappingAgent().map_test_cases(_cases(), _catalog(), "user-1")

        assert (
            mock_cls.return_value.chat.call_args_list[0].kwargs["max_tokens"]
            == LLM_MAX_OUTPUT_TOKENS
        )

    def test_step2_explicit_max_tokens_still_wins(self):
        with patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            mock_cls.return_value.chat.return_value = json.dumps(
                _mapping_payload(
                    [
                        _mapping("TC-001", _voucher_api()),
                        _mapping("TC-002", _audit_api()),
                    ]
                )
            )

            ApiMappingAgent(max_tokens=2048).map_test_cases(
                _cases(), _catalog(), "user-1"
            )

        assert mock_cls.return_value.chat.call_args_list[0].kwargs["max_tokens"] == 2048


# ── قدم‌ها: پاسخِ بریده گزارش می‌شود، ترمیم نمی‌شود ───────────────────────────

class TestTruncationIsReportedByTheSteps:
    def test_step1_reports_a_truncated_response(self):
        with patch(
            "src.agents.test_case_generator.task_analysis.LLMClient"
        ) as mock_cls:
            mock_cls.return_value.chat.return_value = LLMResponse(
                '{"task_summary": "Add vou',
                finish_reason=FINISH_REASON_LENGTH,
                prompt_tokens=_OBSERVED_PROMPT_TOKENS,
                completion_tokens=_OBSERVED_COMPLETION_TOKENS,
                max_tokens=LLM_MAX_OUTPUT_TOKENS,
            )

            with pytest.raises(TaskAnalysisError) as excinfo:
                TaskAnalysisGenerator().generate("Add a voucher", "user-1")

        message = str(excinfo.value)
        assert "truncated because it reached the output token limit" in message
        assert "finish_reason='length'" in message

    def test_step2_reports_a_truncated_response(self):
        with patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            mock_cls.return_value.chat.return_value = LLMResponse(
                '{"mappings": [{"test_case_id": "TC-001", "api": {"method": "PUT"',
                finish_reason=FINISH_REASON_LENGTH,
                max_tokens=LLM_MAX_OUTPUT_TOKENS,
                completion_tokens=LLM_MAX_OUTPUT_TOKENS,
            )

            with pytest.raises(ApiMappingError) as excinfo:
                ApiMappingAgent().map_test_cases(_cases(), _catalog(), "user-1")

        assert "output token limit" in str(excinfo.value)

    def test_a_truncated_response_is_not_reported_as_a_json_error(self):
        """پاسخِ بریده «JSONِ نامعتبر» نیست: علتش باید صریح گفته شود."""
        with patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            mock_cls.return_value.chat.return_value = LLMResponse(
                '{"mappings": [{"test_case_id": "TC-001"',
                finish_reason=FINISH_REASON_LENGTH,
                max_tokens=LLM_MAX_OUTPUT_TOKENS,
            )

            with pytest.raises(ApiMappingError) as excinfo:
                ApiMappingAgent().map_test_cases(_cases(), _catalog(), "user-1")

        assert "invalid JSON" not in str(excinfo.value)

    def test_a_truncated_response_produces_no_result_at_all(self):
        """هیچ نگاشتی از پاسخِ نیمه‌کاره ساخته نمی‌شود — نه نتیجه‌ی جزئی."""
        with patch(
            "src.agents.test_case_generator.task_analysis.LLMClient"
        ) as mock_cls:
            mock_cls.return_value.chat.return_value = LLMResponse(
                '{"task_summary": "Add vou', finish_reason=FINISH_REASON_LENGTH
            )

            generator = TaskAnalysisGenerator()

            with pytest.raises(TaskAnalysisError) as excinfo:
                generator.generate("Add a voucher", "user-1")

        assert "output token limit" in str(excinfo.value)

    def test_the_generator_step_2_propagates_the_truncation_error(self):
        with patch(
            "src.agents.test_case_generator.api_mapping.discover_services",
            return_value=(_catalog(), []),
        ), patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            mock_cls.return_value.chat.return_value = LLMResponse(
                '{"mappings": [',
                finish_reason=FINISH_REASON_LENGTH,
                max_tokens=LLM_MAX_OUTPUT_TOKENS,
            )

            with pytest.raises(ApiMappingError, match="output token limit"):
                Step2ApiMappingGenerator().generate(
                    {"test_cases": _cases()}, ["https://host/swagger"], "user-1"
                )

    def test_malformed_json_with_a_normal_finish_reason_is_still_a_json_error(self):
        """پاسخِ کامل ولی نامعتبر باید همان خطای قبلیِ JSON را بدهد."""
        with patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            mock_cls.return_value.chat.return_value = LLMResponse(
                "I will generate the JSON now.", finish_reason="stop"
            )

            with pytest.raises(ApiMappingError, match="did not return JSON"):
                ApiMappingAgent().map_test_cases(_cases(), _catalog(), "user-1")

    def test_explanatory_text_without_json_is_rejected(self):
        with patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            mock_cls.return_value.chat.return_value = (
                "Self-Correction/Refinement during thought:\n"
                "I will output raw JSON.\n[Output Generation] -> JSON string."
            )

            with pytest.raises(ApiMappingError):
                ApiMappingAgent().map_test_cases(_cases(), _catalog(), "user-1")

    def test_reasoning_preamble_in_front_of_a_cut_response_is_reported_as_truncation(self):
        """همان شکستِ واقعی: متنِ اضافه + JSONِ نیمه‌کاره + finish_reason=length."""
        with patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            mock_cls.return_value.chat.return_value = LLMResponse(
                "I will generate the JSON now.\n"
                '{"mappings": [{"test_case_id": "TC-001", "api": {"method": "PUT",',
                finish_reason=FINISH_REASON_LENGTH,
                max_tokens=LLM_MAX_OUTPUT_TOKENS,
                completion_tokens=LLM_MAX_OUTPUT_TOKENS,
            )

            with pytest.raises(ApiMappingError, match="output token limit"):
                ApiMappingAgent().map_test_cases(_cases(), _catalog(), "user-1")


# ── قراردادِ «فقط JSON» در پرامپت ───────────────────────────────────────────

class TestJsonOnlyOutputContract:
    def test_the_prompt_demands_a_json_only_response(self):
        system_prompt, _ = split_prompt(
            STEP2_PROMPT_PATH.read_text(encoding="utf-8")
        )

        assert "Start your response with the character { and end it with the character }" in (
            system_prompt
        )
        assert "No preamble" in system_prompt
        assert "Self-Correction/Refinement during thought" in system_prompt
        assert "I will generate the JSON now" in system_prompt

    def test_the_prompt_calls_out_the_observed_leaked_phrases(self):
        system_prompt, _ = split_prompt(
            STEP2_PROMPT_PATH.read_text(encoding="utf-8")
        )

        for phrase in (
            "I will generate the JSON now",
            "Self-Correction/Refinement during thought:",
            "I will output raw JSON",
        ):
            assert phrase in system_prompt

    def test_the_prompt_no_longer_claims_the_candidate_list_is_complete(self):
        text = STEP2_PROMPT_PATH.read_text(encoding="utf-8")

        assert "the complete set of operations that exist" not in text
        assert "filtered view" in text

    def test_the_prompt_still_forbids_inventing_operations(self):
        system_prompt, _ = split_prompt(
            STEP2_PROMPT_PATH.read_text(encoding="utf-8")
        )

        assert "You must NOT:" in system_prompt
        assert "Propose an operation that is not in the CANDIDATE APIS list" in (
            system_prompt
        )

    def test_the_prompt_requires_every_string_value_to_be_quoted(self):
        """شکستِ واقعی: مدل ``reason`` را بدونِ نقل‌قول نوشت و JSON نامعتبر شد."""
        system_prompt, _ = split_prompt(
            STEP2_PROMPT_PATH.read_text(encoding="utf-8")
        )

        assert "Every string value must be enclosed in double quotes" in system_prompt
        assert '"reason": The operation matches the requirement.' in system_prompt


# ── فیلترِ نامزدها در جریانِ نگاشت ──────────────────────────────────────────

def _voucher_api() -> dict:
    return {"method": "PUT", "path": "/admin/voucher/{id}", "operation_id": "updateVoucher"}


def _audit_api() -> dict:
    return {
        "method": "DELETE",
        "path": "/admin/audit/session",
        "operation_id": "purgeAuditSession",
    }


def _catalog() -> list[DiscoveredService]:
    """دو عملیات: یکی مرتبط با تست‌کیسِ اول، یکی با هیچ‌کدام."""
    return [
        DiscoveredService(
            name="admin",
            source_url="https://host/swagger",
            base_url="https://host",
            apis=[
                DiscoveredApi(
                    "PUT",
                    "/admin/voucher/{id}",
                    operation_id="updateVoucher",
                    summary="Update voucher",
                ),
                DiscoveredApi(
                    "DELETE",
                    "/admin/audit/session",
                    operation_id="purgeAuditSession",
                    summary="Purge audit sessions",
                ),
            ],
        )
    ]


def _cases() -> list[dict]:
    return [
        {"id": "TC-001", "title": "Update a voucher amount", "steps": ["Change it"]},
        {"id": "TC-002", "title": "Reconcile the nightly ledger"},
    ]


def _mapping(
    case_id: str, api: dict | None, confidence: str = "high", clarification: str = ""
) -> dict:
    return {
        "test_case_id": case_id,
        "api": api,
        "confidence": confidence,
        "reason": "The operation matches the requirement.",
        "clarification": clarification,
    }


def _mapping_payload(mappings: list[dict], clarifications: list[str] | None = None) -> dict:
    return {"mappings": mappings, "clarifications": clarifications or []}


def _map_with(*responses: dict, **kwargs):
    """نگاشت را با پاسخ‌های صف‌شده اجرا می‌کند و (نتیجه، ماکِ کلاینت) را می‌دهد."""
    with patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
        mock_cls.return_value.chat.side_effect = [
            json.dumps(response) for response in responses
        ]
        result = ApiMappingAgent(**kwargs).map_test_cases(
            _cases(), _catalog(), "user-1"
        )
    return result, mock_cls


def _user_messages(mock_cls) -> list[str]:
    return [
        call.kwargs["user_message"]
        for call in mock_cls.return_value.chat.call_args_list
    ]


class TestCandidateFilteringInTheMappingFlow:
    def test_the_prompt_only_contains_the_candidate_operations(self):
        _, mock_cls = _map_with(
            _mapping_payload(
                [
                    _mapping("TC-001", _voucher_api()),
                    _mapping("TC-002", None, confidence="low", clarification="Which one?"),
                ]
            ),
            _mapping_payload([_mapping("TC-002", _audit_api())]),
        )

        first_prompt = _user_messages(mock_cls)[0]

        assert "updateVoucher" in first_prompt
        assert "purgeAuditSession" not in first_prompt

    def test_every_test_case_is_still_sent_to_the_llm(self):
        _, mock_cls = _map_with(
            _mapping_payload(
                [
                    _mapping("TC-001", _voucher_api()),
                    _mapping("TC-002", None, confidence="low", clarification="Which one?"),
                ]
            ),
            _mapping_payload([_mapping("TC-002", _audit_api())]),
        )

        first_prompt = _user_messages(mock_cls)[0]

        assert "TC-001" in first_prompt
        assert "TC-002" in first_prompt

    def test_validation_accepts_an_operation_outside_the_candidate_set(self):
        """اعتبارسنجی با کاتالوگِ کامل است، نه با نامزدهای پرامپت."""
        mappings, clarifications = _map_with(
            _mapping_payload(
                [
                    _mapping("TC-001", _voucher_api()),
                    _mapping("TC-002", _audit_api()),
                ]
            )
        )[0]

        assert [m["test_case_id"] for m in mappings] == ["TC-001", "TC-002"]
        assert mappings[1]["api"]["operation_id"] == "purgeAuditSession"
        assert clarifications == []

    def test_an_unresolved_case_is_retried_against_the_full_catalog(self):
        mappings, _ = _map_with(
            _mapping_payload(
                [
                    _mapping("TC-001", _voucher_api()),
                    _mapping("TC-002", None, confidence="low", clarification="Which one?"),
                ]
            ),
            _mapping_payload([_mapping("TC-002", _audit_api())]),
        )[0]

        assert mappings[1]["api"]["operation_id"] == "purgeAuditSession"

    def test_the_retry_prompt_contains_the_full_catalog(self):
        _, mock_cls = _map_with(
            _mapping_payload(
                [
                    _mapping("TC-001", _voucher_api()),
                    _mapping("TC-002", None, confidence="low", clarification="Which one?"),
                ]
            ),
            _mapping_payload([_mapping("TC-002", _audit_api())]),
        )

        retry_prompt = _user_messages(mock_cls)[1]

        assert "purgeAuditSession" in retry_prompt

    def test_the_retry_asks_only_about_the_unresolved_case(self):
        _, mock_cls = _map_with(
            _mapping_payload(
                [
                    _mapping("TC-001", _voucher_api()),
                    _mapping("TC-002", None, confidence="low", clarification="Which one?"),
                ]
            ),
            _mapping_payload([_mapping("TC-002", _audit_api())]),
        )

        retry_prompt = _user_messages(mock_cls)[1]

        assert "TC-002" in retry_prompt
        assert "TC-001" not in retry_prompt

    def test_no_test_case_is_dropped_after_a_retry(self):
        mappings, _ = _map_with(
            _mapping_payload(
                [
                    _mapping("TC-001", _voucher_api()),
                    _mapping("TC-002", None, confidence="low", clarification="Which one?"),
                ]
            ),
            _mapping_payload([_mapping("TC-002", _audit_api())]),
        )[0]

        assert [m["test_case_id"] for m in mappings] == ["TC-001", "TC-002"]

    def test_a_case_that_stays_unresolved_is_kept_with_its_clarification(self):
        """تست‌کیسِ حل‌نشده حذف نمی‌شود — با ابهامش می‌ماند تا انسان جواب دهد."""
        mappings, clarifications = _map_with(
            _mapping_payload(
                [
                    _mapping("TC-001", _voucher_api()),
                    _mapping("TC-002", None, confidence="low", clarification="Which one?"),
                ]
            ),
            _mapping_payload(
                [_mapping("TC-002", None, confidence="low", clarification="Still unclear.")]
            ),
        )[0]

        assert len(mappings) == 2
        assert mappings[1]["api"] is None
        assert mappings[1]["clarification"] == "Still unclear."

    def test_a_fully_resolved_first_pass_makes_only_one_call(self):
        _, mock_cls = _map_with(
            _mapping_payload(
                [
                    _mapping("TC-001", _voucher_api()),
                    _mapping("TC-002", _audit_api()),
                ]
            )
        )

        assert mock_cls.return_value.chat.call_count == 1

    def test_an_unresolved_case_is_not_retried_when_nothing_was_filtered(self):
        """اگر کاتالوگِ کامل به LLM رفته باشد، فراخوانیِ دومی بی‌دلیل است."""
        cases = [
            {"id": "TC-001", "title": "Update a voucher amount"},
            {"id": "TC-002", "title": "Purge the audit session"},
        ]
        with patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            mock_cls.return_value.chat.return_value = json.dumps(
                _mapping_payload(
                    [
                        _mapping("TC-001", _voucher_api()),
                        _mapping("TC-002", None, confidence="low", clarification="?"),
                    ]
                )
            )

            mappings, _ = ApiMappingAgent().map_test_cases(
                cases, _catalog(), "user-1"
            )

        assert mock_cls.return_value.chat.call_count == 1
        assert [m["test_case_id"] for m in mappings] == ["TC-001", "TC-002"]
        assert mappings[1]["api"] is None


class TestTheStepStillReturnsTheFullCatalog:
    def test_the_result_keeps_operations_that_were_not_candidates(self):
        with patch(
            "src.agents.test_case_generator.api_mapping.discover_services",
            return_value=(_catalog(), []),
        ), patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            mock_cls.return_value.chat.side_effect = [
                json.dumps(
                    _mapping_payload(
                        [
                            _mapping("TC-001", _voucher_api()),
                            _mapping("TC-002", None, confidence="low", clarification="?"),
                        ]
                    )
                ),
                json.dumps(_mapping_payload([_mapping("TC-002", _audit_api())])),
            ]

            result = Step2ApiMappingGenerator().generate(
                {"test_cases": _cases()}, ["https://host/swagger"], "user-1"
            )

        operations = [
            api["operation_id"] for service in result["services"] for api in service["apis"]
        ]
        assert operations == ["updateVoucher", "purgeAuditSession"]
        assert len(result["mappings"]) == 2


# ── ورودیِ بزرگ: تقسیم به دسته، نه بریده‌شدن ─────────────────────────────────

def _case_ids_in_prompt(user_message: str) -> list[str]:
    """شناسه‌های تست‌کیسی که در یک پرامپتِ واقعاً ساخته‌شده آمده‌اند."""
    start = user_message.index("TEST CASES:") + len("TEST CASES:")
    end = user_message.index("CANDIDATE APIS:")
    return [case["id"] for case in json.loads(user_message[start:end].strip())]


def _many_cases(count: int) -> list[dict]:
    return [
        {
            "id": f"TC-{index:03d}",
            "title": f"Update voucher number {index}",
            "steps": [f"Change voucher {index}"],
        }
        for index in range(1, count + 1)
    ]


class TestLargeMappingRequestsAreBatched:
    def test_a_request_larger_than_one_response_is_split_instead_of_truncated(self):
        """همان شرایطِ شکست: اگر همه‌ی تست‌کیس‌ها یک‌جا بروند، پاسخ بریده می‌شود."""
        cases = _many_cases(25)

        def respond(**kwargs):
            case_ids = _case_ids_in_prompt(kwargs["user_message"])
            if len(case_ids) > DEFAULT_MAPPING_BATCH_SIZE:
                return LLMResponse(
                    '{"mappings": [',
                    finish_reason=FINISH_REASON_LENGTH,
                    completion_tokens=LLM_MAX_OUTPUT_TOKENS,
                    max_tokens=LLM_MAX_OUTPUT_TOKENS,
                )
            return json.dumps(
                _mapping_payload([_mapping(case_id, _voucher_api()) for case_id in case_ids])
            )

        with patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            mock_cls.return_value.chat.side_effect = respond

            mappings, _ = ApiMappingAgent().map_test_cases(
                cases, _catalog(), "user-1"
            )

        assert [mapping["test_case_id"] for mapping in mappings] == [
            case["id"] for case in cases
        ]
        assert mock_cls.return_value.chat.call_count == 3

    def test_without_batching_the_same_request_reports_truncation(self):
        """شاهدِ ریشه: یک فراخوانیِ واحد با همان ورودی به سقف می‌خورد."""
        cases = _many_cases(25)

        def respond(**kwargs):
            case_ids = _case_ids_in_prompt(kwargs["user_message"])
            assert len(case_ids) > DEFAULT_MAPPING_BATCH_SIZE
            return LLMResponse(
                '{"mappings": [',
                finish_reason=FINISH_REASON_LENGTH,
                completion_tokens=LLM_MAX_OUTPUT_TOKENS,
                max_tokens=LLM_MAX_OUTPUT_TOKENS,
            )

        with patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            mock_cls.return_value.chat.side_effect = respond

            with pytest.raises(ApiMappingError, match="output token limit"):
                ApiMappingAgent(batch_size=len(cases)).map_test_cases(
                    cases, _catalog(), "user-1"
                )

        assert mock_cls.return_value.chat.call_count == 1


# ── قدم اول: تولیدِ مرحله‌ای وقتی یک پاسخ کافی نیست ─────────────────────────

def _step1_planned(count: int) -> list[dict]:
    return [
        {
            "id": f"TC-{index:03d}",
            "title": f"Voucher scenario {index}",
            "type": "positive",
            "priority": "high",
            "related_service": {"method": "POST", "path": f"/admin/voucher/{index}"},
        }
        for index in range(1, count + 1)
    ]


def _step1_plan(count: int) -> dict:
    return {
        "task_summary": "Add voucher creation.",
        "identified_requirements": ["A voucher can be created."],
        "test_cases": _step1_planned(count),
        "clarifications": [],
    }


def _step1_details(case_ids: list[str]) -> dict:
    return {
        "test_cases": [
            {
                "id": case_id,
                "preconditions": [],
                "steps": [f"Submit {case_id}"],
                "expected_result": f"{case_id} succeeds",
            }
            for case_id in case_ids
        ]
    }


def _step1_ids_in_prompt(user_message: str) -> list[str]:
    """شناسه‌های تست‌کیسِ برنامه/دسته را از پرامپتِ واقعاً ساخته‌شده می‌خواند."""
    return re.findall(r'"id":\s*"(TC-\d+)"', user_message)


def _step1_truncated(completion_tokens: int = LLM_MAX_OUTPUT_TOKENS) -> LLMResponse:
    return LLMResponse(
        '{"task_summary": "Add voucher',
        finish_reason=FINISH_REASON_LENGTH,
        completion_tokens=completion_tokens,
        max_tokens=LLM_MAX_OUTPUT_TOKENS,
    )


class TestLargeStep1RequestsAreStaged:
    """شبیه‌سازیِ همان شکست: یک پاسخِ واحد برای این حجم از تست‌کیس بریده می‌شود."""

    def test_a_truncated_full_response_is_regenerated_as_plan_plus_batches(self):
        planned = 20
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            system = kwargs["system_prompt"]
            if "TEST CASE PLAN (COMPACT)" in system:
                return json.dumps(_step1_plan(planned))
            if "TEST CASE DETAILS" in system:
                ids = _step1_ids_in_prompt(kwargs["user_message"])
                assert len(ids) <= DEFAULT_TEST_CASE_BATCH_SIZE
                return json.dumps(_step1_details(ids))
            return _step1_truncated()

        with patch(
            "src.agents.test_case_generator.task_analysis.LLMClient"
        ) as mock_cls:
            mock_cls.return_value.chat.side_effect = respond

            result = TaskAnalysisGenerator().generate("Add vouchers", "user-1")

        assert [case["id"] for case in result["test_cases"]] == [
            f"TC-{index:03d}" for index in range(1, planned + 1)
        ]
        assert len(result["test_cases"]) == planned
        # ۱ پاسخِ یک‌مرحله‌ایِ بریده + ۱ برنامه + ۳ دسته‌ی گسترش
        assert len(calls) == 2 + 3
        batch_sizes = [
            len(_step1_ids_in_prompt(call["user_message"]))
            for call in calls
            if "TEST CASE DETAILS" in call["system_prompt"]
        ]
        assert batch_sizes == [8, 8, 4]
        assert all(call["max_tokens"] == LLM_MAX_OUTPUT_TOKENS for call in calls)

    def test_a_truncated_expansion_batch_fails_explicitly_with_its_range(self):
        calls = []
        details_calls = 0

        def respond(**kwargs):
            nonlocal details_calls
            calls.append(kwargs)
            system = kwargs["system_prompt"]
            if "TEST CASE PLAN (COMPACT)" in system:
                return json.dumps(_step1_plan(20))
            if "TEST CASE DETAILS" in system:
                details_calls += 1
                if details_calls == 1:
                    ids = _step1_ids_in_prompt(kwargs["user_message"])
                    return json.dumps(_step1_details(ids))
                return _step1_truncated()
            return _step1_truncated()

        with patch(
            "src.agents.test_case_generator.task_analysis.LLMClient"
        ) as mock_cls:
            mock_cls.return_value.chat.side_effect = respond

            with pytest.raises(TaskAnalysisError) as excinfo:
                TaskAnalysisGenerator(batch_size=8).generate("Add vouchers", "user-1")

        message = str(excinfo.value)
        assert "output token limit" in message
        assert "invalid JSON" not in message
        assert "planned test cases 9-16 of 20" in message
        assert "batch 2 of 3" in message

    def test_the_truncated_partial_json_is_never_parsed(self):
        """حتی اگر متن تا وسطِ JSON آمده باشد، هرگز ترمیم یا پذیرفته نمی‌شود."""
        calls = []

        def respond(**kwargs):
            calls.append(kwargs)
            system = kwargs["system_prompt"]
            if "TEST CASE PLAN (COMPACT)" in system:
                return json.dumps(_step1_plan(2))
            if "TEST CASE DETAILS" in system:
                ids = _step1_ids_in_prompt(kwargs["user_message"])
                return json.dumps(_step1_details(ids))
            # JSONِ معتبر تا نیمه، ولی finish_reason=length
            return _step1_truncated()

        with patch(
            "src.agents.test_case_generator.task_analysis.LLMClient"
        ) as mock_cls:
            mock_cls.return_value.chat.side_effect = respond

            result = TaskAnalysisGenerator().generate("Add vouchers", "user-1")

        assert [case["id"] for case in result["test_cases"]] == ["TC-001", "TC-002"]


class TestTruncationReachesTheUi:
    """خطای بریدگی باید همان‌طور که هست به کاربر برسد — نه با برچسبِ گمراه‌کننده."""

    def test_step1_ui_does_not_relabel_a_truncation_as_a_format_error(self):
        streamlit = pytest.importorskip("streamlit")
        from ui import step1_task_analysis as step1_ui  # noqa: E402

        truncated = _step1_truncated().truncation_message()

        with patch.object(
            step1_ui, "TaskAnalysisGenerator"
        ) as generator_cls, patch.object(streamlit, "spinner", MagicMock()):
            generator_cls.return_value.generate.side_effect = TaskAnalysisError(
                truncated
            )
            result, error = step1_ui._run("Add a voucher", "")

        assert result is None
        assert error is not None
        assert error.startswith("Task analysis failed:")
        assert "output token limit" in error
        # پیامِ خطا نباید ادعا کند خروجی با «فرمت» نخوانده است.
        assert "does not match the required test case format" not in error

    def test_step2_ui_propagates_the_batch_range_and_the_cause(self):
        streamlit = pytest.importorskip("streamlit")
        from ui import step2_api_mapping as step2_ui  # noqa: E402

        truncated = LLMResponse(
            '{"mappings": [',
            finish_reason=FINISH_REASON_LENGTH,
            completion_tokens=LLM_MAX_OUTPUT_TOKENS,
            max_tokens=LLM_MAX_OUTPUT_TOKENS,
        ).truncation_message()
        message = (
            f"{truncated}\n\nMapping failed for test cases 1-10 of 25 "
            "(batch 1 of 3, batch size 10)."
        )

        with patch.object(
            step2_ui, "Step2ApiMappingGenerator"
        ) as generator_cls, patch.object(streamlit, "spinner", MagicMock()):
            generator_cls.return_value.generate.side_effect = ApiMappingError(message)
            result, error = step2_ui._run(
                {"test_cases": _cases()}, "https://host/swagger"
            )

        assert result is None
        assert error is not None
        assert error.startswith("API mapping failed:")
        assert "output token limit" in error
        assert "test cases 1-10 of 25" in error
        assert "invalid JSON" not in error
