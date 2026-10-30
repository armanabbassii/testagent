"""
src/task_sources/jira/keys.py — کلید یا نشانیِ issue → کلیدِ issue

پذیرفته می‌شود: «PODIUM-3386»، «podium-3386» و نشانی‌های
«https://<jira>/browse/PODIUM-3386» (با query یا fragment). نشانیِ میزبانِ
دیگری جز JIRA_BASE_URL رد می‌شود — نشستِ Jira نباید جای دیگری خرج شود.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from src.task_sources.jira.errors import JiraReferenceError

ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-[1-9][0-9]*$")
_BROWSE = re.compile(r"/browse/([^/?#]+)/?$")


def parse_issue_reference(reference: str, base_url: str = "") -> str:
    """کلیدِ issue (حروفِ بزرگ) — یا JiraReferenceError با توضیحِ قابلِ فهم."""
    text = (reference or "").strip()
    if not text:
        raise JiraReferenceError("Enter a Jira issue key (e.g. PROJ-123) or an issue URL.")
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise JiraReferenceError(f"Not a Jira issue URL: {text}")
        if base_url and parts.netloc.lower() != urlsplit(base_url).netloc.lower():
            raise JiraReferenceError(
                f"The URL points to {parts.netloc}, not the configured Jira "
                f"({urlsplit(base_url).netloc})."
            )
        match = _BROWSE.search(parts.path)
        if not match:
            raise JiraReferenceError(
                "A Jira issue URL looks like https://<jira>/browse/PROJ-123."
            )
        text = match.group(1)
    key = text.upper()
    if not ISSUE_KEY.match(key):
        raise JiraReferenceError(
            f"{text!r} is not a Jira issue key — expected something like PROJ-123."
        )
    return key


__all__ = ["ISSUE_KEY", "parse_issue_reference"]
