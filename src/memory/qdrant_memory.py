"""
memory/qdrant_memory.py — پیاده‌سازی Long-term Memory با Qdrant

از QdrantVectorStore به عنوان backend استفاده می‌کند.
هر MemoryItem به یک Document تبدیل می‌شود که metadata آن شامل
user_id، memory_type، و created_at است — این‌ها برای filter جستجو
به کار می‌روند.
"""

import uuid
from src.memory.base import BaseMemory, MemoryItem, MemorySearchResult
from src.vector_store.qdrant_store import QdrantVectorStore


class QdrantMemory(BaseMemory):
    """Long-term memory با Qdrant backend.

    پارامترها:
        vector_store : نمونه QdrantVectorStore آماده (collection ساخته شده)
    """

    def __init__(self, vector_store: QdrantVectorStore) -> None:
        self._store = vector_store

    # ── تبدیل بین MemoryItem و Document ──────────────────────────────────────

    def _to_doc_metadata(self, item: MemoryItem) -> dict:
        return {
            "user_id": item.user_id,
            "memory_type": item.memory_type,
            "created_at": item.created_at,
            **item.metadata,
        }

    def _from_search_result(self, result) -> MemorySearchResult:
        doc = result.document
        meta = doc.metadata.copy()
        return MemorySearchResult(
            item=MemoryItem(
                id=doc.id,
                user_id=meta.pop("user_id", ""),
                content=doc.content,
                memory_type=meta.pop("memory_type", "conversation"),
                created_at=meta.pop("created_at", ""),
                metadata=meta,
            ),
            score=result.score,
        )

    # ── interface ─────────────────────────────────────────────────────────────

    def save(self, item: MemoryItem) -> str:
        from src.vector_store.base import Document

        if not item.id:
            item.id = str(uuid.uuid4())

        ids = self._store.upsert([
            Document(
                id=item.id,
                content=item.content,
                metadata=self._to_doc_metadata(item),
            )
        ])
        return ids[0]

    def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[MemorySearchResult]:
        filters: dict = {"user_id": user_id}
        if memory_type:
            filters["memory_type"] = memory_type

        results = self._store.search(query=query, top_k=top_k, filters=filters)
        return [self._from_search_result(r) for r in results]

    def get_recent(
        self,
        user_id: str,
        limit: int = 10,
        memory_type: str | None = None,
    ) -> list[MemoryItem]:
        """Qdrant payload فیلتر می‌کند اما sort بر اساس زمان را به صورت client-side انجام می‌دهیم."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        must = [FieldCondition(key="user_id", match=MatchValue(value=user_id))]
        if memory_type:
            must.append(FieldCondition(key="memory_type", match=MatchValue(value=memory_type)))

        points, _ = self._store._client.scroll(
            collection_name=self._store._collection,
            scroll_filter=Filter(must=must),
            limit=limit * 3,  # بیشتر بگیر تا بعد sort کنیم
            with_payload=True,
            with_vectors=False,
        )

        items = []
        for p in points:
            payload = p.payload or {}
            meta = {k: v for k, v in payload.items()
                    if k not in ("content", "user_id", "memory_type", "created_at")}
            items.append(MemoryItem(
                id=str(p.id),
                user_id=payload.get("user_id", ""),
                content=payload.get("content", ""),
                memory_type=payload.get("memory_type", "conversation"),
                created_at=payload.get("created_at", ""),
                metadata=meta,
            ))

        # مرتب‌سازی نزولی بر اساس زمان
        items.sort(key=lambda x: x.created_at, reverse=True)
        return items[:limit]

    def delete(self, memory_id: str) -> None:
        self._store.delete([memory_id])

    def clear_user(self, user_id: str) -> None:
        from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector

        self._store._client.delete(
            collection_name=self._store._collection,
            points_selector=FilterSelector(
                filter=Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))])
            ),
        )