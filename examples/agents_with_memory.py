"""
examples/agents_with_memory.py — نمونه Multi-Agent Graph با Long-term Memory

پیش‌نیاز:
    docker run -d -p 6333:6333 qdrant/qdrant

اجرا:
    uv run python examples/agents_with_memory.py
"""

import uuid
from langchain_core.messages import HumanMessage
from src.agents import AgentState, build_graph
from src.checkpointer import make_checkpointer  # backend از .env خوانده می‌شود
from src.embedding_client import EmbeddingClient
from src.vector_store import QdrantVectorStore
from src.memory import QdrantMemory, MemoryItem

USER_ID = "user-123"
QDRANT_URL = "http://localhost:6333"


def _make_state(query: str) -> AgentState:
    return {
        "messages": [HumanMessage(content=query)],
        "user_id": USER_ID,
        "metadata": {},
        "memory_context": [],
        "hitl_decision": None,
    }


def _setup_memory() -> QdrantMemory:
    """Vector store و memory را راه‌اندازی می‌کند."""
    emb = EmbeddingClient(user_id=USER_ID)
    store = QdrantVectorStore(
        collection="agent_memory",
        embedding_client=emb,
        url=QDRANT_URL,
    )
    store.create_collection(vector_size=len(emb.embed("test")))
    return QdrantMemory(vector_store=store)


def example_memory_personalization(checkpointer) -> None:
    """ذخیره علاقه‌مندی کاربر و استفاده از آن در پاسخ بعدی."""
    print("── Memory Personalization ───────────────")

    memory = _setup_memory()

    # ذخیره علاقه‌مندی‌های کاربر
    memory.save(MemoryItem(
        user_id=USER_ID,
        content="کاربر علاقه زیادی به فیزیک نظری و نسبیت دارد.",
        memory_type="preference",
    ))
    memory.save(MemoryItem(
        user_id=USER_ID,
        content="در جلسه قبل درباره سیاه‌چاله‌ها و افق رویداد صحبت شد.",
        memory_type="conversation",
    ))
    print("✅ ۲ خاطره ذخیره شد\n")

    # سوال — انتظار داریم memory context لود بشه
    graph = build_graph(checkpointer=checkpointer, memory=memory, memory_top_k=3)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    query = "Tell me something interesting about space."
    print(f"📨 {query}")
    result = graph.invoke(_make_state(query), config)

    # نمایش context لود شده
    ctx = result.get("memory_context", [])
    if ctx:
        print(f"\n📋 Memory context لود شده ({len(ctx)} مورد):")
        for c in ctx:
            print(f"   {c}")

    # پاسخ ایجنت
    ai_msgs = [m for m in result["messages"]
               if hasattr(m, "name") and m.name not in (None, "router")]
    if ai_msgs:
        print(f"\n🤖 [{ai_msgs[-1].name}]: {ai_msgs[-1].content[:300]}")
    print()


def example_memory_search() -> None:
    """جستجو در حافظه بلندمدت."""
    print("── Memory Search ────────────────────────")

    memory = _setup_memory()

    memory.save(MemoryItem(user_id=USER_ID, content="کاربر به موسیقی کلاسیک علاقه دارد.", memory_type="preference"))
    memory.save(MemoryItem(user_id=USER_ID, content="کاربر برنامه‌نویس Python است.", memory_type="fact"))
    memory.save(MemoryItem(user_id=USER_ID, content="درباره FastAPI و async در جلسه قبل صحبت شد.", memory_type="conversation"))

    results = memory.search("برنامه‌نویسی", user_id=USER_ID, top_k=3)
    print(f"نتایج جستجو برای «برنامه‌نویسی» ({len(results)} مورد):")
    for r in results:
        print(f"  score={r.score:.3f} | [{r.item.memory_type}] {r.item.content}")
    print()


if __name__ == "__main__":
    try:
        with make_checkpointer() as checkpointer:
            example_memory_personalization(checkpointer)
            
        example_memory_search()
    except Exception as e:
        print(f"⚠️  خطا: {e}")
        print("   مطمئن شوید Redis و Qdrant در حال اجرا هستند.")