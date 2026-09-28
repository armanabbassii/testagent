"""
tests/conftest.py — fixture های مشترک بین همه تست‌ها

fixture های اینجا به صورت خودکار در دسترس همه تست‌ها هستند.
"""

import pytest
from unittest.mock import MagicMock
from src.debug.levels import DebugLevel
from src.debug.base_handler import BaseHandler, LogRecord
from src.agents.code_review.state import ReviewComment


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


# ── ReviewComment fixtures ────────────────────────────────────────────────────

@pytest.fixture
def critical_comment() -> ReviewComment:
    return ReviewComment(
        file_path="src/auth/login.py",
        line=42,
        severity="critical",
        category="security",
        body="SQL injection vulnerability: user input passed directly to query.",
    )


@pytest.fixture
def major_comment() -> ReviewComment:
    return ReviewComment(
        file_path="src/utils/helpers.py",
        line=15,
        severity="major",
        category="correctness",
        body="Missing null check before dereferencing.",
    )


@pytest.fixture
def minor_comment() -> ReviewComment:
    return ReviewComment(
        file_path="src/models/user.py",
        line=None,
        severity="minor",
        category="style",
        body="Function name does not follow naming convention.",
    )


@pytest.fixture
def suggestion_comment() -> ReviewComment:
    return ReviewComment(
        file_path="src/api/views.py",
        line=88,
        severity="suggestion",
        category="performance",
        body="Consider caching this result to avoid repeated computation.",
    )


@pytest.fixture
def mixed_comments(
    critical_comment, major_comment, minor_comment, suggestion_comment
) -> list[ReviewComment]:
    """یک لیست با همه نوع severity."""
    return [critical_comment, major_comment, minor_comment, suggestion_comment]


# ── pytest plugins ────────────────────────────────────────────────────

pytest_plugins = [
    "tests.fixtures.observability",
]