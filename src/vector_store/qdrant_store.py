"""
vector_store/qdrant_store.py — پیاده‌سازی Qdrant برای BaseVectorStore

نیازمند: qdrant-client>=1.10 (در pyproject.toml)
سرور:    docker run -p 6333:6333 qdrant/qdrant
"""

import uuid
from src.vector_store.base import BaseVectorStore, Document, SearchResult
from src.embedding_client import EmbeddingClient


class QdrantVectorStore(BaseVectorStore):
    """Vector store با backend Qdrant.

    پارامترها:
        collection       : نام collection در Qdrant
        embedding_client : نمونه EmbeddingClient برای تبدیل متن→بردار
        url              : آدرس Qdrant (پیش‌فرض localhost)
        api_key          : برای Qdrant Cloud (اختیاری)
    """

    def __init__(
        self,
        collection: str,
        embedding_client: EmbeddingClient,
        url: str = "http://localhost:6333",
        api_key: str | None = None,
    ) -> None:
        try:
            from qdrant_client import QdrantClient
        except ImportError:
            raise ImportError("qdrant-client نصب نیست. دستور: uv add qdrant-client")

        self._collection = collection
        self._emb = embedding_client
        self._client = QdrantClient(url=url, api_key=api_key)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _embed(self, text: str) -> list[float]:
        return self._emb.embed(text)

    def _to_document(self, point) -> Document:
        payload = point.payload or {}
        # query_points بردار را در point.vector یا point.vectors برمی‌گرداند
        vec = []
        if hasattr(point, "vector") and point.vector:
            raw = point.vector
            vec = list(raw.values()) if isinstance(raw, dict) else list(raw)
        return Document(
            id=str(point.id),
            content=payload.get("content", ""),
            metadata={k: v for k, v in payload.items() if k != "content"},
            embedding=vec,
        )

    # ── نوشتن ────────────────────────────────────────────────────────────────

    def upsert(self, documents: list[Document]) -> list[str]:
        from qdrant_client.models import PointStruct

        points = []
        ids: list[str] = []

        for doc in documents:
            doc_id = doc.id or str(uuid.uuid4())
            embedding = doc.embedding or self._embed(doc.content)
            payload = {"content": doc.content, **doc.metadata}
            points.append(PointStruct(id=doc_id, vector=embedding, payload=payload))
            ids.append(doc_id)

        self._client.upsert(collection_name=self._collection, points=points)
        return ids

    def delete(self, ids: list[str]) -> None:
        from qdrant_client.models import PointIdsList

        self._client.delete(
            collection_name=self._collection,
            points_selector=PointIdsList(points=ids),
        )

    # ── خواندن ───────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[SearchResult]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        query_vector = self._embed(query)

        qdrant_filter = None
        if filters:
            conditions = [
                FieldCondition(key=k, match=MatchValue(value=v))
                for k, v in filters.items()
            ]
            qdrant_filter = Filter(must=conditions)

        # query_points جایگزین search در qdrant-client >= 1.10
        response = self._client.query_points(
            collection_name=self._collection,
            query=query_vector,
            limit=top_k,
            query_filter=qdrant_filter,
            with_payload=True,
            with_vectors=True,
        )

        return [
            SearchResult(document=self._to_document(hit), score=hit.score)
            for hit in response.points
        ]

    def get_by_id(self, doc_id: str) -> Document | None:
        results = self._client.retrieve(
            collection_name=self._collection,
            ids=[doc_id],
            with_payload=True,
            with_vectors=True,
        )
        return self._to_document(results[0]) if results else None

    # ── مدیریت ───────────────────────────────────────────────────────────────

    def create_collection(self, vector_size: int) -> None:
        from qdrant_client.models import Distance, VectorParams

        existing = [c.name for c in self._client.get_collections().collections]
        if self._collection not in existing:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )

    def delete_collection(self) -> None:
        self._client.delete_collection(self._collection)

    def count(self) -> int:
        info = self._client.get_collection(self._collection)
        return info.points_count or 0