"""
src — پکیجِ اصلی

import کردنِ src (یا هر زیرماژولش) مثلِ قبل اول .env را بار می‌کند (src/env_loader.py)، اما
دیگر LLMClient/EmbeddingClient و در نتیجه src/config.py و متغیرهای الزامیِ LLM را بار نمی‌کند:
این دو فقط با اولین دسترسی ساخته می‌شوند (PEP 562). «from src import LLMClient» و
«from src import config» همان رفتارِ قبلی را دارند.
"""

from __future__ import annotations

import importlib

from src.env_loader import load_env as _load_env

_load_env()

_LAZY = {"LLMClient": "src.llm_client", "EmbeddingClient": "src.embedding_client"}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'src' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})


__all__ = ["LLMClient", "EmbeddingClient"]
