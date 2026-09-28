"""تست‌های IngestPipeline."""

from unittest.mock import MagicMock
from src.ingest.pipeline import IngestPipeline
from src.vector_store.base import Document


def _make_pipeline(records=None, documents=None, embed_batch_size=64):
    loader = MagicMock()
    loader.load.return_value = records if records is not None else [{"id": 1}]

    chunker = MagicMock()
    chunker.chunk.return_value = documents if documents is not None else [
        Document(content="chunk 1", id="d1"),
        Document(content="chunk 2", id="d2"),
    ]

    store = MagicMock()
    store.upsert.return_value = ["d1", "d2"]

    emb = MagicMock()
    emb.embed_batch.return_value = [[0.1, 0.2], [0.3, 0.4]]

    pipeline = IngestPipeline(
        loader=loader, chunker=chunker, vector_store=store,
        embedding_client=emb, embed_batch_size=embed_batch_size,
    )
    return pipeline, loader, chunker, store, emb


class TestIngestPipelineRun:
    def test_calls_loader_with_path(self):
        pipeline, loader, *_ = _make_pipeline()
        pipeline.run("some/path.jsonl")
        loader.load.assert_called_once_with("some/path.jsonl")

    def test_calls_chunker_with_loaded_records(self):
        records = [{"id": 1}]
        pipeline, _, chunker, *_ = _make_pipeline(records=records)
        pipeline.run("path")
        chunker.chunk.assert_called_once_with(records)

    def test_embeds_all_chunk_contents(self):
        pipeline, _, _, _, emb = _make_pipeline()
        pipeline.run("path")
        emb.embed_batch.assert_called_once_with(["chunk 1", "chunk 2"])

    def test_sets_embedding_on_documents_before_upsert(self):
        pipeline, _, _, store, _ = _make_pipeline()
        pipeline.run("path")
        upserted_docs = store.upsert.call_args[0][0]
        assert upserted_docs[0].embedding == [0.1, 0.2]
        assert upserted_docs[1].embedding == [0.3, 0.4]

    def test_returns_summary_dict(self):
        pipeline, *_ = _make_pipeline()
        assert pipeline.run("path") == {"records": 1, "chunks": 2, "upserted": 2}

    def test_empty_chunks_skips_embed_and_upsert(self):
        pipeline, _, _, store, emb = _make_pipeline(documents=[])
        result = pipeline.run("path")
        emb.embed_batch.assert_not_called()
        store.upsert.assert_not_called()
        assert result == {"records": 1, "chunks": 0, "upserted": 0}

    def test_respects_embed_batch_size(self):
        documents = [Document(content=f"c{i}", id=str(i)) for i in range(5)]
        pipeline, _, _, store, emb = _make_pipeline(documents=documents, embed_batch_size=2)
        emb.embed_batch.side_effect = lambda texts: [[0.0] for _ in texts]
        store.upsert.return_value = [d.id for d in documents]

        pipeline.run("path")

        assert emb.embed_batch.call_count == 3  # 2 + 2 + 1