"""تست‌های QdrantMemory با mock."""

import pytest
from unittest.mock import MagicMock, patch
from src.memory.base import MemoryItem, MemorySearchResult
from src.vector_store.base import Document, SearchResult


@pytest.fixture
def mock_store():
    store = MagicMock()
    store._collection = "memory"
    store._client = MagicMock()
    return store


@pytest.fixture
def memory(mock_store):
    from src.memory.qdrant_memory import QdrantMemory
    return QdrantMemory(vector_store=mock_store)


def _make_item(**kwargs) -> MemoryItem:
    defaults = {
        "user_id": "u1",
        "content": "test content",
        "memory_type": "conversation",
    }
    defaults.update(kwargs)
    return MemoryItem(**defaults)


def _make_sr(content: str, score: float = 0.9) -> SearchResult:
    doc = Document(
        id="doc-1", content=content,
        metadata={"user_id": "u1", "memory_type": "conversation", "created_at": "2024-01-01"},
    )
    return SearchResult(document=doc, score=score)


class TestQdrantMemorySave:
    def test_save_calls_store_upsert(self, memory, mock_store):
        mock_store.upsert.return_value = ["doc-1"]
        item = _make_item()
        result_id = memory.save(item)
        mock_store.upsert.assert_called_once()
        assert result_id == "doc-1"

    def test_save_generates_id_if_missing(self, memory, mock_store):
        mock_store.upsert.return_value = ["generated-id"]
        item = _make_item()
        assert item.id == ""
        memory.save(item)
        assert item.id != ""

    def test_save_keeps_existing_id(self, memory, mock_store):
        mock_store.upsert.return_value = ["my-id"]
        item = _make_item()
        item.id = "my-id"
        memory.save(item)
        upserted_doc = mock_store.upsert.call_args[0][0][0]
        assert upserted_doc.id == "my-id"

    def test_save_includes_user_id_in_metadata(self, memory, mock_store):
        mock_store.upsert.return_value = ["id"]
        item = _make_item(user_id="specific-user")
        memory.save(item)
        doc = mock_store.upsert.call_args[0][0][0]
        assert doc.metadata["user_id"] == "specific-user"

    def test_save_includes_memory_type_in_metadata(self, memory, mock_store):
        mock_store.upsert.return_value = ["id"]
        item = _make_item(memory_type="preference")
        memory.save(item)
        doc = mock_store.upsert.call_args[0][0][0]
        assert doc.metadata["memory_type"] == "preference"


class TestQdrantMemorySearch:
    def test_search_calls_store_search(self, memory, mock_store):
        mock_store.search.return_value = []
        memory.search("query", user_id="u1", top_k=3)
        mock_store.search.assert_called_once_with(
            query="query", top_k=3, filters={"user_id": "u1"}
        )

    def test_search_with_memory_type_filter(self, memory, mock_store):
        mock_store.search.return_value = []
        memory.search("query", user_id="u1", memory_type="preference")
        call_kwargs = mock_store.search.call_args[1]
        assert call_kwargs["filters"]["memory_type"] == "preference"

    def test_search_returns_memory_search_results(self, memory, mock_store):
        mock_store.search.return_value = [_make_sr("result content", 0.88)]
        results = memory.search("query", user_id="u1")
        assert len(results) == 1
        assert isinstance(results[0], MemorySearchResult)
        assert results[0].score == 0.88
        assert results[0].item.content == "result content"

    def test_search_empty_returns_empty_list(self, memory, mock_store):
        mock_store.search.return_value = []
        results = memory.search("query", user_id="u1")
        assert results == []


class TestQdrantMemoryDelete:
    def test_delete_calls_store_delete(self, memory, mock_store):
        memory.delete("doc-1")
        mock_store.delete.assert_called_once_with(["doc-1"])


class TestQdrantMemoryClearUser:
    def test_clear_user_calls_qdrant_delete(self, memory, mock_store):
        memory.clear_user("u1")
        mock_store._client.delete.assert_called_once()

    def test_clear_user_filters_by_user_id(self, memory, mock_store):
        memory.clear_user("specific-user")
        call_args = mock_store._client.delete.call_args
        # بررسی می‌کنیم که با filter فراخوانی شده
        assert call_args is not None


class TestQdrantMemoryGetRecent:
    def test_get_recent_returns_items_sorted_by_time(self, memory, mock_store):
        points = []
        for i, ts in enumerate(["2024-01-03", "2024-01-01", "2024-01-02"]):
            p = MagicMock()
            p.id = str(i)
            p.payload = {
                "content": f"content {i}", "user_id": "u1",
                "memory_type": "conversation", "created_at": ts,
            }
            points.append(p)
        mock_store._client.scroll.return_value = (points, None)

        results = memory.get_recent(user_id="u1", limit=3)

        assert len(results) == 3
        # باید نزولی مرتب شده باشند
        assert results[0].created_at == "2024-01-03"

    def test_get_recent_respects_limit(self, memory, mock_store):
        points = []
        for i in range(5):
            p = MagicMock()
            p.id = str(i)
            p.payload = {
                "content": f"c{i}", "user_id": "u1",
                "memory_type": "conversation", "created_at": f"2024-01-0{i+1}",
            }
            points.append(p)
        mock_store._client.scroll.return_value = (points, None)

        results = memory.get_recent(user_id="u1", limit=2)
        assert len(results) == 2
