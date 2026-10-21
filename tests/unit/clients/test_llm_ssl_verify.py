"""تست‌های تنظیمِ ``LLM_SSL_VERIFY`` — فقط کلاینتِ LLM، نه Embedding.

هدف: تأییدِ این‌که پیش‌فرض دست‌نخورده (تأییدِ گواهی روشن) است، فقط مقدارِ
صریحِ false آن را خاموش می‌کند، مقدارِ نامعتبر هرگز بی‌صدا خاموش نمی‌کند، و
Embedding و فراخوان‌های قبلیِ کارخانه‌ی HTTP رفتارِ خود را حفظ می‌کنند.
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest

from src import config as config_module
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
