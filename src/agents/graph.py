"""
graph.py — ساخت و کامپایل گراف LangGraph

توپولوژی بدون memory و بدون RAG:
    START → router → (research|summarizer|general) → END

توپولوژی با memory:
    START → memory_loader → router → (...) → memory_saver → END

توپولوژی با RAG (وقتی vector_store پاس داده شود):
    router می‌تواند به "rag" هم مسیریابی کند:
    START → router → (research|summarizer|general|rag) → END

Checkpointer:
    باید از make_checkpointer() ساخته و پاس داده شود.
"""

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.base import BaseCheckpointSaver
from src.agents.state import AgentState
from src.agents.example_agents import RouterAgent, ResearchAgent, SummaryAgent, GeneralAgent
from src.agents.test_case_generator.agent import TestCaseGeneratorAgent
from src.vector_store.base import BaseVectorStore


def _make_router(state: AgentState) -> str:
    route = state.get("metadata", {}).get("route", "general")
    valid = ("research", "summarize", "general", "rag", "test_case_generator")
    return route if route in valid else "general"


def build_graph(
    checkpointer: BaseCheckpointSaver,
    interrupt_before: list[str] | None = None,
    memory=None,
    memory_top_k: int = 5,
    vector_store: BaseVectorStore | None = None,
    rag_top_k: int = 5,
    rag_score_threshold: float = 0.0,
):
    """گراف را می‌سازد و کامپایل‌شده برمی‌گرداند.

    پارامترها:
        checkpointer        : checkpointer آماده (از make_checkpointer() بگیرید)
        interrupt_before    : node هایی که HITL می‌خواهند، مثال: ["research"]
        memory               : نمونه BaseMemory برای long-term memory (اختیاری)
        memory_top_k         : تعداد خاطرات بازیابی‌شده در هر درخواست
        vector_store         : نمونه BaseVectorStore برای فعال‌سازی RAGAgent (اختیاری)
                              اگر داده شود، router می‌تواند سوالات را به "rag" بفرستد
        rag_top_k             : تعداد اسناد بازیابی‌شده توسط RAGAgent
        rag_score_threshold   : حداقل امتیاز شباهت برای پذیرش سند
    """
    enable_rag = vector_store is not None

    router     = RouterAgent(enable_rag=enable_rag)
    research   = ResearchAgent()
    summarizer = SummaryAgent()
    general    = GeneralAgent()
    testcase   = TestCaseGeneratorAgent()

    builder = StateGraph(AgentState)

    # ── node های ثابت ────────────────────────────────────────────────────────
    builder.add_node(router.name,     router)
    builder.add_node(research.name,   research)
    builder.add_node(summarizer.name, summarizer)
    builder.add_node(general.name,    general)
    builder.add_node(testcase.name,   testcase)

    route_map = {
        "research":  research.name,
        "summarize": summarizer.name,
        "general":   general.name,
        "test_case_generator": testcase.name,
    }

    if enable_rag:
        from src.agents.rag import RAGAgent
        rag = RAGAgent(
            vector_store=vector_store,
            top_k=rag_top_k,
            score_threshold=rag_score_threshold,
        )
        builder.add_node(rag.name, rag)
        route_map["rag"] = rag.name

    # ── node های memory (اختیاری) ────────────────────────────────────────────
    if memory is not None:
        from src.agents.memory_nodes import MemoryLoaderNode, MemorySaverNode

        loader = MemoryLoaderNode(memory=memory, top_k=memory_top_k)
        saver  = MemorySaverNode(memory=memory)

        builder.add_node(loader.name, loader)
        builder.add_node(saver.name,  saver)

        # START → memory_loader → router
        builder.add_edge(START, loader.name)
        builder.add_edge(loader.name, router.name)
        builder.add_conditional_edges(router.name, _make_router, route_map)

        for node_name in route_map.values():
            builder.add_edge(node_name, saver.name)
        builder.add_edge(saver.name, END)

    else:
        builder.add_edge(START, router.name)
        builder.add_conditional_edges(router.name, _make_router, route_map)
        for node_name in route_map.values():
            builder.add_edge(node_name, END)

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before or [],
    )