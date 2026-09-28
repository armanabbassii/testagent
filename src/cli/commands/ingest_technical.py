"""
cli/commands/ingest_technical.py — دستور `ingest-technical`

پروژه سورس جاوا/اسپرینگ‌بوت (کلاس‌ها، مستندات md، سواگر، قالب html) را
اسکن، چانک، و در vector store ذخیره می‌کند تا مصرف‌کننده‌های بعدی
(ایجنت کدنویسی، کد ریویو، یا یک چت‌بات فنی) بتوانند روی آن جستجوی
معنایی انجام دهند.

استفاده:
    python -m src.cli ingest-technical --path /path/to/java-project
    python -m src.cli ingest-technical --path /path/to/java-project --collection technical_catalog
    python -m src.cli ingest-technical --path /path/to/java-project --no-html --lang en
"""

import os
import argparse
from src.cli.i18n import Translator
from src.cli import output as out
from src.embedding_client import EmbeddingClient
from src.vector_store.factory import make_vector_store
from src.ingest import IngestPipeline
from src.ingest.technical import TechnicalSourceLoader, TechnicalCodeChunker
from src.debug import DebugConfig


def add_parser(subparsers, t: Translator, lang_parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "ingest-technical",
        help=t.t("ingest_technical_description"),
        parents=[lang_parent],
    )
    parser.add_argument("--path", required=True, help=t.t("ingest_technical_path_help"))
    parser.add_argument("--collection", default="technical_catalog", help=t.t("ingest_technical_collection_help"))
    parser.add_argument("--no-html", action="store_true", help=t.t("ingest_technical_no_html_help"))
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, t: Translator) -> int:
    out.header(t.t("ingest_technical_header", args.collection))

    try:
        debug_config = DebugConfig.from_env()
        emb = EmbeddingClient(user_id=_get_user_id())
        store = make_vector_store(collection=args.collection, embedding_client=emb)
        store.create_collection(vector_size=len(emb.embed("test")))

        pipeline = IngestPipeline(
            loader=TechnicalSourceLoader(include_html=not args.no_html, debug_config=debug_config),
            chunker=TechnicalCodeChunker(debug_config=debug_config),
            vector_store=store,
            embedding_client=emb,
            debug_config=debug_config,
        )

        out.step(t.t("ingest_technical_running"))
        result = pipeline.run(args.path)

        out.success(t.t("ingest_done"))
        out.score_row(t.t("ingest_records"), result["records"])
        out.score_row(t.t("ingest_chunks"), result["chunks"])
        out.score_row(t.t("ingest_upserted"), result["upserted"])
        return 0

    except Exception as e:
        out.error(f"{t.t('error_prefix')}: {e}")
        return 1


def _get_user_id() -> str:
    return os.getenv("CLI_USER_ID", "cli-ingest-technical")
