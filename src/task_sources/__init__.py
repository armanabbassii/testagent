"""
src/task_sources — منبع‌های بیرونیِ توصیفِ تسک (فعلاً Jira)

هر منبع داده‌اش را به یک TaskInputِ مستقل از منبع تبدیل می‌کند و همان به متنِ
ساختاریافته‌ی «Task Description»ِ قدمِ اول تبدیل می‌شود؛ جریانِ پنج‌قدمی هیچ
وابستگی‌ای به Jira ندارد.
"""

from src.task_sources.task_input import (
    ATTACHMENT_FAILED,
    ATTACHMENT_PROCESSED,
    ATTACHMENT_TOO_LARGE,
    ATTACHMENT_UNSUPPORTED,
    TaskAttachment,
    TaskComment,
    TaskInput,
)

__all__ = [
    "ATTACHMENT_FAILED",
    "ATTACHMENT_PROCESSED",
    "ATTACHMENT_TOO_LARGE",
    "ATTACHMENT_UNSUPPORTED",
    "TaskAttachment",
    "TaskComment",
    "TaskInput",
]
