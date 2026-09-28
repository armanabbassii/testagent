from __future__ import annotations

from dataclasses import dataclass

from src import config


@dataclass(slots=True, frozen=True)
class LangfuseSettings:
    """
    تنظیمات Langfuse.

    این کلاس فقط مسئول خواندن تنظیمات از src.config است
    و هیچ وابستگی به SDK ندارد.
    """

    enabled: bool
    public_key: str
    secret_key: str
    host: str

    @classmethod
    def from_env(cls) -> "LangfuseSettings":
        return cls(
            enabled=config.LANGFUSE_ENABLED,
            public_key=config.LANGFUSE_PUBLIC_KEY,
            secret_key=config.LANGFUSE_SECRET_KEY,
            host=config.LANGFUSE_HOST,
        )