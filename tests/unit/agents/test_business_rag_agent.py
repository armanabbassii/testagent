"""تست‌های BusinessRAGAgent."""

import json
import pytest
from unittest.mock import MagicMock, patch
from src.agents.business_rag.agent import BusinessRAGAgent, _parse_filters
from src.vector_store.base import Document, SearchResult
from src.agents.state import AgentState


def _make_search_result(content: str, score: float = 0.9, metadata=None) -> SearchResult:
    doc = Document(content=content, metadata=metadata or {"source": "doc1"})
    return SearchResult(document=doc, score=score)


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.search.return_value = []
    return store


@pytest.fixture
def agent(mock_store):
    return BusinessRAGAgent(vector_store=mock_store, top_k=3)


# ── _parse_filters ────────────────────────────────────────────────────────────

class TestParseFilters:
    def test_valid_json_object(self):
        raw = json.dumps({"doc_type": "webservice", "categories": "payment"})
        result = _parse_filters(raw)
        assert result == {"doc_type": "webservice", "categories": "payment"}

    def test_empty_object_returns_empty_dict(self):
        assert _parse_filters("{}") == {}

    def test_strips_markdown_fences(self):
        raw = "```json\n{\"provider\": \"X\"}\n```"
        assert _parse_filters(raw) == {"provider": "X"}

    def test_invalid_json_returns_empty_dict(self):
        assert _parse_filters("not valid json {{{") == {}

    def test_non_dict_json_returns_empty_dict(self):
        assert _parse_filters("[1, 2, 3]") == {}

    def test_null_and_empty_values_dropped(self):
        raw = json.dumps({"doc_type": None, "categories": "", "provider": "X"})
        result = _parse_filters(raw)
        assert result == {"provider": "X"}


# ── extract_filters ────────────────────────────────────────────────────────────

class TestExtractFilters:
    def test_calls_llm_and_parses_result(self, agent):
        with patch("src.llm_client.LLMClient.chat",
                   return_value=json.dumps({"categories": "payment"})):
            result = agent.extract_filters("سرویس پرداخت می‌خوام", user_id="u1")
        assert result == {"categories": "payment"}

    def test_includes_known_categories_hint_in_prompt(self, agent):
        agent._known_categories = ["payment", "sms"]
        with patch("src.llm_client.LLMClient.chat") as mock_chat:
            mock_chat.return_value = "{}"
            agent.extract_filters("query", user_id="u1")
        system_prompt = mock_chat.call_args.kwargs.get("system_prompt", "")
        assert "payment" in system_prompt

    def test_includes_known_providers_hint_in_prompt(self, agent):
        agent._known_providers = ["ProviderX"]
        with patch("src.llm_client.LLMClient.chat") as mock_chat:
            mock_chat.return_value = "{}"
            agent.extract_filters("query", user_id="u1")
        system_prompt = mock_chat.call_args.kwargs.get("system_prompt", "")
        assert "ProviderX" in system_prompt

    def test_temperature_zero_for_deterministic_extraction(self, agent):
        with patch("src.llm_client.LLMClient.chat") as mock_chat:
            mock_chat.return_value = "{}"
            agent.extract_filters("query", user_id="u1")
        assert mock_chat.call_args.kwargs.get("temperature") == 0.0


# ── retrieve_filtered ────────────────────────────────────────────────────────

class TestRetrieveFiltered:
    def test_returns_formatted_documents(self, agent, mock_store):
        mock_store.search.return_value = [_make_search_result("service A", 0.9)]
        result = agent.retrieve_filtered("query", {"categories": "payment"})
        assert len(result) == 1
        assert result[0]["content"] == "service A"

    def test_applies_score_threshold(self, mock_store):
        mock_store.search.return_value = [
            _make_search_result("high", 0.9),
            _make_search_result("low", 0.1),
        ]
        agent = BusinessRAGAgent(vector_store=mock_store, score_threshold=0.5)
        result = agent.retrieve_filtered("query", {})
        assert len(result) == 1
        assert result[0]["content"] == "high"

    def test_fallback_when_filtered_empty(self, agent, mock_store):
        # اولین جستجو (با فیلتر) نتیجه خالی، دومین (بدون فیلتر) نتیجه دارد
        mock_store.search.side_effect = [[], [_make_search_result("fallback result", 0.8)]]
        result = agent.retrieve_filtered("query", {"provider": "X"})
        assert len(result) == 1
        assert result[0]["content"] == "fallback result"
        assert mock_store.search.call_count == 2

    def test_no_fallback_when_no_filters_given(self, agent, mock_store):
        mock_store.search.return_value = []
        result = agent.retrieve_filtered("query", {})
        assert result == []
        assert mock_store.search.call_count == 1

    def test_no_fallback_when_filtered_results_nonempty(self, agent, mock_store):
        mock_store.search.return_value = [_make_search_result("ok", 0.9)]
        agent.retrieve_filtered("query", {"provider": "X"})
        assert mock_store.search.call_count == 1

    def test_passes_filters_to_store_search(self, agent, mock_store):
        mock_store.search.return_value = [_make_search_result("x", 0.9)]
        agent.retrieve_filtered("query", {"provider": "X"})
        call_kwargs = mock_store.search.call_args.kwargs
        assert call_kwargs["filters"] == {"provider": "X"}

    def test_empty_filters_passed_as_none(self, agent, mock_store):
        mock_store.search.return_value = []
        agent.retrieve_filtered("query", {})
        call_kwargs = mock_store.search.call_args.kwargs
        assert call_kwargs["filters"] is None


# ── recommend ────────────────────────────────────────────────────────────────

class TestRecommend:
    def test_returns_answer_documents_and_filters(self, agent, mock_store):
        mock_store.search.return_value = [_make_search_result("service info", 0.9)]

        with patch("src.llm_client.LLMClient.chat") as mock_chat:
            mock_chat.side_effect = [
                json.dumps({"categories": "payment"}),  # extract_filters call
                "بهترین سرویس برای شما X است.",           # final answer call
            ]
            result = agent.recommend("سرویس پرداخت می‌خوام", user_id="u1")

        assert result["answer"] == "بهترین سرویس برای شما X است."
        assert len(result["documents"]) == 1
        assert result["filters_used"] == {"categories": "payment"}

    def test_context_included_in_final_prompt(self, agent, mock_store):
        mock_store.search.return_value = [_make_search_result("special service X", 0.9)]

        with patch("src.llm_client.LLMClient.chat") as mock_chat:
            mock_chat.side_effect = ["{}", "answer"]
            agent.recommend("query", user_id="u1")

        final_call_kwargs = mock_chat.call_args.kwargs
        assert "special service X" in final_call_kwargs.get("system_prompt", "")

    def test_no_documents_still_answers(self, agent, mock_store):
        mock_store.search.return_value = []
        with patch("src.llm_client.LLMClient.chat") as mock_chat:
            mock_chat.side_effect = ["{}", "چیزی پیدا نشد."]
            result = agent.recommend("query", user_id="u1")
        assert result["documents"] == []
        assert result["answer"] == "چیزی پیدا نشد."

    def test_saves_to_memory_when_provided(self, mock_store):
        mock_memory = MagicMock()
        mock_memory.search.return_value = []
        agent = BusinessRAGAgent(vector_store=mock_store, memory=mock_memory)
        mock_store.search.return_value = []

        with patch("src.llm_client.LLMClient.chat") as mock_chat:
            mock_chat.side_effect = ["{}", "answer"]
            agent.recommend("query", user_id="u1")

        mock_memory.save.assert_called_once()
        saved_item = mock_memory.save.call_args[0][0]
        assert saved_item.user_id == "u1"

    def test_no_memory_does_not_crash(self, agent, mock_store):
        mock_store.search.return_value = []
        with patch("src.llm_client.LLMClient.chat") as mock_chat:
            mock_chat.side_effect = ["{}", "answer"]
            result = agent.recommend("query", user_id="u1")
        assert result["answer"] == "answer"

    def test_memory_context_included_in_prompt(self, mock_store):
        mock_memory = MagicMock()
        history_result = MagicMock()
        history_result.item.content = "User asked about payment before"
        mock_memory.search.return_value = [history_result]
        agent = BusinessRAGAgent(vector_store=mock_store, memory=mock_memory)
        mock_store.search.return_value = []

        with patch("src.llm_client.LLMClient.chat") as mock_chat:
            mock_chat.side_effect = ["{}", "answer"]
            agent.recommend("query", user_id="u1")

        final_call_kwargs = mock_chat.call_args.kwargs
        assert "payment before" in final_call_kwargs.get("system_prompt", "")

    def test_memory_search_called_with_correct_user(self, mock_store):
        mock_memory = MagicMock()
        mock_memory.search.return_value = []
        agent = BusinessRAGAgent(vector_store=mock_store, memory=mock_memory)
        mock_store.search.return_value = []

        with patch("src.llm_client.LLMClient.chat") as mock_chat:
            mock_chat.side_effect = ["{}", "answer"]
            agent.recommend("query", user_id="specific-user")

        mock_memory.search.assert_called_once_with(query="query", user_id="specific-user", top_k=3)


# ── run() contract ─────────────────────────────────────────────────────────────

class TestRunNotImplemented:
    def test_run_raises_not_implemented(self, agent):
        state: AgentState = {
            "messages": [], "user_id": "u1", "metadata": {},
            "memory_context": [], "rag_context": [], "hitl_decision": None,
        }
        with pytest.raises(NotImplementedError):
            agent.run(state)

    def test_call_also_raises(self, agent):
        """__call__ از BaseAgent می‌آید و مستقیم run() را صدا می‌زند."""
        state: AgentState = {
            "messages": [], "user_id": "u1", "metadata": {},
            "memory_context": [], "rag_context": [], "hitl_decision": None,
        }
        with pytest.raises(NotImplementedError):
            agent(state)


# ── agent metadata ───────────────────────────────────────────────────────────

class TestAgentMetadata:
    def test_agent_name_is_business_rag(self, agent):
        assert agent.name == "business_rag"

    def test_system_prompt_loaded_from_file(self, agent):
        assert "business service recommendation" in agent.system_prompt.lower()

    def test_known_categories_default_empty(self, mock_store):
        agent = BusinessRAGAgent(vector_store=mock_store)
        assert agent._known_categories == []

    def test_known_providers_default_empty(self, mock_store):
        agent = BusinessRAGAgent(vector_store=mock_store)
        assert agent._known_providers == []

    def test_known_categories_property_returns_configured_list(self, mock_store):
        agent = BusinessRAGAgent(vector_store=mock_store, known_categories=["payment", "sms"])
        assert agent.known_categories == ["payment", "sms"]

    def test_known_providers_property_returns_configured_list(self, mock_store):
        agent = BusinessRAGAgent(vector_store=mock_store, known_providers=["ProviderX"])
        assert agent.known_providers == ["ProviderX"]

    def test_known_categories_property_returns_copy_not_reference(self, mock_store):
        """جهش دادن مقدار برگشتی نباید state داخلی agent را تغییر دهد."""
        agent = BusinessRAGAgent(vector_store=mock_store, known_categories=["payment"])
        result = agent.known_categories
        result.append("hacked")
        assert agent.known_categories == ["payment"]