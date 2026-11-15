"""max_tokens اختیاری در LLMClient — بدنه‌ی واقعیِ سریال‌شده‌ی درخواست بررسی می‌شود.

``max_tokens=None`` (صریح) یعنی کلید اصلاً در بدنه نباشد — نه ``"max_tokens": null``.
فراخوانی بدونِ این آرگومان همچنان ۱۰۲۴ می‌فرستد و عددِ صریح دست‌نخورده می‌رود.

درخواست‌ها با OpenAI SDKِ واقعی و ``httpx.MockTransport`` ساخته می‌شوند؛ هیچ
درخواستی به شبکه نمی‌رود و هیچ مقداری از .env خوانده نمی‌شود.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.llm_client import FINISH_REASON_LENGTH, LLMClient, LLMResponse

_COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "ok"},
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


def _completion(finish_reason: str = "stop") -> dict:
    body = json.loads(json.dumps(_COMPLETION))
    body["choices"][0]["finish_reason"] = finish_reason
    return body


def _sse_stream() -> str:
    chunk = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}],
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"


@pytest.fixture
def harness(monkeypatch):
    """کلاینتِ واقعی با transportِ ماک؛ بدنه‌ی JSONِ هر درخواست ثبت می‌شود."""
    monkeypatch.setattr("src.llm_client.LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setattr("src.llm_client.LLM_API_KEY", "test-key")
    monkeypatch.setattr("src.llm_client.LLM_MODEL", "test-model")
    bodies: list[dict] = []
    state = {"finish_reason": "stop"}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=_sse_stream(),
            )
        return httpx.Response(200, json=_completion(state["finish_reason"]))

    def make_client(user_id: str, *, verify: bool = True) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    def build(*, observed: MagicMock | None = None) -> LLMClient:
        obs = observed if observed is not None else MagicMock(enabled=False)
        monkeypatch.setattr("src.llm_client.obs", obs)
        with patch("src.llm_client._make_http_client", side_effect=make_client):
            return LLMClient(user_id="user-1", agent_name="test")

    class Harness:
        pass

    h = Harness()
    h.bodies = bodies
    h.state = state
    h.build = build
    return h


def _observed() -> MagicMock:
    obs = MagicMock(enabled=True)
    obs.observation.return_value.__enter__.return_value = MagicMock()
    return obs


class TestRawPath:
    def test_none_omits_the_key_from_the_request_body(self, harness):
        harness.build().chat("hi", system_prompt="sys", max_tokens=None)

        assert len(harness.bodies) == 1
        assert "max_tokens" not in harness.bodies[0]
        # فقط سقف حذف می‌شود؛ بقیه‌ی بدنه همان است.
        assert harness.bodies[0]["model"] == "test-model"
        assert harness.bodies[0]["messages"][1] == {"role": "user", "content": "hi"}

    def test_none_is_never_sent_as_json_null(self, harness):
        harness.build().chat("hi", max_tokens=None)

        assert harness.bodies[0].get("max_tokens", "absent") == "absent"

    def test_an_integer_is_sent_unchanged(self, harness):
        harness.build().chat("hi", max_tokens=12345)

        assert harness.bodies[0]["max_tokens"] == 12345

    def test_an_omitted_argument_still_sends_1024(self, harness):
        harness.build().chat("hi")

        assert harness.bodies[0]["max_tokens"] == 1024

    def test_the_response_records_that_no_limit_was_sent(self, harness):
        response = harness.build().chat("hi", max_tokens=None)

        assert isinstance(response, LLMResponse)
        assert response == "ok"
        assert response.max_tokens is None


class TestTruncationWithoutALimit:
    def test_finish_reason_length_is_still_detected(self, harness):
        harness.state["finish_reason"] = FINISH_REASON_LENGTH

        response = harness.build().chat("hi", max_tokens=None)

        assert response.truncated is True
        assert "max_tokens" not in harness.bodies[0]

    def test_the_message_blames_the_provider_and_invents_no_number(self, harness):
        harness.state["finish_reason"] = FINISH_REASON_LENGTH

        message = harness.build().chat("hi", max_tokens=None).truncation_message()

        assert "finish_reason='length'" in message
        assert "No max_tokens was sent" in message
        assert "not from this application" in message
        assert "None" not in message
        assert "max_tokens=" not in message

    def test_the_message_with_an_integer_limit_is_unchanged(self, harness):
        harness.state["finish_reason"] = FINISH_REASON_LENGTH

        message = harness.build().chat("hi", max_tokens=64).truncation_message()

        assert "max_tokens=64" in message
        assert "Raise LLM_MAX_OUTPUT_TOKENS" in message
        assert "No max_tokens was sent" not in message


class TestObservedPath:
    def test_none_omits_the_key_from_the_request_body(self, harness):
        harness.build(observed=_observed()).chat("hi", max_tokens=None)

        assert "max_tokens" not in harness.bodies[0]

    def test_none_is_left_out_of_langfuse_model_parameters(self, harness):
        obs = _observed()
        harness.build(observed=obs).chat("hi", temperature=0.2, max_tokens=None)

        parameters = obs.observation.call_args.kwargs["model_parameters"]
        assert parameters == {"temperature": 0.2}

    def test_an_integer_is_sent_and_recorded(self, harness):
        obs = _observed()
        harness.build(observed=obs).chat("hi", temperature=0.2, max_tokens=256)

        assert harness.bodies[0]["max_tokens"] == 256
        parameters = obs.observation.call_args.kwargs["model_parameters"]
        assert parameters == {"temperature": 0.2, "max_tokens": 256}

    def test_an_omitted_argument_still_sends_and_records_1024(self, harness):
        obs = _observed()
        harness.build(observed=obs).chat("hi")

        assert harness.bodies[0]["max_tokens"] == 1024
        assert obs.observation.call_args.kwargs["model_parameters"]["max_tokens"] == 1024

    def test_finish_reason_length_is_still_detected(self, harness):
        harness.state["finish_reason"] = FINISH_REASON_LENGTH

        response = harness.build(observed=_observed()).chat("hi", max_tokens=None)

        assert response.truncated is True


class TestStreamPath:
    def test_none_omits_the_key_from_the_request_body(self, harness):
        chunks = list(harness.build().stream_chat("hi", max_tokens=None))

        assert chunks == ["ok"]
        assert harness.bodies[0]["stream"] is True
        assert "max_tokens" not in harness.bodies[0]

    def test_an_integer_is_sent_unchanged(self, harness):
        list(harness.build().stream_chat("hi", max_tokens=77))

        assert harness.bodies[0]["max_tokens"] == 77

    def test_an_omitted_argument_still_sends_1024(self, harness):
        list(harness.build().stream_chat("hi"))

        assert harness.bodies[0]["max_tokens"] == 1024
