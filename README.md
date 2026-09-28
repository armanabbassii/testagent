# Local LLM Agent Framework

زیرساخت multi-agent با LLM و Embedding لوکال از طریق OpenAI-compatible API،
مدیریت ایجنت‌ها با LangGraph، حافظه بلندمدت با Qdrant/Chroma، Human-in-the-Loop،
سیستم لاگ سطح‌بندی‌شده، و Code Review خودکار با GitLab.

---

## پیش‌نیازها

| مورد | حداقل | توضیح |
|------|-------|-------|
| Python | 3.12+ | |
| uv | 0.4.0+ | مدیریت محیط مجازی |
| LLM Server | — | هر سرور OpenAI-compatible |
| Embedding Server | — | هر سرور OpenAI-compatible |
| Docker | اختیاری | برای Qdrant |

---

## راه‌اندازی

```bash
# ۱. نصب وابستگی‌ها
uv sync

# ۲. کپی و ویرایش تنظیمات
cp .env.example .env
nano .env

# ۳. (اختیاری) راه‌اندازی Qdrant
docker run -d -p 6333:6333 qdrant/qdrant

# ۴. اجرای نمونه‌ها
uv run python examples/llm_basic.py
uv run python examples/agents_basic.py
```

---

## ساختار پروژه

```
local-llm-project/
│
├── src/
│   ├── config.py                    # بارگذاری .env
│   ├── llm_client.py                # ✅ هسته — LLMClient
│   ├── embedding_client.py          # ✅ هسته — EmbeddingClient
│   │
│   ├── vector_store/
│   │   ├── base.py                  # BaseVectorStore (abstract)
│   │   ├── qdrant_store.py          # پیاده‌سازی Qdrant
│   │   └── chroma_store.py          # پیاده‌سازی ChromaDB
│   │
│   ├── memory/
│   │   ├── base.py                  # BaseMemory (abstract)
│   │   └── qdrant_memory.py         # پیاده‌سازی با Qdrant
│   │
│   ├── debug/
│   │   ├── levels.py                # DebugLevel enum
│   │   ├── base_handler.py          # BaseHandler (abstract) + LogRecord
│   │   ├── logger.py                # AgentLogger
│   │   ├── config.py                # DebugConfig (from_env / simple / off)
│   │   └── handlers/
│   │       └── file_handler.py      # FileHandler (text + jsonl + rotation)
│   │
│   └── agents/
│       ├── state.py                 # AgentState (TypedDict)
│       ├── base_agent.py            # BaseAgent + memory-aware prompt
│       ├── graph.py                 # build_graph()
│       ├── hitl.py                  # HITLHandler
│       ├── memory_nodes.py          # MemoryLoaderNode, MemorySaverNode
│       ├── router/                  # RouterAgent
│       ├── research/                # ResearchAgent
│       ├── summarizer/              # SummaryAgent
│       ├── general/                 # GeneralAgent
│       └── code_review/
│           ├── state.py             # CodeReviewState
│           ├── graph.py             # build_code_review_graph()
│           ├── gitlab/              # GitLabFetcherAgent, GitLabCommenterAgent
│           ├── reviewer/            # CodeReviewerAgent + prompts/
│           ├── decision/            # DecisionMakerAgent + prompts/
│           └── scoring/             # scorer.py — امتیازدهی + JSONL
│
├── examples/
│   ├── llm_basic.py                 # استفاده از LLMClient
│   ├── embedding_basic.py           # استفاده از EmbeddingClient
│   ├── agents_basic.py              # Multi-agent بدون memory
│   ├── agents_with_memory.py        # Multi-agent با Qdrant memory
│   ├── agents_hitl.py               # Human-in-the-Loop
│   ├── code_review_basic.py         # Code Review با GitLab
│   └── code_review_scores.py        # خواندن تاریخچه امتیازها
│
├── docs/
│   ├── index.md
│   ├── requirements.md
│   ├── installation.md
│   ├── how-to/
│   │   ├── new-agent.md
│   │   └── new-log-driver.md
│   ├── architecture/
│   │   └── core-clients.md
│   ├── contributing/
│   │   └── documentation.md
│   └── roadmap.md
│
├── .env.example
├── pyproject.toml
└── README.md
```

---

## تنظیمات `.env`

### الزامی

```ini
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=llama3.2
LLM_API_KEY=ollama

EMBEDDING_BASE_URL=http://localhost:11434/v1
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_API_KEY=ollama

CLIENT_ID=my-client-id
# توجه: USER_ID از کد برنامه (session/auth) پاس داده می‌شود
```

### Code Review (اختیاری)

```ini
GITLAB_URL=https://gitlab.com
GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
GITLAB_PROJECT_ID=12345678
```

### لاگ (اختیاری)

```ini
LOGGING_ENABLED=true
LOGGING_LEVEL=info              # trace | debug | info | warning | error
LOGGING_DRIVER=file
LOGGING_DRIVER_FILE_PATH=logs/app.log
LOGGING_DRIVER_FILE_FORMAT=text # text | jsonl

# per-agent (ارث‌بری از global در صورت خالی بودن)
LOGGING_AGENTS_CODE_REVIEWER_LEVEL=trace
LOGGING_AGENTS_DECISION_MAKER_ENABLED=false
```

---

## معماری گراف

### General Agents

```
بدون memory:
  START → router → research   → END
                   summarizer → END
                   general    → END

با memory:
  START → memory_loader → router → research   → memory_saver → END
                                   summarizer → memory_saver → END
                                   general    → memory_saver → END
```

### Code Review

```
START → gitlab_fetcher → code_reviewer → gitlab_commenter → decision_maker → END
```

| Node | وظیفه |
|------|-------|
| `gitlab_fetcher` | دریافت diff، اطلاعات MR، و fingerprint کامنت‌های موجود |
| `code_reviewer` | بررسی diff با rule های تخصصی (security، correctness، performance، style) |
| `gitlab_commenter` | ثبت کامنت‌های inline با dedup + کامنت امتیاز |
| `decision_maker` | تصمیم نهایی (approve/reject/needs_work) + ثبت کامنت تصمیم |

---

## نمونه‌های کد

### LLM و Embedding

```python
from src.llm_client import LLMClient
from src.embedding_client import EmbeddingClient

llm = LLMClient(user_id="user-123")
response = llm.chat("سلام!")

for chunk in llm.stream_chat("توضیح بده..."):
    print(chunk, end="")

emb = EmbeddingClient(user_id="user-123")
vector = emb.embed("متن نمونه")
sim = EmbeddingClient.cosine_similarity(vec_a, vec_b)
```

### گراف با Memory و HITL

```python
from src.agents import AgentState, build_graph, HITLHandler
from src.memory import QdrantMemory

graph = build_graph(
    memory=memory,           # اختیاری
    interrupt_before=["research"],  # اختیاری — HITL
)
config = {"configurable": {"thread_id": "session-1"}}

graph.invoke(state, config)                            # اجرا تا interrupt
approved = HITLHandler.prompt_user(graph.get_state(config).values)
HITLHandler.resume(graph, approved=approved, config=config)
```

### Code Review

```bash
# ساده
uv run python examples/code_review_basic.py --mr 42

# با تأیید انسانی قبل از تصمیم نهایی
uv run python examples/code_review_basic.py --mr 42 --hitl

# نمایش تاریخچه امتیازها
uv run python examples/code_review_scores.py --mr 42
```

### لاگ

```python
from src.debug import DebugConfig

# از .env بخوان (پیش‌فرض)
config = DebugConfig.from_env()

# یا دستی
config = DebugConfig.simple(level="debug", log_file="logs/app.log")

logger = config.get_logger("my_agent")
logger.info("شروع پردازش", user_id="u1")
logger.debug("جزئیات", count=42)
```

---

## سرورهای LLM سازگار

| سرور | آدرس پیش‌فرض | یادداشت |
|------|--------------|---------|
| **Ollama** | `http://localhost:11434/v1` | رایج‌ترین گزینه |
| **LM Studio** | `http://localhost:1234/v1` | رابط گرافیکی دارد |
| **vLLM** | `http://localhost:8000/v1` | برای GPU قوی‌تر |
| **llama.cpp** | `http://localhost:8080/v1` | سبک و سریع |

---

## مستندات

مستندات کامل در پوشه `docs/` موجود است:

| سند | توضیح |
|-----|-------|
| [docs/requirements.md](docs/requirements.md) | نیازمندی‌ها و حداقل‌ها |
| [docs/installation.md](docs/installation.md) | نصب گام‌به‌گام |
| [docs/how-to/new-agent.md](docs/how-to/new-agent.md) | ایجاد ایجنت جدید + چک‌لیست |
| [docs/how-to/new-log-driver.md](docs/how-to/new-log-driver.md) | ایجاد درایور لاگ جدید |
| [docs/architecture/core-clients.md](docs/architecture/core-clients.md) | الزامات LLMClient و EmbeddingClient |
| [docs/contributing/documentation.md](docs/contributing/documentation.md) | قوانین مستندسازی و commit |
| [docs/roadmap.md](docs/roadmap.md) | توسعه‌های آینده |

---

## نکات Production

- **Checkpointer:** `MemorySaver` فقط در RAM است. برای production از `SqliteSaver` یا `RedisSaver` استفاده کنید.
- **HITL:** متد `HITLHandler.prompt_user` را با webhook یا UI جایگزین کنید.
- **Memory backend:** برای جایگزینی Qdrant، کافیه `BaseMemory` را extend کنید.
- **Log driver:** برای network handler ها (Elastic، Slack)، از `AsyncQueueHandler` wrapper استفاده کنید (→ roadmap).