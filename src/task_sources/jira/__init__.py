"""
src/task_sources/jira — دریافتِ یک issueِ Jira به‌عنوانِ ورودیِ تسک

    کلید/نشانیِ issue → ورود (ADFS SAML یا PAT) → REST v2 → TaskInput

جزئیات و تنظیمات: docs/architecture/jira-integration.md
"""

from src.task_sources.jira.client import JiraClient
from src.task_sources.jira.errors import (
    JiraAuthenticationError,
    JiraConfigError,
    JiraConnectionError,
    JiraError,
    JiraMfaRequiredError,
    JiraNotFoundError,
    JiraPermissionError,
    JiraRateLimitError,
    JiraReferenceError,
    JiraResponseError,
    JiraServerError,
)
from src.task_sources.jira.importer import JiraImport, import_issue
from src.task_sources.jira.keys import parse_issue_reference
from src.task_sources.jira.settings import AUTH_ADFS, AUTH_PAT, JiraSettings

__all__ = [
    "AUTH_ADFS",
    "AUTH_PAT",
    "JiraAuthenticationError",
    "JiraClient",
    "JiraConfigError",
    "JiraConnectionError",
    "JiraError",
    "JiraImport",
    "JiraMfaRequiredError",
    "JiraNotFoundError",
    "JiraPermissionError",
    "JiraRateLimitError",
    "JiraReferenceError",
    "JiraResponseError",
    "JiraServerError",
    "JiraSettings",
    "import_issue",
    "parse_issue_reference",
]
