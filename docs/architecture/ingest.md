# Ingest — بارگذاری اسناد به Vector Store

## چرا

برای ساخت RAG بیزینسی و فنی، نیاز به یک پایپلاین جدا برای batch-load کردن
اسناد خام (JSONL، سورس کد، Swagger، ...) و ذخیره در vector store داریم —
جدا از agent های runtime که فقط retrieve/answer می‌کنند.

## معماری
```
Loader (خواندن فایل خام) → Chunker (تبدیل به Document) → Embed (batch) → Upsert
```

هر ingest از `BaseIngestLoader` و `BaseIngestChunker` (در `src/ingest/base.py`)
پیاده‌سازی جدا دارد؛ `IngestPipeline` مشترک است.

## Business Ingest
```
src/ingest/business/
├── models.py    # ساختار رکورد JSONL (ServiceGroupRecord, WebServiceRecord, FieldRecord)
├── html.py      # strip_html — حذف تگ HTML از فیلد content
├── loader.py    # BusinessJsonlLoader
└── chunker.py   # BusinessServiceChunker
```
هر رکورد JSONL یک «مجموعه سرویس» با آرایه `webservices` است. چانک‌بندی
دو سطح تولید می‌کند: یک چانک خلاصه برای کل مجموعه، و یک چانک مستقل به‌ازای
هر سرویس وب (با context مجموعه تکرارشده در متن).

## انتخاب Vector Store Backend

مثل checkpointer، انتخاب backend از `.env` خوانده می‌شود:

```ini
# روی سرور
VECTOR_STORE_BACKEND=qdrant
QDRANT_URL=http://localhost:6333

# روی سیستم شخصی
VECTOR_STORE_BACKEND=chroma
CHROMA_PERSIST_PATH=./chroma_data
```

```python
from src.vector_store.factory import make_vector_store
store = make_vector_store(collection="business_catalog", embedding_client=emb)
```

## اجرا

```bash
python -m src.cli ingest-business --file data/services.jsonl
python -m src.cli ingest-business --file data/services.jsonl --collection business_catalog --lang en
```

## تست

```bash
uv run pytest tests/unit/ingest/ tests/unit/vector_store/test_factory.py tests/unit/cli/test_ingest_command.py
```

## Batching در Embed/Upsert

`IngestPipeline` به‌صورت پیش‌فرض هر ۲۰۰ چانک را (پس از embed شدن) بلافاصله
upsert می‌کند تا به محدودیت اندازه درخواست vector store نخوریم. این عدد از
طریق پارامتر `upsert_batch_size` قابل تنظیم است و مستقل از `embed_batch_size`
است.