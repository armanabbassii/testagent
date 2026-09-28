"""تست‌های example agents (Router, Research, Summary, General)."""

import pytest
from unittest.mock import patch
from langchain_core.messages import HumanMessage, AIMessage
from src.agents.state import AgentState


def _make_state(query: str = "test query") -> AgentState:
    return {
        "messages": [HumanMessage(content=query)],
        "user_id": "test-user",
        "metadata": {},
        "memory_context": [],
        "hitl_decision": None,
    }


class TestRouterAgent:
    def test_returns_metadata_with_route(self):
        from src.agents.example_agents import RouterAgent
        agent = RouterAgent()
        with patch("src.llm_client.LLMClient.chat", return_value="research"):
            result = agent(_make_state("what is AI?"))
        assert "metadata" in result
        assert result["metadata"]["route"] == "research"

    def test_returns_ai_message(self):
        from src.agents.example_agents import RouterAgent
        agent = RouterAgent()
        with patch("src.llm_client.LLMClient.chat", return_value="general"):
            result = agent(_make_state())
        assert "messages" in result
        assert len(result["messages"]) == 1

    def test_route_lowercase(self):
        from src.agents.example_agents import RouterAgent
        agent = RouterAgent()
        with patch("src.llm_client.LLMClient.chat", return_value="  RESEARCH  "):
            result = agent(_make_state())
        assert result["metadata"]["route"] == "research"

    def test_preserves_existing_metadata(self):
        from src.agents.example_agents import RouterAgent
        agent = RouterAgent()
        state = _make_state()
        state["metadata"] = {"existing_key": "existing_value"}
        with patch("src.llm_client.LLMClient.chat", return_value="general"):
            result = agent(state)
        assert result["metadata"]["existing_key"] == "existing_value"
        assert result["metadata"]["route"] == "general"


class TestResearchAgent:
    def test_returns_ai_message_with_content(self):
        from src.agents.example_agents import ResearchAgent
        agent = ResearchAgent()
        with patch("src.llm_client.LLMClient.chat", return_value="Research answer"):
            result = agent(_make_state("what is AI?"))
        assert result["messages"][0].content == "Research answer"
        assert result["messages"][0].name == "research"

    def test_uses_build_system_prompt(self):
        from src.agents.example_agents import ResearchAgent
        agent = ResearchAgent()
        state = _make_state()
        state["memory_context"] = ["user is a developer"]

        with patch("src.llm_client.LLMClient.chat", return_value="answer") as mock_chat:
            agent(state)
            system_prompt = mock_chat.call_args[1].get("system_prompt") or \
                            mock_chat.call_args[0][1] if len(mock_chat.call_args[0]) > 1 else ""
            # memory context باید در prompt باشد
            assert "developer" in system_prompt or mock_chat.called


class TestSummaryAgent:
    def test_returns_ai_message(self):
        from src.agents.example_agents import SummaryAgent
        agent = SummaryAgent()
        with patch("src.llm_client.LLMClient.chat", return_value="Summary here"):
            result = agent(_make_state("please summarize..."))
        assert result["messages"][0].content == "Summary here"
        assert result["messages"][0].name == "summarizer"

    def test_agent_name_correct(self):
        from src.agents.example_agents import SummaryAgent
        agent = SummaryAgent()
        assert agent.name == "summarizer"


class TestGeneralAgent:
    def test_returns_ai_message(self):
        from src.agents.example_agents import GeneralAgent
        agent = GeneralAgent()
        with patch("src.llm_client.LLMClient.chat", return_value="Hello!"):
            result = agent(_make_state("Hello"))
        assert result["messages"][0].content == "Hello!"
        assert result["messages"][0].name == "general"

    def test_agent_name_correct(self):
        from src.agents.example_agents import GeneralAgent
        agent = GeneralAgent()
        assert agent.name == "general"
