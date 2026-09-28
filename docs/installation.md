# نصب و راه‌اندازی

## پیش‌نیازها

```bash
# بررسی Python
python --version   # باید 3.12+ باشد

# نصب uv (اگر ندارید)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## ۱. کلون و نصب وابستگی‌ها

```bash
git clone <repo-url>
cd local-llm-project

uv sync
```

## ۲. تنظیم فایل `.env`

```bash
cp .env.example .env
```

سپس فایل `.env` را ویرایش کنید و مقادیر را تنظیم کنید:

```ini
# آدرس سرور LLM لوکال
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=llama3.2
LLM_API_KEY=ollama

# آدرس سرور Embedding لوکال
EMBEDDING_BASE_URL=http://localhost:11434/v1
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_API_KEY=ollama

# شناسه کلاینت (از سرویس‌دهنده دریافت می‌شود)
CLIENT_ID=my-client-id
```

## ۳. راه‌اندازی سرویس‌های اختیاری

### Qdrant (برای long-term memory)

```bash
docker run -d -p 6333:6333 --name qdrant qdrant/qdrant
```

### Ollama (نمونه سرور LLM لوکال)

```bash
# نصب Ollama
curl -fsSL https://ollama.com/install.sh | sh

# دانلود مدل‌ها
ollama pull llama3.2
ollama pull nomic-embed-text
```

## ۴. تأیید نصب

```bash
uv run python main.py
```

اگر همه چیز درست باشد، خروجی تست LLM و Embedding نمایش داده می‌شود.

## ۵. تنظیم لاگ (اختیاری)

برای فعال‌سازی لاگ، این متغیرها را به `.env` اضافه کنید:

```ini
LOGGING_ENABLED=true
LOGGING_LEVEL=info          # trace | debug | info | warning | error
LOGGING_DRIVER=file
LOGGING_DRIVER_FILE_PATH=logs/app.log
LOGGING_DRIVER_FILE_FORMAT=text   # text | jsonl
```

برای debug یک ایجنت خاص:

```ini
LOGGING_AGENTS_CODE_REVIEWER_LEVEL=trace
LOGGING_AGENTS_DECISION_MAKER_ENABLED=false
```

## ۶. تنظیم Code Review (اختیاری)

```ini
GITLAB_URL=https://gitlab.com
GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
GITLAB_PROJECT_ID=12345678
```

Personal Access Token باید scope های `api` و `read_repository` داشته باشد.