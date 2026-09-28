"""
examples/agents_hitl.py — نمونه Human-in-the-Loop

ایجنت قبل از اجرای node مشخص متوقف می‌شود و منتظر تأیید انسانی می‌ماند.

اجرا:
    uv run python examples/agents_hitl.py
"""

import uuid
from langchain_core.messages import HumanMessage
from src.agents import AgentState, build_graph, HITLHandler
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


def example_interrupt_before_research(checkpointer) -> None:
    """گراف قبل از research متوقف می‌شود — کاربر باید تأیید کند."""
    print("── HITL: Interrupt Before Research ─────")

    # گراف با interrupt قبل از node "research"
    graph = build_graph(checkpointer=checkpointer, interrupt_before=["research"])
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    query = "What are black holes?"
    print(f"📨 {query}\n")

    # اجرای اول — تا قبل از research متوقف می‌شود
    print("▶ اجرا شروع شد...")
    graph.invoke(_make_state(query), config)
    print("⏸  متوقف شد — منتظر تأیید انسانی\n")

    # بررسی state فعلی و نمایش به کاربر
    current = graph.get_state(config)
    approved = HITLHandler.prompt_user(current.values)

    if approved:
        print("\n▶ تأیید شد — ادامه اجرا...")
        final = HITLHandler.resume(graph, approved=True, config=config)

        ai_msgs = [m for m in final["messages"]
                   if hasattr(m, "name") and m.name not in (None, "router")]
        if ai_msgs:
            print(f"🤖 [{ai_msgs[-1].name}]: {ai_msgs[-1].content[:400]}")
    else:
        print("\n❌ رد شد — اجرا لغو شد.")
    print()


def example_interrupt_before_general(checkpointer) -> None:
    """interrupt قبل از general agent."""
    print("── HITL: Interrupt Before General ──────")

    graph = build_graph(checkpointer=checkpointer, interrupt_before=["general"])
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    query = "Hello! How are you today?"
    print(f"📨 {query}\n")

    graph.invoke(_make_state(query), config)
    print("⏸  متوقف شد\n")

    current = graph.get_state(config)
    approved = HITLHandler.prompt_user(current.values)

    if approved:
        final = HITLHandler.resume(graph, approved=True, config=config)
        ai_msgs = [m for m in final["messages"]
                   if hasattr(m, "name") and m.name not in (None, "router")]
        if ai_msgs:
            print(f"\n🤖 [{ai_msgs[-1].name}]: {ai_msgs[-1].content[:300]}")
    else:
        print("\n❌ لغو شد.")
    print()


if __name__ == "__main__":
    with make_checkpointer() as checkpointer:
        example_interrupt_before_research(checkpointer)
        example_interrupt_before_general(checkpointer)