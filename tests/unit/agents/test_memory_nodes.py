"""تست‌های MemoryLoaderNode و MemorySaverNode."""

import pytest
from unittest.mock import MagicMock, patch
from langchain_core.messages import HumanMessage, AIMessage
from src.agents.memory_nodes import MemoryLoaderNode, MemorySaverNode
from src.agents.state import AgentState


def _make_state(messages=None, user_id="u1") -> AgentState:
    return {
        "messages": messages or [HumanMessage(content="what is AI?")],
        "user_id": user_id,
        "metadata": {},
        "memory_context": [],
        "hitl_decision": None,
    }


def _make_search_result(content: str, score: float = 0.9):
    result = MagicMock()
    result.score = score
    result.item.memory_type = "conversation"
    result.item.content = content
    return result


class TestMemoryLoaderNode:
    def test_empty_messages_returns_empty_context(self):
        memory = MagicMock()
        node = MemoryLoaderNode(memory=memory, top_k=3)
        state = _make_state()
        state["messages"] = []   # override به خالی واقعی
        result = node(state)
        assert result["memory_context"] == []
        memory.search.assert_not_called()

    def test_calls_memory_search_with_last_human_message(self):
        memory = MagicMock()
        memory.search.return_value = []
        node = MemoryLoaderNode(memory=memory, top_k=5)
        state = _make_state([HumanMessage(content="tell me about Python")])
        node(state)
        memory.search.assert_called_once_with(
            query="tell me about Python",
            user_id="u1",
            top_k=5,
        )

    def test_returns_formatted_context_strings(self):
        memory = MagicMock()
        memory.search.return_value = [
            _make_search_result("user likes Python", 0.95),
            _make_search_result("user is a developer", 0.88),
        ]
        node = MemoryLoaderNode(memory=memory, top_k=5)
        state = _make_state()
        result = node(state)
        assert len(result["memory_context"]) == 2
        assert "user likes Python" in result["memory_context"][0]
        assert "0.95" in result["memory_context"][0]

    def test_uses_last_human_message_as_query(self):
        memory = MagicMock()
        memory.search.return_value = []
        node = MemoryLoaderNode(memory=memory)
        state = _make_state([
            HumanMessage(content="first question"),
            AIMessage(content="first answer"),
            HumanMessage(content="second question"),
        ])
        node(state)
        call_kwargs = memory.search.call_args.kwargs
        assert call_kwargs["query"] == "second question"

    def test_top_k_passed_to_search(self):
        memory = MagicMock()
        memory.search.return_value = []
        node = MemoryLoaderNode(memory=memory, top_k=7)
        node(_make_state())
        assert memory.search.call_args.kwargs["top_k"] == 7


class TestMemorySaverNode:
    def test_saves_conversation_to_memory(self):
        memory = MagicMock()
        node = MemorySaverNode(memory=memory)
        state = _make_state([
            HumanMessage(content="What is AI?"),
            AIMessage(content="AI is artificial intelligence.", name="research"),
        ])
        node(state)
        memory.save.assert_called_once()

    def test_does_not_save_if_no_human_message(self):
        memory = MagicMock()
        node = MemorySaverNode(memory=memory)
        state = _make_state([AIMessage(content="only ai", name="research")])
        node(state)
        memory.save.assert_not_called()

    def test_does_not_save_if_no_agent_response(self):
        memory = MagicMock()
        node = MemorySaverNode(memory=memory)
        state = _make_state([HumanMessage(content="only human")])
        node(state)
        memory.save.assert_not_called()

    def test_skips_router_messages(self):
        memory = MagicMock()
        node = MemorySaverNode(memory=memory)
        state = _make_state([
            HumanMessage(content="question"),
            AIMessage(content="research", name="router"),  # router — نباید ذخیره شود
        ])
        node(state)
        memory.save.assert_not_called()

    def test_saved_item_has_correct_user_id(self):
        memory = MagicMock()
        node = MemorySaverNode(memory=memory)
        state = _make_state([
            HumanMessage(content="question"),
            AIMessage(content="answer", name="research"),
        ])
        state["user_id"] = "specific-user"
        node(state)
        saved_item = memory.save.call_args[0][0]
        assert saved_item.user_id == "specific-user"

    def test_saved_item_memory_type_is_conversation(self):
        memory = MagicMock()
        node = MemorySaverNode(memory=memory)
        state = _make_state([
            HumanMessage(content="question"),
            AIMessage(content="answer", name="general"),
        ])
        node(state)
        saved_item = memory.save.call_args[0][0]
        assert saved_item.memory_type == "conversation"

    def test_returns_empty_dict(self):
        memory = MagicMock()
        node = MemorySaverNode(memory=memory)
        state = _make_state([
            HumanMessage(content="q"),
            AIMessage(content="a", name="general"),
        ])
        result = node(state)
        assert result == {}
