"""
vector_store/base.py — Abstract base class برای همه Vector Store ها

هر پیاده‌سازی باید این interface را پیاده کند تا بشه
بدون تغییر در کد بقیه پروژه، backend رو عوض کرد.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Document:
    """یک سند ذخیره‌شده در vector store.

    - id        : شناسه یکتا (اگر خالی باشد backend تولید می‌کند)
    - content   : متن اصلی
    - embedding : بردار متناظر (اختیاری — برخی backendها خودشان می‌سازند)
    - metadata  : هر داده اضافی (user_id، منبع، تاریخ، ...)
    """

    content: str
    metadata: dict = field(default_factory=dict)
    id: str = ""
    embedding: list[float] = field(default_factory=list)


@dataclass
class SearchResult:
    """نتیجه یک جستجوی semantic."""

    document: Document
    score: float  # هرچه بیشتر، شبیه‌تر (معمولاً cosine similarity)


class BaseVectorStore(ABC):
    """Interface مشترک برای همه Vector Store ها.

    الگوی استفاده:
        store = QdrantVectorStore(collection="my_col", embedding_client=emb)
        store.upsert([Document(content="متن اول", metadata={"user_id": "u1"})])
        results = store.search("جستجوی معنایی", top_k=3)
    """

    # ── نوشتن ────────────────────────────────────────────────────────────────

    @abstractmethod
    def upsert(self, documents: list[Document]) -> list[str]:
        """اضافه یا به‌روزرسانی اسناد. لیست id های ذخیره‌شده برمی‌گرداند."""
        ...

    @abstractmethod
    def delete(self, ids: list[str]) -> None:
        """اسناد با id های داده‌شده را حذف می‌کند."""
        ...

    # ── خواندن ───────────────────────────────────────────────────────────────

    @abstractmethod
    def search(
        self,
        query: str,
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[SearchResult]:
        """جستجوی semantic روی query. filters روی metadata اعمال می‌شود."""
        ...

    @abstractmethod
    def get_by_id(self, doc_id: str) -> Document | None:
        """یک سند را با id مستقیم برمی‌گرداند."""
        ...

    # ── مدیریت ───────────────────────────────────────────────────────────────

    @abstractmethod
    def create_collection(self, vector_size: int) -> None:
        """Collection / index رو می‌سازد (اگر وجود نداشته باشد)."""
        ...

    @abstractmethod
    def delete_collection(self) -> None:
        """کل collection را پاک می‌کند."""
        ...

    @abstractmethod
    def count(self) -> int:
        """تعداد اسناد ذخیره‌شده را برمی‌گرداند."""
        ...