"""
src/task_sources/sources.py — منابعِ متنیِ یک تسک برای سنجشِ شاهد

تصمیم‌های محیط (VDI/عادی) فقط وقتی پذیرفته می‌شوند که جمله‌ی شاهدشان عیناً در
یکی از این منابع باشد. منبع‌ها با کد ساخته می‌شوند، نه با مدل:

    issueِ Jira → Description، هر Comment (با شناسه‌اش)، متنِ هر پیوستِ پردازش‌شده
    متنِ دستی  → یک منبعِ description با کلِ متن

خطوطِ سرصفحه‌ای که to_task_description می‌سازد (# Task …، Source: …، راهنما)
جزوِ هیچ منبعی نیستند؛ پس متنِ تولیدی هرگز شاهد حساب نمی‌شود.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from src.task_sources.task_input import TaskInput

SOURCE_DESCRIPTION = "description"
SOURCE_COMMENT = "comment"
SOURCE_ATTACHMENT = "attachment"
SOURCE_TYPES = (SOURCE_DESCRIPTION, SOURCE_COMMENT, SOURCE_ATTACHMENT)

MANUAL_DESCRIPTION_LABEL = "Task description"


@dataclass(frozen=True)
class TaskSource:
    source_type: str
    # شناسه‌ی کامنت، نامِ فایلِ پیوست، یا "" برای description
    reference: str
    # همان سرعنوانی که مدل در متنِ تسک می‌بیند: «Comment 3»، «Attachment: a.md»
    label: str
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def summary(self) -> dict[str, object]:
        """فراداده‌ی منبع بدونِ متنش — برای نتیجه‌ی ذخیره‌شده و UI."""
        return {
            "type": self.source_type,
            "reference": self.reference,
            "label": self.label,
            "sha256": self.sha256,
            "chars": len(self.text),
        }


def sources_from_text(text: str) -> tuple[TaskSource, ...]:
    return (TaskSource(SOURCE_DESCRIPTION, "", MANUAL_DESCRIPTION_LABEL, text or ""),)


def sources_from_task_input(task: TaskInput) -> tuple[TaskSource, ...]:
    """همان متن‌هایی که to_task_description در بخش‌های Description/Comments/Attachments می‌گذارد."""
    sources = [TaskSource(SOURCE_DESCRIPTION, "", "Description", task.description.strip())]
    sources += [
        TaskSource(SOURCE_COMMENT, comment.id, f"Comment {number}", comment.body.strip())
        for number, comment in enumerate(task.comments, start=1)
    ]
    sources += [
        TaskSource(SOURCE_ATTACHMENT, item.filename, f"Attachment: {item.filename}", item.content)
        for item in task.processed_attachments()
    ]
    return tuple(sources)


def sources_for_task(
    task_description: str, task_input: TaskInput | None = None
) -> tuple[TaskSource, ...]:
    """منابعِ متنِ فعلیِ قدمِ اول — ویزارد و Batch هر دو همین را صدا می‌زنند.

    منابعِ Jira فقط وقتی استفاده می‌شوند که متنِ فعلی دقیقاً همان متنِ ساخته‌شده
    از همان TaskInput باشد؛ متنی که کاربر بعداً ویرایش کرده یک متنِ دستی است.
    """
    if task_input is not None and task_input.to_task_description() == task_description:
        return sources_from_task_input(task_input)
    return sources_from_text(task_description)


__all__ = [
    "MANUAL_DESCRIPTION_LABEL",
    "SOURCE_ATTACHMENT",
    "SOURCE_COMMENT",
    "SOURCE_DESCRIPTION",
    "SOURCE_TYPES",
    "TaskSource",
    "sources_for_task",
    "sources_from_task_input",
    "sources_from_text",
]
