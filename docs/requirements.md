# نیازمندی‌ها و حداقل‌های پروژه

## محیط اجرا

| مورد | حداقل نسخه | توضیح |
|------|-----------|-------|
| Python | 3.12 | استفاده از `TypedDict`، `X \| Y` type hint، `str.removeprefix` |
| uv | 0.4.0 | مدیریت محیط مجازی و وابستگی‌ها |
| Docker | هر نسخه | اجرای Qdrant (اختیاری) |

## سرویس‌های خارجی الزامی

| سرویس | نقش | حداقل |
|-------|-----|-------|
| LLM Server | تولید متن | هر سرور OpenAI-compatible (Ollama، LM Studio، vLLM) |
| Embedding Server | تبدیل متن به بردار | هر سرور OpenAI-compatible |

> **نکته:** هر دو سرور باید هدرهای `x-client-id`، `x-user-id`، و `x-request-id` را پشتیبانی کنند.

## سرویس‌های خارجی اختیاری

| سرویس | نقش | شرط فعال‌شدن |
|-------|-----|-------------|
| Qdrant | Vector store + Long-term memory | استفاده از `QdrantVectorStore` یا `QdrantMemory` |
| ChromaDB | Vector store جایگزین | استفاده از `ChromaVectorStore` |
| GitLab | Code review | استفاده از گراف `code_review` |

## وابستگی‌های Python

```toml
# الزامی
openai>=1.0.0
python-dotenv>=1.0.0
langgraph>=0.2.0
langchain-openai>=0.2.0
langchain-core>=0.3.0

# اختیاری (بر اساس نیاز)
qdrant-client>=1.10.0    # برای QdrantVectorStore / QdrantMemory
chromadb>=0.5.0          # برای ChromaVectorStore
requests>=2.32.0         # برای GitLab agent
```

## متغیرهای محیطی الزامی

تمام متغیرها باید در فایل `.env` تعریف شوند. نمونه کامل در `.env.example` موجود است.

### همیشه الزامی

```ini
LLM_BASE_URL=
LLM_MODEL=
LLM_API_KEY=
EMBEDDING_BASE_URL=
EMBEDDING_MODEL=
EMBEDDING_API_KEY=
CLIENT_ID=
```

### الزامی برای code review

```ini
GITLAB_URL=
GITLAB_TOKEN=
GITLAB_PROJECT_ID=
```

### اختیاری (لاگ)

```ini
LOGGING_ENABLED=true
LOGGING_LEVEL=info
LOGGING_DRIVER=file
LOGGING_DRIVER_FILE_PATH=logs/app.log
LOGGING_DRIVER_FILE_FORMAT=text
```