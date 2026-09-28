"""
تست‌های integration برای جریان گراف multi-agent.

این تست‌ها LLM را mock می‌کنند — نیازی به سرور واقعی ندارند.
"""

import uuid
import pytest
from unittest.mock import patch, MagicMock
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.checkpoint.memory import MemorySaver

from src.agents.state import AgentState
from src.agents.graph import build_graph


@pytest.fixture
def checkpointer():
    return MemorySaver()


def _make_state(query: str, user_id: str = "test-user") -> AgentState:
    return {
        "messages": [HumanMessage(content=query)],
        "user_id": user_id,
        "metadata": {},
        "memory_context": [],
        "hitl_decision": None,
    }


def _config() -> dict:
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


@pytest.fixture
def mock_llm_chat():
    """mock برای LLMClient.chat — پاسخ‌های از پیش تعریف‌شده برمی‌گرداند."""
    responses = {
        "router": "research",
        "default": "This is a mocked LLM response.",
    }

    def _chat(self, user_message: str, system_prompt: str = "", **kwargs) -> str:
        if "routing" in system_prompt.lower() or "classify" in system_prompt.lower():
            return responses["router"]
        return responses["default"]

    return _chat


class TestGraphRouting:
    @patch("src.llm_client.LLMClient.chat")
    def test_graph_completes_without_error(self, mock_chat, checkpointer):
        mock_chat.return_value = "research"

        graph = build_graph(checkpointer=checkpointer)
        config = _config()
        result = graph.invoke(_make_state("What is AI?"), config)

        assert "messages" in result
        assert len(result["messages"]) > 1

    @patch("src.llm_client.LLMClient.chat")
    def test_router_decision_stored_in_metadata(self, mock_chat, checkpointer):
        mock_chat.side_effect = ["research", "Detailed answer about AI."]

        graph = build_graph(checkpointer=checkpointer)
        config = _config()
        result = graph.invoke(_make_state("Explain neural networks"), config)

        assert result["metadata"].get("route") == "research"

    @patch("src.llm_client.LLMClient.chat")
    def test_ai_message_added_by_agent(self, mock_chat, checkpointer):
        mock_chat.side_effect = ["general", "Hello! I'm doing well."]

        graph = build_graph(checkpointer=checkpointer)
        config = _config()
        result = graph.invoke(_make_state("Hello!"), config)

        ai_msgs = [m for m in result["messages"] if isinstance(m, AIMessage)]
        assert len(ai_msgs) >= 1

    @patch("src.llm_client.LLMClient.chat")
    def test_user_id_propagated_through_graph(self, mock_chat, checkpointer):
        mock_chat.return_value = "general"
        user_id = "specific-user-999"

        graph = build_graph(checkpointer=checkpointer)
        config = _config()
        state = _make_state("Hello", user_id=user_id)
        result = graph.invoke(state, config)

        assert result["user_id"] == user_id


class TestGraphHITL:
    @patch("src.llm_client.LLMClient.chat")
    def test_interrupt_before_research_stops_graph(self, mock_chat, checkpointer):
        mock_chat.return_value = "research"

        graph = build_graph(checkpointer=checkpointer, interrupt_before=["research"])
        config = _config()
        graph.invoke(_make_state("What is quantum physics?"), config)

        # گراف باید متوقف شده باشد
        state = graph.get_state(config)
        assert "research" in state.next

    @patch("src.llm_client.LLMClient.chat")
    def test_state_preserved_in_checkpointer_after_interrupt(self, mock_chat, checkpointer):
        mock_chat.return_value = "research"

        graph = build_graph(checkpointer=checkpointer, interrupt_before=["research"])
        config = _config()
        graph.invoke(_make_state("test question"), config)

        # باید بتوان state را بعد از interrupt خواند
        state = graph.get_state(config)
        assert state.values is not None
        assert len(state.values.get("messages", [])) > 0

    @patch("src.llm_client.LLMClient.chat")
    def test_resume_after_interrupt_completes_graph(self, mock_chat, checkpointer):
        from langgraph.types import Command
        mock_chat.side_effect = ["research", "Answer about quantum physics."]

        graph = build_graph(checkpointer=checkpointer, interrupt_before=["research"])
        config = _config()

        # اجرای اول تا interrupt
        graph.invoke(_make_state("What is quantum physics?"), config)

        # resume
        final = graph.invoke(Command(resume={"hitl_decision": True}), config)

        ai_msgs = [m for m in final["messages"]
                   if isinstance(m, AIMessage) and getattr(m, "name", None) not in (None, "router")]
        assert len(ai_msgs) >= 1


class TestGraphMemoryContext:
    @patch("src.llm_client.LLMClient.chat")
    def test_memory_context_empty_by_default(self, mock_chat, checkpointer):
        mock_chat.return_value = "general"

        graph = build_graph(checkpointer=checkpointer)
        config = _config()
        result = graph.invoke(_make_state("Hello"), config)

        assert result.get("memory_context", []) == []