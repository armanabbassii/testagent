# پیشنهادها و توسعه‌های آینده

## وضعیت کلی

| قابلیت | وضعیت |
|--------|--------|
| Redis + SQLite Checkpointer | ✅ انجام شد |
| Test Suite (451 تست، 94% coverage) | ✅ انجام شد |
| CLI (review + scores، FA/EN) | ✅ انجام شد |
| Memory Backend (Qdrant + JSON) | ✅ انجام شد |
| ConsoleHandler + FileHandler | ✅ انجام شد |
| Custom Rules (RuleLoader) | ✅ انجام شد |
| RAG Agent | ✅ انجام شد |
| Prompt Injection Guard | ✅ انجام شد |
| SonarQube Integration (CE-compatible) | ✅ انجام شد |
| CI/CD Pipeline | 🔲 باقی‌مانده |
| Async Support | 🔲 باقی‌مانده |

---

## ۱. زیرساخت و معماری

### ~~Checkpointer پایدار~~ ✅ انجام شد

**Redis + SQLite backend پیاده‌سازی شد** — سوئیچ از طریق `.env` بدون تغییر کد:

```ini
CHECKPOINTER_BACKEND=sqlite   # dev/test/single-node
CHECKPOINTER_BACKEND=redis    # production/multi-node
```

تمام حالت‌های احراز هویت Redis (بدون auth، فقط password، username+password)
از طریق `REDIS_URL` قابل تنظیم است.
مستندات کامل: [architecture/checkpointer.md](architecture/checkpointer.md)

---

### Async Support
**اولویت: بالا — بعد از رسیدن به production**

> **چرا الان نیاز نیست:** پروژه هنوز production نیست و LLM call چند ثانیه طول
> می‌کشد — async کردن لایه‌های اطراف تفاوت محسوسی ایجاد نمی‌کند تا زمانی که
> چندین request همزمان نداشته باشیم. وقتی load واقعی دیده شد، این مورد را پیاده کنید.

موارد لازم:
- `LLMClient.achat()` و `astream_chat()`
- `EmbeddingClient.aembed()` و `aembed_batch()`
- `BaseAgent.arun()` و تبدیل graph به async

---

### ~~Memory Backend جایگزین~~ ✅ انجام شد

**`JsonMemory(BaseMemory)` پیاده‌سازی شد** — بدون نیاز به Qdrant یا هیچ سرور خارجی.

دو حالت جستجو قابل انتخاب:
```python
# keyword — بدون embedding، مناسب dev و تست
memory = JsonMemory(path="memory.json", search_mode="keyword")

# embedding — دقیق‌تر، نیاز به EmbeddingClient
memory = JsonMemory(path="memory.json", search_mode="embedding",
                    embedding_client=EmbeddingClient(user_id="u1"))
```

هر دو با گراف سازگارند:
```python
graph = build_graph(checkpointer=cp, memory=memory)
```

---

### ~~لاگ Handler های بیشتر~~ ✅ ConsoleHandler انجام شد

```python
# از .env
LOGGING_DRIVER=console
LOGGING_DRIVER_CONSOLE_FORMAT=text    # text | jsonl
LOGGING_DRIVER_CONSOLE_SHOW_TIME=false

# یا مستقیم در کد
from src.debug import ConsoleHandler, DebugConfig
config = DebugConfig(handlers=[ConsoleHandler(colorize=True)])
```

باقی‌مانده:
```python
class ElasticsearchHandler(BaseHandler): ...
class DatabaseHandler(BaseHandler): ...
class SlackHandler(BaseHandler): ...
```

---

### AsyncQueueHandler — لاگ غیرهمزمان
**اولویت: متوسط — همزمان با اولین network handler**

> **چرا الان نیاز نیست:** `FileHandler` لوکال زیر ۱ms است. به محض اضافه کردن
> اولین network driver (Elastic، Slack)، این wrapper باید پیاده‌سازی شود.

```python
class AsyncQueueHandler(BaseHandler):
    """پیام‌ها را در queue می‌ریزد — یه background thread می‌نویسد."""

    def __init__(self, inner: BaseHandler, queue_size: int = 1000) -> None:
        self._inner = inner
        self._queue = Queue(maxsize=queue_size)
        self._worker = Thread(target=self._run, daemon=True)
        self._worker.start()

    def emit(self, record: LogRecord) -> None:
        try:
            self._queue.put_nowait(record)
        except Full:
            pass

    def close(self) -> None:
        self._queue.put(None)
        self._worker.join(timeout=5)
        self._inner.close()
```

---

## ۲. Code Review

### پشتیبانی از GitHub
**اولویت: متوسط**

```
src/agents/code_review/
└── vcs/
    ├── base.py     ← BaseVCSClient (abstract)
    ├── gitlab.py   ← GitLabClient
    └── github.py   ← GitHubClient
```

---

### ~~Rule های اختصاصی زبان و پروژه~~ ✅ انجام شد

**`RuleLoader` پیاده‌سازی شد** — قوانین سفارشی از `project_rules/` بارگذاری و به prompt inject می‌شوند.

```
project_rules/
├── code_review/
│   ├── reviewer.md          ← قوانین کلی (زبان، سبک، ...)
│   └── languages/
│       ├── python.md
│       └── javascript.md
└── gitlab/
    └── commenter.md
```

```python
from src.rules import RuleLoader
loader = RuleLoader()   # از RULES_DIR در .env یا "project_rules"

graph = build_code_review_graph(
    checkpointer=cp,
    rule_loader=loader,
    reviewer_rule_paths=["code_review/reviewer", "code_review/languages/python"],
    decision_rule_paths=["gitlab/commenter"],
)
```

---

### تاریخچه و Trend امتیاز
**اولویت: پایین**

```python
def get_project_trend(project_id: int, last_n: int = 20) -> list[dict]:
    """n ریویو آخر یک پروژه را با امتیازشان برمی‌گرداند."""
```

---

### Auto-fix پیشنهادی
**اولویت: پایین**

```python
class AutoFixAgent(BaseAgent):
    """بر اساس کامنت‌های ریویو، patch Git پیشنهاد می‌دهد."""
```

---

## ۳. ایجنت‌های عمومی

### ~~ایجنت RAG (Retrieval-Augmented Generation)~~ ✅ انجام شد

**`RAGAgent` پیاده‌سازی شد** — با هر `BaseVectorStore` کار می‌کند (Qdrant، Chroma، یا پیاده‌سازی دیگر).

دو حالت استفاده:

```python
# ۱. standalone — بدون گراف
from src.agents.rag import RAGAgent
agent = RAGAgent(vector_store=store, top_k=5, score_threshold=0.5)
result = agent.retrieve_and_answer("سوال کاربر", user_id="u1")
# result = {"answer": "...", "documents": [...]}

# ۲. به عنوان node در گراف عمومی — router خودکار تشخیص می‌دهد
graph = build_graph(checkpointer=cp, vector_store=store, rag_top_k=5)
# وقتی vector_store داده شود، router سوالات مرتبط با اسناد را به "rag" می‌فرستد
```

ویژگی‌ها:
- `score_threshold` برای فیلتر نتایج کم‌ربط
- `filters` برای محدود کردن جستجو به metadata خاص (مثلاً `{"project": "docs"}`)
- با `memory_context` ترکیب می‌شود (از `BaseAgent._build_system_prompt`)
- نتایج بازیابی در `state["rag_context"]` ذخیره می‌شوند

---

### ایجنت جستجو (Web Search)
**اولویت: متوسط**

```python
class SearchAgent(BaseAgent):
    """اطلاعات به‌روز را از وب جستجو می‌کند و به context اضافه می‌کند."""
```

---

### ایجنت ارزیاب (Evaluator)
**اولویت: متوسط**

```python
class EvaluatorAgent(BaseAgent):
    """پاسخ ایجنت قبلی را از نظر دقت، کامل بودن، و مرتبط بودن ارزیابی می‌کند."""
```

---

## ۴. ابزارهای توسعه

### ~~CLI~~ ✅ انجام شد

```bash
python -m src.cli review --mr 42
python -m src.cli review --mr 42 --hitl
python -m src.cli review --mr 42 --lang en
python -m src.cli scores
python -m src.cli scores --mr 42 --last 5
```

پشتیبانی از FA/EN با `--lang` در هر جایی از دستور.
برای افزودن دستور جدید: `src/cli/commands/` → `add_parser()` → `__main__.py`.

---

### ~~Test Suite~~ ✅ انجام شد

**306 تست، 93% coverage** — تمام unit tests بدون نیاز به سرور خارجی اجرا می‌شوند.

نکته مهم در تست generator functions: از **pytest fixture با `yield`** استفاده کنید
تا patch در طول کل اجرای generator فعال بماند:

```python
@pytest.fixture
def llm_client():
    with patch("src.llm_client.OpenAI"), \
         patch("src.llm_client._make_http_client"):
        client = LLMClient(user_id="test-user")
        mock_inner = MagicMock()
        client._client = mock_inner
        yield client, mock_inner  # ← patch تا پایان تست فعال است
```

مستندات کامل: [contributing/testing.md](contributing/testing.md)

---

### CI/CD Pipeline
**اولویت: بالا**

pre-commit hook با `--no-verify` قابل bypass است.
راهکار واقعی: **GitLab CI** که سمت server اجرا می‌شود.

```yaml
# .gitlab-ci.yml
stages:
  - test

run-tests:
  stage: test
  image: python:3.12-slim
  before_script:
    - pip install uv
    - uv sync --extra dev
  script:
    - uv run pytest --cov=src --cov-fail-under=80
  coverage: '/TOTAL.*\s+(\d+%)$/'
```

در Settings → Repository → Protected Branches:
- شاخه `main` را protected کنید
- **"Pipelines must succeed"** را فعال کنید

---

### ~~Prompt Injection Guard~~ ✅ انجام شد

**`PromptGuard` پیاده‌سازی شد** — محافظت در برابر prompt injection با ۴ سطح حساسیت.

```python
from src.security import PromptGuard, PromptInjectionError, GuardLevel

# از .env بخواند (PROMPT_GUARD_LEVEL=MODERATE)
guard = PromptGuard.from_env()

# sanitize ورودی کاربر
try:
    safe_input = guard.sanitize_input(user_message)
except PromptInjectionError as e:
    # injection تشخیص داده شد
    ...

# validate خروجی مدل (نشت system prompt)
findings = guard.validate_output(llm_response)
```

تنظیمات `.env`:
```ini
PROMPT_GUARD_LEVEL=MODERATE          # OFF | LENIENT | MODERATE | STRICT
PROMPT_GUARD_MAX_INPUT_LEN=10000
PROMPT_GUARD_RAISE_ON_WARN=false
```

در `BaseAgent` با `_safe_last_human_message()` یکپارچه شده.

---

### پنل مانیتورینگ
**اولویت: پایین**

داشبورد ساده (FastAPI + HTML):
- نمایش لاگ‌های real-time
- تاریخچه ریویوها و امتیازها
- وضعیت سرویس‌های LLM و Embedding

### ~~SonarQube Integration~~ ✅ انجام شد

**`SonarAnalyzerAgent` پیاده‌سازی شد** — به‌صورت node موازی با `code_reviewer`
در گراف کد ریویو. چون سرور شرکت **Community Edition** است (بدون پشتیبانی
branch/PR analysis)، هر MR با یک project key موقت اسکن می‌شود.

```python
graph = build_code_review_graph(checkpointer=cp, enable_sonar=True)
```

```bash
python -m src.cli review --mr 42 --sonar
```

مستندات کامل: [architecture/sonarqube.md](architecture/sonarqube.md)

باقی‌مانده (خارج از scope فعلی):
- فیلتر issue‌ها به فقط خطوط تغییریافته در diff (الان کل کد برنچ اسکن می‌شود)
- استفاده از deep clone برای قابلیت SCM blame در Sonar