"""تست‌های BaseAgent."""

import pytest
from unittest.mock import patch, MagicMock
from langchain_core.messages import HumanMessage, AIMessage
from src.agents.base_agent import BaseAgent
from src.agents.state import AgentState


class ConcreteAgent(BaseAgent):
    """پیاده‌سازی ساده برای تست."""
    name = "concrete"

    def run(self, state: AgentState) -> dict:
        return {"messages": [AIMessage(content="test", name=self.name)]}


@pytest.fixture
def agent():
    return ConcreteAgent(name="concrete", system_prompt="You are a test agent.")


def _make_state(messages=None, memory_context=None) -> AgentState:
    return {
        "messages": messages or [HumanMessage(content="hello")],
        "user_id": "test-user",
        "metadata": {},
        "memory_context": memory_context or [],
        "hitl_decision": None,
    }


class TestBaseAgentLastHumanMessage:
    def test_returns_last_human_message(self, agent):
        state = _make_state([
            HumanMessage(content="first"),
            AIMessage(content="reply"),
            HumanMessage(content="second"),
        ])
        assert agent._last_human_message(state) == "second"

    def test_empty_messages_returns_empty_string(self, agent):
        state = _make_state([])
        state["messages"] = []   # override کامل
        assert agent._last_human_message(state) == ""

    def test_no_human_message_returns_empty(self, agent):
        state = _make_state([AIMessage(content="only ai")])
        assert agent._last_human_message(state) == ""


class TestBaseAgentBuildSystemPrompt:
    def test_no_memory_returns_base_prompt(self, agent):
        state = _make_state(memory_context=[])
        result = agent._build_system_prompt(state)
        assert result == "You are a test agent."

    def test_with_memory_context_appended(self, agent):
        state = _make_state(memory_context=["user likes Python", "user is a developer"])
        result = agent._build_system_prompt(state)
        assert "You are a test agent." in result
        assert "user likes Python" in result
        assert "user is a developer" in result

    def test_memory_context_in_structured_format(self, agent):
        state = _make_state(memory_context=["fact 1"])
        result = agent._build_system_prompt(state)
        assert "Relevant memory" in result or "memory" in result.lower()


class TestBaseAgentGetLlm:
    def test_returns_llm_client(self, agent):
        with patch("src.agents.base_agent.LLMClient") as mock_cls:
            mock_cls.return_value = MagicMock()
            llm = agent._get_llm("user-123")
            mock_cls.assert_called_once_with(user_id="user-123", agent_name="concrete")

    def test_user_id_passed_correctly(self, agent):
        with patch("src.agents.base_agent.LLMClient") as mock_cls:
            agent._get_llm("specific-user")
            mock_cls.assert_called_with(user_id="specific-user", agent_name="concrete")


class TestBaseAgentCall:
    def test_call_delegates_to_run(self, agent):
        state = _make_state()
        result = agent(state)
        assert "messages" in result
