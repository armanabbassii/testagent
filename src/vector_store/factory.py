"""
vector_store/factory.py — factory برای انتخاب vector store backend از .env

متغیر تعیین‌کننده:
    VECTOR_STORE_BACKEND=qdrant | chroma   (پیش‌فرض: qdrant)

تنظیمات per-backend:
    Qdrant : QDRANT_URL (پیش‌فرض: http://localhost:6333)، QDRANT_API_KEY (اختیاری)
    Chroma : CHROMA_PERSIST_PATH (پیش‌فرض: ./chroma_data)، CHROMA_HOST، CHROMA_PORT

نحوه استفاده:
    from src.vector_store.factory import make_vector_store

    store = make_vector_store(collection="business_catalog", embedding_client=emb)
"""

import os
from src.embedding_client import EmbeddingClient
from src.vector_store.base import BaseVectorStore

_SUPPORTED = ("qdrant", "chroma")


def make_vector_store(collection: str, embedding_client: EmbeddingClient) -> BaseVectorStore:
    """backend را از VECTOR_STORE_BACKEND در .env می‌خواند و vector store می‌سازد.

    مثال .env:
        # روی سرور
        VECTOR_STORE_BACKEND=qdrant
        QDRANT_URL=http://localhost:6333

        # روی سیستم شخصی
        VECTOR_STORE_BACKEND=chroma
        CHROMA_PERSIST_PATH=./chroma_data
    """
    backend = os.getenv("VECTOR_STORE_BACKEND", "qdrant").lower().strip()

    if backend == "qdrant":
        from src.vector_store.qdrant_store import QdrantVectorStore
        url = os.getenv("QDRANT_URL", "http://localhost:6333")
        api_key = os.getenv("QDRANT_API_KEY") or None
        return QdrantVectorStore(collection=collection, embedding_client=embedding_client,
                                  url=url, api_key=api_key)

    if backend == "chroma":
        from src.vector_store.chroma_store import ChromaVectorStore
        persist_path = os.getenv("CHROMA_PERSIST_PATH", "./chroma_data")
        host = os.getenv("CHROMA_HOST") or None
        port = int(os.getenv("CHROMA_PORT", "8000"))
        return ChromaVectorStore(collection=collection, embedding_client=embedding_client,
                                  persist_path=persist_path, host=host, port=port)

    raise ValueError(
        f"VECTOR_STORE_BACKEND نامعتبر: '{backend}'. "
        f"مقادیر مجاز: {', '.join(_SUPPORTED)}"
    )