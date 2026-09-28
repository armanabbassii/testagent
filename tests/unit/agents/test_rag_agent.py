"""تست‌های RAGAgent."""

import pytest
from unittest.mock import MagicMock, patch
from langchain_core.messages import HumanMessage
from src.agents.rag.agent import RAGAgent, _format_context
from src.vector_store.base import Document, SearchResult


def _make_state(query: str = "what is the policy?", rag_context=None) -> dict:
    return {
        "messages": [HumanMessage(content=query)],
        "user_id": "u1",
        "metadata": {},
        "memory_context": [],
        "rag_context": rag_context or [],
        "hitl_decision": None,
    }


def _make_search_result(content: str, score: float = 0.9, source: str = "doc1") -> SearchResult:
    doc = Document(content=content, metadata={"source": source})
    return SearchResult(document=doc, score=score)


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.search.return_value = []
    return store


# ── _format_context ────────────────────────────────────────────────────────────

class TestFormatContext:
    def test_empty_list_returns_no_documents_message(self):
        result = _format_context([])
        assert "No relevant documents" in result

    def test_single_document_formatted(self):
        docs = [{"content": "some text", "score": 0.9, "metadata": {"source": "doc1"}}]
        result = _format_context(docs)
        assert "some text" in result
        assert "doc1" in result

    def test_multiple_documents_numbered(self):
        docs = [
            {"content": "first", "score": 0.9, "metadata": {}},
            {"content": "second", "score": 0.8, "metadata": {}},
        ]
        result = _format_context(docs)
        assert "Document 1" in result
        assert "Document 2" in result

    def test_missing_source_shows_unknown(self):
        docs = [{"content": "text", "score": 0.5, "metadata": {}}]
        result = _format_context(docs)
        assert "unknown" in result


# ── retrieve ──────────────────────────────────────────────────────────────────

class TestRAGAgentRetrieve:
    def test_retrieve_calls_store_search(self, mock_store):
        agent = RAGAgent(vector_store=mock_store, top_k=3)
        agent.retrieve("my query")
        mock_store.search.assert_called_once_with(query="my query", top_k=3, filters=None)

    def test_retrieve_returns_formatted_documents(self, mock_store):
        mock_store.search.return_value = [_make_search_result("doc content", 0.9)]
        agent = RAGAgent(vector_store=mock_store)
        results = agent.retrieve("query")
        assert len(results) == 1
        assert results[0]["content"] == "doc content"
        assert results[0]["score"] == 0.9

    def test_retrieve_applies_score_threshold(self, mock_store):
        mock_store.search.return_value = [
            _make_search_result("high score", 0.9),
            _make_search_result("low score", 0.2),
        ]
        agent = RAGAgent(vector_store=mock_store, score_threshold=0.5)
        results = agent.retrieve("query")
        assert len(results) == 1
        assert results[0]["content"] == "high score"

    def test_retrieve_passes_filters(self, mock_store):
        agent = RAGAgent(vector_store=mock_store, filters={"project": "docs"})
        agent.retrieve("query")
        call_kwargs = mock_store.search.call_args.kwargs
        assert call_kwargs["filters"] == {"project": "docs"}

    def test_retrieve_empty_results(self, mock_store):
        mock_store.search.return_value = []
        agent = RAGAgent(vector_store=mock_store)
        results = agent.retrieve("query")
        assert results == []


# ── retrieve_and_answer (standalone) ─────────────────────────────────────────

class TestRetrieveAndAnswer:
    def test_returns_answer_and_documents(self, mock_store):
        mock_store.search.return_value = [_make_search_result("relevant info", 0.9)]
        agent = RAGAgent(vector_store=mock_store)

        with patch("src.llm_client.LLMClient.chat", return_value="The answer is X."):
            result = agent.retrieve_and_answer("what is X?", user_id="u1")

        assert result["answer"] == "The answer is X."
        assert len(result["documents"]) == 1

    def test_context_included_in_system_prompt(self, mock_store):
        mock_store.search.return_value = [_make_search_result("key fact", 0.9)]
        agent = RAGAgent(vector_store=mock_store)

        with patch("src.llm_client.LLMClient.chat") as mock_chat:
            mock_chat.return_value = "answer"
            agent.retrieve_and_answer("query", user_id="u1")

        system_prompt = mock_chat.call_args.kwargs.get("system_prompt", "")
        assert "key fact" in system_prompt

    def test_no_documents_still_answers(self, mock_store):
        mock_store.search.return_value = []
        agent = RAGAgent(vector_store=mock_store)

        with patch("src.llm_client.LLMClient.chat", return_value="I don't have info on that."):
            result = agent.retrieve_and_answer("unknown topic", user_id="u1")

        assert result["documents"] == []
        assert result["answer"] == "I don't have info on that."


# ── run (graph node) ─────────────────────────────────────────────────────────

class TestRAGAgentRun:
    def test_returns_rag_context_in_state(self, mock_store):
        mock_store.search.return_value = [_make_search_result("doc text", 0.9)]
        agent = RAGAgent(vector_store=mock_store)

        with patch("src.llm_client.LLMClient.chat", return_value="answer"):
            result = agent(_make_state("question"))

        assert "rag_context" in result
        assert len(result["rag_context"]) == 1

    def test_returns_ai_message(self, mock_store):
        mock_store.search.return_value = []
        agent = RAGAgent(vector_store=mock_store)

        with patch("src.llm_client.LLMClient.chat", return_value="my answer"):
            result = agent(_make_state())

        assert result["messages"][0].content == "my answer"
        assert result["messages"][0].name == "rag"

    def test_uses_build_system_prompt_with_memory(self, mock_store):
        """باید memory_context هم در prompt لحاظ شود (از BaseAgent)."""
        mock_store.search.return_value = []
        agent = RAGAgent(vector_store=mock_store)
        state = _make_state()
        state["memory_context"] = ["user is a developer"]

        with patch("src.llm_client.LLMClient.chat") as mock_chat:
            mock_chat.return_value = "answer"
            agent(state)

        system_prompt = mock_chat.call_args.kwargs.get("system_prompt", "")
        assert "developer" in system_prompt

    def test_agent_name_is_rag(self, mock_store):
        agent = RAGAgent(vector_store=mock_store)
        assert agent.name == "rag"