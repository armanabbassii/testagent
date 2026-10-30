"""
src/task_sources/jira/settings.py — تنظیماتِ Jira از src/config.py

مقدارها همان ثابت‌های src/config.py اند (که .env را بار می‌کند)؛ این‌جا فقط در
لحظه‌ی استفاده خوانده و اعتبارسنجی می‌شوند تا نبودِ Jira هرگز جلوی بالا آمدنِ
بقیه‌ی برنامه را نگیرد. repr هیچ رازی را نشان نمی‌دهد.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from src.task_sources.jira.errors import JiraConfigError

AUTH_ADFS = "adfs"
AUTH_PAT = "pat"
AUTH_METHODS = (AUTH_ADFS, AUTH_PAT)


@dataclass(frozen=True)
class JiraSettings:
    base_url: str
    auth_method: str = AUTH_ADFS
    sso_url: str = ""
    username: str = ""
    password: str = field(default="", repr=False)
    token: str = field(default="", repr=False)
    ca_bundle: str = ""
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    max_attachment_bytes: int = 10 * 1024 * 1024
    max_attachment_chars: int = 100_000

    @property
    def verify(self) -> str | bool:
        """مقدارِ verify برای requests — فایلِ CA یا True؛ هرگز False."""
        return self.ca_bundle or True

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)

    def validate(self) -> JiraSettings:
        """همه‌ی کمبودها را با هم گزارش می‌دهد؛ خروجی خودِ تنظیمات است."""
        problems: list[str] = []
        if not self.base_url:
            problems.append("JIRA_BASE_URL is not set")
        elif urlsplit(self.base_url).scheme != "https":
            problems.append("JIRA_BASE_URL must be an https:// URL")
        if self.auth_method not in AUTH_METHODS:
            problems.append(
                f"JIRA_AUTH_METHOD must be one of {', '.join(AUTH_METHODS)} "
                f"(got {self.auth_method!r})"
            )
        elif self.auth_method == AUTH_ADFS:
            if not self.sso_url:
                problems.append("JIRA_SSO_URL is not set")
            elif urlsplit(self.sso_url).scheme != "https":
                problems.append("JIRA_SSO_URL must be an https:// URL")
            if not self.username:
                problems.append("JIRA_USERNAME is not set")
            if not self.password:
                problems.append("JIRA_PASSWORD is not set")
        elif not self.token:
            problems.append("JIRA_TOKEN is not set (required for JIRA_AUTH_METHOD=pat)")
        if self.ca_bundle and not Path(self.ca_bundle).is_file():
            problems.append(f"JIRA_CA_BUNDLE points to a missing file: {self.ca_bundle}")
        if self.max_attachment_bytes <= 0 or self.max_attachment_chars <= 0:
            problems.append("JIRA_MAX_ATTACHMENT_BYTES and JIRA_MAX_ATTACHMENT_CHARS must be > 0")
        if problems:
            raise JiraConfigError(
                "Jira is not configured: " + "; ".join(problems) + ". Set these in .env "
                "(see docs/architecture/jira-integration.md)."
            )
        return self

    @classmethod
    def from_config(cls) -> JiraSettings:
        """مقدارهای src/config.py — در لحظه‌ی فراخوانی خوانده می‌شوند (قابلِ patch در تست)."""
        from src import config

        return cls(
            base_url=config.JIRA_BASE_URL.strip().rstrip("/"),
            auth_method=(config.JIRA_AUTH_METHOD or AUTH_ADFS).strip().lower(),
            sso_url=config.JIRA_SSO_URL.strip(),
            username=config.JIRA_USERNAME.strip(),
            password=config.JIRA_PASSWORD,
            token=config.JIRA_TOKEN.strip(),
            ca_bundle=config.JIRA_CA_BUNDLE.strip(),
            connect_timeout=config.JIRA_CONNECT_TIMEOUT_SECONDS,
            read_timeout=config.JIRA_READ_TIMEOUT_SECONDS,
            max_attachment_bytes=config.JIRA_MAX_ATTACHMENT_BYTES,
            max_attachment_chars=config.JIRA_MAX_ATTACHMENT_CHARS,
        )


__all__ = ["AUTH_ADFS", "AUTH_METHODS", "AUTH_PAT", "JiraSettings"]
