"""
checkpointer/backends/sqlite_backend.py — SQLite backend

مناسب برای:
  - توسعه محلی (local dev)
  - تست
  - single-node بدون Redis

نیازمند: langgraph-checkpoint-sqlite
نصب:     uv add langgraph-checkpoint-sqlite
"""

from contextlib import contextmanager
from pathlib import Path


@contextmanager
def make_sqlite_checkpointer(db_path: str = "checkpoints.db"):
    """SQLite checkpointer می‌سازد.

    پارامترها:
        db_path : مسیر فایل SQLite (پیش‌فرض: checkpoints.db در پوشه جاری)
                  برای in-memory (تست): ":memory:"

    متغیر .env:
        CHECKPOINTER_SQLITE_PATH : مسیر فایل (اگر خالی باشد db_path استفاده می‌شود)
    """
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:
        raise ImportError(
            "langgraph-checkpoint-sqlite نصب نیست.\n"
            "دستور: uv add langgraph-checkpoint-sqlite"
        )

    # ساخت پوشه والد در صورت نیاز (به جز in-memory)
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    with SqliteSaver.from_conn_string(db_path) as checkpointer:
        yield checkpointer