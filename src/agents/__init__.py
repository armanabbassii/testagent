"""
src.agents — exportهای این پکیج فقط با اولین دسترسی import می‌شوند (PEP 562)

import کردنِ یک زیرماژول (مثلاً src.agents.test_case_generator.swagger_snapshots) دیگر graph،
HITL و حافظه و وابستگی‌های LLM ِ آن‌ها را بار نمی‌کند. «from src.agents import build_graph»
همان رفتارِ قبلی را دارد.
"""

from __future__ import annotations

import importlib

_LAZY = {
    "AgentState": "src.agents.state",
    "BaseAgent": "src.agents.base_agent",
    "build_graph": "src.agents.graph",
    "HITLHandler": "src.agents.hitl",
    "MemoryLoaderNode": "src.agents.memory_nodes",
    "MemorySaverNode": "src.agents.memory_nodes",
}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'src.agents' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})


__all__ = [
    "AgentState",
    "BaseAgent",
    "build_graph",
    "HITLHandler",
    "MemoryLoaderNode",
    "MemorySaverNode",
]
