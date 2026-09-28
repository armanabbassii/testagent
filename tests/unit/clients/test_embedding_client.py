"""تست‌های EmbeddingClient."""

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_openai():
    with patch("src.embedding_client.OpenAI") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        yield instance


def _make_embedding_response(vectors: list[list[float]]):
    response = MagicMock()
    response.data = []
    for i, vec in enumerate(vectors):
        item = MagicMock()
        item.embedding = vec
        item.index = i
        response.data.append(item)
    return response


class TestEmbeddingClientEmbed:
    def test_embed_returns_list_of_floats(self, mock_openai):
        mock_openai.embeddings.create.return_value = _make_embedding_response([[0.1, 0.2, 0.3]])
        from src.embedding_client import EmbeddingClient
        with patch("src.embedding_client.OpenAI", return_value=mock_openai):
            client = EmbeddingClient(user_id="u1")
            client._client = mock_openai
            result = client.embed("test text")
        assert isinstance(result, list)
        assert result == [0.1, 0.2, 0.3]

    def test_embed_batch_returns_multiple_vectors(self, mock_openai):
        vectors = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
        mock_openai.embeddings.create.return_value = _make_embedding_response(vectors)
        from src.embedding_client import EmbeddingClient
        with patch("src.embedding_client.OpenAI", return_value=mock_openai):
            client = EmbeddingClient(user_id="u1")
            client._client = mock_openai
            result = client.embed_batch(["a", "b", "c"])
        assert len(result) == 3
        assert result[0] == [0.1, 0.2]

    def test_embed_batch_preserves_order(self, mock_openai):
        """مرتب‌سازی بر اساس index باید ترتیب اصلی را حفظ کند."""
        # ترتیب برعکس در response
        response = MagicMock()
        items = []
        for i, vec in enumerate([[0.3, 0.4], [0.1, 0.2]]):
            item = MagicMock()
            item.embedding = vec
            item.index = 1 - i  # index برعکس
            items.append(item)
        response.data = items
        mock_openai.embeddings.create.return_value = response

        from src.embedding_client import EmbeddingClient
        with patch("src.embedding_client.OpenAI", return_value=mock_openai):
            client = EmbeddingClient(user_id="u1")
            client._client = mock_openai
            result = client.embed_batch(["a", "b"])

        # باید بر اساس index مرتب شده باشد
        assert result[0] == [0.1, 0.2]  # index=0
        assert result[1] == [0.3, 0.4]  # index=1


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self):
        from src.embedding_client import EmbeddingClient
        vec = [1.0, 0.0, 0.0]
        assert abs(EmbeddingClient.cosine_similarity(vec, vec) - 1.0) < 1e-6

    def test_orthogonal_vectors_score_zero(self):
        from src.embedding_client import EmbeddingClient
        sim = EmbeddingClient.cosine_similarity([1.0, 0.0], [0.0, 1.0])
        assert abs(sim) < 1e-6

    def test_opposite_vectors_score_minus_one(self):
        from src.embedding_client import EmbeddingClient
        sim = EmbeddingClient.cosine_similarity([1.0, 0.0], [-1.0, 0.0])
        assert abs(sim - (-1.0)) < 1e-6

    def test_zero_vector_returns_zero(self):
        from src.embedding_client import EmbeddingClient
        sim = EmbeddingClient.cosine_similarity([0.0, 0.0], [1.0, 0.0])
        assert sim == 0.0

    def test_similarity_is_symmetric(self):
        from src.embedding_client import EmbeddingClient
        a, b = [0.3, 0.4, 0.5], [0.1, 0.9, 0.2]
        assert abs(
            EmbeddingClient.cosine_similarity(a, b) -
            EmbeddingClient.cosine_similarity(b, a)
        ) < 1e-9
