from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(slots=True, frozen=True)
class TraceContext:
    """
    اطلاعات مربوط به Trace جاری.
    """

    user_id: str
    session_id: str
    thread_id: str

    trace_name: str | None = None

    metadata: Mapping[str, Any] | None = None