"""
api/business_chat.py — لایه HTTP نازک روی BusinessRAGAgent

این ماژول جایگزین UI نیست و منطق کسب‌وکاری‌ای ندارد — فقط endpoint های سبک
برای مصرف توسط چت‌بات/UI فراهم می‌کند. تمام منطق پیشنهاد سرویس همچنان در
BusinessRAGAgent (src/agents/business_rag) قرار دارد.

BusinessRAGAgent به صورت singleton و lazy ساخته می‌شود تا اتصال به
vector store و embedding client فقط یک‌بار برای کل عمر سرویس برقرار شود،
نه به ازای هر request.

نکته درباره‌ی /categories و /providers:
    BaseVectorStore هیچ متدی برای اسکن مقادیر یکتای متادیتا ندارد (و طبق قوانین
    پروژه نباید این abstraction تغییر کند). بنابراین این دو endpoint مقادیری را
    برمی‌گردانند که هنگام راه‌اندازی سرویس از طریق متغیرهای محیطی پیکربندی
    شده‌اند (BUSINESS_KNOWN_CATEGORIES / BUSINESS_KNOWN_PROVIDERS)، نه نتیجه‌ی
    اسکن زنده‌ی collection. این مقادیر همان‌هایی هستند که به‌عنوان راهنما به LLM
    برای استخراج فیلتر داده می‌شوند (BusinessRAGAgent.known_categories/known_providers).

نصب وابستگی‌های اختیاری:
    uv sync --extra api

تنظیمات محیطی اختیاری:
    BUSINESS_KNOWN_CATEGORIES="پرداخت,پیامک,احراز هویت"   # comma-separated
    BUSINESS_KNOWN_PROVIDERS="ProviderA,ProviderB"          # comma-separated
    BUSINESS_CHAT_CORS_ORIGINS="*"                          # comma-separated یا * برای dev

اجرا:
    uv run uvicorn src.api.business_chat:app --reload --port 8080

سپس UI حداقلی در دسترس است:
    http://localhost:8080/ui

تست دستی API:
    curl -X POST http://localhost:8080/chat \
        -H "Content-Type: application/json" \
        -d '{"query": "یه سرویس پرداخت می‌خوام", "user_id": "customer-1"}'
"""

import os
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.vector_store import QdrantVectorStore
from src.embedding_client import EmbeddingClient
from src.memory import JsonMemory
from src.agents.business_rag import BusinessRAGAgent
from src.debug import DebugConfig

_SERVICE_USER_ID = "business-chat-api"
_COLLECTION_NAME = "business_ingest"
_MEMORY_PATH = "output/business_chat_memory.json"
_STATIC_DIR = Path(__file__).parent / "static"

_log = DebugConfig.from_env().get_logger("business_chat_api")


def _split_env_list(env_var: str) -> list[str]:
    """یک متغیر محیطی comma-separated را به لیست تمیز تبدیل می‌کند."""
    raw = os.getenv(env_var, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_agent() -> BusinessRAGAgent:
    """نمونه singleton (lazy) از BusinessRAGAgent می‌سازد.

    به عنوان FastAPI dependency استفاده می‌شود؛ در تست‌ها با
    app.dependency_overrides[get_agent] قابل جایگزینی است.
    """
    _log.info("ساخت نمونه BusinessRAGAgent برای سرویس API")
    emb = EmbeddingClient(user_id=_SERVICE_USER_ID)
    store = QdrantVectorStore(collection=_COLLECTION_NAME, embedding_client=emb)
    memory = JsonMemory(path=_MEMORY_PATH, search_mode="keyword")
    return BusinessRAGAgent(
        vector_store=store,
        top_k=5,
        known_categories=_split_env_list("BUSINESS_KNOWN_CATEGORIES"),
        known_providers=_split_env_list("BUSINESS_KNOWN_PROVIDERS"),
        memory=memory,
        debug_config=DebugConfig.from_env(),
    )


# ── مدل‌های ورودی/خروجی ──────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, description="پیام/سؤال مشتری")
    user_id: str = Field(..., min_length=1, description="شناسه یکتای کاربر/نشست چت")


class ChatResponse(BaseModel):
    answer: str
    documents: list[dict]
    filters_used: dict


class CategoriesResponse(BaseModel):
    categories: list[str]


class ProvidersResponse(BaseModel):
    providers: list[str]


# ── اپلیکیشن ──────────────────────────────────────────────────────────────────

app = FastAPI(title="Business RAG Chat API", version="0.1.0")

# CORS برای زمانی که UI به‌صورت پروژه‌ی جدا (دامنه/پورت متفاوت) سرو شود.
# مقدار پیش‌فرض "*" فقط برای توسعه محلی مناسب است — در production حتماً
# BUSINESS_CHAT_CORS_ORIGINS را به دامنه‌های واقعی محدود کنید.
_cors_origins = _split_env_list("BUSINESS_CHAT_CORS_ORIGINS") or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

if _STATIC_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(_STATIC_DIR), html=True), name="ui")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/categories", response_model=CategoriesResponse)
def categories(agent: BusinessRAGAgent = Depends(get_agent)) -> CategoriesResponse:
    return CategoriesResponse(categories=agent.known_categories)


@app.get("/providers", response_model=ProvidersResponse)
def providers(agent: BusinessRAGAgent = Depends(get_agent)) -> ProvidersResponse:
    return ProvidersResponse(providers=agent.known_providers)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, agent: BusinessRAGAgent = Depends(get_agent)) -> ChatResponse:
    _log.info("درخواست چت دریافت شد", user_id=req.user_id)

    try:
        result = agent.recommend(query=req.query, user_id=req.user_id)
    except Exception as e:
        _log.error("پردازش چت با خطا مواجه شد", user_id=req.user_id, error=str(e))
        raise HTTPException(status_code=500, detail="خطا در پردازش درخواست") from e

    _log.debug("پاسخ چت آماده شد", user_id=req.user_id, doc_count=len(result.get("documents", [])))
    return ChatResponse(**result)