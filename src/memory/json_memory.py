"""
memory/json_memory.py — پیاده‌سازی Long-term Memory با فایل JSON

مناسب برای:
  - توسعه و تست (بدون نیاز به Qdrant)
  - پروژه‌های کوچک و single-user
  - محیط‌هایی که نصب سرور vector خارجی ممکن نیست

حالت‌های جستجو (search_mode):
  - "embedding" : cosine similarity روی embedding — دقیق‌تر، نیاز به EmbeddingClient
  - "keyword"   : جستجوی کلمه‌کلیدی ساده — بدون embedding، سریع‌تر

ساختار فایل JSON:
  {
    "items": [
      {
        "id": "uuid",
        "user_id": "u1",
        "content": "...",
        "memory_type": "conversation",
        "created_at": "2024-01-01T00:00:00+00:00",
        "metadata": {},
        "embedding": [...]   # فقط در حالت embedding
      }
    ]
  }
"""

import json
import uuid
import threading
from pathlib import Path
from typing import Literal

from src.memory.base import BaseMemory, MemoryItem, MemorySearchResult

SearchMode = Literal["embedding", "keyword"]


class JsonMemory(BaseMemory):
    """Long-term memory با فایل JSON روی disk.

    پارامترها:
        path        : مسیر فایل JSON (پوشه‌های ناموجود ساخته می‌شوند)
        search_mode : "embedding" یا "keyword"
        embedding_client : نمونه EmbeddingClient (فقط برای search_mode="embedding")
    """

    def __init__(
        self,
        path: str | Path = "memory.json",
        search_mode: SearchMode = "keyword",
        embedding_client=None,
    ) -> None:
        if search_mode == "embedding" and embedding_client is None:
            raise ValueError(
                "search_mode='embedding' نیاز به embedding_client دارد."
            )
        if search_mode not in ("embedding", "keyword"):
            raise ValueError(
                f"search_mode نامعتبر: '{search_mode}'. مقادیر مجاز: embedding, keyword"
            )

        self._path = Path(path)
        self._search_mode = search_mode
        self._emb = embedding_client
        self._lock = threading.Lock()

        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write({"items": []})

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return {"items": []}

    def _write(self, data: dict) -> None:
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── تبدیل بین MemoryItem و dict ──────────────────────────────────────────

    @staticmethod
    def _to_dict(item: MemoryItem, embedding: list[float] | None = None) -> dict:
        return {
            "id": item.id,
            "user_id": item.user_id,
            "content": item.content,
            "memory_type": item.memory_type,
            "created_at": item.created_at,
            "metadata": item.metadata,
            "embedding": embedding or [],
        }

    @staticmethod
    def _from_dict(d: dict) -> MemoryItem:
        return MemoryItem(
            id=d["id"],
            user_id=d["user_id"],
            content=d["content"],
            memory_type=d.get("memory_type", "conversation"),
            created_at=d.get("created_at", ""),
            metadata=d.get("metadata", {}),
        )

    # ── جستجو ────────────────────────────────────────────────────────────────

    def _score_embedding(self, query: str, record: dict) -> float:
        """امتیاز cosine similarity بین query و embedding ذخیره‌شده."""
        stored_emb = record.get("embedding", [])
        if not stored_emb:
            return 0.0
        query_emb = self._emb.embed(query)
        from src.embedding_client import EmbeddingClient
        return EmbeddingClient.cosine_similarity(query_emb, stored_emb)

    def _score_keyword(self, query: str, record: dict) -> float:
        """امتیاز keyword matching — نسبت کلمات مشترک به کل کلمات query."""
        query_words = set(query.lower().split())
        content_words = set(record.get("content", "").lower().split())
        if not query_words:
            return 0.0
        common = query_words & content_words
        return len(common) / len(query_words)

    def _score(self, query: str, record: dict) -> float:
        if self._search_mode == "embedding":
            return self._score_embedding(query, record)
        return self._score_keyword(query, record)

    # ── interface ─────────────────────────────────────────────────────────────

    def save(self, item: MemoryItem) -> str:
        if not item.id:
            item.id = str(uuid.uuid4())

        embedding: list[float] = []
        if self._search_mode == "embedding":
            embedding = self._emb.embed(item.content)

        with self._lock:
            data = self._read()
            # اگر id از قبل وجود داشت، آپدیت کن
            existing = next((i for i, r in enumerate(data["items"]) if r["id"] == item.id), None)
            record = self._to_dict(item, embedding)
            if existing is not None:
                data["items"][existing] = record
            else:
                data["items"].append(record)
            self._write(data)

        return item.id

    def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[MemorySearchResult]:
        with self._lock:
            data = self._read()

        candidates = [
            r for r in data["items"]
            if r["user_id"] == user_id
            and (memory_type is None or r.get("memory_type") == memory_type)
        ]

        scored = [
            (r, self._score(query, r))
            for r in candidates
        ]
        # مرتب‌سازی نزولی بر اساس امتیاز
        scored.sort(key=lambda x: x[1], reverse=True)

        return [
            MemorySearchResult(item=self._from_dict(r), score=score)
            for r, score in scored[:top_k]
            if score > 0.0
        ]

    def get_recent(
        self,
        user_id: str,
        limit: int = 10,
        memory_type: str | None = None,
    ) -> list[MemoryItem]:
        with self._lock:
            data = self._read()

        items = [
            self._from_dict(r)
            for r in data["items"]
            if r["user_id"] == user_id
            and (memory_type is None or r.get("memory_type") == memory_type)
        ]

        items.sort(key=lambda x: x.created_at, reverse=True)
        return items[:limit]

    def delete(self, memory_id: str) -> None:
        with self._lock:
            data = self._read()
            data["items"] = [r for r in data["items"] if r["id"] != memory_id]
            self._write(data)

    def clear_user(self, user_id: str) -> None:
        with self._lock:
            data = self._read()
            data["items"] = [r for r in data["items"] if r["user_id"] != user_id]
            self._write(data)

    # ── متدهای کمکی ──────────────────────────────────────────────────────────

    def count(self, user_id: str | None = None) -> int:
        """تعداد خاطرات — اگر user_id داده شود، فقط برای آن کاربر."""
        with self._lock:
            data = self._read()
        if user_id is None:
            return len(data["items"])
        return sum(1 for r in data["items"] if r["user_id"] == user_id)

    def all_items(self, user_id: str | None = None) -> list[MemoryItem]:
        """همه خاطرات — برای debug یا export."""
        with self._lock:
            data = self._read()
        records = data["items"]
        if user_id is not None:
            records = [r for r in records if r["user_id"] == user_id]
        return [self._from_dict(r) for r in records]