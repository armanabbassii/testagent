"""
vector_store/chroma_store.py — پیاده‌سازی ChromaDB برای BaseVectorStore

نیازمند: chromadb (در pyproject.toml)
نصب:     uv add chromadb
حالت‌ها:
  - local (پیش‌فرض): داده روی disk ذخیره می‌شود، نیازی به سرور جداگانه نیست
  - http : اتصال به سرور Chroma جداگانه
"""

import uuid
from src.vector_store.base import BaseVectorStore, Document, SearchResult
from src.embedding_client import EmbeddingClient


class ChromaVectorStore(BaseVectorStore):
    """Vector store با backend ChromaDB.

    پارامترها:
        collection       : نام collection
        embedding_client : نمونه EmbeddingClient برای تبدیل متن→بردار
        persist_path     : مسیر ذخیره روی disk (حالت local، پیش‌فرض: ./chroma_data)
        host             : اگر داده شود، به سرور HTTP وصل می‌شود
        port             : پورت سرور HTTP (پیش‌فرض: 8000)
    """

    def __init__(
        self,
        collection: str,
        embedding_client: EmbeddingClient,
        persist_path: str = "./chroma_data",
        host: str | None = None,
        port: int = 8000,
    ) -> None:
        try:
            import chromadb
        except ImportError:
            raise ImportError("chromadb نصب نیست. دستور: uv add chromadb")

        self._collection_name = collection
        self._emb = embedding_client

        if host:
            self._client = chromadb.HttpClient(host=host, port=port)
        else:
            self._client = chromadb.PersistentClient(path=persist_path)

        # collection را می‌سازد یا موجود را برمی‌گرداند
        self._collection = self._client.get_or_create_collection(
            name=collection,
            metadata={"hnsw:space": "cosine"},
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _embed(self, text: str) -> list[float]:
        return self._emb.embed(text)

    def _to_document(self, doc_id: str, content: str, metadata: dict, embedding: list[float] | None = None) -> Document:
        return Document(
            id=doc_id,
            content=content,
            metadata=metadata,
            embedding=embedding or [],
        )

    # ── نوشتن ────────────────────────────────────────────────────────────────

    def upsert(self, documents: list[Document]) -> list[str]:
        ids, embeddings, contents, metadatas = [], [], [], []

        for doc in documents:
            doc_id = doc.id or str(uuid.uuid4())
            embedding = doc.embedding or self._embed(doc.content)
            ids.append(doc_id)
            embeddings.append(embedding)
            contents.append(doc.content)
            metadatas.append(doc.metadata or {})

        self._collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=contents,
            metadatas=metadatas,
        )
        return ids

    def delete(self, ids: list[str]) -> None:
        self._collection.delete(ids=ids)

    # ── خواندن ───────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[SearchResult]:
        query_embedding = self._embed(query)

        where = filters if filters else None

        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, self.count() or 1),
            where=where,
            include=["documents", "metadatas", "distances", "embeddings"],
        )

        search_results = []
        if not results["ids"] or not results["ids"][0]:
            return []

        for i, doc_id in enumerate(results["ids"][0]):
            # Chroma فاصله برمی‌گرداند، برای cosine: score = 1 - distance
            distance = results["distances"][0][i]
            score = 1.0 - distance

            doc = self._to_document(
                doc_id=doc_id,
                content=results["documents"][0][i],
                metadata=results["metadatas"][0][i] or {},
                embedding=results["embeddings"][0][i] if results.get("embeddings") else [],
            )
            search_results.append(SearchResult(document=doc, score=score))

        return search_results

    def get_by_id(self, doc_id: str) -> Document | None:
        results = self._collection.get(
            ids=[doc_id],
            include=["documents", "metadatas", "embeddings"],
        )
        if not results["ids"]:
            return None

        return self._to_document(
            doc_id=results["ids"][0],
            content=results["documents"][0],
            metadata=results["metadatas"][0] or {},
            embedding=results["embeddings"][0] if results.get("embeddings") else [],
        )

    # ── مدیریت ───────────────────────────────────────────────────────────────

    def create_collection(self, vector_size: int) -> None:
        # Chroma در __init__ خودکار می‌سازد، اینجا نیازی به کار نیست
        pass

    def delete_collection(self) -> None:
        self._client.delete_collection(self._collection_name)

    def count(self) -> int:
        return self._collection.count()