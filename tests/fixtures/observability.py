from __future__ import annotations

import pytest

from src.observability.models import TraceContext
from src.observability.settings import LangfuseSettings


@pytest.fixture
def langfuse_settings() -> LangfuseSettings:
    return LangfuseSettings(
        enabled=True,
        public_key="pk-test",
        secret_key="sk-test",
        host="http://localhost:3000",
    )


@pytest.fixture
def disabled_settings() -> LangfuseSettings:
    return LangfuseSettings(
        enabled=False,
        public_key="",
        secret_key="",
        host="",
    )


@pytest.fixture
def trace_context() -> TraceContext:
    return TraceContext(
        user_id="user-123",
        session_id="session-123",
        thread_id="thread-123",
        trace_name="code-review",
        metadata={
            "mr_iid": 42,
        },
    )