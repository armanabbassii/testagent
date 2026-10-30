"""
src/task_sources/jira/errors.py — خطاهای صریحِ Jira

هر شکست نوعِ خودش را دارد تا UI بتواند «ورود ناموفق» را از «دسترسی به issue
نیست» جدا نشان دهد. پیام‌ها هرگز رمز، کوکی، توکن یا SAML assertion ندارند.
"""

from __future__ import annotations


class JiraError(Exception):
    """پایه‌ی همه‌ی خطاهای Jira."""


class JiraConfigError(JiraError):
    """تنظیماتِ Jira ناقص یا نامعتبر است (متغیرهای محیطی)."""


class JiraReferenceError(JiraError):
    """کلید یا نشانیِ issue نامعتبر است."""


class JiraAuthenticationError(JiraError):
    """ورود ناموفق بود یا نشستِ فعلی دیگر معتبر نیست."""


class JiraMfaRequiredError(JiraAuthenticationError):
    """ADFS گامِ تعاملیِ دیگری (MFA، …) خواست — دور زده نمی‌شود."""


class JiraPermissionError(JiraError):
    """ورود موفق بود ولی کاربر به این منبع دسترسی ندارد (403)."""


class JiraNotFoundError(JiraError):
    """issue یا منبع پیدا نشد (404)."""


class JiraRateLimitError(JiraError):
    """Jira درخواست‌ها را محدود کرده است (429)."""


class JiraServerError(JiraError):
    """خطای سمتِ سرورِ Jira (5xx)."""


class JiraConnectionError(JiraError):
    """شبکه، timeout یا تأییدِ گواهیِ SSL."""


class JiraResponseError(JiraError):
    """پاسخِ Jira با قراردادِ REST جور نیست."""


__all__ = [
    "JiraAuthenticationError",
    "JiraConfigError",
    "JiraConnectionError",
    "JiraError",
    "JiraMfaRequiredError",
    "JiraNotFoundError",
    "JiraPermissionError",
    "JiraRateLimitError",
    "JiraReferenceError",
    "JiraResponseError",
    "JiraServerError",
]
