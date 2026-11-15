"""
عیب‌یابیِ قدم اول — لاگِ فقط-فراداده برای هر فراخوانی و هر تلاشِ HTTP

ثابت می‌کند که:
    * هر فراخوانیِ Task Analysis برچسبِ درستِ مرحله را دارد (direct، plan، expand i/n)؛
    * اندازه‌ی ورودی (کاراکتر و بایتِ UTF-8) درست اندازه‌گیری می‌شود؛
    * پاسخِ موفق زمان و usage را ثبت می‌کند و پاسخِ بریده truncated است؛
    * HTTP 504 مرحله، زمان، کد و request id را پیش از پرتاب ثبت می‌کند و خودِ همان
      خطا بدونِ تغییر بالا می‌رود؛
    * هیچ متنِ پرامپت، Jira، پیوست، نامِ فایل یا کلیدِ API در لاگ نمی‌آید؛
    * رفتارِ retryِ SDK (۳ تلاش) و نتیجه‌ی تولید عوض نشده است.

هیچ فراخوانیِ واقعیِ LLM انجام نمی‌شود: یا LLMClient ماک است یا OpenAI SDKِ واقعی
با ``httpx.MockTransport``.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

import src.llm_client as llm_client
from src.agents.test_case_generator import task_analysis_diagnostics as diag
from src.agents.test_case_generator.postman_summary import CollectionSummary, PostmanSummary
from src.agents.test_case_generator.task_analysis import TaskAnalysisGenerator
from src.debug import DebugConfig
from src.debug.base_handler import BaseHandler
from src.debug.levels import DebugLevel
from src.debug.logger import AgentLogger
from src.llm_client import FINISH_REASON_LENGTH, LLMClient, LLMResponse
from src.task_sources.sources import sources_from_task_input
from src.task_sources.task_input import (
    ATTACHMENT_PROCESSED,
    ATTACHMENT_UNSUPPORTED,
    TaskAttachment,
    TaskComment,
    TaskInput,
)
from tests.unit.test_case_generator.test_output_token_limits import (
    _PARTIAL,
    _completion,
    _details,
    _full,
    _ids,
    _plan,
    _stage,
    _staged_responder,
)

_PATCH = "src.agents.test_case_generator.task_analysis.LLMClient"
SENT = "درخواستِ LLMِ تحلیلِ تسک ارسال می‌شود"
RECEIVED = "پاسخِ LLMِ تحلیلِ تسک دریافت شد"
FAILED = "درخواستِ LLMِ تحلیلِ تسک شکست خورد"
COMPOSITION = "ترکیبِ ورودیِ تحلیلِ تسک"
ATTEMPT_STARTED = "تلاشِ HTTPِ LLM شروع شد"
ATTEMPT_ANSWERED = "تلاشِ HTTPِ LLM پاسخ گرفت"


class _ListHandler(BaseHandler):
    def __init__(self) -> None:
        super().__init__(DebugLevel.TRACE)
        self.records = []

    def emit(self, record) -> None:
        self.records.append(record)

    def close(self) -> None:
        pass

    def named(self, message: str) -> list[dict]:
        return [r.extra for r in self.records if r.message == message]

    def dump(self) -> str:
        return "\n".join(f"{r.message!r} {r.extra!r}" for r in self.records)


@pytest.fixture
def logs(monkeypatch):
    """لاگِ Task Analysis و لاگِ llm_client هر دو در یک فهرست جمع می‌شوند."""
    handler = _ListHandler()
    config = DebugConfig(global_level=DebugLevel.TRACE, handlers=[handler])
    client_logger = AgentLogger("llm_client", DebugLevel.TRACE, [handler])
    monkeypatch.setattr("src.llm_client._response_logger", lambda: client_logger)
    return config, handler


def _generator(config, **kwargs) -> TaskAnalysisGenerator:
    return TaskAnalysisGenerator(debug_config=config, **kwargs)


def _where(extra: dict) -> tuple:
    return extra["stage"], extra.get("batch"), extra.get("batches")


# ── برچسبِ مرحله ─────────────────────────────────────────────────────────────


class TestStageIdentifiers:
    def test_the_staged_path_labels_direct_plan_and_every_batch(self, logs):
        config, handler = logs
        calls, labels = [], []
        respond = _staged_responder(calls, planned=20)

        def chat(**kwargs):
            labels.append(llm_client._diagnostic_stage.get())
            return respond(**kwargs)

        with patch(_PATCH) as cls:
            cls.return_value.chat.side_effect = chat
            _generator(config).generate("Add vouchers", user_id="u")

        expected = [
            ("direct", None, None),
            ("plan", None, None),
            ("expand", 1, 3),
            ("expand", 2, 3),
            ("expand", 3, 3),
        ]
        assert [_where(e) for e in handler.named(SENT)] == expected
        assert [_where(e) for e in handler.named(RECEIVED)] == expected
        # برچسبی که hookهای HTTP می‌بینند در طولِ هر فراخوانی تنظیم است.
        assert labels == ["direct", "plan", "expand 1/3", "expand 2/3", "expand 3/3"]
        assert [_stage(c) for c in calls] == ["single", "plan"] + ["details"] * 3
        # بیرون از فراخوانی برچسب پاک می‌شود.
        assert llm_client._diagnostic_stage.get() is None

    def test_a_successful_single_call_is_only_direct(self, logs):
        config, handler = logs
        with patch(_PATCH) as cls:
            cls.return_value.chat.return_value = json.dumps(_full(2))
            _generator(config).generate("Add vouchers", user_id="u")

        assert [_where(e) for e in handler.named(SENT)] == [("direct", None, None)]
        assert [_where(e) for e in handler.named(RECEIVED)] == [("direct", None, None)]

    def test_stage_label_format(self):
        assert diag.stage_label("direct") == "direct"
        assert diag.stage_label("expand", 2, 5) == "expand 2/5"


# ── اندازه‌ی ورودی ───────────────────────────────────────────────────────────


class TestRequestSizes:
    def test_sizes_match_the_exact_prompts_sent(self, logs):
        config, handler = logs
        seen = []

        def chat(**kwargs):
            seen.append(kwargs)
            return json.dumps(_full(1))

        with patch(_PATCH) as cls:
            cls.return_value.chat.side_effect = chat
            _generator(config).generate("افزودنِ کوپنِ تخفیف", user_id="u")

        (sent,) = handler.named(SENT)
        system, user = seen[0]["system_prompt"], seen[0]["user_message"]
        assert sent["system_chars"] == len(system)
        assert sent["system_bytes"] == len(system.encode("utf-8"))
        assert sent["user_chars"] == len(user)
        assert sent["user_bytes"] == len(user.encode("utf-8"))
        assert sent["input_chars"] == len(system) + len(user)
        assert sent["input_bytes"] == len(system.encode()) + len(user.encode())
        # متنِ فارسی: بایت از کاراکتر بیشتر است — هر دو جداگانه ثبت می‌شوند.
        assert sent["user_bytes"] > sent["user_chars"]

    def test_request_sizes_helper(self):
        assert diag.request_sizes("ab", "سلام") == {
            "system_chars": 2,
            "system_bytes": 2,
            "user_chars": 4,
            "user_bytes": 8,
            "input_chars": 6,
            "input_bytes": 10,
        }
        assert diag.text_size(None) == (0, 0)


# ── پاسخِ موفق و بریده ───────────────────────────────────────────────────────


class TestResponses:
    def test_success_records_elapsed_time_and_token_usage(self, logs):
        config, handler = logs
        clock = MagicMock()
        # شروعِ generate، شروعِ فراخوانی، پایانِ فراخوانی، پایانِ generate
        clock.perf_counter.side_effect = [100.0, 101.0, 103.5, 104.25]
        response = LLMResponse(
            json.dumps(_full(2)),
            finish_reason="stop",
            prompt_tokens=1234,
            completion_tokens=567,
            total_tokens=1801,
            response_id="chatcmpl-abc",
        )
        with (
            patch(_PATCH) as cls,
            patch("src.agents.test_case_generator.task_analysis.time", clock),
        ):
            cls.return_value.chat.return_value = response
            _generator(config).generate("Add vouchers", user_id="u")

        (received,) = handler.named(RECEIVED)
        assert received["elapsed_s"] == 2.5
        assert received["outcome"] == "success"
        assert received["finish_reason"] == "stop"
        assert received["prompt_tokens"] == 1234
        assert received["completion_tokens"] == 567
        assert received["total_tokens"] == 1801
        assert received["response_id"] == "chatcmpl-abc"
        (total,) = handler.named("زمانِ کلِ تحلیلِ تسک")
        assert total["total_elapsed_s"] == 4.25
        assert total["test_cases"] == 2

    def test_a_truncated_response_is_identified(self, logs):
        config, handler = logs
        with patch(_PATCH) as cls:
            cls.return_value.chat.side_effect = _staged_responder([], planned=3)
            _generator(config).generate("Add vouchers", user_id="u")

        direct, plan, batch = handler.named(RECEIVED)
        assert direct["outcome"] == "truncated"
        assert direct["finish_reason"] == FINISH_REASON_LENGTH
        assert direct["completion_tokens"] == 4000
        # ماکِ ساده رشته برمی‌گرداند: finish_reason ندارد، پس «other».
        assert plan["outcome"] == batch["outcome"] == "other"
        assert plan["finish_reason"] is None

    @pytest.mark.parametrize(
        ("reason", "outcome"),
        [
            ("stop", "success"),
            ("length", "truncated"),
            ("content_filter", "other"),
            (None, "other"),
        ],
    )
    def test_outcome_mapping(self, reason, outcome):
        assert diag.outcome_of(reason) == outcome


# ── HTTP 504 ─────────────────────────────────────────────────────────────────


def _gateway_timeout(request_id: str = "rid-504") -> openai.InternalServerError:
    request = httpx.Request(
        "POST", "https://llm.example/v1/chat/completions", headers={"x-request-id": request_id}
    )
    response = httpx.Response(504, request=request, text="<html>SECRET-GATEWAY-BODY</html>")
    return openai.InternalServerError(
        "SECRET-ERROR-MESSAGE", response=response, body="SECRET-GATEWAY-BODY"
    )


class TestGatewayTimeout:
    def test_direct_504_is_logged_with_stage_and_elapsed_before_propagating(self, logs):
        config, handler = logs
        error = _gateway_timeout()
        with patch(_PATCH) as cls:
            cls.return_value.chat.side_effect = error
            with pytest.raises(openai.InternalServerError) as caught:
                _generator(config).generate("Add vouchers", user_id="u")

        assert caught.value is error  # همان شیء، بدونِ wrap
        (failed,) = handler.named(FAILED)
        assert failed["stage"] == "direct"
        assert isinstance(failed["elapsed_s"], float) and failed["elapsed_s"] >= 0
        assert failed["error_type"] == "InternalServerError"
        assert failed["http_status"] == 504
        assert failed["request_id"] == "rid-504"
        assert handler.named(RECEIVED) == []
        assert "SECRET" not in handler.dump()

    def test_a_504_in_an_expansion_batch_names_that_batch(self, logs):
        config, handler = logs
        error = _gateway_timeout()
        respond = _staged_responder([], planned=20)
        batch_calls = []

        def chat(**kwargs):
            if _stage(kwargs) == "details":
                batch_calls.append(1)
                if len(batch_calls) == 2:
                    raise error
            return respond(**kwargs)

        with patch(_PATCH) as cls:
            cls.return_value.chat.side_effect = chat
            with pytest.raises(openai.InternalServerError) as caught:
                _generator(config).generate("Add vouchers", user_id="u")

        assert caught.value is error
        (failed,) = handler.named(FAILED)
        assert _where(failed) == ("expand", 2, 3)
        assert failed["http_status"] == 504

    def test_a_504_in_the_plan_names_the_plan(self, logs):
        config, handler = logs
        error = _gateway_timeout()

        def chat(**kwargs):
            if _stage(kwargs) == "plan":
                raise error
            return LLMResponse(_PARTIAL, finish_reason=FINISH_REASON_LENGTH)

        with patch(_PATCH) as cls:
            cls.return_value.chat.side_effect = chat
            with pytest.raises(openai.InternalServerError):
                _generator(config).generate("Add vouchers", user_id="u")

        (failed,) = handler.named(FAILED)
        assert _where(failed) == ("plan", None, None)

    def test_error_metadata_without_a_request(self):
        meta = diag.error_metadata(ValueError("SECRET"))
        assert meta == {"error_type": "ValueError", "http_status": None, "request_id": None}


# ── حریمِ خصوصی ──────────────────────────────────────────────────────────────


def _sensitive_task() -> TaskInput:
    return TaskInput(
        title="SECRET-TITLE",
        description="SECRET-DESCRIPTION کوپن",
        comments=(TaskComment(author="SECRET-AUTHOR", created="2026", body="SECRET-COMMENT"),),
        attachments=(
            TaskAttachment(
                filename="SECRET-FILENAME.md",
                media_type="text/markdown",
                size=4096,
                status=ATTACHMENT_PROCESSED,
                content="SECRET-ATTACHMENT-TEXT",
                truncated=True,
            ),
            TaskAttachment(
                filename="SECRET-OTHER.exe",
                media_type="application/octet-stream",
                size=99,
                status=ATTACHMENT_UNSUPPORTED,
            ),
        ),
        source="jira",
        source_reference="SECRET-KEY-1",
    )


@pytest.fixture
def wire(monkeypatch):
    """LLMClientِ واقعی + MockTransport؛ هر درخواست ثبت و با پاسخِ مرحله جواب داده می‌شود."""
    monkeypatch.setattr("src.llm_client.LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setattr("src.llm_client.LLM_API_KEY", "sk-SECRET-API-KEY")
    monkeypatch.setattr("src.llm_client.LLM_MODEL", "test-model")
    monkeypatch.setattr("src.llm_client.obs", MagicMock(enabled=False))
    # retry بدونِ انتظار؛ تعداد و سیاستِ retry دست نمی‌خورد.
    monkeypatch.setattr(openai._base_client.SyncAPIClient, "_sleep_for_retry", lambda *a, **k: None)
    state = SimpleNamespace(requests=[], failures_before_success=0, always_fail=False)

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        if state.always_fail or len(state.requests) <= state.failures_before_success:
            return httpx.Response(504, json={"error": {"message": "SECRET-GATEWAY-BODY"}})
        body = json.loads(request.content)
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if "TEST CASE PLAN (COMPACT)" in system:
            return httpx.Response(200, json=_completion(json.dumps(_plan(3)), "stop"))
        if "TEST CASE DETAILS" in system:
            return httpx.Response(200, json=_completion(json.dumps(_details(_ids(user))), "stop"))
        return httpx.Response(200, json=_completion(json.dumps(_full(2)), "stop"))

    def make_client(user_id: str, *, verify: bool = True) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("src.llm_client._make_http_client", make_client)
    return state


class TestPrivacy:
    def test_logs_contain_no_prompt_jira_attachment_or_credential_text(self, logs, wire):
        config, handler = logs
        task = _sensitive_task()
        _generator(config).generate(
            task.to_task_description(),
            user_id="u",
            developed_services="SECRET-SERVICES",
            sources=sources_from_task_input(task),
        )

        dump = handler.dump()
        assert handler.named(SENT) and handler.named(ATTEMPT_ANSWERED)
        assert "SECRET" not in dump
        assert "Bearer" not in dump and "authorization" not in dump.lower()
        # اندازه‌ها هست، متن نیست.
        (composition,) = handler.named(COMPOSITION)
        assert composition["input_mode"] == "structured"
        assert composition["comments"] == 1
        assert composition["comments_chars"] == len("SECRET-COMMENT")
        assert composition["attachments_in_prompt"] == 1
        assert composition["attachment_chars"] == [len("SECRET-ATTACHMENT-TEXT")]
        assert composition["services_chars"] == len("SECRET-SERVICES")

    def test_task_input_composition_is_metadata_only(self):
        task = _sensitive_task()
        result = diag.task_input_composition(task)
        assert "SECRET" not in repr(result)
        assert result["comments"] == 1
        assert result["attachments"] == 2
        assert result["attachments_processed"] == 1
        assert result["attachments_truncated"] == 1
        assert result["attachment_details"] == [
            {
                "status": "processed",
                "file_bytes": 4096,
                "text_chars": 22,
                "text_bytes": 22,
                "truncated": True,
                "postman_requests": 0,
            },
            {
                "status": "unsupported",
                "file_bytes": 99,
                "text_chars": 0,
                "text_bytes": 0,
                "truncated": False,
                "postman_requests": 0,
            },
        ]
        assert result["description_bytes"] > result["description_chars"]
        assert result["task_chars"] == len(task.to_task_description())

    def test_postman_counts_come_from_the_summary(self):
        summary = PostmanSummary(
            text="x" * 50,
            collections=(CollectionSummary("SECRET-LABEL", 12, 10, 8, 2, 0),),
        )
        result = diag.input_composition(None, "task", "", summary)
        assert result["input_mode"] == "text"
        assert result["postman_collections"] == 1
        assert result["postman_requests_extracted"] == 10
        assert result["postman_requests_listed"] == 8
        assert result["postman_summary_chars"] == 50
        assert "SECRET" not in repr(result)


# ── retry و رفتارِ کلاینت ────────────────────────────────────────────────────


class TestRetryBehaviour:
    def test_task_analysis_makes_exactly_one_attempt_after_a_504(self, logs, wire):
        config, handler = logs
        wire.failures_before_success = 1  # تلاشِ دوم موفق می‌شد — ولی انجام نمی‌شود
        with pytest.raises(openai.InternalServerError):
            _generator(config).generate("Add vouchers", user_id="u")

        assert [r.headers["x-stainless-retry-count"] for r in wire.requests] == ["0"]
        started = handler.named(ATTEMPT_STARTED)
        answered = handler.named(ATTEMPT_ANSWERED)
        assert [(e["stage"], e["attempt"], e["max_attempts"]) for e in started] == [
            ("direct", 1, 1)
        ]
        assert [(e["attempt"], e["http_status"]) for e in answered] == [(1, 504)]
        (failed,) = handler.named(FAILED)
        assert failed["stage"] == "direct" and failed["http_status"] == 504

    def test_the_504_reaches_the_existing_ui_error_path(self, wire):
        import ui.pipeline as pipeline

        wire.always_fail = True
        result, error = pipeline.run_task_analysis("Add vouchers", "")

        assert result is None
        assert error.startswith("Task analysis failed: InternalServerError")
        assert len(wire.requests) == 1

    def test_other_agents_keep_the_sdk_default_of_three_attempts(self, wire):
        wire.always_fail = True
        client = LLMClient(user_id="u", agent_name="api_mapping")
        with pytest.raises(openai.InternalServerError):
            client.chat("hi", max_tokens=10)

        assert client._client.max_retries == openai.DEFAULT_MAX_RETRIES == 2
        assert [r.headers["x-stainless-retry-count"] for r in wire.requests] == ["0", "1", "2"]

    def test_only_task_analysis_passes_max_retries(self):
        import pathlib
        import re

        offenders = []
        for path in pathlib.Path("src").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for call in re.findall(r"LLMClient\(([^)]*)\)", text):
                if "max_retries" in call and path.name != "task_analysis.py":
                    offenders.append(str(path))
        assert offenders == []

    def test_the_timeout_is_unchanged_and_retries_are_per_client(self, wire):
        default = LLMClient(user_id="u", agent_name="scenario_analysis")
        none = LLMClient(user_id="u", agent_name="task_analysis", max_retries=0)
        assert default._client.max_retries == 2
        assert none._client.max_retries == 0
        assert default._client.timeout == none._client.timeout == openai.DEFAULT_TIMEOUT

    @pytest.mark.parametrize("value", [-1, True, 1.5, "0"])
    def test_an_invalid_retry_count_is_rejected(self, value, wire):
        with pytest.raises(ValueError, match="max_retries"):
            LLMClient(user_id="u", agent_name="x", max_retries=value)

    def test_the_httpx_client_is_still_built_with_only_headers_and_verify(self):
        with patch("src.llm_client.httpx.Client") as client_cls:
            llm_client._make_http_client("u", verify=False)
        assert set(client_cls.call_args.kwargs) == {"headers", "verify"}

    def test_hooks_are_silent_outside_a_diagnostic_stage(self, logs, wire):
        _, handler = logs
        response = LLMClient(user_id="u", agent_name="api_mapping").chat("hi", max_tokens=10)
        assert response.finish_reason == "stop"
        assert handler.named(ATTEMPT_STARTED) == handler.named(ATTEMPT_ANSWERED) == []

    def test_a_failing_logger_never_breaks_the_request(self, monkeypatch, wire):
        def broken():
            raise RuntimeError("logger down")

        monkeypatch.setattr("src.llm_client._response_logger", broken)
        client = LLMClient(user_id="u", agent_name="task_analysis")
        with llm_client.diagnostic_stage("direct"):
            # _log_response (DEBUG، قبلی) هم از همین logger است؛ فقط hookها را می‌سنجیم.
            response = client._client.chat.completions.create(
                model="test-model",
                messages=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "hi"},
                ],
            )
        assert response.choices[0].finish_reason == "stop"

    def test_existing_event_hooks_are_kept(self):
        def mine(_):
            return None

        client = httpx.Client(event_hooks={"request": [mine]})
        llm_client._attach_attempt_hooks(client, "task_analysis", 3)
        assert client.event_hooks["request"][0] is mine
        assert len(client.event_hooks["request"]) == 2
        assert len(client.event_hooks["response"]) == 1
        llm_client._attach_attempt_hooks(MagicMock(), "task_analysis", 3)  # بی‌اثر


# ── نتیجه‌ی تولید ────────────────────────────────────────────────────────────


class TestGenerationIsUnchanged:
    @pytest.mark.parametrize("planned", [3, 20])
    def test_results_match_with_and_without_diagnostic_logging(self, logs, planned):
        config, _ = logs
        results = []
        for debug_config in (DebugConfig.off(), config):
            calls = []
            with patch(_PATCH) as cls:
                cls.return_value.chat.side_effect = _staged_responder(calls, planned=planned)
                results.append(_generator(debug_config).generate("Add vouchers", user_id="u"))
        assert results[0] == results[1]
        assert len(results[0]["test_cases"]) == planned

    def test_chat_arguments_are_unchanged(self, logs):
        config, _ = logs
        with patch(_PATCH) as cls:
            cls.return_value.chat.return_value = json.dumps(_full(1))
            _generator(config).generate("Add vouchers", user_id="u")
        kwargs = cls.return_value.chat.call_args.kwargs
        assert set(kwargs) == {"user_message", "system_prompt", "temperature", "max_tokens"}
        assert kwargs["temperature"] == 0.1 and kwargs["max_tokens"] == 8192


# ── ترکیبِ TaskInput در pipeline ─────────────────────────────────────────────


class TestPipelineTaskInputComposition:
    def _state(self, task, text):
        return SimpleNamespace(
            task_import=SimpleNamespace(ok=True, task_input=task),
            input_value=lambda key: text,
        )

    def test_logged_when_step1_uses_the_imported_issue(self, logs):
        import ui.pipeline as pipeline

        config, handler = logs
        task = _sensitive_task()
        with patch.object(pipeline.DebugConfig, "from_env", return_value=config):
            pipeline._log_task_input_composition(self._state(task, task.to_task_description()))

        (record,) = handler.named("ترکیبِ issueِ Jira برای قدمِ اول")
        assert record["attachments_truncated"] == 1
        assert "SECRET" not in handler.dump()

    def test_not_logged_for_edited_or_manual_text(self, logs):
        import ui.pipeline as pipeline

        config, handler = logs
        with patch.object(pipeline.DebugConfig, "from_env", return_value=config):
            pipeline._log_task_input_composition(self._state(_sensitive_task(), "edited"))
            pipeline._log_task_input_composition(
                SimpleNamespace(task_import=None, input_value=lambda key: "manual")
            )
        assert handler.records == []

    def test_a_diagnostic_failure_is_swallowed(self):
        import ui.pipeline as pipeline

        broken = SimpleNamespace(input_value=lambda key: "x")  # task_import ندارد
        pipeline._log_task_input_composition(broken)
