# LLMClient و EmbeddingClient — هسته اصلی پروژه

## اصل بنیادی

`LLMClient` و `EmbeddingClient` **الزامات معماری** این پروژه هستند.
تمام توسعه‌ها باید حول محور این دو کلاس انجام شوند.

> ⚠️ هرگز مستقیم با OpenAI SDK یا HTTP client ارتباط برقرار نکنید.
> همیشه از این دو کلاس استفاده کنید.

## دلیل

این دو کلاس مسئولیت‌های مشترک تمام ارتباطات با سرویس‌های AI را مدیریت می‌کنند:

- **Custom headers**: `x-client-id`، `x-user-id`، `x-request-id` (UUID تازه per-instance)
- **تنظیمات متمرکز**: آدرس سرور، نام مدل، و API key از `.env` خوانده می‌شوند
- **یکپارچگی**: تغییر سرور یا مدل فقط در `.env` انجام می‌شود، نه در کد

## LLMClient

```python
# src/llm_client.py

from src.llm_client import LLMClient

llm = LLMClient(user_id="user-123")   # ← user_id از session/auth می‌آید

# پاسخ ساده
response = llm.chat(
    user_message="سوال کاربر",
    system_prompt="You are ...",
    temperature=0.7,
    max_tokens=1024,
)

# streaming
for chunk in llm.stream_chat(user_message="...", system_prompt="..."):
    print(chunk, end="")
```

### قانون `user_id`

- `user_id` **هرگز** در `.env` ذخیره نمی‌شود
- باید از لایه session یا authentication پاس داده شود
- در ایجنت‌ها از `state["user_id"]` خوانده می‌شود:

```python
# ✅ درست
llm = self._get_llm(state["user_id"])

# ❌ اشتباه
llm = LLMClient(user_id="hardcoded-user")
```

## EmbeddingClient

```python
from src.embedding_client import EmbeddingClient

emb = EmbeddingClient(user_id="user-123")

# تبدیل یک متن
vector: list[float] = emb.embed("متن نمونه")

# تبدیل دسته‌ای (batch)
vectors: list[list[float]] = emb.embed_batch(["متن اول", "متن دوم"])

# شباهت cosine (بدون numpy)
score = EmbeddingClient.cosine_similarity(vector_a, vector_b)
```

## استفاده در ایجنت‌ها

در `BaseAgent` متد `_get_llm()` این الزام را تضمین می‌کند:

```python
class BaseAgent(ABC):
    def _get_llm(self, user_id: str) -> LLMClient:
        return LLMClient(user_id=user_id)   # هر بار با user_id صحیح
```

برای Embedding در ایجنت‌ها (مثلاً memory_nodes):

```python
from src.embedding_client import EmbeddingClient

emb = EmbeddingClient(user_id=state["user_id"])
```

## تنظیمات در `.env`

```ini
# LLM
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=llama3.2
LLM_API_KEY=ollama

# Embedding
EMBEDDING_BASE_URL=http://localhost:11434/v1
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_API_KEY=ollama

# هدر ثابت (user_id از کد می‌آید)
CLIENT_ID=my-client-id
```

## هدرهای HTTP

هر instance از این دو کلاس یک `httpx.Client` با هدرهای زیر می‌سازد:

| هدر | مقدار | منبع |
|-----|-------|-------|
| `x-client-id` | ثابت | `CLIENT_ID` از `.env` |
| `x-user-id` | per-request | پارامتر `user_id` در constructor |
| `x-request-id` | UUID v4 | تولید خودکار per-instance |

## قوانین توسعه

1. **هرگز** `OpenAI(...)` را مستقیم در کد ایجنت‌ها صدا نزنید
2. **هرگز** `httpx.Client` یا `requests` را برای ارتباط با LLM/Embedding استفاده نکنید
3. برای feature جدید (مثلاً function calling)، آن را به `LLMClient` اضافه کنید، نه به ایجنت
4. هر تغییر در این دو کلاس باید backward compatible باشد