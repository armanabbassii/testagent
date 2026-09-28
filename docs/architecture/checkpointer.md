# Checkpointer

## چرا Checkpointer

LangGraph برای پشتیبانی از HITL و ادامه مکالمه بین درخواست‌ها به checkpointer نیاز دارد.
بدون آن، state با هر restart از بین می‌رود.

---

## Backend ها

| Backend | مناسب برای | نیاز به سرور |
|---------|-----------|-------------|
| `sqlite` | توسعه، تست، single-node | ❌ (فایل لوکال) |
| `redis`  | production، multi-node  | ✅ Redis server |

---

## تنظیم در `.env`

```ini
# ── انتخاب backend ───────────────────────────────
CHECKPOINTER_BACKEND=sqlite   # یا redis

# ── SQLite (وقتی backend=sqlite) ─────────────────
CHECKPOINTER_SQLITE_PATH=checkpoints.db
# برای تست in-memory:
# CHECKPOINTER_SQLITE_PATH=:memory:

# ── Redis (وقتی backend=redis) ───────────────────
# بدون احراز هویت:
REDIS_URL=redis://localhost:6379
# فقط password:
# REDIS_URL=redis://:mypassword@localhost:6379
# username + password:
# REDIS_URL=redis://myuser:mypassword@localhost:6379
```

---

## استفاده در کد

```python
from src.checkpointer import make_checkpointer

# backend از .env خوانده می‌شود — همیشه همین رو استفاده کنید
with make_checkpointer() as checkpointer:
    graph = build_graph(checkpointer=checkpointer)
    result = graph.invoke(state, config)
```

برای override مستقیم (مثلاً در تست):

```python
from src.checkpointer import make_sqlite_checkpointer, make_redis_checkpointer

# SQLite in-memory برای تست سریع
with make_sqlite_checkpointer(":memory:") as cp:
    graph = build_graph(checkpointer=cp)

# Redis مستقیم
with make_redis_checkpointer() as cp:
    graph = build_graph(checkpointer=cp)
```

---

## راه‌اندازی

### SQLite (نیازی به نصب سرور نیست)

```bash
# فقط کافیه dependency نصب باشه
uv add langgraph-checkpoint-sqlite
```

### Redis با Docker (برای dev با Redis)

```bash
docker run -d -p 6379:6379 --name redis redis:alpine

# با password
docker run -d -p 6379:6379 --name redis redis:alpine \
  redis-server --requirepass mypassword
```

---

## چه چیزی و چه وقت ذخیره می‌شود

| رویداد | آنچه ذخیره می‌شود |
|--------|-----------------|
| پایان هر node | snapshot کامل state |
| interrupt (HITL) | state در نقطه توقف |
| پایان گراف | آخرین state نهایی |

**کلید SQLite/Redis:** `langgraph:{thread_id}:{checkpoint_id}`

**TTL:** فقط در Redis قابل تنظیم است (پیش‌فرض: ۲۴ ساعت).
در SQLite فایل به صورت دائمی نگه‌داری می‌شود.

---

## نکات مهم

- `thread_id` باید per-session یکتا باشد — برای هر مکالمه جدید UUID تازه بسازید
- `thread_id` یکسان = ادامه همان session (مفید برای multi-turn و HITL)
- برای تست‌های unit از `":memory:"` استفاده کنید تا فایل روی disk نمانَد