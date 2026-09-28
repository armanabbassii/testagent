"""تست‌های QdrantVectorStore با mock."""

import pytest
from unittest.mock import MagicMock, patch
from src.vector_store.base import Document, SearchResult


def _make_store():
    """یک QdrantVectorStore با mock client می‌سازد."""
    mock_client = MagicMock()
    mock_emb = MagicMock()
    mock_emb.embed.return_value = [0.1, 0.2, 0.3]

    from src.vector_store.qdrant_store import QdrantVectorStore
    store = QdrantVectorStore.__new__(QdrantVectorStore)
    store._collection = "test_col"
    store._emb = mock_emb
    store._client = mock_client
    return store, mock_client, mock_emb


class TestQdrantStoreUpsert:
    def test_upsert_calls_qdrant(self):
        s, mock_client, _ = _make_store()
        s.upsert([Document(content="hello", id="d1")])
        mock_client.upsert.assert_called_once()

    def test_upsert_generates_id_if_missing(self):
        s, mock_client, _ = _make_store()
        ids = s.upsert([Document(content="no id")])
        assert len(ids) == 1 and len(ids[0]) > 0

    def test_upsert_uses_provided_id(self):
        s, mock_client, _ = _make_store()
        ids = s.upsert([Document(content="with id", id="my-id")])
        assert ids[0] == "my-id"

    def test_upsert_calls_embed_if_no_embedding(self):
        s, _, mock_emb = _make_store()
        s.upsert([Document(content="text")])
        mock_emb.embed.assert_called_once_with("text")

    def test_upsert_skips_embed_if_embedding_provided(self):
        s, _, mock_emb = _make_store()
        s.upsert([Document(content="text", embedding=[0.5, 0.6])])
        mock_emb.embed.assert_not_called()

    def test_upsert_includes_metadata_in_payload(self):
        s, mock_client, _ = _make_store()
        s.upsert([Document(content="text", metadata={"user_id": "u1"})])
        points = mock_client.upsert.call_args[1]["points"]
        assert points[0].payload["user_id"] == "u1"


class TestQdrantStoreDelete:
    def test_delete_calls_qdrant(self):
        s, mock_client, _ = _make_store()
        s.delete(["id1", "id2"])
        mock_client.delete.assert_called_once()


class TestQdrantStoreSearch:
    def _make_hit(self, doc_id="doc-1", content="text", score=0.9):
        hit = MagicMock()
        hit.id = doc_id
        hit.score = score
        hit.payload = {"content": content, "user_id": "u1"}
        hit.vector = [0.1, 0.2, 0.3]
        return hit

    def test_search_returns_search_results(self):
        s, mock_client, _ = _make_store()
        mock_client.query_points.return_value.points = [self._make_hit()]
        results = s.search("query", top_k=3)
        assert len(results) == 1
        assert isinstance(results[0], SearchResult)
        assert results[0].score == 0.9
        assert results[0].document.content == "text"

    def test_search_with_filters_sets_query_filter(self):
        s, mock_client, _ = _make_store()
        mock_client.query_points.return_value.points = []
        s.search("query", top_k=5, filters={"user_id": "u1"})
        call_kwargs = mock_client.query_points.call_args[1]
        assert call_kwargs.get("query_filter") is not None

    def test_search_without_filters_query_filter_is_none(self):
        s, mock_client, _ = _make_store()
        mock_client.query_points.return_value.points = []
        s.search("query")
        call_kwargs = mock_client.query_points.call_args[1]
        assert call_kwargs.get("query_filter") is None

    def test_search_embeds_query(self):
        s, mock_client, mock_emb = _make_store()
        mock_client.query_points.return_value.points = []
        s.search("my question")
        mock_emb.embed.assert_called_once_with("my question")

    def test_search_empty_results(self):
        s, mock_client, _ = _make_store()
        mock_client.query_points.return_value.points = []
        results = s.search("query")
        assert results == []


class TestQdrantStoreGetById:
    def test_get_by_id_returns_document(self):
        s, mock_client, _ = _make_store()
        point = MagicMock()
        point.id = "doc-1"
        point.payload = {"content": "the content", "tag": "v1"}
        point.vector = [0.1]
        mock_client.retrieve.return_value = [point]
        doc = s.get_by_id("doc-1")
        assert doc is not None
        assert doc.content == "the content"

    def test_get_by_id_returns_none_if_not_found(self):
        s, mock_client, _ = _make_store()
        mock_client.retrieve.return_value = []
        assert s.get_by_id("nonexistent") is None


class TestQdrantStoreCollection:
    def test_create_collection_not_called_if_exists(self):
        s, mock_client, _ = _make_store()
        col = MagicMock()
        col.name = "test_col"
        mock_client.get_collections.return_value.collections = [col]
        s.create_collection(vector_size=3)
        mock_client.create_collection.assert_not_called()

    def test_create_collection_called_if_missing(self):
        s, mock_client, _ = _make_store()
        mock_client.get_collections.return_value.collections = []
        s.create_collection(vector_size=3)
        mock_client.create_collection.assert_called_once()

    def test_delete_collection(self):
        s, mock_client, _ = _make_store()
        s.delete_collection()
        mock_client.delete_collection.assert_called_once_with("test_col")

    def test_count_returns_correct_number(self):
        s, mock_client, _ = _make_store()
        mock_client.get_collection.return_value.points_count = 42
        assert s.count() == 42

    def test_count_none_returns_zero(self):
        s, mock_client, _ = _make_store()
        mock_client.get_collection.return_value.points_count = None
        assert s.count() == 0
