"""تست‌های Document و SearchResult از base.py."""

import pytest
from src.vector_store.base import Document, SearchResult


class TestDocument:
    def test_default_values(self):
        doc = Document(content="hello")
        assert doc.id == ""
        assert doc.metadata == {}
        assert doc.embedding == []

    def test_with_all_fields(self):
        doc = Document(
            content="test",
            metadata={"user_id": "u1"},
            id="doc-1",
            embedding=[0.1, 0.2],
        )
        assert doc.id == "doc-1"
        assert doc.metadata["user_id"] == "u1"
        assert doc.embedding == [0.1, 0.2]

    def test_content_is_required(self):
        with pytest.raises(TypeError):
            Document()   # type: ignore


class TestSearchResult:
    def test_search_result_fields(self):
        doc = Document(content="test", id="d1")
        result = SearchResult(document=doc, score=0.95)
        assert result.document is doc
        assert result.score == 0.95

    def test_score_can_be_zero(self):
        doc = Document(content="test")
        result = SearchResult(document=doc, score=0.0)
        assert result.score == 0.0