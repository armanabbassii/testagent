"""تست‌های دستور ingest-business CLI."""

import pytest
from unittest.mock import patch, MagicMock
from src.cli.i18n import Translator
from src.cli.commands.ingest import run


class TestIngestCommandRun:
    @patch("src.cli.commands.ingest.IngestPipeline")
    @patch("src.cli.commands.ingest.make_vector_store")
    @patch("src.cli.commands.ingest.EmbeddingClient")
    def test_returns_zero_on_success(self, mock_emb_cls, mock_make_store, mock_pipeline_cls):
        mock_emb = MagicMock()
        mock_emb.embed.return_value = [0.1, 0.2]
        mock_emb_cls.return_value = mock_emb

        mock_store = MagicMock()
        mock_make_store.return_value = mock_store

        mock_pipeline = MagicMock()
        mock_pipeline.run.return_value = {"records": 3, "chunks": 6, "upserted": 6}
        mock_pipeline_cls.return_value = mock_pipeline

        import argparse
        args = argparse.Namespace(file="data.jsonl", collection="business_catalog", batch_size=20)
        t = Translator("en")

        result = run(args, t)
        assert result == 0
        mock_pipeline.run.assert_called_once_with("data.jsonl")

    @patch("src.cli.commands.ingest.EmbeddingClient", side_effect=Exception("connection failed"))
    def test_returns_one_on_exception(self, mock_emb_cls):
        import argparse
        args = argparse.Namespace(file="data.jsonl", collection="business_catalog")
        t = Translator("en")

        result = run(args, t)
        assert result == 1

    @patch("src.cli.commands.ingest.IngestPipeline")
    @patch("src.cli.commands.ingest.make_vector_store")
    @patch("src.cli.commands.ingest.EmbeddingClient")
    def test_uses_given_collection_name(self, mock_emb_cls, mock_make_store, mock_pipeline_cls):
        mock_emb_cls.return_value = MagicMock(embed=MagicMock(return_value=[0.1]))
        mock_pipeline_cls.return_value = MagicMock(run=MagicMock(return_value={"records": 0, "chunks": 0, "upserted": 0}))

        import argparse
        args = argparse.Namespace(file="data.jsonl", collection="custom_collection")
        t = Translator("en")

        run(args, t)
        assert mock_make_store.call_args[1]["collection"] == "custom_collection"