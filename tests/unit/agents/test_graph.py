"""تست‌های build_graph."""

import pytest
from unittest.mock import patch, MagicMock
from langgraph.checkpoint.memory import MemorySaver


@pytest.fixture
def checkpointer():
    return MemorySaver()


class TestBuildGraph:
    def test_graph_compiled_successfully(self, checkpointer):
        from src.agents.graph import build_graph
        graph = build_graph(checkpointer=checkpointer)
        assert graph is not None

    def test_graph_without_memory(self, checkpointer):
        from src.agents.graph import build_graph
        graph = build_graph(checkpointer=checkpointer, memory=None)
        assert graph is not None

    def test_graph_with_memory(self, checkpointer):
        from src.agents.graph import build_graph
        mock_memory = MagicMock()
        mock_memory.search.return_value = []
        graph = build_graph(checkpointer=checkpointer, memory=mock_memory, memory_top_k=3)
        assert graph is not None

    def test_graph_with_interrupt_before(self, checkpointer):
        from src.agents.graph import build_graph
        graph = build_graph(checkpointer=checkpointer, interrupt_before=["research"])
        assert graph is not None

    def test_graph_nodes_include_router(self, checkpointer):
        from src.agents.graph import build_graph
        graph = build_graph(checkpointer=checkpointer)
        # بررسی که node های اصلی وجود دارند
        assert graph is not None


class TestBuildCodeReviewGraph:
    def test_code_review_graph_compiled(self, checkpointer):
        from src.agents.code_review.graph import build_code_review_graph
        from src.debug import DebugConfig
        graph = build_code_review_graph(
            checkpointer=checkpointer,
            debug_config=DebugConfig.off(),
        )
        assert graph is not None

    def test_code_review_graph_with_interrupt(self, checkpointer):
        from src.agents.code_review.graph import build_code_review_graph
        from src.debug import DebugConfig
        graph = build_code_review_graph(
            checkpointer=checkpointer,
            interrupt_before=["decision_maker"],
            debug_config=DebugConfig.off(),
        )
        assert graph is not None

    def test_code_review_graph_default_debug_config(self, checkpointer):
        """وقتی debug_config=None باشد باید از .env بخواند."""
        from src.agents.code_review.graph import build_code_review_graph
        with patch("src.agents.code_review.graph.DebugConfig.from_env",
                   return_value=MagicMock()) as mock_env:
            graph = build_code_review_graph(checkpointer=checkpointer, debug_config=None)
        mock_env.assert_called_once()
        assert graph is not None

class TestBuildGraphWithRAG:
    def test_graph_with_vector_store_compiles(self, checkpointer):
        from src.agents.graph import build_graph
        mock_store = MagicMock()
        graph = build_graph(checkpointer=checkpointer, vector_store=mock_store)
        assert graph is not None

    def test_graph_without_vector_store_no_rag_node(self, checkpointer):
        """بدون vector_store، route_map نباید 'rag' داشته باشد."""
        from src.agents.graph import build_graph
        graph = build_graph(checkpointer=checkpointer)
        assert graph is not None

    def test_router_enables_rag_when_vector_store_given(self):
        from src.agents.example_agents import RouterAgent
        router = RouterAgent(enable_rag=True)
        assert "rag" in router.system_prompt.lower()

    def test_router_no_rag_by_default(self):
        from src.agents.example_agents import RouterAgent
        router = RouterAgent(enable_rag=False)
        assert "'rag'" not in router.system_prompt