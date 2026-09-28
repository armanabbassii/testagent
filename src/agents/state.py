"""
state.py — تعریف State مشترک بین تمام ایجنت‌ها در گراف

LangGraph با TypedDict کار می‌کند، نه dataclass.
دسترسی به فیلدها باید از طریق state["key"] باشد.
"""

from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    """State مشترکی که بین node های گراف رد و بدل می‌شود.

    - messages       : تاریخچه کامل مکالمه (با reducer خودکار LangGraph)
    - user_id        : شناسه کاربر، برای پاس دادن به LLM/Embedding client
    - metadata       : داده‌های اضافی که ایجنت‌ها می‌توانند بنویسند/بخوانند
    - memory_context : خاطرات بازیابی‌شده از long-term memory
    - rag_context    : اسناد بازیابی‌شده از RAGAgent (هر کدام با متن و منبع)
    - hitl_decision  : نتیجه بررسی انسانی — None/True/False
    """

    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    metadata: dict
    memory_context: list[str]
    rag_context: list[dict]
    hitl_decision: bool | None