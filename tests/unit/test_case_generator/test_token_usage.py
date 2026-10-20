"""
تستِ یکپارچه‌ی شمارشِ مصرف در یک قدمِ واقعی — قدم دوم (نگاشتِ API)

این‌جا ``LLMClient`` واقعی اجرا می‌شود و فقط لایه‌ی شبکه (OpenAI) ماک است؛ پس
مسیرِ «فراخوانی → گزارشِ مصرف» همان مسیری است که در برنامه اجرا می‌شود.
هدف: نشان دادنِ این‌که دسته‌ها و تلاشِ دوباره‌ی JSONِ نامعتبر هرکدام دقیقاً یک
بار شمرده می‌شوند و خطای پایانی مصرفِ انجام‌شده را از بین نمی‌برد.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.agents.test_case_generator.api_discovery import DiscoveredApi, DiscoveredService
from src.agents.test_case_generator.api_mapping import ApiMappingAgent, ApiMappingError
from src.token_usage import TokenUsage, collect_usage

_UPDATED_API = {
    "method": "PUT",
    "path": "/admin/voucher/{id}",
    "operation_id": "updateVoucher",
}


def _service() -> DiscoveredService:
    return DiscoveredService(
        name="admin",
        source_url="https://host/swagger.json",
        base_url="https://podium-admin.sandpod.ir",
        title="Admin API",
        version="1.0.0",
        apis=[
            DiscoveredApi(
                "PUT",
                "/admin/voucher/{id}",
                operation_id="updateVoucher",
                summary="Update voucher",
            )
        ],
    )


def _cases(count: int = 19) -> list[dict]:
    return [
        {
            "id": f"TC-{index:03d}",
            "title": f"Update voucher number {index}",
            "type": "positive",
            "priority": "high",
            "steps": [f"Update voucher {index}"],
            "expected_result": "The voucher is updated.",
            "related_service": None,
        }
        for index in range(1, count + 1)
    ]


def _valid_payload(case_ids: list[str]) -> str:
    return json.dumps(
        {
            "mappings": [
                {
                    "test_case_id": case_id,
                    "api": dict(_UPDATED_API),
                    "confidence": "high",
                    "reason": "The operation updates the voucher.",
                    "clarification": "",
                }
                for case_id in case_ids
            ],
            "clarifications": [],
        }
    )


_MALFORMED = (
    '{"mappings": [{"test_case_id": "TC-011", "api": {"method": "PUT", '
    '"path": "/admin/voucher/{id}", "operation_id": "updateVoucher"}, '
    '"confidence": "high", "reason": The update operation}, "clarifications": []}'
)


def _provider_response(content: str, prompt: int, completion: int) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            prompt_tokens_details=None,
        ),
    )


@pytest.fixture
def provider():
    """OpenAI ماک‌شده؛ خروجی: صفِ پاسخ‌های ارائه‌دهنده."""
    with patch("src.llm_client.OpenAI") as mock_openai, patch(
        "src.llm_client._make_http_client"
    ), patch("src.llm_client.obs", MagicMock(enabled=False)):
        create = mock_openai.return_value.chat.completions.create
        yield create


def _all_ids() -> list[str]:
    return [case["id"] for case in _cases()]


class TestBatchesAndRetriesAreCountedOnce:
    def test_three_calls_produce_three_usage_records(self, provider):
        ids = _all_ids()
        provider.side_effect = [
            _provider_response(_valid_payload(ids[:10]), 100, 10),
            _provider_response(_MALFORMED, 200, 20),
            _provider_response(_valid_payload(ids[10:]), 300, 30),
        ]
        records: list[TokenUsage] = []

        with collect_usage(records.append):
            mappings, _ = ApiMappingAgent(batch_size=10).map_test_cases(
                _cases(), [_service()], "user-1"
            )

        # دسته‌ی اول + پاسخِ نامعتبر + تلاشِ دوباره = سه فراخوانی، هرکدام یک بار.
        assert len(records) == 3
        assert [record.calls for record in records] == [1, 1, 1]

        aggregate = TokenUsage()
        for record in records:
            aggregate = aggregate.combine(record)
        assert aggregate == TokenUsage(3, 600, 60, 660)

        # نتیجه هم کامل و به ترتیب است — شمارش چیزی را از نتیجه کم نکرده.
        assert [mapping["test_case_id"] for mapping in mappings] == ids

    def test_a_failed_step_keeps_the_usage_of_its_calls(self, provider):
        ids = _all_ids()
        provider.side_effect = [
            _provider_response(_valid_payload(ids[:10]), 100, 10),
            _provider_response(_MALFORMED, 200, 20),
            _provider_response(_MALFORMED, 300, 30),
        ]
        records: list[TokenUsage] = []

        with pytest.raises(ApiMappingError):
            with collect_usage(records.append):
                ApiMappingAgent(batch_size=10).map_test_cases(
                    _cases(), [_service()], "user-1"
                )

        assert len(records) == 3
        assert sum(record.prompt_tokens for record in records) == 600
        assert sum(record.calls for record in records) == 3
