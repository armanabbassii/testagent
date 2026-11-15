"""
task_analysis_diagnostics.py — اندازه‌گیریِ فقط-فراداده برای عیب‌یابیِ قدم اول

هدف: پیدا کردنِ این‌که کدام فراخوانیِ Task Analysis (یک‌مرحله‌ای، برنامه، دسته‌ی
گسترش) کند است یا با HTTP 504 شکست می‌خورد، و ورودی‌اش چقدر بزرگ است.

همه‌ی خروجی‌های این ماژول فقط عدد، نامِ مرحله، کدِ وضعیت و شناسه‌اند. هیچ متنی از
پرامپت، توصیفِ Jira، کامنت، پیوست، نامِ فایل، مقدارِ Postman، هدر یا پیامِ خطا
برگردانده نمی‌شود. اندازه‌ها به کاراکتر و بایتِ UTF-8 است؛ توکن شمرده نمی‌شود چون
پروژه tokenizer ندارد — تعدادِ واقعیِ توکن فقط از usageِ پاسخِ موفق می‌آید.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.agents.test_case_generator.postman_summary import PostmanSummary
    from src.task_sources.sources import TaskSource
    from src.task_sources.task_input import TaskInput

# نامِ مرحله‌ها در لاگ — همان سه فراخوانیِ TaskAnalysisGenerator.
DIAG_STAGE_DIRECT = "direct"
DIAG_STAGE_PLAN = "plan"
DIAG_STAGE_EXPAND = "expand"

OUTCOME_SUCCESS = "success"
OUTCOME_TRUNCATED = "truncated"
OUTCOME_OTHER = "other"


def text_size(text: str | None) -> tuple[int, int]:
    """(تعدادِ کاراکتر، تعدادِ بایتِ UTF-8) — برای متنِ فارسی این دو برابر نیستند."""
    value = text or ""
    return len(value), len(value.encode("utf-8"))


def request_sizes(system_prompt: str, user_message: str) -> dict[str, int]:
    """اندازه‌ی ورودیِ یک فراخوانی — بدونِ خودِ متن."""
    system_chars, system_bytes = text_size(system_prompt)
    user_chars, user_bytes = text_size(user_message)
    return {
        "system_chars": system_chars,
        "system_bytes": system_bytes,
        "user_chars": user_chars,
        "user_bytes": user_bytes,
        "input_chars": system_chars + user_chars,
        "input_bytes": system_bytes + user_bytes,
    }


def stage_label(stage: str, batch: int | None = None, batches: int | None = None) -> str:
    """برچسبِ کوتاهِ مرحله برای لاگِ هر تلاشِ HTTP: «direct»، «plan»، «expand 2/3»."""
    if batch is None:
        return stage
    return f"{stage} {batch}/{batches}"


def outcome_of(finish_reason: object) -> str:
    """success برای «stop»، truncated برای «length»، و other برای هر چیزِ دیگر."""
    if finish_reason == "length":
        return OUTCOME_TRUNCATED
    if finish_reason == "stop":
        return OUTCOME_SUCCESS
    return OUTCOME_OTHER


def response_metadata(raw: object) -> dict[str, Any]:
    """فراداده‌ی امنِ LLMResponse؛ برای رشته‌ی ساده (مثلاً ماکِ تست) مقدارها None‌اند."""
    finish_reason = getattr(raw, "finish_reason", None) or None
    return {
        "outcome": outcome_of(finish_reason),
        "finish_reason": finish_reason,
        "prompt_tokens": getattr(raw, "prompt_tokens", None),
        "completion_tokens": getattr(raw, "completion_tokens", None),
        "total_tokens": getattr(raw, "total_tokens", None),
        "response_id": getattr(raw, "response_id", None) or None,
        "response_chars": len(raw) if isinstance(raw, str) else None,
    }


def error_metadata(exc: BaseException) -> dict[str, Any]:
    """نوعِ خطا، کدِ HTTP و x-request-id — هرگز پیام، بدنه یا هدرهای دیگر.

    پیامِ خطای سرویس ممکن است بخشی از بدنه‌ی پاسخ را داشته باشد، پس عمداً ثبت
    نمی‌شود.
    """
    status = getattr(exc, "status_code", None)
    request_id = None
    try:
        request = getattr(exc, "request", None)
        if request is not None:
            request_id = request.headers.get("x-request-id") or None
    except Exception:  # noqa: BLE001 — فراداده‌ی اختیاری؛ نباید خطای اصلی را بپوشاند
        request_id = None
    return {
        "error_type": type(exc).__name__,
        "http_status": status if isinstance(status, int) else None,
        "request_id": request_id,
    }


def input_composition(
    sources: tuple[TaskSource, ...] | None,
    task_description: str,
    developed_services: str,
    postman: PostmanSummary,
) -> dict[str, Any]:
    """ترکیبِ ورودیِ قدم اول از منابع و خلاصه‌ی Postman — فقط شمارش و اندازه.

    ``sources`` همان منابعی است که generate می‌گیرد؛ None یعنی متنِ دستی.
    """
    # import محلی: task_sources در زمانِ import این ماژول لازم نیست.
    from src.task_sources.sources import SOURCE_ATTACHMENT, SOURCE_COMMENT, SOURCE_DESCRIPTION

    items = tuple(sources or ())
    description = [s for s in items if s.source_type == SOURCE_DESCRIPTION]
    comments = [s for s in items if s.source_type == SOURCE_COMMENT]
    attachments = [s for s in items if s.source_type == SOURCE_ATTACHMENT]

    task_chars, task_bytes = text_size(task_description)
    services_chars, services_bytes = text_size(developed_services)
    description_chars, description_bytes = text_size(
        "".join(s.text for s in description) if description else task_description
    )
    summary_chars, summary_bytes = text_size(postman.text)
    return {
        "input_mode": "structured" if (comments or attachments) else "text",
        "task_chars": task_chars,
        "task_bytes": task_bytes,
        "services_chars": services_chars,
        "services_bytes": services_bytes,
        "description_chars": description_chars,
        "description_bytes": description_bytes,
        "comments": len(comments),
        "comments_chars": sum(text_size(s.text)[0] for s in comments),
        "comments_bytes": sum(text_size(s.text)[1] for s in comments),
        "attachments_in_prompt": len(attachments),
        "attachment_chars": [text_size(s.text)[0] for s in attachments],
        "attachment_bytes": [text_size(s.text)[1] for s in attachments],
        "postman_collections": len(postman.collections),
        "postman_requests_extracted": postman.extracted,
        "postman_requests_listed": postman.included,
        "postman_summary_chars": summary_chars,
        "postman_summary_bytes": summary_bytes,
    }


def task_input_composition(task: TaskInput) -> dict[str, Any]:
    """ترکیبِ یک TaskInput (مثلاً issueِ Jira) — بدونِ نامِ فایل، متن یا هویت.

    برای هر پیوست به همان ترتیبِ issue فقط وضعیت، اندازه‌ی فایل، اندازه‌ی متنِ
    واردشده به پرامپت، بریده‌شدن و تعدادِ درخواست‌های Postman ثبت می‌شود.
    """
    description_chars, description_bytes = text_size(task.description.strip())
    task_chars, task_bytes = text_size(task.to_task_description())
    attachments = []
    for item in task.attachments:
        text_chars, text_bytes = text_size(item.content)
        attachments.append(
            {
                "status": item.status,
                "file_bytes": item.size,
                "text_chars": text_chars,
                "text_bytes": text_bytes,
                "truncated": item.truncated,
                "postman_requests": len(item.postman.requests) if item.postman else 0,
            }
        )
    collections = [item.postman for item in task.attachments if item.postman is not None]
    return {
        "task_chars": task_chars,
        "task_bytes": task_bytes,
        "description_chars": description_chars,
        "description_bytes": description_bytes,
        "comments": len(task.comments),
        "comments_chars": sum(text_size(c.body.strip())[0] for c in task.comments),
        "comments_bytes": sum(text_size(c.body.strip())[1] for c in task.comments),
        "attachments": len(task.attachments),
        "attachments_processed": len(task.processed_attachments()),
        "attachments_truncated": sum(1 for item in task.attachments if item.truncated),
        "attachment_details": attachments,
        "postman_collections": len(collections),
        "postman_requests": sum(len(c.requests) for c in collections),
    }


__all__ = [
    "OUTCOME_OTHER",
    "OUTCOME_SUCCESS",
    "OUTCOME_TRUNCATED",
    "DIAG_STAGE_DIRECT",
    "DIAG_STAGE_EXPAND",
    "DIAG_STAGE_PLAN",
    "error_metadata",
    "input_composition",
    "outcome_of",
    "request_sizes",
    "response_metadata",
    "stage_label",
    "task_input_composition",
    "text_size",
]
