"""
سقفِ توکنِ خروجی — قدم اول هیچ سقفی نمی‌فرستد، قدم‌های دیگر پیش‌فرضِ مشترکِ ۸۱۹۲

Task Analysis:
    * برنامه هیچ سقفِ توکنِ خروجی‌ای اعمال نمی‌کند: همه‌ی فراخوانی‌ها (یک‌مرحله‌ای،
      برنامه، دسته‌های گسترش) ``max_tokens=None`` می‌دهند و LLMClient کلید را از
      بدنه‌ی درخواست حذف می‌کند — نه ``"max_tokens": null`` و نه هیچ عددِ جایگزین.
    * هیچ متغیرِ محیطی (LLM_MAX_OUTPUT_TOKENS، LLM_MAX_OUTPUT_TOKENS_TASK_ANALYSIS)
      این را عوض نمی‌کند.
    * بریدگی از سمتِ ارائه‌دهنده همچنان fail-closed است.

قدم‌های دیگر (نگاشتِ API، تحلیلِ سناریو، تحلیلِ منابع) همان LLM_MAX_OUTPUT_TOKENS
(پیش‌فرض ۸۱۹۲) را دارند.

هیچ فراخوانیِ واقعیِ LLM انجام نمی‌شود: یا LLMClient ماک است یا OpenAI SDKِ واقعی
با ``httpx.MockTransport``.
"""

import importlib
import json
import os
import re
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest

import src.agents.test_case_generator.task_analysis as task_analysis
import src.config as config_module
from src.agents.test_case_generator.api_mapping import ApiMappingAgent
from src.agents.test_case_generator.scenario_analysis import ScenarioAnalysisAgent
from src.agents.test_case_generator.source_semantics import SemanticSourceAnalyzer
from src.agents.test_case_generator.task_analysis import (
    DIRECT_MAX_OUTPUT_TOKENS,
    LOW_OUTPUT_LIMIT_WARNING_THRESHOLD,
    TaskAnalysisError,
    TaskAnalysisGenerator,
)
from src.config import LLM_MAX_OUTPUT_TOKENS
from src.llm_client import FINISH_REASON_LENGTH, LLMResponse

SHARED = "LLM_MAX_OUTPUT_TOKENS"
TASK = "LLM_MAX_OUTPUT_TOKENS_TASK_ANALYSIS"
_PATCH = "src.agents.test_case_generator.task_analysis.LLMClient"


# ── پیکربندی: خواندنِ دوباره‌ی src.config با محیطِ کنترل‌شده ─────────────────


@contextmanager
def _config_with(tmp_path, **values: str | None):
    """config را با مقدارهای مشخص می‌خواند و بعد محیط را دقیقاً برمی‌گرداند.

    ENV_FILE به فایلی ناموجود اشاره می‌کند تا .envِ واقعیِ پروژه خوانده نشود.
    مقدارِ None یعنی متغیر حذف شود.
    """
    keys = (SHARED, TASK, "ENV_FILE")
    original = {key: os.environ.get(key) for key in keys}
    try:
        os.environ["ENV_FILE"] = str(tmp_path / "missing.env")
        for key in (SHARED, TASK):
            value = values.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield importlib.reload(config_module)
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(config_module)


class TestSharedConfiguration:
    def test_the_shared_default_is_8192(self, tmp_path):
        with _config_with(tmp_path) as cfg:
            assert cfg.LLM_MAX_OUTPUT_TOKENS == 8192
            assert cfg.DEFAULT_LLM_MAX_OUTPUT_TOKENS == 8192

    def test_an_explicit_shared_value_is_used(self, tmp_path):
        with _config_with(tmp_path, **{SHARED: "3000"}) as cfg:
            assert cfg.LLM_MAX_OUTPUT_TOKENS == 3000

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_shared_value_counts_as_unset(self, tmp_path, blank):
        with _config_with(tmp_path, **{SHARED: blank}) as cfg:
            assert cfg.LLM_MAX_OUTPUT_TOKENS == 8192

    @pytest.mark.parametrize("value", ["0", "-5", "abc", "1.5", "8k", "1e4", "1_000", "١٠٠٠"])
    def test_an_invalid_shared_value_is_rejected(self, tmp_path, value):
        with pytest.raises(ValueError, match=rf"'{SHARED}'.*positive whole number"):
            with _config_with(tmp_path, **{SHARED: value}):
                pass

    def test_the_task_analysis_settings_no_longer_exist(self, tmp_path):
        with _config_with(tmp_path, **{TASK: "65536", SHARED: "8192"}) as cfg:
            assert not hasattr(cfg, "LLM_MAX_OUTPUT_TOKENS_TASK_ANALYSIS")
            assert not hasattr(cfg, "LLM_MAX_OUTPUT_TOKENS_TASK_ANALYSIS_SOURCE")
        assert not hasattr(task_analysis, "LLM_MAX_OUTPUT_TOKENS_TASK_ANALYSIS")
        assert not hasattr(task_analysis, "LLM_MAX_OUTPUT_TOKENS")

    def test_the_old_task_analysis_variable_is_ignored_even_if_invalid(self, tmp_path):
        # دیگر خوانده نمی‌شود؛ پس مقدارِ نامعتبرش هم خطایی نمی‌دهد.
        with _config_with(tmp_path, **{TASK: "abc"}) as cfg:
            assert cfg.LLM_MAX_OUTPUT_TOKENS == 8192


# ── داده‌های ساختگیِ قدم اول ─────────────────────────────────────────────────


def _planned(count: int) -> list[dict]:
    return [
        {
            "id": f"TC-{index:03d}",
            "title": f"Voucher scenario {index}",
            "type": "positive",
            "priority": "high",
            "related_service": {"method": "POST", "path": "/admin/voucher"},
        }
        for index in range(1, count + 1)
    ]


def _full(count: int) -> dict:
    return {
        "task_summary": "Add voucher creation.",
        "identified_requirements": ["A voucher can be created."],
        "test_cases": [
            {
                **case,
                "preconditions": [],
                "steps": [f"Submit {case['id']}"],
                "expected_result": f"{case['id']} succeeds",
            }
            for case in _planned(count)
        ],
        "clarifications": [],
    }


def _plan(count: int) -> dict:
    return {
        "task_summary": "Add voucher creation.",
        "identified_requirements": ["A voucher can be created."],
        "test_cases": _planned(count),
        "clarifications": [],
    }


def _details(ids: list[str]) -> dict:
    return {
        "test_cases": [
            {
                "id": case_id,
                "preconditions": [],
                "steps": [f"Submit {case_id}"],
                "expected_result": f"{case_id} succeeds",
            }
            for case_id in ids
        ]
    }


def _ids(user_message: str) -> list[str]:
    return re.findall(r'"id":\s*"(TC-\d+)"', user_message)


# پاسخِ بریده‌ای که شبیهِ شروعِ یک JSONِ معتبر است — نباید هیچ تست‌کیسی از آن خوانده شود.
_PARTIAL = '{"task_summary": "Add vouchers", "test_cases": [{"id": "TC-001", "title": "Cre'


def _truncated(limit: int | None = None) -> LLMResponse:
    return LLMResponse(
        _PARTIAL,
        finish_reason=FINISH_REASON_LENGTH,
        completion_tokens=4000 if limit is None else limit,
        max_tokens=limit,
    )


def _stage(call: dict) -> str:
    system = call["system_prompt"]
    if "TEST CASE PLAN (COMPACT)" in system:
        return "plan"
    if "TEST CASE DETAILS" in system:
        return "details"
    return "single"


def _staged_responder(calls: list[dict], *, planned: int = 20, limit: int | None = None):
    """پاسخِ یک‌مرحله‌ای بریده می‌شود؛ برنامه و همه‌ی دسته‌ها موفق‌اند."""

    def respond(**kwargs):
        calls.append(kwargs)
        stage = _stage(kwargs)
        if stage == "plan":
            return json.dumps(_plan(planned))
        if stage == "details":
            return json.dumps(_details(_ids(kwargs["user_message"])))
        return _truncated(limit)

    return respond


def _passes_none(call: dict) -> bool:
    return "max_tokens" in call and call["max_tokens"] is None


# ── قدم اول: هیچ سقفی از سمتِ برنامه ────────────────────────────────────────


class TestTaskAnalysisOutputLimits:
    """فقط Direct سقفِ ۸۱۹۲ دارد؛ برنامه و گسترش بدونِ سقف و مستقل از آن."""

    def test_defaults(self):
        generator = TaskAnalysisGenerator()
        assert generator._direct_max_tokens == DIRECT_MAX_OUTPUT_TOKENS == 8192
        assert generator._max_tokens is None

    def test_the_single_call_uses_the_direct_cap(self):
        with patch(_PATCH) as mock_cls:
            mock_cls.return_value.chat.return_value = json.dumps(_full(2))
            result = TaskAnalysisGenerator().generate("Add vouchers", "user-1")

        assert len(result["test_cases"]) == 2
        assert mock_cls.return_value.chat.call_args.kwargs["max_tokens"] == 8192

    def test_plan_and_every_batch_do_not_inherit_the_direct_cap(self):
        calls: list[dict] = []
        with patch(_PATCH) as mock_cls:
            mock_cls.return_value.chat.side_effect = _staged_responder(calls)
            result = TaskAnalysisGenerator().generate("Add vouchers", "user-1")

        # ۱ یک‌مرحله‌ای + ۱ برنامه + ۳ دسته (۸، ۸، ۴)
        assert len(result["test_cases"]) == 20
        assert [_stage(call) for call in calls] == ["single", "plan"] + ["details"] * 3
        assert calls[0]["max_tokens"] == 8192
        # صریحاً None — نه حذفِ آرگومان، که پیش‌فرضِ ۱۰۲۴ِ chat را فعال می‌کرد.
        assert all(_passes_none(call) for call in calls[1:])

    def test_the_direct_cap_can_be_removed_or_changed_without_touching_the_others(self):
        for direct, expected in ((None, None), (4096, 4096)):
            calls: list[dict] = []
            with patch(_PATCH) as mock_cls:
                mock_cls.return_value.chat.side_effect = _staged_responder(calls, planned=3)
                TaskAnalysisGenerator(direct_max_tokens=direct).generate("Add vouchers", "u")
            assert calls[0]["max_tokens"] == expected
            assert all(_passes_none(call) for call in calls[1:])

    @pytest.mark.parametrize("value", [0, -1, True, 1.5, "8192"])
    def test_an_invalid_direct_cap_is_rejected(self, value):
        with pytest.raises(ValueError, match="direct_max_tokens must be a positive integer"):
            TaskAnalysisGenerator(direct_max_tokens=value)

    @pytest.mark.parametrize(
        "env",
        [
            {TASK: "65536"},
            {SHARED: "8192"},
            {TASK: "16384", SHARED: "1000"},
            {TASK: "abc"},
        ],
    )
    def test_environment_variables_never_change_the_limits(self, tmp_path, env):
        calls: list[dict] = []
        with _config_with(tmp_path, **env):
            with patch(_PATCH) as mock_cls:
                mock_cls.return_value.chat.side_effect = _staged_responder(calls, planned=3)
                TaskAnalysisGenerator().generate("Add vouchers", "user-1")

        assert [_stage(call) for call in calls] == ["single", "plan", "details"]
        assert calls[0]["max_tokens"] == 8192
        assert all(_passes_none(call) for call in calls[1:])

    def test_no_low_limit_warning_is_logged(self):
        log = MagicMock()
        debug_config = MagicMock()
        debug_config.get_logger.return_value = log

        TaskAnalysisGenerator(debug_config=debug_config)

        log.warning.assert_not_called()

    def test_ui_constructs_the_generator_without_a_limit(self):
        source = open("ui/pipeline.py", encoding="utf-8").read()
        constructions = re.findall(r"TaskAnalysisGenerator\(([^)]*)\)", source)

        assert constructions
        assert all("max_tokens" not in args for args in constructions)


class TestProviderTruncationStillFailsClosed:
    def test_a_truncated_single_call_starts_staged_generation(self):
        calls: list[dict] = []
        with patch(_PATCH) as mock_cls:
            mock_cls.return_value.chat.side_effect = _staged_responder(calls, planned=3)
            result = TaskAnalysisGenerator().generate("Add vouchers", "user-1")

        assert [_stage(call) for call in calls] == ["single", "plan", "details"]
        # همه‌ی تست‌کیس‌ها از برنامه + جزئیات آمده‌اند، نه از پاسخِ بریده.
        assert [case["steps"] for case in result["test_cases"]] == [
            [f"Submit TC-00{index}"] for index in (1, 2, 3)
        ]

    def test_a_truncated_plan_fails_without_partial_test_cases(self):
        calls: list[dict] = []

        def respond(**kwargs):
            calls.append(kwargs)
            return _truncated()

        with patch(_PATCH) as mock_cls:
            mock_cls.return_value.chat.side_effect = respond
            with pytest.raises(TaskAnalysisError) as exc_info:
                TaskAnalysisGenerator().generate("Add vouchers", "user-1")

        message = str(exc_info.value)
        assert [_stage(call) for call in calls] == ["single", "plan"]
        assert "compact test case plan also reached the output token limit" in message
        assert "No partial test case list was used" in message
        _assert_names_the_provider_limit(message)

    def test_a_truncated_expansion_batch_fails_without_partial_results(self):
        calls: list[dict] = []
        respond_ok = _staged_responder(calls)

        def respond(**kwargs):
            if (
                _stage(kwargs) == "details"
                and sum(_stage(call) == "details" for call in calls) == 1
            ):
                calls.append(kwargs)
                return _truncated()
            return respond_ok(**kwargs)

        with patch(_PATCH) as mock_cls:
            mock_cls.return_value.chat.side_effect = respond
            with pytest.raises(TaskAnalysisError) as exc_info:
                TaskAnalysisGenerator().generate("Add vouchers", "user-1")

        message = str(exc_info.value)
        assert "planned test cases 9-16 of 20" in message
        assert "No partial result can be used" in message
        _assert_names_the_provider_limit(message)


def _assert_names_the_provider_limit(message: str) -> None:
    assert "None" not in message
    assert "max_tokens=" not in message
    assert "output limit of" not in message
    assert "does not impose an output token limit" in message
    assert "not from this application" in message
    # متغیرهای قدیمی دیگر راهِ تنظیمِ این قدم نیستند و پیشنهاد نمی‌شوند.
    assert TASK not in message
    assert SHARED not in message


# ── بدنه‌ی واقعیِ درخواست: OpenAI SDK + MockTransport ─────────────────────────


def _completion(content: str, finish_reason: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.fixture
def wire(monkeypatch):
    """LLMClientِ واقعی با transportِ ماک؛ بدنه‌ی JSONِ هر درخواست ثبت می‌شود.

    پاسخِ یک‌مرحله‌ای بریده است (finish_reason=length)، برنامه ۲۰ تست‌کیس دارد و
    هر دسته‌ی گسترش جزئیاتِ شناسه‌های خودش را برمی‌گرداند.
    """
    monkeypatch.setattr("src.llm_client.LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setattr("src.llm_client.LLM_API_KEY", "test-key")
    monkeypatch.setattr("src.llm_client.LLM_MODEL", "test-model")
    monkeypatch.setattr("src.llm_client.obs", MagicMock(enabled=False))
    bodies: list[dict] = []
    raw_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw_bodies.append(request.content)
        body = json.loads(request.content)
        bodies.append(body)
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if "TEST CASE PLAN (COMPACT)" in system:
            return httpx.Response(200, json=_completion(json.dumps(_plan(20)), "stop"))
        if "TEST CASE DETAILS" in system:
            return httpx.Response(200, json=_completion(json.dumps(_details(_ids(user))), "stop"))
        return httpx.Response(200, json=_completion(_PARTIAL, FINISH_REASON_LENGTH))

    def make_client(user_id: str, *, verify: bool = True) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("src.llm_client._make_http_client", make_client)
    return bodies, raw_bodies


class TestTheRequestPayload:
    def test_only_direct_sends_max_tokens(self, wire):
        bodies, raw_bodies = wire

        result = TaskAnalysisGenerator().generate("Add vouchers", "user-1")

        assert len(result["test_cases"]) == 20
        stages = [
            "plan"
            if "TEST CASE PLAN (COMPACT)" in body["messages"][0]["content"]
            else "details"
            if "TEST CASE DETAILS" in body["messages"][0]["content"]
            else "single"
            for body in bodies
        ]
        assert stages == ["single", "plan"] + ["details"] * 3
        assert bodies[0]["max_tokens"] == 8192
        for body, raw in zip(bodies[1:], raw_bodies[1:], strict=True):
            assert "max_tokens" not in body
            assert "max_completion_tokens" not in body
            # نه null، نه هیچ عددی — کلید اصلاً در بایت‌های درخواست نیست.
            assert b"max_tokens" not in raw
            assert body["model"] == "test-model"
            assert body["temperature"] == 0.1

    def test_environment_variables_do_not_reach_the_payload(self, wire, tmp_path):
        bodies, raw_bodies = wire

        with _config_with(tmp_path, **{TASK: "65536", SHARED: "8192"}):
            TaskAnalysisGenerator().generate("Add vouchers", "user-1")

        assert len(bodies) == 5
        assert bodies[0]["max_tokens"] == 8192
        assert all(b"max_tokens" not in raw for raw in raw_bodies[1:])


# ── سقفِ صریحِ برنامه‌نویسی (فقط اگر فراخوان عددی به سازنده بدهد) ────────────────


class TestExplicitConstructorLimit:
    def test_an_explicit_limit_applies_to_plan_and_expansion_only(self):
        calls: list[dict] = []
        with patch(_PATCH) as mock_cls:
            mock_cls.return_value.chat.side_effect = _staged_responder(calls, limit=2048)
            TaskAnalysisGenerator(max_tokens=2048).generate("Add vouchers", "user-1")

        assert len(calls) == 5
        assert calls[0]["max_tokens"] == 8192
        assert all(call["max_tokens"] == 2048 for call in calls[1:])

    @pytest.mark.parametrize("value", [0, -1, True, 1.5, "8192"])
    def test_an_invalid_explicit_limit_is_rejected(self, value):
        with pytest.raises(ValueError, match="max_tokens must be a positive integer"):
            TaskAnalysisGenerator(max_tokens=value)

    def test_the_hint_names_the_explicit_limit(self):
        with patch(_PATCH) as mock_cls:
            mock_cls.return_value.chat.side_effect = lambda **_: _truncated(5000)
            with pytest.raises(TaskAnalysisError) as exc_info:
                TaskAnalysisGenerator(max_tokens=5000).generate("Add vouchers", "user-1")

        message = str(exc_info.value)
        assert "output limit of 5000 tokens per call (source: max_tokens argument)" in message
        assert TASK not in message

    def test_a_low_explicit_limit_logs_a_warning(self):
        log = MagicMock()
        debug_config = MagicMock()
        debug_config.get_logger.return_value = log

        TaskAnalysisGenerator(debug_config=debug_config, max_tokens=1000)

        log.warning.assert_called_once()
        assert log.warning.call_args.kwargs == {
            "max_tokens": 1000,
            "source": "max_tokens argument",
            "threshold": LOW_OUTPUT_LIMIT_WARNING_THRESHOLD,
        }

    def test_no_warning_at_or_above_the_threshold(self):
        log = MagicMock()
        debug_config = MagicMock()
        debug_config.get_logger.return_value = log

        TaskAnalysisGenerator(
            debug_config=debug_config, max_tokens=LOW_OUTPUT_LIMIT_WARNING_THRESHOLD
        )

        log.warning.assert_not_called()


# ── قدم‌های دیگر: همان مقدارِ مشترک (پیش‌فرض ۸۱۹۲) ─────────────────────────────


class TestOtherStepsAreUnaffected:
    def test_step2_mapping_keeps_the_shared_limit(self):
        assert ApiMappingAgent()._max_tokens == LLM_MAX_OUTPUT_TOKENS

    def test_step2_source_semantics_keeps_the_shared_limit(self):
        assert SemanticSourceAnalyzer(user_id="user-1")._max_tokens == LLM_MAX_OUTPUT_TOKENS

    def test_step3_keeps_the_shared_limit(self):
        assert ScenarioAnalysisAgent()._max_tokens == LLM_MAX_OUTPUT_TOKENS

    def test_other_steps_hold_an_integer_limit(self):
        for limit in (
            ApiMappingAgent()._max_tokens,
            SemanticSourceAnalyzer(user_id="user-1")._max_tokens,
            ScenarioAnalysisAgent()._max_tokens,
        ):
            assert isinstance(limit, int) and not isinstance(limit, bool)

    def test_explicit_limits_still_win_in_other_steps(self):
        assert ApiMappingAgent(max_tokens=777)._max_tokens == 777
        assert ScenarioAnalysisAgent(max_tokens=777)._max_tokens == 777

    def test_other_steps_read_only_the_shared_setting(self):
        import src.agents.test_case_generator.api_mapping as api_mapping
        import src.agents.test_case_generator.scenario_analysis as scenario_analysis
        import src.agents.test_case_generator.source_semantics as source_semantics

        for module in (api_mapping, scenario_analysis, source_semantics):
            assert module.LLM_MAX_OUTPUT_TOKENS == LLM_MAX_OUTPUT_TOKENS
            assert not hasattr(module, "LLM_MAX_OUTPUT_TOKENS_TASK_ANALYSIS")
