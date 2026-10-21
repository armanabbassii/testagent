"""پیامِ خطا وقتی endpointِ LLM به‌جای JSON، HTML برمی‌گرداند.

مشاهده‌ی واقعی: OpenAI SDK مسیرِ ``/chat/completions`` را به base URL
چسباند و سرویسِ مقصد به‌جای JSON، صفحه‌ی HTML برگرداند. انتظار: نوعِ خطا
(``NotFoundError``) و بدنه‌ی اصلی حفظ شود، ولی پیام کوتاه، بدونِ HTML و
بدونِ credentials باشد.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import NotFoundError, PermissionDeniedError

from src.llm_client import LLMClient

_HTML = (
    "<!DOCTYPE html><html><head>"
    "<title>Token Usage Dashboard</title>"
    "</head><body>404</body></html>"
)


def _payload() -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
    }


@pytest.fixture
def chat(monkeypatch):
    """``LLMClient.chat`` روی یک transportِ ماک — هیچ درخواستِ شبکه‌ای نیست.

    درخواست همان مسیری را می‌رود که SDK می‌سازد؛ فقط transport عوض شده است.
    """

    def run(
        response: httpx.Response,
        *,
        base_url: str = "https://llm.example/v1",
        verify: bool = False,
    ) -> str:
        monkeypatch.setattr("src.llm_client.LLM_BASE_URL", base_url)
        monkeypatch.setattr("src.llm_client.LLM_SSL_VERIFY", verify)

        def handler(request: httpx.Request) -> httpx.Response:
            return response

        def make_client(user_id: str, *, verify: bool = True) -> httpx.Client:
            return httpx.Client(transport=httpx.MockTransport(handler))

        with patch("src.llm_client.obs", MagicMock(enabled=False)), patch(
            "src.llm_client._make_http_client", side_effect=make_client
        ):
            client = LLMClient(user_id="user-1", agent_name="test")
            return client.chat(user_message="hi", system_prompt="sys")

    return run


class TestHtmlErrorResponses:
    def test_an_html_404_becomes_a_short_actionable_error(self, chat):
        response = httpx.Response(
            404, headers={"content-type": "text/html"}, text=_HTML
        )

        with pytest.raises(NotFoundError) as excinfo:
            chat(response, base_url="https://llm.example/", verify=False)

        message = str(excinfo.value)
        assert "HTTP 404" in message
        assert "text/html" in message
        assert "https://llm.example/chat/completions" in message
        assert "LLM_BASE_URL" in message
        assert "Token Usage Dashboard" not in message
        assert _HTML not in message

    def test_the_original_exception_and_body_are_preserved(self, chat):
        response = httpx.Response(
            404, headers={"content-type": "text/html"}, text=_HTML
        )

        with pytest.raises(NotFoundError) as excinfo:
            chat(response)

        cause = excinfo.value.__cause__
        assert isinstance(cause, NotFoundError)
        assert "Token Usage Dashboard" in str(cause)
        assert excinfo.value.body == _HTML

    def test_any_html_status_error_is_made_actionable(self, chat):
        response = httpx.Response(
            403, headers={"content-type": "text/html"}, text="<html>forbidden</html>"
        )

        with pytest.raises(PermissionDeniedError) as excinfo:
            chat(response)

        assert "HTTP 403" in str(excinfo.value)
        assert "text/html" in str(excinfo.value)

    def test_credentials_in_the_base_url_are_redacted(self, chat):
        response = httpx.Response(
            404, headers={"content-type": "text/html"}, text=_HTML
        )

        with pytest.raises(NotFoundError) as excinfo:
            chat(response, base_url="https://user:supersecret@llm.example/v1")

        message = str(excinfo.value)
        assert "supersecret" not in message
        assert "@llm.example" not in message
        assert "https://llm.example/v1/chat/completions" in message


class TestNonHtmlErrorsAreUnchanged:
    def test_a_json_404_keeps_the_sdk_message(self, chat):
        response = httpx.Response(
            404,
            headers={"content-type": "application/json"},
            json={"error": {"message": "no such route", "type": "not_found"}},
        )

        with pytest.raises(NotFoundError) as excinfo:
            chat(response)

        assert "no such route" in str(excinfo.value)
        assert "LLM_BASE_URL" not in str(excinfo.value)

    @pytest.mark.parametrize("verify", [True, False])
    def test_a_successful_response_is_unchanged(self, chat, verify):
        response = httpx.Response(200, json=_payload())

        assert chat(response, verify=verify) == "ok"
