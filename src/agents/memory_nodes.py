"""
memory_nodes.py — node های load و save حافظه بلندمدت در گراف

توپولوژی با memory:
    START → memory_loader → router → (research|summarizer|general) → memory_saver → END

MemoryLoaderNode:
  - آخرین پیام کاربر را می‌گیرد
  - خاطرات مرتبط را از Qdrant جستجو می‌کند
  - نتایج را در state["memory_context"] می‌ریزد تا ایجنت‌ها استفاده کنند

MemorySaverNode:
  - آخرین پیام کاربر + پاسخ آخرین ایجنت را می‌گیرد
  - خلاصه‌ای به عنوان خاطره جدید ذخیره می‌کند
  - برای ذخیره حقایق مهم قابل extend است
"""

from langchain_core.messages import HumanMessage, AIMessage
from src.agents.state import AgentState
from src.memory.base import BaseMemory, MemoryItem


class MemoryLoaderNode:
    """قبل از router اجرا می‌شود و memory_context را پر می‌کند."""

    name = "memory_loader"

    def __init__(self, memory: BaseMemory, top_k: int = 5) -> None:
        self._memory = memory
        self._top_k = top_k

    def __call__(self, state: AgentState) -> dict:
        user_id = state["user_id"]

        # آخرین پیام کاربر را به عنوان query جستجو استفاده می‌کنیم
        query = ""
        for msg in reversed(state["messages"]):
            if isinstance(msg, HumanMessage):
                query = msg.content
                break

        if not query:
            return {"memory_context": []}

        results = self._memory.search(
            query=query,
            user_id=user_id,
            top_k=self._top_k,
        )

        # هر خاطره را به یک رشته متنی تبدیل می‌کنیم
        context = [
            f"[{r.item.memory_type} | score={r.score:.2f}] {r.item.content}"
            for r in results
        ]

        return {"memory_context": context}


class MemorySaverNode:
    """بعد از ایجنت پاسخ‌دهنده اجرا می‌شود و مکالمه را ذخیره می‌کند."""

    name = "memory_saver"

    def __init__(self, memory: BaseMemory) -> None:
        self._memory = memory

    def __call__(self, state: AgentState) -> dict:
        user_id = state["user_id"]
        messages = state["messages"]

        # آخرین پیام کاربر
        user_text = ""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                user_text = msg.content
                break

        # آخرین پاسخ ایجنت (نه router)
        agent_text = ""
        agent_name = ""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and getattr(msg, "name", None) not in (None, "router"):
                agent_text = msg.content
                agent_name = msg.name or ""
                break

        if not user_text or not agent_text:
            return {}

        # ذخیره به عنوان یک خاطره مکالمه
        content = f"User: {user_text}\n{agent_name}: {agent_text}"
        self._memory.save(MemoryItem(
            user_id=user_id,
            content=content,
            memory_type="conversation",
            metadata={"agent": agent_name},
        ))

        return {}