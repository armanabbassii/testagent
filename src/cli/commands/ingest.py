"""
cli/commands/ingest.py — دستور `ingest-business`

استفاده:
    python -m src.cli ingest-business --file data/services.jsonl
    python -m src.cli ingest-business --file data/services.jsonl --collection business_catalog
    python -m src.cli ingest-business --file data/services.jsonl --lang en
"""

import os
import argparse
from src.cli.i18n import Translator
from src.cli import output as out
from src.embedding_client import EmbeddingClient
from src.vector_store.factory import make_vector_store
from src.ingest import IngestPipeline
from src.ingest.business import BusinessJsonlLoader, BusinessServiceChunker
from src.debug import DebugConfig


def add_parser(subparsers, t: Translator, lang_parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "ingest-business",
        help=t.t("ingest_description"),
        parents=[lang_parent],
    )
    parser.add_argument("--file", required=True, help=t.t("ingest_file_help"))
    parser.add_argument("--collection", default="business_catalog", help=t.t("ingest_collection_help"))
    parser.add_argument("--batch-size", default=64, help=t.t("ingest_embed_batch_size_help"), type=int)
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, t: Translator) -> int:
    out.header(t.t("ingest_header", args.collection))

    try:
        debug_config = DebugConfig.from_env()
        emb = EmbeddingClient(user_id=_get_user_id())
        store = make_vector_store(collection=args.collection, embedding_client=emb)
        store.create_collection(vector_size=len(emb.embed("test")))

        pipeline = IngestPipeline(
            loader=BusinessJsonlLoader(debug_config=debug_config),
            chunker=BusinessServiceChunker(debug_config=debug_config),
            vector_store=store,
            embedding_client=emb,
            debug_config=debug_config,
            embed_batch_size=int(getattr(args, "batch_size", 64) or 64),
        )

        out.step(t.t("ingest_running"))
        result = pipeline.run(args.file)

        out.success(t.t("ingest_done"))
        out.score_row(t.t("ingest_records"), result["records"])
        out.score_row(t.t("ingest_chunks"), result["chunks"])
        out.score_row(t.t("ingest_upserted"), result["upserted"])
        return 0

    except Exception as e:
        out.error(f"{t.t('error_prefix')}: {e}")
        return 1


def _get_user_id() -> str:
    return os.getenv("CLI_USER_ID", "cli-ingest")