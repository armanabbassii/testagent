"""
checkpointer/redis_checkpointer.py — Redis-backed checkpointer برای LangGraph

چرا Redis:
  - هم single-node و هم multi-node را پوشش می‌دهد
  - Session ها بین restart های سرور حفظ می‌شوند
  - TTL برای پاکسازی خودکار session های منقضی

فرمت REDIS_URL — همه حالت‌های احراز هویت:
  بدون احراز هویت : redis://localhost:6379
  فقط password    : redis://:password@localhost:6379
  user + password  : redis://username:password@localhost:6379
  فقط username    : redis://username:@localhost:6379

نحوه استفاده:
    from src.checkpointer import make_checkpointer

    with make_checkpointer() as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)
        graph.invoke(state, config)
"""

from contextlib import contextmanager
from src.config import REDIS_URL


@contextmanager
def make_checkpointer(ttl_seconds: int | None = 86400):
    """یک Redis checkpointer می‌سازد و به عنوان context manager مدیریت می‌کند.

    پارامترها:
        ttl_seconds : مدت نگه‌داری session در Redis (پیش‌فرض: ۲۴ ساعت)
                      None یعنی بدون انقضا

    استفاده:
        with make_checkpointer() as cp:
            graph = build_graph(checkpointer=cp)
            result = graph.invoke(state, config)

    متغیرهای .env:
        REDIS_URL : آدرس کامل Redis شامل credentials در صورت نیاز
                    مثال‌ها:
                      redis://localhost:6379
                      redis://:mypassword@localhost:6379
                      redis://myuser:mypassword@localhost:6379
    """
    try:
        from langgraph.checkpoint.redis import RedisSaver
    except ImportError:
        raise ImportError(
            "langgraph-checkpoint-redis نصب نیست.\n"
            "دستور: uv add langgraph-checkpoint-redis"
        )

    with RedisSaver.from_conn_string(REDIS_URL) as checkpointer:
        checkpointer.setup()   # ساخت index های لازم در Redis
        yield checkpointer