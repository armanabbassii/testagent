"""
classify.py — PASS / FAIL / ERROR از یک اجرای Newman

کدِ خروج به‌تنهایی تصمیم نمی‌گیرد (Newman برای شکستِ assertion و خطای فنی هر دو
۱ برمی‌گرداند)؛ تصمیم از رخدادها و گزارشِ JSON است و کدِ خروج فقط سنجیده می‌شود.
ترتیب مهم است — اولین شرطِ برقرار تعیین‌کننده است:

  ERROR  پروسه شروع نشد · timeout · runner/Newman خطا داد · run.error ·
         گزارش/رخدادها غایب یا خراب یا ناسازگار · خطای شبکه (بدونِ پاسخ) ·
         خطای اجرای اسکریپت
  FAIL   دست‌کم یک assertion شکست خورد (و هیچ‌کدام از بالا نیست)
  PASS   در غیرِ این صورت؛ بدونِ هیچ assertion → no_assertions=True

کدِ وضعیتِ HTTP (401، 403، 500، …) هیچ نقشی ندارد؛ فقط assertionهای کالکشن.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.test_execution.newman.models import (
    ERROR_NETWORK,
    ERROR_NEWMAN_RUNTIME,
    ERROR_PROCESS_CANCELLED,
    ERROR_PROCESS_FAILED,
    ERROR_PROCESS_START_FAILED,
    ERROR_PROCESS_TIMEOUT,
    ERROR_REPORT_INCONSISTENT,
    ERROR_REPORT_MALFORMED,
    ERROR_REPORT_MISSING,
    ERROR_REQUEST_NOT_EXECUTED,
    ERROR_SCRIPT,
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
    RequestResult,
    RunStats,
)
from src.test_execution.newman.process import ProcessOutcome

# کدهای خروجِ runner.js
EXIT_OK, EXIT_FAILURES, EXIT_NEWMAN_ERROR, EXIT_RUN_ERROR, EXIT_WRAPPER = 0, 1, 2, 3, 4


@dataclass(frozen=True)
class Verdict:
    status: str
    error_kind: str = ""
    message: str = ""
    no_assertions: bool = False


def _names(requests: list[RequestResult], kind: str) -> str:
    names = [
        result.name for result in requests if any(error["kind"] == kind for error in result.errors)
    ]
    return ", ".join(repr(name) for name in names[:5]) + (" …" if len(names) > 5 else "")


def classify(
    outcome: ProcessOutcome,
    *,
    done: dict | None,
    events_error: tuple[str, str] | None,
    summary_error: tuple[str, str] | None,
    inconsistency: str,
    requests: list[RequestResult],
    stats: RunStats,
    bail: bool = False,
) -> Verdict:
    """``events_error`` / ``summary_error``: (kind, message) با kind ∈ {missing, malformed}."""
    if outcome.cancelled:
        # پیش از هر قاعده‌ی دیگر: گزارشِ ناقص یا غایبِ یک اجرای لغوشده «خراب» نیست.
        return Verdict(
            STATUS_ERROR,
            ERROR_PROCESS_CANCELLED,
            "The Newman run was cancelled"
            + (
                f" after {stats.requests_executed} request(s) had run."
                if outcome.started
                else " before it started."
            ),
        )
    if not outcome.started:
        return Verdict(
            STATUS_ERROR,
            ERROR_PROCESS_START_FAILED,
            outcome.error or "Newman could not be started.",
        )
    if outcome.timed_out:
        return Verdict(
            STATUS_ERROR,
            ERROR_PROCESS_TIMEOUT,
            f"Newman did not finish within the process timeout and was terminated; "
            f"{stats.requests_executed} request(s) had run.",
        )
    if outcome.exit_code not in (EXIT_OK, EXIT_FAILURES, EXIT_NEWMAN_ERROR, EXIT_RUN_ERROR):
        return Verdict(
            STATUS_ERROR,
            ERROR_PROCESS_FAILED,
            f"The Newman process exited with code {outcome.exit_code}.",
        )
    if events_error is not None:
        kind, message = events_error
        return Verdict(
            STATUS_ERROR,
            ERROR_REPORT_MISSING if kind == "missing" else ERROR_REPORT_MALFORMED,
            message,
        )
    if done is None:
        return Verdict(
            STATUS_ERROR, ERROR_PROCESS_FAILED, "Newman stopped before it finished the run."
        )
    for key in ("error", "run_error"):
        error = done.get(key)
        if isinstance(error, dict):
            name = str(error.get("name") or "Error")
            return Verdict(
                STATUS_ERROR, ERROR_NEWMAN_RUNTIME, f"Newman reported a runtime error ({name})."
            )
    if summary_error is not None:
        kind, message = summary_error
        return Verdict(
            STATUS_ERROR,
            ERROR_REPORT_MISSING if kind == "missing" else ERROR_REPORT_MALFORMED,
            message,
        )
    if inconsistency:
        return Verdict(STATUS_ERROR, ERROR_REPORT_INCONSISTENT, inconsistency)
    failures = done.get("failures") if isinstance(done.get("failures"), int) else -1
    if (outcome.exit_code == EXIT_FAILURES) != (failures > 0):
        return Verdict(
            STATUS_ERROR,
            ERROR_REPORT_INCONSISTENT,
            f"Newman exited with code {outcome.exit_code} but reported {failures} failure(s).",
        )
    if stats.network_errors:
        return Verdict(
            STATUS_ERROR,
            ERROR_NETWORK,
            f"{stats.network_errors} request(s) received no response ({_names(requests, 'network')}); "
            f"the run cannot be trusted. {stats.assertions_failed} assertion failure(s) are preserved.",
        )
    if stats.script_errors:
        return Verdict(
            STATUS_ERROR,
            ERROR_SCRIPT,
            f"{stats.script_errors} script error(s) stopped the run from being trustworthy "
            f"({_names(requests, 'script')}).",
        )
    if stats.unexplained_skips:
        names = ", ".join(repr(r.name) for r in requests if r.skip_kind == "unexplained")
        return Verdict(
            STATUS_ERROR,
            ERROR_REQUEST_NOT_EXECUTED,
            f"{stats.unexplained_skips} request(s) were skipped without a failed check that "
            f"explains why ({names}); the run cannot be trusted.",
        )
    if stats.items_not_started and not (bail and stats.assertions_failed):
        return Verdict(
            STATUS_ERROR,
            ERROR_REQUEST_NOT_EXECUTED,
            f"{stats.items_not_started} request(s) of the collection never started.",
        )
    if stats.assertions_failed:
        return Verdict(STATUS_FAIL, message=f"{stats.assertions_failed} assertion(s) failed.")
    return Verdict(STATUS_PASS, no_assertions=stats.assertions_total == 0)


__all__ = ["EXIT_FAILURES", "EXIT_OK", "Verdict", "classify"]
