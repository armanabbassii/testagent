"""
checkpointer/backends/redis_backend.py — Redis backend

مناسب برای:
  - production
  - multi-node
  - هر محیطی که Redis در دسترس است

فرمت REDIS_URL — همه حالت‌های احراز هویت:
  بدون احراز هویت : redis://localhost:6379
  فقط password    : redis://:password@localhost:6379
  username + pass  : redis://username:password@localhost:6379
  فقط username    : redis://username:@localhost:6379

نیازمند: langgraph-checkpoint-redis
نصب:     uv add langgraph-checkpoint-redis
"""

from contextlib import contextmanager
from src.config import REDIS_URL


@contextmanager
def make_redis_checkpointer(ttl_seconds: int | None = 86400):
    """Redis checkpointer می‌سازد.

    پارامترها:
        ttl_seconds : مدت نگه‌داری session (پیش‌فرض: ۲۴ ساعت، None = بدون انقضا)

    متغیر .env:
        REDIS_URL : آدرس کامل Redis شامل credentials
    """
    try:
        from langgraph.checkpoint.redis import RedisSaver
    except ImportError:
        raise ImportError(
            "langgraph-checkpoint-redis نصب نیست.\n"
            "دستور: uv add langgraph-checkpoint-redis"
        )

    with RedisSaver.from_conn_string(REDIS_URL) as checkpointer:
        checkpointer.setup()
        yield checkpointer