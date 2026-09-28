# مستندات پروژه Local LLM Agent Framework

> زیرساخت multi-agent با LLM و Embedding لوکال، مدیریت ایجنت‌ها با LangGraph،
> حافظه بلندمدت با Qdrant/Chroma، و Human-in-the-Loop.

---

## فهرست مستندات

| سند | توضیح |
|-----|-------|
| [requirements.md](requirements.md) | نیازمندی‌ها و حداقل‌های پروژه |
| [installation.md](installation.md) | نصب و راه‌اندازی |
| [how-to/new-agent.md](how-to/new-agent.md) | ایجاد ایجنت جدید |
| [how-to/new-log-driver.md](how-to/new-log-driver.md) | ایجاد درایور لاگ جدید |
| [architecture/core-clients.md](architecture/core-clients.md) | LLMClient و EmbeddingClient |
| [architecture/checkpointer.md](architecture/checkpointer.md) | Redis Checkpointer — نحوه کار و تنظیم |
| [architecture/observability.md](architecture/observability.md) | Observability |
| [architecture/technical_ingest.md](architecture/technical_ingest.md) | Technical Ingest — کد جاوا، مستندات، Swagger |
| [contributing/documentation.md](contributing/documentation.md) | قوانین مستندسازی |
| [contributing/testing.md](contributing/testing.md) | راهنمای تست‌نویسی، ساختار، چک‌لیست |
| [roadmap.md](roadmap.md) | پیشنهادها و توسعه‌های آینده |

---

## ساختار کلی پروژه

```
src/
├── config.py                    # بارگذاری .env
├── llm_client.py                # ✅ هسته اصلی — LLMClient
├── embedding_client.py          # ✅ هسته اصلی — EmbeddingClient
│
├── vector_store/                # Abstract + Qdrant + Chroma
├── memory/                      # Abstract + QdrantMemory
├── debug/                       # سیستم لاگ سطح‌بندی‌شده
│
└── agents/
    ├── state.py / base_agent.py / graph.py / hitl.py / memory_nodes.py
    ├── router/ research/ summarizer/ general/
    └── code_review/
        ├── gitlab/ reviewer/ decision/ scoring/
        └── state.py / graph.py
```