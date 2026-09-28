"""تست‌های LLMClient."""

import pytest
from unittest.mock import MagicMock, patch
from src.llm_client import LLMClient


@pytest.fixture
def llm_client():
    """LLMClient با mock کامل — patch در طول کل تست فعال است."""
    with patch("src.llm_client.OpenAI"), \
         patch("src.llm_client._make_http_client"):
        client = LLMClient(user_id="test-user", agent_name="test-llm-client")
        mock_inner = MagicMock()
        client._client = mock_inner
        yield client, mock_inner


class TestLLMClientInit:
    def test_creates_successfully(self, llm_client):
        client, _ = llm_client
        assert client is not None

    def test_model_attribute_is_string(self, llm_client):
        client, _ = llm_client
        assert isinstance(client.model, str)


class TestLLMClientChat:
    def test_chat_returns_content(self, llm_client):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value.choices[0].message.content = "response"
        assert client.chat("hello") == "response"

    def test_chat_none_content_returns_empty_string(self, llm_client):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value.choices[0].message.content = None
        assert client.chat("hello") == ""

    def test_chat_calls_create(self, llm_client):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value.choices[0].message.content = "ok"
        client.chat("question")
        assert mock_inner.chat.completions.create.called

    def test_chat_passes_temperature(self, llm_client):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value.choices[0].message.content = "ok"
        client.chat("hello", temperature=0.1)
        assert mock_inner.chat.completions.create.call_args[1]["temperature"] == 0.1

    def test_chat_passes_max_tokens(self, llm_client):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value.choices[0].message.content = "ok"
        client.chat("hello", max_tokens=512)
        assert mock_inner.chat.completions.create.call_args[1]["max_tokens"] == 512


class TestLLMClientStreamChat:
    def _chunk(self, content):
        c = MagicMock()
        c.choices[0].delta.content = content
        return c

    # todo: fix test
    def test_stream_chat_yields_chunks(self, llm_client):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value = [
            self._chunk("hello"),
            self._chunk(" world"),
            self._chunk(None),
        ]
        assert list(client.stream_chat("hi")) == ["hello", " world"]

    def test_stream_chat_skips_none_content(self, llm_client):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value = [self._chunk(None)]
        assert list(client.stream_chat("hi")) == []

    def test_stream_chat_empty_response(self, llm_client):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value = []
        assert list(client.stream_chat("hi")) == []

    def test_stream_chat_calls_with_stream_true(self, llm_client):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value = []
        list(client.stream_chat("hi"))
        assert mock_inner.chat.completions.create.call_args[1].get("stream") is True

    # todo: fix test
    def test_stream_chat_single_chunk(self, llm_client):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value = [self._chunk("only one")]
        assert list(client.stream_chat("hi")) == ["only one"]