"""تست‌های API لایه business_chat (FastAPI) — با mock روی BusinessRAGAgent."""

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from src.api.business_chat import app, get_agent


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.recommend.return_value = {
        "answer": "بهترین سرویس X است.",
        "documents": [{"content": "doc", "score": 0.9, "metadata": {}}],
        "filters_used": {"categories": "payment"},
    }
    agent.known_categories = ["payment", "sms"]
    agent.known_providers = ["ProviderX"]
    return agent


@pytest.fixture
def client(mock_agent):
    """TestClient با override شدن dependency واقعی get_agent با mock."""
    app.dependency_overrides[get_agent] = lambda: mock_agent
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── /health ───────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_returns_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


# ── /categories ───────────────────────────────────────────────────────────────

class TestCategoriesEndpoint:
    def test_returns_200(self, client):
        response = client.get("/categories")
        assert response.status_code == 200

    def test_returns_configured_categories(self, client):
        response = client.get("/categories")
        assert response.json() == {"categories": ["payment", "sms"]}

    def test_empty_when_agent_has_none(self, mock_agent, client):
        mock_agent.known_categories = []
        response = client.get("/categories")
        assert response.json() == {"categories": []}


# ── /providers ────────────────────────────────────────────────────────────────

class TestProvidersEndpoint:
    def test_returns_200(self, client):
        response = client.get("/providers")
        assert response.status_code == 200

    def test_returns_configured_providers(self, client):
        response = client.get("/providers")
        assert response.json() == {"providers": ["ProviderX"]}

    def test_empty_when_agent_has_none(self, mock_agent, client):
        mock_agent.known_providers = []
        response = client.get("/providers")
        assert response.json() == {"providers": []}


# ── /chat — مسیر موفق ────────────────────────────────────────────────────────

class TestChatEndpointSuccess:
    def test_chat_returns_200(self, client):
        response = client.post("/chat", json={"query": "سرویس پرداخت می‌خوام", "user_id": "u1"})
        assert response.status_code == 200

    def test_chat_returns_answer_field(self, client):
        response = client.post("/chat", json={"query": "q", "user_id": "u1"})
        assert response.json()["answer"] == "بهترین سرویس X است."

    def test_chat_returns_filters_used(self, client):
        response = client.post("/chat", json={"query": "q", "user_id": "u1"})
        assert response.json()["filters_used"] == {"categories": "payment"}

    def test_chat_returns_documents(self, client):
        response = client.post("/chat", json={"query": "q", "user_id": "u1"})
        docs = response.json()["documents"]
        assert len(docs) == 1
        assert docs[0]["content"] == "doc"

    def test_chat_calls_agent_recommend_with_correct_args(self, client, mock_agent):
        client.post("/chat", json={"query": "test query", "user_id": "user-42"})
        mock_agent.recommend.assert_called_once_with(query="test query", user_id="user-42")


# ── /chat — اعتبارسنجی ورودی ─────────────────────────────────────────────────

class TestChatEndpointValidation:
    def test_empty_query_returns_422(self, client):
        response = client.post("/chat", json={"query": "", "user_id": "u1"})
        assert response.status_code == 422

    def test_missing_user_id_returns_422(self, client):
        response = client.post("/chat", json={"query": "hello"})
        assert response.status_code == 422

    def test_missing_query_returns_422(self, client):
        response = client.post("/chat", json={"user_id": "u1"})
        assert response.status_code == 422

    def test_empty_user_id_returns_422(self, client):
        response = client.post("/chat", json={"query": "hello", "user_id": ""})
        assert response.status_code == 422


# ── /chat — خطای داخلی ───────────────────────────────────────────────────────

class TestChatEndpointErrorHandling:
    def test_agent_exception_returns_500(self, client, mock_agent):
        mock_agent.recommend.side_effect = Exception("boom")
        response = client.post("/chat", json={"query": "q", "user_id": "u1"})
        assert response.status_code == 500

    def test_agent_exception_does_not_leak_internal_message(self, client, mock_agent):
        mock_agent.recommend.side_effect = Exception("internal secret detail")
        response = client.post("/chat", json={"query": "q", "user_id": "u1"})
        assert "internal secret detail" not in response.text


# ── CORS ──────────────────────────────────────────────────────────────────────

class TestCors:
    def test_preflight_allows_post_to_chat(self, client):
        response = client.options(
            "/chat",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code in (200, 204)


# ── _split_env_list ───────────────────────────────────────────────────────────

class TestSplitEnvList:
    def test_splits_comma_separated_values(self, monkeypatch):
        from src.api.business_chat import _split_env_list
        monkeypatch.setenv("TEST_LIST_VAR", "a, b ,c")
        assert _split_env_list("TEST_LIST_VAR") == ["a", "b", "c"]

    def test_missing_env_returns_empty_list(self, monkeypatch):
        from src.api.business_chat import _split_env_list
        monkeypatch.delenv("TEST_LIST_VAR_MISSING", raising=False)
        assert _split_env_list("TEST_LIST_VAR_MISSING") == []

    def test_empty_string_returns_empty_list(self, monkeypatch):
        from src.api.business_chat import _split_env_list
        monkeypatch.setenv("TEST_LIST_VAR_EMPTY", "")
        assert _split_env_list("TEST_LIST_VAR_EMPTY") == []

    def test_ignores_extra_whitespace_entries(self, monkeypatch):
        from src.api.business_chat import _split_env_list
        monkeypatch.setenv("TEST_LIST_VAR_WS", "a,, ,b")
        assert _split_env_list("TEST_LIST_VAR_WS") == ["a", "b"]


# ── get_agent — singleton ────────────────────────────────────────────────────

class TestGetAgentSingleton:
    def test_get_agent_is_cached(self, monkeypatch):
        from src.api import business_chat as mod
        mod.get_agent.cache_clear()

        created = []

        def fake_business_rag_agent(*args, **kwargs):
            created.append(1)
            return MagicMock()

        monkeypatch.setattr(mod, "BusinessRAGAgent", fake_business_rag_agent)
        monkeypatch.setattr(mod, "EmbeddingClient", MagicMock())
        monkeypatch.setattr(mod, "QdrantVectorStore", MagicMock())
        monkeypatch.setattr(mod, "JsonMemory", MagicMock())

        agent1 = mod.get_agent()
        agent2 = mod.get_agent()

        assert agent1 is agent2
        assert len(created) == 1

        mod.get_agent.cache_clear()

    def test_get_agent_passes_known_lists_from_env(self, monkeypatch):
        from src.api import business_chat as mod
        mod.get_agent.cache_clear()

        captured_kwargs = {}

        def fake_business_rag_agent(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        monkeypatch.setenv("BUSINESS_KNOWN_CATEGORIES", "payment,sms")
        monkeypatch.setenv("BUSINESS_KNOWN_PROVIDERS", "ProviderX")
        monkeypatch.setattr(mod, "BusinessRAGAgent", fake_business_rag_agent)
        monkeypatch.setattr(mod, "EmbeddingClient", MagicMock())
        monkeypatch.setattr(mod, "QdrantVectorStore", MagicMock())
        monkeypatch.setattr(mod, "JsonMemory", MagicMock())

        mod.get_agent()

        assert captured_kwargs["known_categories"] == ["payment", "sms"]
        assert captured_kwargs["known_providers"] == ["ProviderX"]

        mod.get_agent.cache_clear()