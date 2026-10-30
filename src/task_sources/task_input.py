"""
src/task_sources/task_input.py — ورودیِ تسکِ مستقل از منبع

Jira (یا هر سامانه‌ی مدیریتِ تسکِ دیگری در آینده) داده‌اش را به TaskInput
تبدیل می‌کند؛ قدمِ اول فقط متنی را می‌بیند که to_task_description() می‌سازد.
پس جریانِ پنج‌قدمی، Run All و انتخابِ منابعِ Swagger هیچ وابستگی‌ای به منبع
ندارند.

متنِ ساخته‌شده عمداً بخش‌بندی‌شده است، نه چسباندنِ همه‌چیز پشتِ هم:

  * Description «نیازمندیِ اصلی» است.
  * Comments به ترتیبِ زمان می‌آیند، با نویسنده و تاریخ — بحثِ بعدی‌اند و هر
    کامنتی نیازمندی نیست.
  * Attachments محتوای کمکی‌اند (نمونه‌ی cURL، JSON، …) و به‌تنهایی معتبر
    حساب نمی‌شوند. محتوای هر پیوست داخلِ fence می‌آید تا مرزش با بقیه‌ی متن
    روشن باشد — محتوای پیوست ورودیِ نامطمئن است.
  * پیوست‌های پردازش‌نشده (پشتیبانی‌نشده، بزرگ، خراب) با دلیل فهرست می‌شوند تا
    هیچ‌کدام بی‌صدا «پردازش‌شده» دیده نشود.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.task_sources.text import fence_block

ATTACHMENT_PROCESSED = "processed"
ATTACHMENT_UNSUPPORTED = "unsupported"
ATTACHMENT_TOO_LARGE = "too_large"
ATTACHMENT_FAILED = "failed"

ATTACHMENT_STATUS_LABELS: dict[str, str] = {
    ATTACHMENT_PROCESSED: "processed",
    ATTACHMENT_UNSUPPORTED: "unsupported file type",
    ATTACHMENT_TOO_LARGE: "too large",
    ATTACHMENT_FAILED: "failed",
}

_READING_GUIDE = (
    "How to read this task: the Description is the original requirement. Comments "
    "are later discussion in time order — they may clarify or change the requirement, "
    "but not every comment is a requirement. Attachments are supporting material "
    "(examples, samples, notes) and are not authoritative on their own."
)


@dataclass(frozen=True)
class TaskComment:
    author: str
    created: str
    body: str


@dataclass(frozen=True)
class TaskAttachment:
    """یک پیوست و نتیجه‌ی پردازشش — content فقط برای status=processed پر است."""

    filename: str
    media_type: str
    size: int
    status: str
    content: str = ""
    detail: str = ""
    truncated: bool = False

    @property
    def processed(self) -> bool:
        return self.status == ATTACHMENT_PROCESSED

    @property
    def status_label(self) -> str:
        return ATTACHMENT_STATUS_LABELS.get(self.status, self.status)


@dataclass(frozen=True)
class TaskInput:
    title: str
    description: str
    comments: tuple[TaskComment, ...] = ()
    attachments: tuple[TaskAttachment, ...] = ()
    # «jira»، … — و شناسه‌ی همان منبع (کلیدِ issue) و نشانیِ قابلِ بازکردنش.
    source: str = ""
    source_reference: str = ""
    source_url: str = ""
    # نوع، وضعیت، زمانِ دریافت و هشدارهای استخراج — فقط برای نمایش و ردیابی.
    metadata: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    def processed_attachments(self) -> list[TaskAttachment]:
        return [item for item in self.attachments if item.processed]

    def unprocessed_attachments(self) -> list[TaskAttachment]:
        return [item for item in self.attachments if not item.processed]

    def to_task_description(self) -> str:
        """متنِ ساختاریافته‌ی ورودیِ «Task Description»ِ قدمِ اول."""
        reference = self.source_reference or self.source
        lines = [f"# Task {reference}: {self.title}".rstrip()]
        origin = f"Source: {self.source} {self.source_reference}".rstrip()
        if self.source_url:
            origin += f" ({self.source_url})"
        details = [
            f"{label}: {self.metadata[key]}"
            for key, label in (("issue_type", "type"), ("status", "status"))
            if self.metadata.get(key)
        ]
        if details:
            origin += " · " + " · ".join(details)
        lines += [origin, "", _READING_GUIDE, ""]

        lines += ["## Description (original requirement)", ""]
        lines.append(self.description.strip() or "(The issue has no description.)")

        lines += ["", f"## Comments ({len(self.comments)}, oldest first)"]
        if not self.comments:
            lines += ["", "(No comments.)"]
        for number, comment in enumerate(self.comments, start=1):
            lines += [
                "",
                f"### Comment {number} — {comment.author or 'unknown author'}, "
                f"{comment.created or 'unknown date'}",
                "",
                comment.body.strip() or "(empty comment)",
            ]

        processed = self.processed_attachments()
        skipped = self.unprocessed_attachments()
        lines += [
            "",
            f"## Attachments ({len(processed)} processed, {len(skipped)} not processed)",
        ]
        if not self.attachments:
            lines += ["", "(No attachments.)"]
        for item in processed:
            lines += [
                "",
                f"### Attachment: {item.filename} ({item.media_type or 'unknown type'}, "
                f"{item.size} bytes)",
            ]
            if item.detail:
                lines.append(f"Note: {item.detail}")
            lines += ["", fence_block(item.content, _fence_language(item.filename))]
        if skipped:
            lines += ["", "### Attachments not processed", ""]
            lines += [
                f"- {item.filename}: {item.status_label}"
                + (f" — {item.detail}" if item.detail else "")
                for item in skipped
            ]
        return "\n".join(lines).rstrip() + "\n"


_LANGUAGES = {".json": "json", ".yaml": "yaml", ".yml": "yaml", ".md": "markdown"}


def _fence_language(filename: str) -> str:
    suffix = filename[filename.rfind(".") :].lower() if "." in filename else ""
    return _LANGUAGES.get(suffix, "text")


__all__ = [
    "ATTACHMENT_FAILED",
    "ATTACHMENT_PROCESSED",
    "ATTACHMENT_STATUS_LABELS",
    "ATTACHMENT_TOO_LARGE",
    "ATTACHMENT_UNSUPPORTED",
    "TaskAttachment",
    "TaskComment",
    "TaskInput",
]
