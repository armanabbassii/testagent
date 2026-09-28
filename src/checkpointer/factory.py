"""
checkpointer/factory.py — factory برای انتخاب backend از .env

متغیر تعیین‌کننده:
    CHECKPOINTER_BACKEND=sqlite | redis   (پیش‌فرض: sqlite)

تنظیمات per-backend:
    SQLite: CHECKPOINTER_SQLITE_PATH (پیش‌فرض: checkpoints.db)
    Redis:  REDIS_URL
"""

import os
from contextlib import contextmanager

_SUPPORTED = ("sqlite", "redis")


@contextmanager
def make_checkpointer():
    """backend را از CHECKPOINTER_BACKEND در .env می‌خواند و checkpointer می‌سازد.

    مثال .env:
        # توسعه / تست
        CHECKPOINTER_BACKEND=sqlite
        CHECKPOINTER_SQLITE_PATH=checkpoints.db

        # production / multi-node
        CHECKPOINTER_BACKEND=redis
        REDIS_URL=redis://localhost:6379
    """
    backend = os.getenv("CHECKPOINTER_BACKEND", "sqlite").lower().strip()

    if backend == "sqlite":
        from src.checkpointer.backends.sqlite_backend import make_sqlite_checkpointer
        db_path = os.getenv("CHECKPOINTER_SQLITE_PATH", "checkpoints.db")
        with make_sqlite_checkpointer(db_path=db_path) as cp:
            yield cp

    elif backend == "redis":
        from src.checkpointer.backends.redis_backend import make_redis_checkpointer
        with make_redis_checkpointer() as cp:
            yield cp

    else:
        raise ValueError(
            f"CHECKPOINTER_BACKEND نامعتبر: '{backend}'. "
            f"مقادیر مجاز: {', '.join(_SUPPORTED)}"
        )