from __future__ import annotations

import os
import uuid

import pytest

from src.observability.models import TraceContext
from src.observability.observability import Observability


pytestmark = pytest.mark.integration


def _langfuse_is_configured() -> bool:
    required = (
        "LANGFUSE_ENABLED",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
    )

    return all(os.getenv(key) for key in required)


@pytest.mark.skipif(
    not _langfuse_is_configured(),
    reason="Langfuse is not configured.",
)
def test_langfuse_integration():
    obs = Observability.from_env()

    ctx = TraceContext(
        user_id="integration-user",
        session_id=f"it-session-{uuid.uuid4()}",
        thread_id=f"it-thread-{uuid.uuid4()}",
        trace_name="integration-test",
        metadata={
            "test": True,
        },
    )

    config = obs.graph_config(ctx)

    assert config["configurable"]["thread_id"] == ctx.thread_id

    with obs.trace(ctx):
        pass

    obs.flush()