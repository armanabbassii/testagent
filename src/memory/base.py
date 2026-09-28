"""
memory/base.py — Abstract base class برای Long-term Memory

طراحی:
  - هر "خاطره" یک MemoryItem است با content، user_id، و metadata
  - backend می‌تواند هر چیزی باشد: Qdrant، Redis، فایل JSON، ...
  - ایجنت‌ها فقط با این interface کار می‌کنند، نه با backend مستقیم
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class MemoryItem:
    """یک واحد حافظه بلندمدت.

    - user_id   : حافظه به یک کاربر خاص تعلق دارد
    - content   : متن خاطره (خلاصه مکالمه، حقیقت آموخته‌شده، ...)
    - memory_type: دسته‌بندی (conversation | fact | preference | ...)
    - metadata  : هر داده اضافی (session_id، agent_name، ...)
    - created_at: زمان ثبت (ISO format)
    - id        : شناسه یکتا (backend تولید می‌کند)
    """

    user_id: str
    content: str
    memory_type: str = "conversation"
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    id: str = ""


@dataclass
class MemorySearchResult:
    item: MemoryItem
    score: float


class BaseMemory(ABC):
    """Interface مشترک برای همه Memory backend ها."""

    @abstractmethod
    def save(self, item: MemoryItem) -> str:
        """یک خاطره را ذخیره می‌کند. id ذخیره‌شده را برمی‌گرداند."""
        ...

    @abstractmethod
    def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[MemorySearchResult]:
        """خاطرات مرتبط با query را برای یک کاربر جستجو می‌کند."""
        ...

    @abstractmethod
    def get_recent(
        self,
        user_id: str,
        limit: int = 10,
        memory_type: str | None = None,
    ) -> list[MemoryItem]:
        """آخرین خاطرات کاربر را برمی‌گرداند (بر اساس زمان)."""
        ...

    @abstractmethod
    def delete(self, memory_id: str) -> None:
        """یک خاطره را با id حذف می‌کند."""
        ...

    @abstractmethod
    def clear_user(self, user_id: str) -> None:
        """همه خاطرات یک کاربر را پاک می‌کند."""
        ...