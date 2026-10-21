"""تست‌های تنظیمِ ``LLM_SSL_VERIFY`` — فقط کلاینتِ LLM، نه Embedding.

هدف: تأییدِ این‌که پیش‌فرض دست‌نخورده (تأییدِ گواهی روشن) است، فقط مقدارِ
صریحِ false آن را خاموش می‌کند، مقدارِ نامعتبر هرگز بی‌صدا خاموش نمی‌کند، و
Embedding و فراخوان‌های قبلیِ کارخانه‌ی HTTP رفتارِ خود را حفظ می‌کنند.
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import NotFoundError, OpenAI

from src import config as config_module
from src.config import LLM_BASE_URL
from src.llm_client import LLMClient, _make_http_client

_ENV_KEY = "LLM_SSL_VERIFY"


def _read_llm_ssl_verify(value: str | None) -> bool:
    """config را با مقدارِ محیطیِ مشخص می‌خواند و بعد به حالتِ اول برمی‌گردد."""
    original = os.environ.get(_ENV_KEY)
    try:
        if value is None:
            os.environ.pop(_ENV_KEY, None)
        else:
            os.environ[_ENV_KEY] = value
        importlib.reload(config_module)
        return config_module.LLM_SSL_VERIFY
    finally:
        if original is None:
            os.environ.pop(_ENV_KEY, None)
        else:
            os.environ[_ENV_KEY] = original
        importlib.reload(config_module)


class TestConfigParsing:
    def test_absent_keeps_verification_enabled(self):
        assert _read_llm_ssl_verify(None) is True

    def test_empty_keeps_verification_enabled(self):
        assert _read_llm_ssl_verify("") is True

    def test_true_keeps_verification_enabled(self):
        assert _read_llm_ssl_verify("true") is True

    def test_false_disables_verification(self):
        assert _read_llm_ssl_verify("false") is False

    @pytest.mark.parametrize("value", ["0", "no", "FALSE", " False "])
    def test_recognized_false_values_disable_verification(self, value):
        assert _read_llm_ssl_verify(value) is False

    @pytest.mark.parametrize("value", ["maybe", "enabled", "disabled", "2"])
    def test_an_invalid_value_raises_instead_of_disabling_verification(self, value):
        with pytest.raises(ValueError, match="Invalid boolean value"):
            _read_llm_ssl_verify(value)


class TestHttpClientFactory:
    def test_existing_callers_keep_verification_enabled(self):
        """فراخوانِ تک‌آرگومانیِ قبلی نباید رفتارش عوض شود."""
        with patch("src.llm_client.httpx.Client") as client_cls:
            _make_http_client("user-1")

        assert client_cls.call_args.kwargs["verify"] is True

    def test_headers_are_preserved(self):
        with patch("src.llm_client.httpx.Client") as client_cls:
            _make_http_client("user-1")

        headers = client_cls.call_args.kwargs["headers"]
        assert set(headers) == {"x-client-id", "x-user-id", "x-request-id"}
        assert headers["x-user-id"] == "user-1"

    def test_only_the_verify_setting_changes(self):
        """هیچ تنظیمِ دیگری (timeout، proxy، …) نه اضافه و نه حذف نشده است."""
        with patch("src.llm_client.httpx.Client") as client_cls:
            _make_http_client("user-1", verify=False)

        kwargs = client_cls.call_args.kwargs
        assert kwargs["verify"] is False
        assert set(kwargs) == {"headers", "verify"}


class TestLlmClientWiring:
    def test_the_default_setting_keeps_verification_enabled(self, monkeypatch):
        monkeypatch.setattr("src.llm_client.LLM_SSL_VERIFY", True)

        with patch("src.llm_client.OpenAI"), patch(
            "src.llm_client._make_http_client"
        ) as make_client:
            LLMClient(user_id="user-1", agent_name="test")

        make_client.assert_called_once_with("user-1", verify=True)

    def test_disabling_the_setting_reaches_the_http_client(self, monkeypatch):
        monkeypatch.setattr("src.llm_client.LLM_SSL_VERIFY", False)

        with patch("src.llm_client.OpenAI"), patch(
            "src.llm_client._make_http_client"
        ) as make_client:
            LLMClient(user_id="user-1", agent_name="test")

        make_client.assert_called_once_with("user-1", verify=False)

    @pytest.mark.parametrize("verify", [True, False])
    def test_the_configured_base_url_reaches_the_sdk_unchanged(self, monkeypatch, verify):
        """هیچ نرمال‌سازی/دستکاری‌ای روی ``LLM_BASE_URL`` انجام نمی‌شود."""
        monkeypatch.setattr("src.llm_client.LLM_SSL_VERIFY", verify)

        with patch("src.llm_client.OpenAI") as openai_cls, patch(
            "src.llm_client._make_http_client"
        ):
            LLMClient(user_id="user-1", agent_name="test")

        assert openai_cls.call_args.kwargs["base_url"] == LLM_BASE_URL


class TestEmbeddingIsUnaffected:
    def test_embedding_http_client_keeps_its_previous_configuration(self, monkeypatch):
        monkeypatch.setattr("src.llm_client.LLM_SSL_VERIFY", False)
        from src import embedding_client

        with patch("src.embedding_client.httpx.Client") as client_cls:
            embedding_client._make_http_client("user-2")

        kwargs = client_cls.call_args.kwargs
        # کارخانه‌ی Embedding اصلاً verify نمی‌گیرد؛ پیش‌فرضِ httpx یعنی True.
        assert kwargs.get("verify", True) is True
        assert set(kwargs) == {"headers"}

    def test_the_embedding_client_still_builds_a_verifying_client(self):
        from src.embedding_client import EmbeddingClient

        with patch("src.embedding_client.OpenAI"), patch(
            "src.embedding_client.httpx.Client"
        ) as client_cls:
            EmbeddingClient(user_id="user-2")

        assert client_cls.call_args.kwargs.get("verify", True) is True


def _chat_completion_payload() -> dict:
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


def _openai_with_transport(base_url: str, handler) -> OpenAI:
    """OpenAI واقعی با transport ماک — هیچ درخواستِ شبکه‌ای انجام نمی‌شود."""
    return OpenAI(
        base_url=base_url,
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


class TestVerifyDoesNotChangeTheRequestUrl:
    """``LLM_SSL_VERIFY`` فقط TLS را عوض می‌کند؛ URL و method را نه.

    مسیرِ ``/chat/completions`` را خودِ OpenAI SDK به base URL می‌چسباند
    (``chat.completions.create`` → ``"/chat/completions"``). خطای HTML 404
    نشان می‌داد درخواست به همان مسیرِ استانداردِ SDK رفته است؛ خاموش‌کردنِ
    تأییدِ گواهی فقط اجازه داد درخواست از مرحله‌ی TLS رد شود.
    """

    @pytest.mark.parametrize("verify", [True, False])
    def test_verify_does_not_change_the_final_request_url(self, monkeypatch, verify):
        monkeypatch.setattr("src.llm_client.LLM_SSL_VERIFY", verify)
        seen_verify = []
        seen_url = {}

        def handler(request):
            seen_url["url"] = str(request.url)
            seen_url["method"] = request.method
            return httpx.Response(200, json=_chat_completion_payload())

        def make_client(user_id, *, verify=True):
            seen_verify.append(verify)
            return httpx.Client(transport=httpx.MockTransport(handler))

        with patch("src.llm_client.obs", MagicMock(enabled=False)), patch(
            "src.llm_client._make_http_client", side_effect=make_client
        ):
            client = LLMClient(user_id="user-1", agent_name="test")
            raw = client.chat(user_message="hi", system_prompt="sys")

        assert seen_verify == [verify]
        assert seen_url["method"] == "POST"
        assert seen_url["url"] == LLM_BASE_URL.rstrip("/") + "/chat/completions"
        assert raw == "ok"

    @pytest.mark.parametrize(
        ("base_url", "expected"),
        [
            (
                "https://aifa-chatbot.dev.dotin.ir/",
                "https://aifa-chatbot.dev.dotin.ir/chat/completions",
            ),
            (
                "https://aifa-chatbot.dev.dotin.ir/v1",
                "https://aifa-chatbot.dev.dotin.ir/v1/chat/completions",
            ),
        ],
    )
    def test_the_base_url_alone_decides_the_api_path(self, base_url, expected):
        urls = []

        def handler(request):
            urls.append(str(request.url))
            return httpx.Response(200, json=_chat_completion_payload())

        client = _openai_with_transport(base_url, handler)
        client.chat.completions.create(
            model="developer-model",
            messages=[{"role": "user", "content": "hi"}],
        )

        assert urls == [expected]

    @pytest.mark.parametrize("verify", [True, False])
    def test_the_intended_base_url_is_sent_unchanged(self, monkeypatch, verify):
        """پایان‌به‌پایان: همان base URLِ پیکربندی‌شده، همان مسیرِ SDK، همان method."""
        monkeypatch.setattr(
            "src.llm_client.LLM_BASE_URL", "https://aifa-chatbot.dev.dotin.ir/"
        )
        monkeypatch.setattr("src.llm_client.LLM_SSL_VERIFY", verify)
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["method"] = request.method
            return httpx.Response(200, json=_chat_completion_payload())

        def make_client(user_id, *, verify=True):
            return httpx.Client(transport=httpx.MockTransport(handler))

        with patch("src.llm_client.obs", MagicMock(enabled=False)), patch(
            "src.llm_client._make_http_client", side_effect=make_client
        ):
            client = LLMClient(user_id="user-1", agent_name="test")
            client.chat(user_message="hi", system_prompt="sys")

        assert seen == {
            "url": "https://aifa-chatbot.dev.dotin.ir/chat/completions",
            "method": "POST",
        }

    def test_an_html_404_is_surfaced_as_not_found_error(self):
        """توضیحِ مشاهده: بدنه‌ی HTML همان چیزی است که در پیامِ خطا دیده شد."""
        html = (
            "<!DOCTYPE html><html><head>"
            "<title>Token Usage Dashboard</title>"
            "</head></html>"
        )

        def handler(request):
            return httpx.Response(
                404, headers={"content-type": "text/html"}, text=html
            )

        client = _openai_with_transport("https://aifa-chatbot.dev.dotin.ir/", handler)

        with pytest.raises(NotFoundError) as excinfo:
            client.chat.completions.create(
                model="developer-model",
                messages=[{"role": "user", "content": "hi"}],
            )

        assert "Token Usage Dashboard" in str(excinfo.value)

