"""
محافظ‌های مشترکِ تست‌های همگام‌سازیِ Swagger:

  * هیچ اتصالِ شبکه‌ای (connect/getaddrinfo) — هر تلاش AssertionError است؛
  * پوشه‌ی پیش‌فرضِ snapshot به یک پوشه‌ی موقت برگردانده می‌شود، پس تستی که directory را
    فراموش کند هرگز به data/swagger دست نمی‌زند؛
  * SWAGGER_SYNC_* و SWAGGER_API_DOCS_BASE_URL ِ محیط پاک می‌شوند؛
  * هشِ فایل‌های واقعیِ data/swagger پیش و پس از کلِ نشست باید یکی باشد.
"""

from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path

import pytest

from src.agents.test_case_generator import swagger_snapshots
from src.swagger_sync import __main__ as sync_cli

REAL_SWAGGER = Path(__file__).resolve().parents[3] / "data" / "swagger"
REAL_LOGS = Path(__file__).resolve().parents[3] / "logs" / "swagger_sync"


def _hashes() -> dict[str, str]:
    if not REAL_SWAGGER.is_dir():
        return {}
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(REAL_SWAGGER.iterdir())
        if p.is_file()
    }


@pytest.fixture(scope="session", autouse=True)
def _real_swagger_files_are_never_touched():
    before, logs_existed = _hashes(), REAL_LOGS.exists()
    yield
    assert _hashes() == before, "the real data/swagger files changed during the tests"
    assert REAL_LOGS.exists() == logs_existed, "the tests created the real logs/swagger_sync"


@pytest.fixture(autouse=True)
def _offline_and_isolated(monkeypatch, tmp_path):
    def blocked(*args, **kwargs):
        raise AssertionError("No network access in Swagger sync tests.")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(swagger_snapshots, "SNAPSHOT_DIR", tmp_path / "default_snapshots")
    for key in list(os.environ):
        if key.startswith("SWAGGER_SYNC_") or key == "SWAGGER_API_DOCS_BASE_URL":
            monkeypatch.delenv(key)
    monkeypatch.setenv("SWAGGER_SYNC_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(sync_cli, "DEFAULT_LOG_DIR", tmp_path / "default_logs")
