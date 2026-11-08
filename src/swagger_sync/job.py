"""
src/swagger_sync/job.py — موتورِ همگام‌سازیِ snapshotهای Swagger

    report = SyncJob(load_sync_settings()).run(MODE_RUN)

سه حالت:
  * MODE_RUN     — برای هر منبع: fetch_validated → write_snapshot زیرِ قفلِ نوشتنِ مشترک (محافظِ
                   کاهش، «بدونِ تغییر» = بدونِ بازنویسی، جایگزینیِ اتمی).
  * MODE_CHECK   — همان GET و اعتبارسنجی و decide_update؛ هیچ snapshotی نوشته نمی‌شود.
                   probe_https: نسخه‌ی https ِ هر نشانیِ http یک بار (بی retry) بررسی می‌شود.
  * MODE_DRY_RUN — بدونِ شبکه و بدونِ هیچ نوشتنی: تنظیمات، منابع، فایل‌های موجود، آستانه‌ها و
                   برنامه‌ی retry گزارش می‌شوند.

Retry تا موفقیت (RUN و CHECK):
  * هر منبع زمان‌بندیِ جدای خودش را دارد. ابتدا همه یک بار تلاش می‌شوند؛ منبعِ موفق دیگر
    درخواستی نمی‌گیرد و فقط منابعِ ناموفق، هر کدام سرِ موعدِ خودش، دوباره تلاش می‌شوند.
  * هر خطای دریافت/پردازش retry می‌شود: HTTP (4xx/5xx/429)، اتصال، timeout، پاسخِ خالی، JSON یا
    OpenAPI ِ نامعتبر، محافظِ کاهش، قفلِ مشغول، دسترسی/نوشتن، و حتی خطای پیش‌بینی‌نشده. فقط خطای
    پیکربندی (kind=configuration) fail-closed است.
  * فاصله: interval × multiplier^(n-1) با سقفِ max_retry_interval (کف ۱ ثانیه)؛ Retry-After ِ
    عددی رعایت می‌شود ولی از همان سقف بیشتر نمی‌شود.
  * تعدادِ تلاش و زمانِ کل به‌طورِ پیش‌فرض نامحدودند (SWAGGER_SYNC_MAX_ATTEMPTS /
    SWAGGER_SYNC_MAX_RUNTIME_MINUTES = 0). اگر سقفی تنظیم شده باشد، منبعِ ناتمام FAILED است و
    اجرا هرگز SUCCESS نمی‌شود.
  * قفلِ اجرا (OS lock) در تمامِ مدت نگه داشته می‌شود؛ اجرای دوم BUSY است. شکستِ هیچ منبعی
    فایلِ قبلی را خراب نمی‌کند؛ پس از crash اجرای بعدی از اول و امن شروع می‌شود (فایل‌های موقتِ
    جامانده پاک می‌شوند).
  * فقط فایل‌های <key>.json ِ منابعِ ثبت‌شده؛ نامِ فایل از رجیستری می‌آید، نه از پاسخ.

گزارش (callback ِ progress پس از هر تلاش + گزارشِ نهایی): برای هر منبع وضعیت، تاریخچه‌ی
تلاش‌ها (شماره، زمان، مدت، کدِ HTTP، نوعِ خطا، فاصله تا retry)، اندازه و تعدادِ operation ِ قبلی و
جدید، اعتبارسنجی، محافظِ کاهش، مقایسه و نتیجه‌ی نوشتن. هیچ بدنه‌ی پاسخ یا header ثبت نمی‌شود.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from src.agents.test_case_generator import swagger_snapshots as snaps
from src.agents.test_case_generator.swagger_locks import SwaggerLockUnavailable, try_run_lock
from src.swagger_settings import (
    SWAGGER_API_DOCS_BASE_URL,
    SwaggerSettingsError,
    SyncSettings,
    max_shrink_ratio,
)

MODE_RUN = "run"
MODE_CHECK = "check"
MODE_DRY_RUN = "dry-run"

OUTCOME_UPDATED = "UPDATED"
OUTCOME_UNCHANGED = "UNCHANGED"
OUTCOME_WOULD_UPDATE = "WOULD_UPDATE"  # فقط --check
OUTCOME_PENDING = "PENDING_RETRY"  # هنوز موفق نشده؛ در انتظارِ تلاشِ بعدی
OUTCOME_FAILED = "FAILED"  # فقط با خطای پیکربندی یا سقفِ تنظیم‌شده
OUTCOME_SKIPPED_DEADLINE = "SKIPPED_DEADLINE"
OUTCOME_INTERRUPTED = "INTERRUPTED"
OUTCOME_PLANNED = "PLANNED"  # فقط --dry-run
SUCCESS_OUTCOMES = frozenset(
    {OUTCOME_UPDATED, OUTCOME_UNCHANGED, OUTCOME_WOULD_UPDATE, OUTCOME_PLANNED}
)

RESULT_SUCCESS = "SUCCESS"
RESULT_PARTIAL = "PARTIAL_FAILURE"
RESULT_FAILED = "FAILED"
RESULT_DISABLED = "DISABLED"
RESULT_BUSY = "ALREADY_RUNNING"
RESULT_CONFIG_ERROR = "CONFIGURATION_ERROR"
RESULT_IN_PROGRESS = "IN_PROGRESS"
RESULT_INTERRUPTED = "INTERRUPTED"
RESULT_NOT_DUE = "NOT_DUE"

EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_FAILED = 2
EXIT_CONFIG = 3
EXIT_BUSY = 4
EXIT_INTERRUPTED = 5

# فقط این‌ها retry نمی‌شوند — بقیه‌ی خطاهای دریافت و پردازش تا موفقیت retry می‌شوند
NON_RETRYABLE_KINDS = frozenset({snaps.KIND_CONFIGURATION})
# خطاهایی که پیش از رسیدن به اعتبارسنجی رخ می‌دهند / خودِ اعتبارسنجی‌اند
FETCH_KINDS = frozenset(
    {
        snaps.KIND_CONNECT_TIMEOUT,
        snaps.KIND_READ_TIMEOUT,
        snaps.KIND_CONNECTION,
        snaps.KIND_REQUEST,
        snaps.KIND_HTTP_STATUS,
    }
)
VALIDATION_KINDS = frozenset(
    {
        snaps.KIND_EMPTY,
        snaps.KIND_INVALID_JSON,
        snaps.KIND_NOT_OPENAPI,
        snaps.KIND_UNUSABLE,
        snaps.KIND_NO_OPERATIONS,
    }
)
KIND_UNEXPECTED = "unexpected"
KIND_DEADLINE = "deadline"

MAX_ATTEMPT_HISTORY = 50  # تاریخچه‌ی هر منبع در گزارش؛ قدیمی‌ترها فقط شمرده می‌شوند
SCHEDULE_PREVIEW = 8  # چند فاصله‌ی اول در خلاصه‌ی تنظیمات

# وضعیت‌های گزارش
VALIDATION_PASSED, VALIDATION_FAILED, VALIDATION_NOT_REACHED = "passed", "failed", "not_reached"
SHRINK_PASSED, SHRINK_BLOCKED, SHRINK_DISABLED, SHRINK_NOT_APPLICABLE = (
    "passed",
    "blocked",
    "disabled",
    "not_applicable",
)
COMPARE_IDENTICAL, COMPARE_DIFFERENT, COMPARE_NO_PREVIOUS = (
    "identical",
    "different",
    "no_previous_file",
)
UPDATE_REPLACED, UPDATE_NOT_WRITTEN, UPDATE_WOULD_REPLACE, UPDATE_NOT_NEEDED = (
    "replaced",
    "not_written_identical",
    "would_replace",
    "not_needed_identical",
)


def is_retryable(error: snaps.SwaggerRefreshError) -> bool:
    return error.kind not in NON_RETRYABLE_KINDS


def https_variant(url: str) -> str | None:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme="https")) if parts.scheme == "http" else None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _utc_after(seconds: float) -> str:
    moment = datetime.now(UTC) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


# ── «آیا اجرا لازم است؟» (run --if-due ِ Task Scheduler) ─────────────────────
def last_boundary(daily_time: str, now: datetime) -> datetime:
    """آخرین وقوعِ ساعتِ روزانه که از now گذشته است (یا برابرِ آن است)."""
    hour, minute = (int(part) for part in daily_time.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate <= now else candidate - timedelta(days=1)


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def due_reason(last: dict | None, daily_time: str, now: datetime) -> str | None:
    """None یعنی لازم نیست (پس از آخرین زمانِ برنامه‌ریزی‌شده یک اجرای موفق ثبت شده)؛ در غیرِ این
    صورت دلیلِ لازم بودن. now باید aware (زمانِ محلی با منطقه) باشد."""
    if not last:
        return "no previous run report"
    if last.get("result") != RESULT_SUCCESS:
        return f"the last run ended {last.get('result') or 'without a result'}"
    finished = _parse_utc(last.get("finished_at"))
    if finished is None:
        return "the last run report has no valid finish time"
    boundary = last_boundary(daily_time, now)
    if finished < boundary:
        return f"no successful run since {boundary:%Y-%m-%d %H:%M}"
    return None


@dataclass
class AttemptRecord:
    number: int
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    http_status: int | None = None
    error_kind: str = ""
    error: str = ""
    retry_in_s: float | None = None


@dataclass
class SourceReport:
    key: str
    label: str
    url: str
    file: str
    outcome: str = ""
    attempts: int = 0
    started_at: str = ""
    finished_at: str = ""
    http_status: int | None = None
    error_kind: str = ""
    error: str = ""
    duration_s: float = 0.0
    size_bytes: int | None = None
    previous_size_bytes: int | None = None
    operations: int | None = None
    previous_operations: int | None = None
    shrink_ratio: float | None = None
    sha256_prefix: str = ""
    validation: str = ""
    shrink_guard: str = ""
    comparison: str = ""
    update: str = ""
    next_retry_at: str = ""
    https_probe: dict[str, Any] | None = None
    attempt_history: list[AttemptRecord] = field(default_factory=list)
    attempts_omitted: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    mode: str
    started_at: str
    finished_at: str = ""
    result: str = ""
    exit_code: int = EXIT_SUCCESS
    base_url: str = SWAGGER_API_DOCS_BASE_URL
    warnings: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    succeeded: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    cleaned_temp_files: list[str] = field(default_factory=list)
    sources: list[SourceReport] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SyncJob:
    def __init__(
        self,
        settings: SyncSettings,
        *,
        directory: Path | str | None = None,
        session_factory: Callable[[], Any] = requests.Session,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], str] = _utc_now,
        logger: logging.Logger | None = None,
        progress: Callable[[RunReport], None] | None = None,
    ) -> None:
        self.settings = settings
        self.directory = Path(directory) if directory is not None else snaps.SNAPSHOT_DIR
        self._session_factory = session_factory
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._progress = progress
        self.log = logger or logging.getLogger("swagger_sync")

    # ── منابع ──────────────────────────────────────────────────────────────
    def sources(self) -> list[snaps.SwaggerSource]:
        """منابعِ این اجرا به ترتیبِ رجیستری؛ کلیدِ ناشناخته/غیرفعال خطای پیکربندی است."""
        if not self.settings.sources:
            return list(snaps.enabled_sources())
        chosen = []
        for key in self.settings.sources:
            try:
                source = snaps.source_for(key)
            except snaps.SwaggerSnapshotError:
                raise SwaggerSettingsError(
                    "SWAGGER_SYNC_SOURCES names an unknown Swagger source; valid keys: "
                    + ", ".join(s.key for s in snaps.SWAGGER_SOURCES)
                    + "."
                ) from None
            if not source.enabled:
                raise SwaggerSettingsError(
                    f"SWAGGER_SYNC_SOURCES names the disabled source {source.key!r}."
                )
            chosen.append(source)
        order = {s.key: n for n, s in enumerate(snaps.SWAGGER_SOURCES)}
        return sorted({s.key: s for s in chosen}.values(), key=lambda s: order[s.key])

    def _settings_summary(self) -> dict[str, Any]:
        s = self.settings
        steps = SCHEDULE_PREVIEW if s.unlimited_attempts else max(0, s.max_attempts - 1)
        return {
            "enabled": s.enabled,
            "daily_time": s.daily_time,
            "sources": list(s.sources) or "all enabled",
            "max_attempts": "unlimited" if s.unlimited_attempts else s.max_attempts,
            "retry_interval_s": s.retry_interval_s,
            "backoff_multiplier": s.backoff_multiplier,
            "max_retry_interval_s": s.max_retry_interval_s,
            "max_runtime_s": "unlimited" if s.unlimited_runtime else s.max_runtime_s,
            "connect_timeout_s": s.connect_timeout_s,
            "read_timeout_s": s.read_timeout_s,
            "retry_schedule_s": [s.retry_delay(n) for n in range(1, steps + 1)],
        }

    def _limit_warnings(self) -> list[str]:
        s, warnings = self.settings, []
        if not s.unlimited_attempts:
            warnings.append(
                f"Retries are capped by SWAGGER_SYNC_MAX_ATTEMPTS={s.max_attempts}; a source "
                "that keeps failing ends FAILED instead of being retried until it succeeds."
            )
        if not s.unlimited_runtime:
            warnings.append(
                f"The run is capped by SWAGGER_SYNC_MAX_RUNTIME_MINUTES={s.max_runtime_s / 60:g}; "
                "sources still failing at that point end FAILED."
            )
        return warnings

    # ── اجرا ───────────────────────────────────────────────────────────────
    def run(self, mode: str = MODE_RUN, *, probe_https: bool = False) -> RunReport:
        if mode not in (MODE_RUN, MODE_CHECK, MODE_DRY_RUN):
            raise ValueError(f"Unknown mode {mode!r}.")
        if probe_https and mode != MODE_CHECK:
            raise ValueError("The HTTPS probe is available only with --check.")
        report = RunReport(mode=mode, started_at=self._now(), settings=self._settings_summary())
        self.log.info("Swagger sync started (mode=%s).", mode)
        if urlsplit(SWAGGER_API_DOCS_BASE_URL).scheme == "http":
            report.warnings.append(
                "The Swagger base URL uses http://; documents are not protected in transit."
            )
        report.warnings.extend(self._limit_warnings())
        try:
            sources = self.sources()
            ratios = {source.key: max_shrink_ratio(source.key) for source in sources}
        except SwaggerSettingsError as exc:
            return self._finish(report, RESULT_CONFIG_ERROR, EXIT_CONFIG, str(exc))
        if mode == MODE_DRY_RUN:
            for source in sources:
                report.sources.append(self._plan(source, ratios[source.key]))
            self._update_lists(report)
            return self._finish(report, RESULT_SUCCESS, EXIT_SUCCESS)
        if mode == MODE_RUN and not self.settings.enabled:
            return self._finish(
                report, RESULT_DISABLED, EXIT_SUCCESS, "SWAGGER_SYNC_ENABLED is false."
            )
        try:
            run_lock = try_run_lock(self.directory)
        except SwaggerLockUnavailable:
            return self._finish(
                report, RESULT_FAILED, EXIT_FAILED, "The run lock could not be confirmed."
            )
        if run_lock is None:
            return self._finish(
                report, RESULT_BUSY, EXIT_BUSY, "Another Swagger sync is already running."
            )
        interrupted = False
        try:
            report.result = RESULT_IN_PROGRESS
            if mode == MODE_RUN:
                report.cleaned_temp_files = snaps.remove_stale_temp_files(self.directory)
                for name in report.cleaned_temp_files:
                    self.log.warning(
                        "Removed a temporary file left by an interrupted write: %s", name
                    )
            try:
                self._sync_all(sources, ratios, report, mode, probe_https)
            except KeyboardInterrupt:
                interrupted = True
        finally:
            run_lock.release()
        return self._conclude(report, interrupted)

    def _conclude(self, report: RunReport, interrupted: bool) -> RunReport:
        if interrupted:
            for item in report.sources:
                if item.outcome not in SUCCESS_OUTCOMES and item.outcome != OUTCOME_FAILED:
                    item.outcome, item.next_retry_at = OUTCOME_INTERRUPTED, ""
            self._update_lists(report)
            return self._finish(
                report,
                RESULT_INTERRUPTED,
                EXIT_INTERRUPTED,
                "The run was interrupted; files already written are complete and the next run "
                "starts again from the beginning.",
            )
        self._update_lists(report)
        if not report.failed and not report.pending:
            return self._finish(report, RESULT_SUCCESS, EXIT_SUCCESS)
        if any(s.error_kind == snaps.KIND_CONFIGURATION for s in report.sources):
            return self._finish(
                report,
                RESULT_CONFIG_ERROR,
                EXIT_CONFIG,
                "A source failed with a configuration error; fix the configuration and run again.",
            )
        if not report.succeeded:
            return self._finish(report, RESULT_FAILED, EXIT_FAILED)
        return self._finish(report, RESULT_PARTIAL, EXIT_PARTIAL)

    def _finish(self, report: RunReport, result: str, code: int, error: str = "") -> RunReport:
        report.result, report.exit_code, report.error = result, code, error
        report.finished_at = self._now()
        for warning in report.warnings:
            self.log.warning(warning)
        if error:
            self.log.error(error)
        if report.sources and result not in (RESULT_DISABLED, RESULT_BUSY):
            self.log.info(
                "Succeeded: %s; failed: %s.",
                ", ".join(report.succeeded) or "none",
                ", ".join(report.failed + report.pending) or "none",
            )
        self.log.info("Swagger sync finished: %s (exit %d).", result, code)
        return report

    def _update_lists(self, report: RunReport) -> None:
        report.succeeded = [s.key for s in report.sources if s.outcome in SUCCESS_OUTCOMES]
        report.pending = [s.key for s in report.sources if s.outcome == OUTCOME_PENDING]
        report.failed = [
            s.key
            for s in report.sources
            if s.outcome not in SUCCESS_OUTCOMES and s.outcome != OUTCOME_PENDING
        ]

    def _emit(self, report: RunReport) -> None:
        self._update_lists(report)
        if self._progress is None:
            return
        try:
            self._progress(report)
        except Exception as exc:  # noqa: BLE001 — گزارشِ میانی هرگز اجرا را متوقف نمی‌کند
            self.log.warning("The progress report could not be written (%s).", type(exc).__name__)

    def _sync_all(
        self,
        sources: list[snaps.SwaggerSource],
        ratios: dict[str, float],
        report: RunReport,
        mode: str,
        probe_https: bool,
    ) -> None:
        items: dict[str, SourceReport] = {}
        for source in sources:
            item = SourceReport(
                source.key,
                source.label,
                source.remote_url,
                source.filename,
                outcome=OUTCOME_PENDING,
                shrink_ratio=ratios[source.key],
            )
            report.sources.append(item)
            items[source.key] = item
        start = self._clock()
        deadline = None if self.settings.unlimited_runtime else start + self.settings.max_runtime_s
        next_due = {source.key: start for source in sources}
        pending = list(sources)
        self._emit(report)
        while pending:
            now = self._clock()
            if deadline is not None and now >= deadline:
                self._stop_at_deadline(pending, items)
                return
            due = [source for source in pending if next_due[source.key] <= now]
            if not due:
                wake = min(next_due[source.key] for source in pending)
                if deadline is not None and wake >= deadline:
                    self._stop_at_deadline(pending, items)
                    return
                self._sleep(max(0.0, wake - now))
                continue
            for source in due:
                if deadline is not None and self._clock() >= deadline:
                    break
                item = items[source.key]
                delay = self._attempt(source, item, mode)
                if delay is None:  # تمام شد (موفق یا شکستِ نهایی)
                    pending.remove(source)
                    if probe_https and item.outcome in SUCCESS_OUTCOMES:
                        item.https_probe = self._probe(source)
                else:
                    next_due[source.key] = self._clock() + delay
                self._emit(report)

    def _stop_at_deadline(
        self, pending: list[snaps.SwaggerSource], items: dict[str, SourceReport]
    ) -> None:
        for source in pending:
            item = items[source.key]
            item.next_retry_at = ""
            if item.attempts == 0:
                item.outcome = OUTCOME_SKIPPED_DEADLINE
                item.error_kind, item.error = KIND_DEADLINE, "The run deadline was reached."
            else:  # آخرین خطا حفظ می‌شود
                item.outcome = OUTCOME_FAILED
                item.notes.append("No further retry: the run deadline was reached.")
            item.finished_at = self._now()
            self._log_item(item)

    def _attempt(self, source: snaps.SwaggerSource, item: SourceReport, mode: str) -> float | None:
        """یک تلاش. None = منبع تمام شد (موفق یا شکستِ نهایی)؛ عدد = ثانیه تا تلاشِ بعدی."""
        settings = self.settings
        item.attempts += 1
        record = AttemptRecord(item.attempts, self._now())
        if not item.started_at:
            item.started_at = record.started_at
        began = self._clock()
        failure: snaps.SwaggerRefreshError | None = None
        try:
            with self._session_factory() as session:
                document = snaps.fetch_validated(
                    source.key,
                    session=session,
                    timeout=(settings.connect_timeout_s, settings.read_timeout_s),
                )
            record.http_status = item.http_status = document.status
            item.validation = VALIDATION_PASSED
            item.size_bytes = len(document.content)
            item.operations = len(document.service.apis)
            item.sha256_prefix = hashlib.sha256(document.content).hexdigest()[:12]
            item.previous_size_bytes = _file_size(
                snaps.snapshot_path(source.key, directory=self.directory)
            )
            if mode == MODE_RUN:
                result = snaps.write_snapshot(
                    source.key,
                    document.content,
                    document.service,
                    directory=self.directory,
                    shrink_ratio=item.shrink_ratio,
                )
                changed, item.previous_operations = result.changed, result.previous_operations
                item.outcome = OUTCOME_UPDATED if changed else OUTCOME_UNCHANGED
                item.update = UPDATE_REPLACED if changed else UPDATE_NOT_WRITTEN
            else:
                decision = snaps.decide_update(
                    source.key,
                    document.content,
                    document.service,
                    directory=self.directory,
                    shrink_ratio=item.shrink_ratio,
                )
                changed = decision.action == snaps.DECISION_UPDATE
                item.previous_operations = decision.previous_operations
                item.outcome = OUTCOME_WOULD_UPDATE if changed else OUTCOME_UNCHANGED
                item.update = UPDATE_WOULD_REPLACE if changed else UPDATE_NOT_NEEDED
            if not changed:
                item.comparison = COMPARE_IDENTICAL
            elif item.previous_size_bytes is None:
                item.comparison = COMPARE_NO_PREVIOUS
            else:
                item.comparison = COMPARE_DIFFERENT
            item.shrink_guard = self._shrink_state(item, changed)
        except snaps.SwaggerRefreshError as exc:
            failure = exc
            record.http_status = item.http_status = exc.status
            record.error_kind, record.error = exc.kind, exc.reason
        except Exception as exc:  # noqa: BLE001 — یک منبع هرگز کلِ اجرا را نمی‌کشد
            record.http_status = item.http_status = None
            record.error_kind = KIND_UNEXPECTED
            record.error = f"Unexpected error ({type(exc).__name__}); the existing file was kept."
        record.finished_at = self._now()
        record.duration_s = round(self._clock() - began, 3)
        item.duration_s = round(item.duration_s + record.duration_s, 3)
        self._remember(item, record)

        if not record.error_kind:  # موفق
            item.error_kind = item.error = item.next_retry_at = ""
            item.finished_at = record.finished_at
            self.log.info(
                "%s attempt %d succeeded in %.1fs: HTTP %s; validation passed; operations %s "
                "(previous %s); size %s bytes (previous %s); shrink guard %s; content %s; %s.",
                item.label,
                record.number,
                record.duration_s,
                record.http_status,
                item.operations,
                item.previous_operations,
                item.size_bytes,
                item.previous_size_bytes,
                item.shrink_guard,
                item.comparison,
                item.update,
            )
            self._log_item(item)
            return None

        item.error_kind, item.error = record.error_kind, record.error
        self._classify_failure(item, record.error_kind)
        final = (failure is not None and not is_retryable(failure)) or (
            not settings.unlimited_attempts and record.number >= settings.max_attempts
        )
        if final:
            item.outcome, item.next_retry_at = OUTCOME_FAILED, ""
            item.finished_at = record.finished_at
            self._log_attempt_failure(item, record)
            self._log_item(item)
            return None
        delay = settings.retry_delay(record.number)
        if failure is not None and failure.retry_after is not None:
            delay = min(max(delay, failure.retry_after), settings.max_retry_interval_s)
        record.retry_in_s = delay
        item.outcome = OUTCOME_PENDING
        item.next_retry_at = _utc_after(delay)
        self._log_attempt_failure(item, record)
        return delay

    @staticmethod
    def _shrink_state(item: SourceReport, changed: bool) -> str:
        if not changed or not item.previous_operations:
            return SHRINK_NOT_APPLICABLE
        if not item.shrink_ratio:
            return SHRINK_DISABLED
        return SHRINK_PASSED

    @staticmethod
    def _classify_failure(item: SourceReport, kind: str) -> None:
        item.update = ""
        if kind in FETCH_KINDS:
            item.validation, item.shrink_guard, item.comparison = VALIDATION_NOT_REACHED, "", ""
        elif kind in VALIDATION_KINDS:
            item.validation, item.shrink_guard, item.comparison = VALIDATION_FAILED, "", ""
        elif kind == snaps.KIND_SHRINK:
            item.validation, item.shrink_guard = VALIDATION_PASSED, SHRINK_BLOCKED
            item.comparison = COMPARE_DIFFERENT
        else:  # قفل، دسترسی، نوشتن، پیکربندی، پیش‌بینی‌نشده: سند پیش‌تر معتبر بود یا نامعلوم
            item.shrink_guard = item.comparison = ""
            if kind == KIND_UNEXPECTED:
                item.validation = ""

    @staticmethod
    def _remember(item: SourceReport, record: AttemptRecord) -> None:
        item.attempt_history.append(record)
        if len(item.attempt_history) > MAX_ATTEMPT_HISTORY:
            item.attempt_history.pop(0)
            item.attempts_omitted += 1

    def _log_attempt_failure(self, item: SourceReport, record: AttemptRecord) -> None:
        retry = (
            f"retry in {record.retry_in_s:g}s"
            if record.retry_in_s is not None
            else "no further retry"
        )
        self.log.warning(
            "%s attempt %d failed in %.1fs: HTTP %s, %s — %s; %s.",
            item.label,
            record.number,
            record.duration_s,
            record.http_status if record.http_status is not None else "-",
            record.error_kind,
            record.error,
            retry,
        )

    def _plan(self, source: snaps.SwaggerSource, ratio: float) -> SourceReport:
        """--dry-run: فقط خواندنِ فایلِ محلی — هیچ شبکه، قفل یا نوشتنی."""
        item = SourceReport(
            source.key,
            source.label,
            source.remote_url,
            source.filename,
            outcome=OUTCOME_PLANNED,
            shrink_ratio=ratio,
        )
        path = snaps.snapshot_path(source.key, directory=self.directory)
        try:
            item.previous_operations = len(
                snaps.load_snapshot(source.key, directory=self.directory).apis
            )
            item.sha256_prefix = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
            item.previous_size_bytes = item.size_bytes = path.stat().st_size
        except (snaps.SwaggerSnapshotError, OSError) as exc:
            item.notes.append(f"Current file not usable: {type(exc).__name__}.")
        if not os.access(self.directory, os.W_OK):
            item.notes.append("The snapshot directory is not writable.")
        self.log.info(
            "[dry-run] %s: %s -> %s (current operations: %s, shrink ratio %g).",
            source.label,
            source.remote_url,
            path.name,
            item.previous_operations,
            ratio,
        )
        return item

    def _probe(self, source: snaps.SwaggerSource) -> dict[str, Any]:
        """--check --probe-https: نسخه‌ی https یک بار، بی retry، بی نوشتن."""
        url = https_variant(source.remote_url)
        if url is None:
            return {"url": source.remote_url, "result": "already https"}
        try:
            with self._session_factory() as session:
                document = snaps.fetch_validated(
                    source.key,
                    session=session,
                    timeout=(self.settings.connect_timeout_s, self.settings.read_timeout_s),
                    url=url,
                )
            outcome = {
                "url": url,
                "result": "valid",
                "operations": len(document.service.apis),
            }
        except snaps.SwaggerRefreshError as exc:
            outcome = {
                "url": url,
                "result": "failed",
                "error_kind": exc.kind,
                "http_status": exc.status,
            }
        except Exception as exc:  # noqa: BLE001
            outcome = {
                "url": url,
                "result": "failed",
                "error_kind": KIND_UNEXPECTED,
                "error": type(exc).__name__,
            }
        self.log.info("HTTPS probe %s: %s.", source.label, outcome["result"])
        return outcome

    def _log_item(self, item: SourceReport) -> None:
        level = logging.INFO if item.outcome in SUCCESS_OUTCOMES else logging.ERROR
        self.log.log(
            level,
            "%s: %s after %d attempt(s) in %.1fs (operations %s, previous %s%s).",
            item.label,
            item.outcome,
            item.attempts,
            item.duration_s,
            item.operations,
            item.previous_operations,
            f", {item.error_kind}: {item.error}" if item.error_kind else "",
        )


__all__ = [
    "EXIT_BUSY",
    "EXIT_CONFIG",
    "EXIT_FAILED",
    "EXIT_INTERRUPTED",
    "EXIT_PARTIAL",
    "EXIT_SUCCESS",
    "MODE_CHECK",
    "MODE_DRY_RUN",
    "MODE_RUN",
    "AttemptRecord",
    "RunReport",
    "SourceReport",
    "SyncJob",
    "due_reason",
    "https_variant",
    "is_retryable",
    "last_boundary",
]
