"""تست‌های دستور ingest-technical CLI."""

import pytest
from unittest.mock import patch, MagicMock
from src.cli.i18n import Translator
from src.cli.commands.ingest_technical import run


class TestIngestTechnicalCommandRun:
    @patch("src.cli.commands.ingest_technical.IngestPipeline")
    @patch("src.cli.commands.ingest_technical.make_vector_store")
    @patch("src.cli.commands.ingest_technical.EmbeddingClient")
    def test_returns_zero_on_success(self, mock_emb_cls, mock_make_store, mock_pipeline_cls):
        mock_emb = MagicMock()
        mock_emb.embed.return_value = [0.1, 0.2]
        mock_emb_cls.return_value = mock_emb

        mock_store = MagicMock()
        mock_make_store.return_value = mock_store

        mock_pipeline = MagicMock()
        mock_pipeline.run.return_value = {"records": 5, "chunks": 12, "upserted": 12}
        mock_pipeline_cls.return_value = mock_pipeline

        import argparse
        args = argparse.Namespace(path="/repo/java-app", collection="technical_catalog", no_html=False)
        t = Translator("en")

        result = run(args, t)
        assert result == 0
        mock_pipeline.run.assert_called_once_with("/repo/java-app")

    @patch("src.cli.commands.ingest_technical.EmbeddingClient", side_effect=Exception("connection failed"))
    def test_returns_one_on_exception(self, mock_emb_cls):
        import argparse
        args = argparse.Namespace(path="/repo/java-app", collection="technical_catalog", no_html=False)
        t = Translator("en")

        result = run(args, t)
        assert result == 1

    @patch("src.cli.commands.ingest_technical.IngestPipeline")
    @patch("src.cli.commands.ingest_technical.make_vector_store")
    @patch("src.cli.commands.ingest_technical.EmbeddingClient")
    def test_uses_given_collection_name(self, mock_emb_cls, mock_make_store, mock_pipeline_cls):
        mock_emb_cls.return_value = MagicMock(embed=MagicMock(return_value=[0.1]))
        mock_pipeline_cls.return_value = MagicMock(run=MagicMock(return_value={"records": 0, "chunks": 0, "upserted": 0}))

        import argparse
        args = argparse.Namespace(path="/repo", collection="custom_technical", no_html=False)
        t = Translator("en")

        run(args, t)
        assert mock_make_store.call_args[1]["collection"] == "custom_technical"

    @patch("src.cli.commands.ingest_technical.IngestPipeline")
    @patch("src.cli.commands.ingest_technical.make_vector_store")
    @patch("src.cli.commands.ingest_technical.EmbeddingClient")
    def test_no_html_flag_passed_to_loader(self, mock_emb_cls, mock_make_store, mock_pipeline_cls):
        mock_emb_cls.return_value = MagicMock(embed=MagicMock(return_value=[0.1]))
        mock_pipeline_cls.return_value = MagicMock(run=MagicMock(return_value={"records": 0, "chunks": 0, "upserted": 0}))

        import argparse
        args = argparse.Namespace(path="/repo", collection="technical_catalog", no_html=True)
        t = Translator("en")

        run(args, t)
        loader_used = mock_pipeline_cls.call_args[1]["loader"]
        assert loader_used._include_html is False
