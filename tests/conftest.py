"""
tests/conftest.py — fixture های مشترک بین همه تست‌ها

fixture های اینجا به صورت خودکار در دسترس همه تست‌ها هستند.
"""

import pytest
from unittest.mock import MagicMock
from src.debug.levels import DebugLevel
from src.debug.base_handler import BaseHandler, LogRecord


# ── Handlers ──────────────────────────────────────────────────────────────────

class MemoryHandler(BaseHandler):
    """یک handler تستی که لاگ‌ها را در حافظه نگه می‌دارد."""

    def __init__(self, min_level: DebugLevel = DebugLevel.TRACE):
        super().__init__(min_level=min_level)
        self.records: list[LogRecord] = []

    def emit(self, record: LogRecord) -> None:
        self.records.append(record)

    def close(self) -> None:
        self.records.clear()

    def messages(self) -> list[str]:
        return [r.message for r in self.records]

    def levels(self) -> list[str]:
        return [r.level_label() for r in self.records]


@pytest.fixture
def memory_handler() -> MemoryHandler:
    """یک handler تستی که لاگ‌ها را در حافظه نگه می‌دارد."""
    return MemoryHandler()


# ── pytest plugins ────────────────────────────────────────────────────

pytest_plugins = [
    "tests.fixtures.observability",
]