"""
تست‌های شمارشِ مصرفِ توکن — src/token_usage.py و اتصالِ آن در LLMClient

اینجا دقیقاً همان قراردادی سنجیده می‌شود که سایدبار روی آن حساب می‌کند:

  * اعداد فقط از فراداده‌ی ارائه‌دهنده می‌آیند (هیچ تخمینی از متن نیست)؛
  * usage نبودن باعثِ خطا نمی‌شود و با صفر قاطی نمی‌شود؛
  * هر فراخوانیِ واقعیِ ``chat`` دقیقاً یک بار گزارش می‌شود.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.llm_client import LLMClient
from src.token_usage import TokenUsage, as_int, collect_usage, report_usage


def _provider_response(
    content: str = "{}",
    *,
    prompt: int | None = 10,
    completion: int | None = 2,
    total: int | None = 12,
    cached: int | None = None,
    finish_reason: str = "stop",
) -> SimpleNamespace:
    """پاسخِ ارائه‌دهنده با همان شکلی که SDK برمی‌گرداند (بدونِ نیاز به شبکه)."""
    details = None if cached is None else SimpleNamespace(cached_tokens=cached)
    usage = None
    if prompt is not None or completion is not None or total is not None:
        usage = SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            prompt_tokens_details=details,
        )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        ],
        usage=usage,
    )


@pytest.fixture
def create():
    """OpenAI و observability را قطعی می‌کند؛ خروجی: تابعِ create قابلِ تنظیم."""
    with patch("src.llm_client.OpenAI") as mock_openai, patch(
        "src.llm_client._make_http_client"
    ), patch("src.llm_client.obs", MagicMock(enabled=False)):
        yield mock_openai.return_value.chat.completions.create


def _client() -> LLMClient:
    return LLMClient(user_id="user-1", agent_name="token-usage-test")


class TestExtractingUsage:
    def test_all_reported_fields_are_captured(self, create):
        create.return_value = _provider_response(
            prompt=33142, completion=7382, total=40524, cached=3592
        )

        response = _client().chat("hello")

        assert response.prompt_tokens == 33142
        assert response.completion_tokens == 7382
        assert response.total_tokens == 40524
        assert response.cached_tokens == 3592
        assert TokenUsage.from_response(response) == TokenUsage(
            calls=1,
            prompt_tokens=33142,
            completion_tokens=7382,
            total_tokens=40524,
            cached_tokens=3592,
        )

    def test_a_response_without_usage_is_counted_but_not_guessed(self, create):
        create.return_value = _provider_response(prompt=None, completion=None, total=None)

        usage = TokenUsage.from_response(_client().chat("hello"))

        assert usage == TokenUsage(calls=1)
        assert usage.prompt_tokens is None
        assert usage.total_tokens is None

    def test_total_is_summed_from_reported_components_only(self):
        report = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=None)

        assert TokenUsage.from_response(report).total_tokens == 150

        partial = SimpleNamespace(prompt_tokens=100, completion_tokens=None, total_tokens=None)
        assert TokenUsage.from_response(partial).total_tokens is None

    def test_cached_tokens_are_read_from_prompt_details(self):
        response = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=1,
            total_tokens=11,
            prompt_tokens_details=SimpleNamespace(cached_tokens=7),
        )

        assert TokenUsage.from_response(response).cached_tokens == 7

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(12, 12), (0, 0), (None, None), (True, None), ("12", None), (1.5, None)],
    )
    def test_only_real_integers_are_accepted(self, value, expected):
        assert as_int(value) == expected


class TestAggregation:
    def test_calls_and_tokens_are_summed(self):
        first = TokenUsage(1, 100, 10, 110, 5)
        second = TokenUsage(1, 200, 20, 220, None)

        assert first.combine(second) == TokenUsage(2, 300, 30, 330, 5)

    def test_unreported_values_stay_unreported(self):
        combined = TokenUsage(1).combine(TokenUsage(1))

        assert combined == TokenUsage(calls=2)
        assert combined.prompt_tokens is None

    def test_a_zero_total_is_not_confused_with_a_missing_one(self):
        combined = TokenUsage(1, 0, 0, 0).combine(TokenUsage(1, 5, 5, 10))

        assert combined == TokenUsage(2, 5, 5, 10)


class TestReportingExactlyOnce:
    def test_every_chat_call_is_reported_once(self, create):
        create.side_effect = [
            _provider_response(prompt=10, completion=1, total=11),
            _provider_response(prompt=20, completion=2, total=22),
        ]
        records: list[TokenUsage] = []

        with collect_usage(records.append):
            client = _client()
            client.chat("first")
            client.chat("second")

        assert [record.calls for record in records] == [1, 1]
        assert [record.prompt_tokens for record in records] == [10, 20]

    def test_no_collector_means_no_error_and_no_report(self, create):
        create.return_value = _provider_response()

        # بدونِ collect_usage هیچ گیرنده‌ای نیست؛ فراخوانی باید عادی بماند.
        response = _client().chat("hello")

        assert str(response) == "{}"

    def test_the_outer_collector_is_restored_after_a_nested_scope(self):
        outer: list[TokenUsage] = []
        inner: list[TokenUsage] = []

        with collect_usage(outer.append):
            with collect_usage(inner.append):
                report_usage(SimpleNamespace(prompt_tokens=1, completion_tokens=1))
            report_usage(SimpleNamespace(prompt_tokens=2, completion_tokens=2))

        assert [item.prompt_tokens for item in inner] == [1]
        assert [item.prompt_tokens for item in outer] == [2]

    def test_reporting_outside_a_scope_is_a_no_op(self):
        report_usage(SimpleNamespace(prompt_tokens=1))

        # هیچ استثنایی رخ نمی‌دهد و وضعیتِ سراسری هم ساخته نمی‌شود.
        records: list[TokenUsage] = []
        with collect_usage(records.append):
            report_usage(SimpleNamespace(prompt_tokens=1))
        assert len(records) == 1
