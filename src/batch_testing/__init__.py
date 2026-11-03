"""
src/batch_testing — اجرای دسته‌ایِ تست برای issueهای واجدِ شرایطِ یک بُردِ Jira

مرحله‌ی فعلی فقط کشف و سنجشِ واجدِ شرایط بودن است (فقط‌خواندنی).
"""

from src.batch_testing.discovery import (
    DEFAULT_CRITERIA,
    BoardContext,
    Candidate,
    DiscoveryError,
    DiscoveryResult,
    EligibilityCriteria,
    discover,
    evaluate,
    resolve_board,
)

__all__ = [
    "DEFAULT_CRITERIA",
    "BoardContext",
    "Candidate",
    "DiscoveryError",
    "DiscoveryResult",
    "EligibilityCriteria",
    "discover",
    "evaluate",
    "resolve_board",
]
