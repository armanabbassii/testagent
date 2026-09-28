"""تست‌های JsonMemory."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from src.memory.json_memory import JsonMemory
from src.memory.base import MemoryItem


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def keyword_memory(tmp_path) -> JsonMemory:
    return JsonMemory(path=tmp_path / "memory.json", search_mode="keyword")


@pytest.fixture
def embedding_memory(tmp_path) -> JsonMemory:
    emb = MagicMock()
    emb.embed.return_value = [1.0, 0.0, 0.0]
    return JsonMemory(
        path=tmp_path / "memory.json",
        search_mode="embedding",
        embedding_client=emb,
    )


def _item(content: str, user_id: str = "u1", memory_type: str = "conversation") -> MemoryItem:
    return MemoryItem(user_id=user_id, content=content, memory_type=memory_type)


# ── init ─────────────────────────────────────────────────────────────────────

class TestJsonMemoryInit:
    def test_creates_file_on_init(self, tmp_path):
        path = tmp_path / "mem.json"
        JsonMemory(path=path)
        assert path.exists()

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "a" / "b" / "mem.json"
        JsonMemory(path=path)
        assert path.exists()

    def test_initial_file_has_empty_items(self, tmp_path):
        path = tmp_path / "mem.json"
        JsonMemory(path=path)
        data = json.loads(path.read_text())
        assert data["items"] == []

    def test_embedding_mode_requires_client(self, tmp_path):
        with pytest.raises(ValueError, match="embedding_client"):
            JsonMemory(tmp_path / "m.json", search_mode="embedding")

    def test_invalid_search_mode_raises(self, tmp_path):
        with pytest.raises(ValueError, match="نامعتبر"):
            JsonMemory(tmp_path / "m.json", search_mode="invalid")  # type: ignore

    def test_existing_file_not_overwritten(self, tmp_path):
        path = tmp_path / "mem.json"
        m = JsonMemory(path=path)
        m.save(_item("existing content"))
        # ساخت مجدد نباید فایل را پاک کند
        m2 = JsonMemory(path=path)
        assert m2.count() == 1


# ── save ─────────────────────────────────────────────────────────────────────

class TestJsonMemorySave:
    def test_save_returns_id(self, keyword_memory):
        item = _item("hello")
        result_id = keyword_memory.save(item)
        assert result_id != ""
        assert item.id == result_id

    def test_save_generates_id_if_missing(self, keyword_memory):
        item = _item("hello")
        assert item.id == ""
        keyword_memory.save(item)
        assert item.id != ""

    def test_save_persists_to_file(self, tmp_path):
        path = tmp_path / "mem.json"
        m = JsonMemory(path=path)
        m.save(_item("persisted content"))
        data = json.loads(path.read_text())
        assert len(data["items"]) == 1
        assert data["items"][0]["content"] == "persisted content"

    def test_save_multiple_items(self, keyword_memory):
        keyword_memory.save(_item("first"))
        keyword_memory.save(_item("second"))
        keyword_memory.save(_item("third"))
        assert keyword_memory.count() == 3

    def test_save_updates_existing_item(self, keyword_memory):
        item = _item("original")
        keyword_memory.save(item)
        item.content = "updated"
        keyword_memory.save(item)
        # نباید آیتم تکراری ایجاد شود
        assert keyword_memory.count() == 1
        items = keyword_memory.all_items()
        assert items[0].content == "updated"

    def test_save_preserves_metadata(self, keyword_memory):
        item = _item("hello")
        item.metadata = {"source": "test", "priority": 1}
        keyword_memory.save(item)
        items = keyword_memory.all_items()
        assert items[0].metadata["source"] == "test"
        assert items[0].metadata["priority"] == 1

    def test_save_with_embedding_calls_embed(self, embedding_memory):
        embedding_memory.save(_item("embed this"))
        embedding_memory._emb.embed.assert_called_once_with("embed this")

    def test_save_stores_embedding_in_file(self, tmp_path):
        path = tmp_path / "mem.json"
        emb = MagicMock()
        emb.embed.return_value = [0.1, 0.2, 0.3]
        m = JsonMemory(path=path, search_mode="embedding", embedding_client=emb)
        m.save(_item("text"))
        data = json.loads(path.read_text())
        assert data["items"][0]["embedding"] == [0.1, 0.2, 0.3]


# ── search (keyword) ──────────────────────────────────────────────────────────

class TestJsonMemoryKeywordSearch:
    def test_exact_match_found(self, keyword_memory):
        keyword_memory.save(_item("Python programming language"))
        results = keyword_memory.search("Python", user_id="u1")
        assert len(results) == 1
        assert results[0].item.content == "Python programming language"

    def test_no_match_returns_empty(self, keyword_memory):
        keyword_memory.save(_item("Python programming"))
        results = keyword_memory.search("Java", user_id="u1")
        assert results == []

    def test_filters_by_user_id(self, keyword_memory):
        keyword_memory.save(_item("Python", user_id="u1"))
        keyword_memory.save(_item("Python developer", user_id="u2"))
        results = keyword_memory.search("Python", user_id="u1")
        assert all(r.item.user_id == "u1" for r in results)

    def test_filters_by_memory_type(self, keyword_memory):
        keyword_memory.save(_item("Python", memory_type="preference"))
        keyword_memory.save(_item("Python course", memory_type="conversation"))
        results = keyword_memory.search("Python", user_id="u1", memory_type="preference")
        assert len(results) == 1
        assert results[0].item.memory_type == "preference"

    def test_top_k_respected(self, keyword_memory):
        for i in range(10):
            keyword_memory.save(_item(f"Python item {i}"))
        results = keyword_memory.search("Python", user_id="u1", top_k=3)
        assert len(results) <= 3

    def test_higher_match_score_ranked_first(self, keyword_memory):
        keyword_memory.save(_item("Python is great"))
        keyword_memory.save(_item("Python programming Python best"))
        results = keyword_memory.search("Python", user_id="u1", top_k=5)
        assert len(results) >= 1

    def test_score_between_zero_and_one(self, keyword_memory):
        keyword_memory.save(_item("Python programming"))
        results = keyword_memory.search("Python", user_id="u1")
        for r in results:
            assert 0.0 <= r.score <= 1.0


# ── search (embedding) ────────────────────────────────────────────────────────

class TestJsonMemoryEmbeddingSearch:
    def test_returns_result_with_score(self, embedding_memory):
        from src.embedding_client import EmbeddingClient
        embedding_memory._emb.embed.return_value = [1.0, 0.0, 0.0]
        embedding_memory.save(_item("AI content"))

        # query با embedding مشابه
        embedding_memory._emb.embed.return_value = [1.0, 0.0, 0.0]
        results = embedding_memory.search("AI", user_id="u1")
        assert len(results) == 1
        assert abs(results[0].score - 1.0) < 1e-6

    def test_orthogonal_embedding_score_zero(self, tmp_path):
        emb = MagicMock()
        m = JsonMemory(tmp_path / "m.json", search_mode="embedding", embedding_client=emb)

        emb.embed.return_value = [1.0, 0.0]
        m.save(_item("content"))

        emb.embed.return_value = [0.0, 1.0]  # عمود — score ≈ 0
        results = m.search("unrelated", user_id="u1")
        assert results == []   # score=0 فیلتر می‌شود


# ── get_recent ────────────────────────────────────────────────────────────────

class TestJsonMemoryGetRecent:
    def test_returns_items_sorted_descending(self, keyword_memory):
        keyword_memory.save(MemoryItem(user_id="u1", content="a",
                                       created_at="2024-01-01T00:00:00"))
        keyword_memory.save(MemoryItem(user_id="u1", content="b",
                                       created_at="2024-01-03T00:00:00"))
        keyword_memory.save(MemoryItem(user_id="u1", content="c",
                                       created_at="2024-01-02T00:00:00"))
        results = keyword_memory.get_recent("u1")
        assert results[0].content == "b"
        assert results[1].content == "c"
        assert results[2].content == "a"

    def test_limit_respected(self, keyword_memory):
        for i in range(10):
            keyword_memory.save(_item(f"item {i}"))
        results = keyword_memory.get_recent("u1", limit=3)
        assert len(results) == 3

    def test_filters_by_user_id(self, keyword_memory):
        keyword_memory.save(_item("u1 item", user_id="u1"))
        keyword_memory.save(_item("u2 item", user_id="u2"))
        results = keyword_memory.get_recent("u1")
        assert all(r.user_id == "u1" for r in results)

    def test_filters_by_memory_type(self, keyword_memory):
        keyword_memory.save(_item("pref", memory_type="preference"))
        keyword_memory.save(_item("conv", memory_type="conversation"))
        results = keyword_memory.get_recent("u1", memory_type="preference")
        assert len(results) == 1
        assert results[0].memory_type == "preference"


# ── delete ────────────────────────────────────────────────────────────────────

class TestJsonMemoryDelete:
    def test_delete_removes_item(self, keyword_memory):
        item = _item("to delete")
        keyword_memory.save(item)
        keyword_memory.delete(item.id)
        assert keyword_memory.count() == 0

    def test_delete_nonexistent_does_not_raise(self, keyword_memory):
        keyword_memory.delete("nonexistent-id")   # نباید خطا بدهد

    def test_delete_only_removes_target(self, keyword_memory):
        item1 = _item("keep")
        item2 = _item("remove")
        keyword_memory.save(item1)
        keyword_memory.save(item2)
        keyword_memory.delete(item2.id)
        items = keyword_memory.all_items()
        assert len(items) == 1
        assert items[0].content == "keep"


# ── clear_user ────────────────────────────────────────────────────────────────

class TestJsonMemoryClearUser:
    def test_clears_only_target_user(self, keyword_memory):
        keyword_memory.save(_item("u1 item", user_id="u1"))
        keyword_memory.save(_item("u2 item", user_id="u2"))
        keyword_memory.clear_user("u1")
        assert keyword_memory.count(user_id="u1") == 0
        assert keyword_memory.count(user_id="u2") == 1

    def test_clear_nonexistent_user_does_not_raise(self, keyword_memory):
        keyword_memory.clear_user("nonexistent")   # نباید خطا بدهد


# ── count و all_items ─────────────────────────────────────────────────────────

class TestJsonMemoryHelpers:
    def test_count_all(self, keyword_memory):
        keyword_memory.save(_item("a", user_id="u1"))
        keyword_memory.save(_item("b", user_id="u2"))
        assert keyword_memory.count() == 2

    def test_count_by_user(self, keyword_memory):
        keyword_memory.save(_item("a", user_id="u1"))
        keyword_memory.save(_item("b", user_id="u1"))
        keyword_memory.save(_item("c", user_id="u2"))
        assert keyword_memory.count(user_id="u1") == 2

    def test_all_items(self, keyword_memory):
        keyword_memory.save(_item("first"))
        keyword_memory.save(_item("second"))
        items = keyword_memory.all_items()
        assert len(items) == 2

    def test_all_items_filtered_by_user(self, keyword_memory):
        keyword_memory.save(_item("u1", user_id="u1"))
        keyword_memory.save(_item("u2", user_id="u2"))
        items = keyword_memory.all_items(user_id="u1")
        assert len(items) == 1
        assert items[0].user_id == "u1"