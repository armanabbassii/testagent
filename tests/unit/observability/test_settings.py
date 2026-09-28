from __future__ import annotations
from dataclasses import FrozenInstanceError

import pytest

from src.observability.settings import LangfuseSettings


def test_from_env(monkeypatch):
    from src import config

    monkeypatch.setattr(config, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(config, "LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setattr(config, "LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setattr(config, "LANGFUSE_HOST", "http://localhost")

    settings = LangfuseSettings.from_env()

    assert settings.enabled is True
    assert settings.public_key == "pk"
    assert settings.secret_key == "sk"
    assert settings.host == "http://localhost"


def test_settings_are_immutable():
    settings = LangfuseSettings(
        enabled=True,
        public_key="pk",
        secret_key="sk",
        host="host",
    )

    assert settings.enabled
    assert settings.public_key == "pk"


def test_settings_are_frozen():
    settings = LangfuseSettings(
        enabled=True,
        public_key="pk",
        secret_key="sk",
        host="host",
    )

    with pytest.raises(FrozenInstanceError):
        settings.host = "another"