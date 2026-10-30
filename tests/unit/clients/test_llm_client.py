"""تست‌های LLMClient."""

import pytest
from unittest.mock import MagicMock, patch
from src.llm_client import LLMClient
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage
from src.debug import DebugConfig
from src.debug.levels import DebugLevel


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



class TestResponseLogging:
    """پاسخ دیگر چاپ نمی‌شود؛ فقط فراداده‌ی امن لاگ می‌شود."""

    @staticmethod
    def _completion() -> ChatCompletion:
        return ChatCompletion(
            id="chatcmpl-123",
            object="chat.completion",
            created=0,
            model="test-model",
            choices=[
                Choice(
                    index=0,
                    finish_reason="stop",
                    message=ChatCompletionMessage(
                        role="assistant",
                        content='{"answer": "VISIBLE-CONTENT-42"}',
                        reasoning="PRIVATE-REASONING-99",
                    ),
                )
            ],
            usage=CompletionUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )

    def test_the_response_id_is_kept(self, llm_client):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value = self._completion()

        assert client.chat("hi").response_id == "chatcmpl-123"

    def test_a_missing_or_non_string_id_becomes_empty(self, llm_client):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value.choices[0].message.content = "x"

        assert client.chat("hi").response_id == ""

    def test_nothing_is_printed(self, llm_client, capsys, memory_handler):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value = self._completion()
        logger = DebugConfig(global_level=DebugLevel.TRACE, handlers=[memory_handler])
        with patch("src.llm_client._response_logger", return_value=logger.get_logger("llm_client")):
            client.chat("hi")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "PRIVATE-REASONING-99" not in captured.err

    def test_only_safe_metadata_is_logged(self, llm_client, memory_handler):
        client, mock_inner = llm_client
        mock_inner.chat.completions.create.return_value = self._completion()
        logger = DebugConfig(global_level=DebugLevel.TRACE, handlers=[memory_handler])
        with patch("src.llm_client._response_logger", return_value=logger.get_logger("llm_client")):
            client.chat("SECRET-PROMPT-7", system_prompt="SECRET-SYSTEM-8", max_tokens=64)

        (record,) = memory_handler.records
        assert record.extra == {
            "model": client.model,
            "agent": "test-llm-client",
            "response_id": "chatcmpl-123",
            "finish_reason": "stop",
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "max_tokens": 64,
            "content_chars": len('{"answer": "VISIBLE-CONTENT-42"}'),
        }
        logged = f"{record.message} {record.extra}"
        for secret in ("PRIVATE-REASONING-99", "VISIBLE-CONTENT-42", "SECRET-PROMPT-7", "SECRET-SYSTEM-8"):
            assert secret not in logged
