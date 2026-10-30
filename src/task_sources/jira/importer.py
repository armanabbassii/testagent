"""
src/task_sources/jira/importer.py — یک issueِ Jira → TaskInput + گزارشِ استخراج

مراحل جدا گزارش می‌شوند تا UI بگوید دقیقاً کجا شکست خورد: تنظیمات، کلیدِ
issue، ورود، دریافتِ issue، کامنت‌ها. شکستِ یک پیوست کلِ دریافت را خراب
نمی‌کند ولی با وضعیت و دلیلش گزارش می‌شود.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.task_sources.attachments import process_attachment
from src.task_sources.jira.client import JiraClient
from src.task_sources.jira.errors import JiraError, JiraReferenceError
from src.task_sources.jira.keys import parse_issue_reference
from src.task_sources.jira.markup import normalize_wiki
from src.task_sources.jira.settings import JiraSettings
from src.task_sources.task_input import TaskAttachment, TaskComment, TaskInput

SOURCE_JIRA = "jira"

STAGE_CONFIGURATION = "configuration"
STAGE_REFERENCE = "issue reference"
STAGE_AUTHENTICATION = "authentication"
STAGE_ISSUE = "issue"
STAGE_COMMENTS = "comments"


@dataclass
class JiraImport:
    """نتیجه‌ی دریافت — task_input فقط وقتی پر است که همه‌ی مراحلِ اصلی موفق باشند."""

    reference: str
    issue_key: str = ""
    task_input: TaskInput | None = None
    auth_method: str = ""
    identity: str = ""
    authenticated: bool = False
    issue_found: bool = False
    comments_retrieved: bool = False
    error: str = ""
    error_stage: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.task_input is not None and not self.error

    def fail(self, stage: str, exc: Exception) -> JiraImport:
        self.error_stage = stage
        self.error = str(exc)
        return self


def format_timestamp(value: str) -> str:
    """«2026-10-14T18:09:41.000+0330» → «2026-10-14 18:09 +03:30»؛ نامعتبر همان‌طور می‌ماند."""
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except (TypeError, ValueError):
        return value or ""
    offset = moment.strftime("%z")
    return f"{moment:%Y-%m-%d %H:%M} {offset[:3]}:{offset[3:]}"


def _person(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("displayName") or value.get("name") or "")
    return ""


def _comment(raw: dict[str, Any]) -> TaskComment:
    created = format_timestamp(str(raw.get("created") or ""))
    updated = format_timestamp(str(raw.get("updated") or ""))
    if updated and updated != created:
        created = f"{created} (edited {updated})"
    return TaskComment(
        author=_person(raw.get("author")),
        created=created,
        body=normalize_wiki(str(raw.get("body") or "")),
    )


def _attachment_warnings(attachments: list[TaskAttachment]) -> list[str]:
    warnings = []
    for item in attachments:
        if not item.processed:
            warnings.append(f"Attachment {item.filename}: {item.status_label} — {item.detail}")
        elif item.detail:
            warnings.append(f"Attachment {item.filename}: {item.detail}")
    return warnings


def import_issue(
    reference: str,
    *,
    settings: JiraSettings | None = None,
    client: JiraClient | None = None,
) -> JiraImport:
    """issue را می‌خواند و TaskInput می‌سازد — هرگز JiraError بیرون نمی‌دهد."""
    result = JiraImport(reference=reference)
    try:
        settings = (settings or JiraSettings.from_config()).validate()
    except JiraError as exc:
        return result.fail(STAGE_CONFIGURATION, exc)
    try:
        key = parse_issue_reference(reference, settings.base_url)
    except JiraReferenceError as exc:
        return result.fail(STAGE_REFERENCE, exc)
    result.issue_key = key

    client = client or JiraClient(settings)
    result.auth_method = client.auth_method_name
    try:
        identity = client.ensure_authenticated()
    except JiraError as exc:
        return result.fail(STAGE_AUTHENTICATION, exc)
    result.authenticated = True
    result.identity = str(identity.get("displayName") or identity.get("name") or "")

    try:
        issue = client.issue(key)
    except JiraError as exc:
        return result.fail(STAGE_ISSUE, exc)
    result.issue_found = True
    fields = issue["fields"]

    try:
        comments = [_comment(raw) for raw in client.comments(key)]
    except JiraError as exc:
        return result.fail(STAGE_COMMENTS, exc)
    result.comments_retrieved = True

    attachments = []
    for meta in fields.get("attachment") or []:
        content_url = str(meta.get("content") or "")

        def download(limit: int, url: str = content_url) -> bytes:
            if not url:
                raise JiraError("Jira did not provide a download URL")
            return client.download(url, limit)

        attachments.append(
            process_attachment(
                filename=str(meta.get("filename") or "(unnamed)"),
                media_type=str(meta.get("mimeType") or ""),
                size=int(meta.get("size") or 0),
                download=download,
                max_bytes=settings.max_attachment_bytes,
                max_chars=settings.max_attachment_chars,
            )
        )
    result.warnings = _attachment_warnings(attachments)

    result.task_input = TaskInput(
        title=str(fields.get("summary") or "").strip(),
        description=normalize_wiki(str(fields.get("description") or "")),
        comments=tuple(comments),
        attachments=tuple(attachments),
        source=SOURCE_JIRA,
        source_reference=key,
        source_url=f"{settings.base_url}/browse/{key}",
        metadata={
            "issue_type": _name(fields.get("issuetype")),
            "status": _name(fields.get("status")),
            "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "warnings": list(result.warnings),
        },
    )
    return result


def _name(value: Any) -> str:
    return str(value.get("name") or "") if isinstance(value, dict) else ""


__all__ = [
    "SOURCE_JIRA",
    "STAGE_AUTHENTICATION",
    "STAGE_COMMENTS",
    "STAGE_CONFIGURATION",
    "STAGE_ISSUE",
    "STAGE_REFERENCE",
    "JiraImport",
    "format_timestamp",
    "import_issue",
]
