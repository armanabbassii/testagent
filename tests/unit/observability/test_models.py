from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from src.observability.models import TraceContext


def test_trace_context_creation():
    ctx = TraceContext(
        user_id="reza",
        session_id="session-1",
        thread_id="thread-1",
        trace_name="review",
        metadata={"mr": 1},
    )

    assert ctx.user_id == "reza"
    assert ctx.session_id == "session-1"
    assert ctx.thread_id == "thread-1"
    assert ctx.trace_name == "review"
    assert ctx.metadata == {"mr": 1}


def test_trace_context_is_frozen():
    ctx = TraceContext(
        user_id="reza",
        session_id="s1",
        thread_id="t1",
    )

    with pytest.raises(FrozenInstanceError):
        ctx.user_id = "another"