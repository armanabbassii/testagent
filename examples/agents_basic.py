"""
examples/agents_basic.py — نمونه استفاده از Multi-Agent Graph (بدون memory)

اجرا:
    uv run python examples/agents_basic.py
"""

import uuid
from langchain_core.messages import HumanMessage
from src.agents import AgentState, build_graph
from src.checkpointer import make_checkpointer  # backend از .env خوانده می‌شود

USER_ID = "user-123"


def _make_state(query: str) -> AgentState:
    return {
        "messages": [HumanMessage(content=query)],
        "user_id": USER_ID,
        "metadata": {},
        "memory_context": [],
        "hitl_decision": None,
    }


def _print_result(result: dict) -> None:
    ai_msgs = [
        m for m in result["messages"]
        if hasattr(m, "name") and m.name not in (None, "router")
    ]
    if ai_msgs:
        last = ai_msgs[-1]
        preview = last.content[:300] + ("..." if len(last.content) > 300 else "")
        print(f"🤖 [{last.name}]: {preview}\n")


def example_research_query(checkpointer) -> None:
    print("── Research Query ───────────────────────")
    graph = build_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    query = "What is the theory of relativity?"
    print(f"📨 {query}")
    result = graph.invoke(_make_state(query), config)
    _print_result(result)


def example_summarize_query(checkpointer) -> None:
    print("── Summarize Query ──────────────────────")
    graph = build_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    query = "Please summarize: Artificial intelligence is transforming industries by automating repetitive tasks."
    print(f"📨 {query[:60]}...")
    result = graph.invoke(_make_state(query), config)
    _print_result(result)


def example_general_query(checkpointer) -> None:
    print("── General Query ────────────────────────")
    graph = build_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    query = "Hello! Can you recommend a good book?"
    print(f"📨 {query}")
    result = graph.invoke(_make_state(query), config)
    _print_result(result)


if __name__ == "__main__":
    with make_checkpointer() as checkpointer:
        example_research_query(checkpointer)
        example_summarize_query(checkpointer)
        example_general_query(checkpointer)