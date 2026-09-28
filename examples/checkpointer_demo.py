"""
examples/checkpointer_demo.py — نمونه Redis Checkpointer

نشان می‌دهد که:
  ۱. state بین دو invoke مختلف در Redis حفظ می‌شود
  ۲. HITL با Redis چطور کار می‌کند (interrupt + resume)
  ۳. multi-turn مکالمه با thread_id یکسان

پیش‌نیاز:
    docker run -d -p 6379:6379 --name redis redis:alpine

اجرا:
    uv run python examples/checkpointer_demo.py
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


def example_state_persistence() -> None:
    """نشان می‌دهد که state بعد از اتمام در Redis باقی می‌ماند."""
    print("── State Persistence ────────────────────")

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    with make_checkpointer() as checkpointer:
        graph = build_graph(checkpointer=checkpointer)

        # اجرای اول
        result = graph.invoke(_make_state("What is Python?"), config)
        ai_msgs = [m for m in result["messages"]
                   if hasattr(m, "name") and m.name not in (None, "router")]
        print(f"📨 پاسخ اول: {ai_msgs[-1].content[:100]}...\n")

        # خواندن state از Redis
        saved = graph.get_state(config)
        msg_count = len(saved.values.get("messages", []))
        print(f"✅ {msg_count} پیام در Redis ذخیره شد (thread_id: {thread_id[:8]}...)")
        print(f"   next nodes: {saved.next}\n")


def example_hitl_with_redis() -> None:
    """HITL: interrupt در Redis ذخیره می‌شود — resume از همانجا ادامه می‌دهد."""
    print("── HITL با Redis ────────────────────────")

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    with make_checkpointer() as checkpointer:
        graph = build_graph(checkpointer=checkpointer, interrupt_before=["research"])

        # اجرای اول — متوقف قبل از research
        print("▶ اجرا تا interrupt...")
        graph.invoke(_make_state("Explain quantum computing"), config)

        # state در Redis است — حتی اگر سرور restart شود
        current = graph.get_state(config)
        print(f"⏸  متوقف شد | next: {current.next}")
        print(f"   state در Redis با thread_id: {thread_id[:8]}...\n")

        approved = HITLHandler.prompt_user(current.values)

        if approved:
            print("\n▶ resume از Redis state...")
            final = HITLHandler.resume(graph, approved=True, config=config)
            ai_msgs = [m for m in final["messages"]
                       if hasattr(m, "name") and m.name not in (None, "router")]
            if ai_msgs:
                print(f"🤖 [{ai_msgs[-1].name}]: {ai_msgs[-1].content[:300]}")
        else:
            print("❌ لغو شد.")
    print()


def example_multi_turn() -> None:
    """multi-turn: thread_id یکسان → LangGraph از همان session ادامه می‌دهد."""
    print("── Multi-turn با thread_id ثابت ─────────")

    # یک thread_id ثابت برای کل مکالمه
    thread_id = "demo-multiturn-session"
    config = {"configurable": {"thread_id": thread_id}}

    with make_checkpointer() as checkpointer:
        graph = build_graph(checkpointer=checkpointer)

        turns = [
            "My name is Alex.",
            "What's my name?",  # انتظار داریم پاسخ دهد: Alex
        ]

        for query in turns:
            print(f"📨 {query}")
            result = graph.invoke(_make_state(query), config)
            ai_msgs = [m for m in result["messages"]
                       if hasattr(m, "name") and m.name not in (None, "router")]
            if ai_msgs:
                print(f"🤖 {ai_msgs[-1].content[:150]}\n")
    print()


if __name__ == "__main__":
    try:
        example_state_persistence()
        example_hitl_with_redis()
        example_multi_turn()
    except Exception as e:
        print(f"⚠️  خطا: {e}")
        print("   مطمئن شوید Redis در حال اجراست:")
        print("   docker run -d -p 6379:6379 --name redis redis:alpine")