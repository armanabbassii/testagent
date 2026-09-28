"""
examples/memory_json.py — نمونه استفاده از JsonMemory

نشان می‌دهد:
  ۱. keyword search — بدون embedding
  ۲. embedding search — با EmbeddingClient
  ۳. ادغام با گراف multi-agent

اجرا:
    uv run python examples/memory_json.py
"""

import uuid
from langchain_core.messages import HumanMessage
from src.memory import JsonMemory, MemoryItem
from src.agents import AgentState, build_graph
from src.checkpointer import make_checkpointer

USER_ID = "user-demo"


def example_keyword_search() -> None:
    print("── Keyword Search ───────────────────────")
    memory = JsonMemory(path="output/demo_memory.json", search_mode="keyword")

    memory.save(MemoryItem(user_id=USER_ID, content="کاربر به فیزیک نظری علاقه دارد.",
                            memory_type="preference"))
    memory.save(MemoryItem(user_id=USER_ID, content="در جلسه قبل درباره نسبیت صحبت شد.",
                            memory_type="conversation"))
    memory.save(MemoryItem(user_id=USER_ID, content="کاربر برنامه‌نویس Python است.",
                            memory_type="fact"))

    print(f"✅ {memory.count()} خاطره ذخیره شد\n")

    results = memory.search("فیزیک", user_id=USER_ID, top_k=3)
    print(f"جستجوی «فیزیک» — {len(results)} نتیجه:")
    for r in results:
        print(f"  score={r.score:.2f} | [{r.item.memory_type}] {r.item.content}")

    recent = memory.get_recent(USER_ID, limit=2)
    print(f"\nآخرین {len(recent)} خاطره:")
    for item in recent:
        print(f"  [{item.memory_type}] {item.content}")

    # پاکسازی
    memory.clear_user(USER_ID)
    print(f"\n🗑  پاکسازی — {memory.count()} خاطره باقی‌مانده\n")


def example_embedding_search() -> None:
    print("── Embedding Search ─────────────────────")
    try:
        from src.embedding_client import EmbeddingClient
        emb = EmbeddingClient(user_id=USER_ID)

        memory = JsonMemory(
            path="output/demo_memory_emb.json",
            search_mode="embedding",
            embedding_client=emb,
        )

        memory.save(MemoryItem(user_id=USER_ID, content="کاربر به هوش مصنوعی علاقه دارد.",
                                memory_type="preference"))
        memory.save(MemoryItem(user_id=USER_ID, content="Python زبان مورد علاقه کاربر است.",
                                memory_type="fact"))
        memory.save(MemoryItem(user_id=USER_ID, content="امروز هوا آفتابی است.",
                                memory_type="general"))

        print(f"✅ {memory.count()} خاطره با embedding ذخیره شد\n")

        results = memory.search("machine learning", user_id=USER_ID, top_k=3)
        print(f"جستجوی «machine learning» — {len(results)} نتیجه:")
        for r in results:
            print(f"  score={r.score:.3f} | {r.item.content}")

        memory.clear_user(USER_ID)
        print()

    except Exception as e:
        print(f"⚠️  نیاز به سرور Embedding: {e}\n")


def example_with_agent_graph() -> None:
    print("── JsonMemory با Agent Graph ─────────────")
    memory = JsonMemory(path="output/demo_memory_graph.json", search_mode="keyword")

    # ذخیره چند خاطره اولیه
    memory.save(MemoryItem(user_id=USER_ID, content="کاربر دانشجوی فیزیک است.",
                            memory_type="fact"))

    try:
        with make_checkpointer() as checkpointer:
            graph = build_graph(checkpointer=checkpointer, memory=memory, memory_top_k=3)
            config = {"configurable": {"thread_id": str(uuid.uuid4())}}

            state: AgentState = {
                "messages": [HumanMessage(content="Hello! Can you help me?")],
                "user_id": USER_ID,
                "metadata": {},
                "memory_context": [],
                "hitl_decision": None,
            }

            result = graph.invoke(state, config)
            ctx = result.get("memory_context", [])
            print(f"Memory context لود شده: {len(ctx)} مورد")
            for c in ctx:
                print(f"  {c}")

    except Exception as e:
        print(f"⚠️  خطا در اجرای گراف: {e}")

    finally:
        memory.clear_user(USER_ID)
    print()


if __name__ == "__main__":
    example_keyword_search()
    example_embedding_search()
    example_with_agent_graph()