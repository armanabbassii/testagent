"""
tests/unit/sonar/conftest.py — فیکسچرهای مشترک تست‌های ماژول sonar
"""

import pytest


def _sonar_issue(
    severity: str = "MAJOR",
    issue_type: str = "BUG",
    component: str = "my-project:src/foo.py",
    line: int = 10,
    message: str = "خطای نمونه",
    rule: str = "python:S1234",
) -> dict:
    """یک issue خام سونار (فرمت خروجی api/issues/search) می‌سازد."""
    return {
        "severity": severity,
        "type": issue_type,
        "component": component,
        "message": message,
        "rule": rule,
        "textRange": {"startLine": line},
    }


@pytest.fixture
def sonar_issue_factory():
    """یک factory برای ساخت issue خام سونار با مقادیر قابل override."""
    return _sonar_issue


@pytest.fixture
def sample_sonar_issue() -> dict:
    return _sonar_issue()