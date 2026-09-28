"""تست‌های vector_store factory."""

import pytest
from unittest.mock import patch, MagicMock


class TestMakeVectorStore:
    @patch("src.vector_store.qdrant_store.QdrantVectorStore")
    def test_qdrant_backend_selected(self, mock_qdrant):
        mock_qdrant.return_value = MagicMock()
        with patch.dict("os.environ", {"VECTOR_STORE_BACKEND": "qdrant"}):
            from src.vector_store.factory import make_vector_store
            make_vector_store(collection="c", embedding_client=MagicMock())
        mock_qdrant.assert_called_once()

    @patch("src.vector_store.chroma_store.ChromaVectorStore")
    def test_chroma_backend_selected(self, mock_chroma):
        mock_chroma.return_value = MagicMock()
        with patch.dict("os.environ", {"VECTOR_STORE_BACKEND": "chroma"}):
            from src.vector_store.factory import make_vector_store
            make_vector_store(collection="c", embedding_client=MagicMock())
        mock_chroma.assert_called_once()

    def test_default_backend_is_qdrant(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("VECTOR_STORE_BACKEND", None)
            with patch("src.vector_store.qdrant_store.QdrantVectorStore") as mock_qdrant:
                mock_qdrant.return_value = MagicMock()
                from src.vector_store.factory import make_vector_store
                make_vector_store(collection="c", embedding_client=MagicMock())
            mock_qdrant.assert_called_once()

    def test_invalid_backend_raises(self):
        with patch.dict("os.environ", {"VECTOR_STORE_BACKEND": "oracle"}):
            from src.vector_store.factory import make_vector_store
            with pytest.raises(ValueError, match="نامعتبر"):
                make_vector_store(collection="c", embedding_client=MagicMock())

    @patch("src.vector_store.qdrant_store.QdrantVectorStore")
    def test_qdrant_reads_url_from_env(self, mock_qdrant):
        mock_qdrant.return_value = MagicMock()
        with patch.dict("os.environ", {
            "VECTOR_STORE_BACKEND": "qdrant",
            "QDRANT_URL": "http://custom:6333",
        }):
            from src.vector_store.factory import make_vector_store
            make_vector_store(collection="c", embedding_client=MagicMock())
        assert mock_qdrant.call_args[1]["url"] == "http://custom:6333"

    @patch("src.vector_store.chroma_store.ChromaVectorStore")
    def test_chroma_reads_persist_path_from_env(self, mock_chroma):
        mock_chroma.return_value = MagicMock()
        with patch.dict("os.environ", {
            "VECTOR_STORE_BACKEND": "chroma",
            "CHROMA_PERSIST_PATH": "/tmp/custom_chroma",
        }):
            from src.vector_store.factory import make_vector_store
            make_vector_store(collection="c", embedding_client=MagicMock())
        assert mock_chroma.call_args[1]["persist_path"] == "/tmp/custom_chroma"