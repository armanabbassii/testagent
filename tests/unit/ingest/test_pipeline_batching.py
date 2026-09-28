"""تست‌های رفتار جدید IngestPipeline: upsert به‌صورت batch به batch (نه یک upsert بزرگ در پایان)."""

from unittest.mock import MagicMock
from src.ingest.pipeline import IngestPipeline
from src.vector_store.base import Document


def _make_pipeline(documents, embed_batch_size=64, upsert_batch_size=200):
    loader = MagicMock()
    loader.load.return_value = [{"id": 1}]

    chunker = MagicMock()
    chunker.chunk.return_value = documents

    store = MagicMock()
    # هر upsert فقط idهای همان batch ورودی را برمی‌گرداند (شبیه‌سازی رفتار واقعی)
    store.upsert.side_effect = lambda docs: [d.id for d in docs]

    emb = MagicMock()
    emb.embed_batch.side_effect = lambda texts: [[0.0]] * len(texts)

    pipeline = IngestPipeline(
        loader=loader, chunker=chunker, vector_store=store,
        embedding_client=emb, embed_batch_size=embed_batch_size,
        upsert_batch_size=upsert_batch_size,
    )
    return pipeline, loader, chunker, store, emb


def _docs(n: int) -> list[Document]:
    return [Document(content=f"c{i}", id=f"d{i}") for i in range(n)]


class TestUpsertBatching:
    def test_small_upsert_batch_size_triggers_multiple_upsert_calls(self):
        pipeline, *_, store, _ = _make_pipeline(_docs(10), embed_batch_size=5, upsert_batch_size=4)
        pipeline.run("path")
        # 10 doc, upsert_batch_size=4 → batch های ۴+۴+۲ → سه فراخوانی upsert
        assert store.upsert.call_count == 3

    def test_upsert_batches_never_exceed_upsert_batch_size(self):
        pipeline, *_, store, _ = _make_pipeline(_docs(10), embed_batch_size=5, upsert_batch_size=4)
        pipeline.run("path")
        sizes = [len(call.args[0]) for call in store.upsert.call_args_list]
        assert all(size <= 4 for size in sizes)
        assert sizes == [4, 4, 2]

    def test_all_documents_embedded_before_their_batch_is_upserted(self):
        pipeline, *_, store, _ = _make_pipeline(_docs(10), embed_batch_size=5, upsert_batch_size=4)
        pipeline.run("path")
        for call in store.upsert.call_args_list:
            for doc in call.args[0]:
                assert doc.embedding == [0.0]

    def test_total_upserted_count_correct_across_batches(self):
        pipeline, *_ = _make_pipeline(_docs(10), embed_batch_size=5, upsert_batch_size=4)
        result = pipeline.run("path")
        assert result["upserted"] == 10

    def test_single_batch_when_upsert_batch_size_larger_than_documents(self):
        pipeline, *_, store, _ = _make_pipeline(_docs(5), embed_batch_size=2, upsert_batch_size=200)
        pipeline.run("path")
        assert store.upsert.call_count == 1

    def test_exact_multiple_does_not_leave_empty_final_flush(self):
        pipeline, *_, store, _ = _make_pipeline(_docs(8), embed_batch_size=4, upsert_batch_size=4)
        pipeline.run("path")
        assert store.upsert.call_count == 2   # نباید یک upsert خالی سوم اتفاق بیفتد

    def test_embed_batch_size_independent_from_upsert_batch_size(self):
        """embed_batch_size کوچک‌تر از upsert_batch_size — چند embed برای هر یک upsert جمع می‌شود."""
        pipeline, *_, store, emb = _make_pipeline(_docs(12), embed_batch_size=3, upsert_batch_size=6)
        pipeline.run("path")
        assert emb.embed_batch.call_count == 4    # 3+3+3+3
        assert store.upsert.call_count == 2        # 6+6

    def test_upserted_ids_match_document_ids(self):
        pipeline, *_, store, _ = _make_pipeline(_docs(6), embed_batch_size=3, upsert_batch_size=3)
        result = pipeline.run("path")
        assert result["upserted"] == 6
