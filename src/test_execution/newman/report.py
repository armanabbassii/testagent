"""
report.py — نتیجه‌ی هر درخواست از رخدادهای runner.js، و خواندنِ گزارشِ JSONِ Newman

نتیجه‌ی درخواست‌ها از فایلِ رخداد (NDJSON) ساخته می‌شود — همان منبع برای اجرای
کامل و برای اجرای قطع‌شده با timeout، پس درخواست‌هایی که پیش از قطع اجرا شده‌اند
از دست نمی‌روند. گزارشِ JSONِ Newman برای سنجشِ سازگاری (تعدادِ درخواست‌ها و
assertionها) و run.error خوانده می‌شود.

همه‌ی متن‌ها (URL، پیامِ assertion، پیامِ خطا) پیش از ذخیره redact می‌شوند.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.test_execution.newman.models import (
    SKIP_FAILED_PRECONDITION,
    SKIP_UNEXPLAINED,
    AssertionResult,
    RequestResult,
    RunStats,
)
from src.test_execution.newman.redaction import redact_url, scrub_text, url_secrets


class ReportError(RuntimeError):
    """فایلِ رخداد یا گزارش وجود ندارد یا خراب است — پیام هیچ مقدارِ خامی ندارد."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class Events:
    records: list[dict] = field(default_factory=list)
    done: dict | None = None


def read_events(path: Path) -> Events:
    """خطِ آخرِ ناقص (پروسه وسطِ نوشتن کشته شده) نادیده گرفته می‌شود؛ خرابیِ دیگر خطاست."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReportError("missing", "The Newman event log was not written.") from exc
    events = Events()
    for number, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            if number == len(lines) - 1:
                break
            raise ReportError(
                "malformed", f"The Newman event log is malformed at line {number + 1}."
            ) from exc
        if not isinstance(record, dict):
            raise ReportError(
                "malformed", f"The Newman event log is malformed at line {number + 1}."
            )
        events.records.append(record)
        if record.get("type") == "done":
            events.done = record
    return events


def event_secrets(events: Events) -> set[str]:
    """مقادیرِ حساسی که فقط در URLهای resolve‌شده دیده می‌شوند (query parameterهای حساس)."""
    secrets: set[str] = set()
    for record in events.records:
        if record.get("type") == "request" and isinstance(record.get("url"), str):
            secrets |= url_secrets(record["url"])
    return secrets


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _error_text(error: Any, secrets: set[str]) -> str:
    if not isinstance(error, dict):
        return ""
    code, message = _text(error.get("code")), _text(error.get("message"))
    text = (
        f"{code}: {message}"
        if code and code not in message
        else message or code or _text(error.get("name"))
    )
    return scrub_text(text, secrets)


def build_requests(events: Events, secrets: set[str]) -> list[RequestResult]:
    """یک نتیجه برای هر itemِ شروع‌شده، به ترتیبِ اجرا."""
    by_index: dict[int, RequestResult] = {}

    def result_for(record: dict) -> RequestResult:
        index = record.get("index")
        index = index if isinstance(index, int) else len(by_index)
        if index not in by_index:
            by_index[index] = RequestResult(
                index=index, name=scrub_text(_text(record.get("item_name")), secrets)
            )
        return by_index[index]

    for record in events.records:
        kind = record.get("type")
        if kind == "item_start":
            result_for(record)
        elif kind == "request":
            result = result_for(record)
            result.executed = True
            result.method = _text(record.get("method"))
            result.url = redact_url(_text(record.get("url")), secrets)
            code = record.get("status_code")
            result.status_code = code if isinstance(code, int) else None
            result.status_text = _text(record.get("status_text"))
            elapsed = record.get("response_time_ms")
            result.response_time_ms = elapsed if isinstance(elapsed, int) else None
            if record.get("error"):
                result.errors.append(
                    {"kind": "network", "message": _error_text(record["error"], secrets)}
                )
        elif kind == "assertion":
            error = record.get("error")
            skipped = bool(record.get("skipped"))
            result_for(record).assertions.append(
                AssertionResult(
                    name=scrub_text(_text(record.get("assertion")), secrets),
                    passed=not error and not skipped,
                    skipped=skipped,
                    message=_error_text(error, secrets),
                )
            )
        elif kind == "script_error":
            result_for(record).errors.append(
                {
                    "kind": "script",
                    "event": _text(record.get("event")),
                    "message": _error_text(record.get("error"), secrets),
                }
            )
        elif kind == "item_done":
            result_for(record).completed = True

    for result in by_index.values():
        if result.completed and not result.executed:
            # درخواست فرستاده نشد — معمولاً guardِ وابستگی (pm.execution.skipRequest).
            failed = [
                item.message for item in result.assertions if not item.passed and not item.skipped
            ]
            result.skipped = True
            if failed:
                result.skip_kind, result.skip_reason = SKIP_FAILED_PRECONDITION, failed[0]
            else:
                # هیچ بررسیِ شکست‌خورده‌ای دلیلِ نفرستادن را نمی‌گوید — نتیجه قابلِ اتکا نیست.
                result.skip_kind = SKIP_UNEXPLAINED
                result.skip_reason = "The request was not sent and no failed check explains why."
    return [by_index[index] for index in sorted(by_index)]


def stats_of(requests: list[RequestResult]) -> RunStats:
    assertions = [item for result in requests for item in result.assertions if not item.skipped]
    return RunStats(
        items_total=len(requests),
        requests_executed=sum(1 for result in requests if result.executed),
        requests_skipped=sum(1 for result in requests if result.skipped),
        unexplained_skips=sum(1 for result in requests if result.skip_kind == SKIP_UNEXPLAINED),
        network_errors=sum(
            1 for result in requests for error in result.errors if error["kind"] == "network"
        ),
        script_errors=sum(
            1 for result in requests for error in result.errors if error["kind"] == "script"
        ),
        assertions_total=len(assertions),
        assertions_failed=sum(1 for item in assertions if not item.passed),
    )


def read_summary(path: Path) -> dict:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError("missing", "Newman did not write its JSON report.") from exc
    try:
        summary = json.loads(raw)
    except ValueError as exc:
        raise ReportError("malformed", "The Newman JSON report is not valid JSON.") from exc
    if not isinstance(summary, dict) or not isinstance(summary.get("run"), dict):
        raise ReportError("malformed", "The Newman JSON report has no 'run' section.")
    if not isinstance(summary["run"].get("stats"), dict) or not isinstance(
        summary["run"].get("executions"), list
    ):
        raise ReportError(
            "malformed", "The Newman JSON report has no run statistics or executions."
        )
    return summary


def inconsistency(summary: dict, stats: RunStats) -> str:
    """گزارشِ Newman و رخدادها باید یک چیز بگویند — وگرنه هیچ‌کدام قابلِ اتکا نیست."""
    run_stats = summary["run"]["stats"]

    def count(section: str, key: str) -> int | None:
        value = (run_stats.get(section) or {}).get(key)
        return value if isinstance(value, int) else None

    checks = (
        ("requests", count("requests", "total"), stats.requests_executed),
        ("assertions", count("assertions", "total"), stats.assertions_total),
        ("failed assertions", count("assertions", "failed"), stats.assertions_failed),
    )
    for label, reported, observed in checks:
        if reported is None or reported != observed:
            return f"The Newman report counts {reported} {label} but {observed} were observed during the run."
    return ""


__all__ = [
    "Events",
    "ReportError",
    "build_requests",
    "event_secrets",
    "inconsistency",
    "read_events",
    "read_summary",
    "stats_of",
]
