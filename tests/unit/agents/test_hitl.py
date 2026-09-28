"""تست‌های HITLHandler."""

import pytest
from unittest.mock import MagicMock, patch
from src.agents.hitl import HITLHandler
from src.agents.state import AgentState
from langchain_core.messages import HumanMessage, AIMessage


def _make_state(messages=None) -> AgentState:
    return {
        "messages": messages or [HumanMessage(content="test")],
        "user_id": "u1",
        "metadata": {},
        "memory_context": [],
        "hitl_decision": None,
    }


class TestHITLHandlerPromptUser:
    @patch("builtins.input", return_value="y")
    def test_yes_returns_true(self, mock_input):
        state = _make_state()
        result = HITLHandler.prompt_user(state)
        assert result is True

    @patch("builtins.input", return_value="n")
    def test_no_returns_false(self, mock_input):
        state = _make_state()
        result = HITLHandler.prompt_user(state)
        assert result is False

    @patch("builtins.input", return_value="بله")
    def test_fa_yes_returns_true(self, mock_input):
        state = _make_state()
        result = HITLHandler.prompt_user(state)
        assert result is True

    @patch("builtins.input", return_value="خیر")
    def test_fa_no_returns_false(self, mock_input):
        state = _make_state()
        result = HITLHandler.prompt_user(state)
        assert result is False

    @patch("builtins.input", side_effect=["invalid", "y"])
    def test_invalid_input_retries(self, mock_input):
        state = _make_state()
        result = HITLHandler.prompt_user(state)
        assert result is True
        assert mock_input.call_count == 2

    @patch("builtins.input", return_value="y")
    def test_shows_recent_messages(self, mock_input, capsys):
        state = _make_state([
            HumanMessage(content="user question"),
            AIMessage(content="agent response", name="router"),
        ])
        HITLHandler.prompt_user(state)
        output = capsys.readouterr().out
        assert "user question" in output or "agent response" in output


class TestHITLHandlerResume:
    def test_resume_calls_graph_invoke(self):
        graph = MagicMock()
        graph.invoke.return_value = {"messages": [], "decision": "approve"}
        config = {"configurable": {"thread_id": "t1"}}

        HITLHandler.resume(graph, approved=True, config=config)

        assert graph.invoke.called

    def test_resume_passes_hitl_decision(self):
        from langgraph.types import Command
        graph = MagicMock()
        graph.invoke.return_value = {}
        config = {"configurable": {"thread_id": "t1"}}

        HITLHandler.resume(graph, approved=True, config=config)

        call_args = graph.invoke.call_args
        command = call_args[0][0]
        assert isinstance(command, Command)
