"""
engine.py — اجرای یک Postman Collection با Newman و ساختِ نتیجه‌ی ساختاریافته

    NewmanExecutionEngine().run(NewmanRunRequest.for_dev(collection, output_dir))

مراحل:

  1. یافتنِ Node/Newman/htmlextra و preflightِ فقط‌خواندنیِ ورودی‌ها (+ SHA-256)
  2. اجرای runner.js با گزارش‌های خام فقط در یک پوشه‌ی موقتِ خصوصی
  3. پاک‌سازیِ گزارشِ JSON، HTML، stdout/stderr و نتیجه‌ی درخواست‌ها و بررسیِ
     دوباره‌ی نبودِ هر مقدارِ محرمانه — اگر ممکن نشد: ERROR و هیچ گزارشی منتشر
     نمی‌شود
  4. طبقه‌بندیِ PASS/FAIL/ERROR و انتشار در پوشه‌ی یکتای همین اجرا
  5. حذفِ پوشه‌ی موقت در هر حالت؛ بررسیِ اینکه environment/globals دست نخورده‌اند

توکنِ دسترسی (request.access_token) فقط در زمانِ اجرا تزریق می‌شود: «Bearer <token>»
از راهِ محیطِ پروسه‌ی runner.js به envVarِ Newman می‌رسد و جای متغیرِ token ِ
Environment را می‌گیرد؛ نه در config.json نوشته می‌شود و نه در فایل‌های ورودی. هر دو
شکلِ خام و Bearer جزوِ مقادیرِ محرمانه‌اند و از همه‌ی خروجی‌ها پاک می‌شوند.

موتور به Streamlit، Batch یا Jira وابسته نیست و هیچ استثنایی بیرون نمی‌دهد.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from src.debug import DebugConfig
from src.test_execution.newman.classify import classify
from src.test_execution.newman.locator import REPO_ROOT, NewmanInstallation, locate
from src.test_execution.newman.models import (
    ERROR_ACCESS_TOKEN_INVALID,
    ERROR_INPUT_FILE_MODIFIED,
    ERROR_INTERNAL,
    ERROR_INVALID_OPTIONS,
    ERROR_OUTPUT_DIRECTORY,
    ERROR_PRIVATE_DIRECTORY,
    ERROR_SANITIZATION_FAILED,
    NO_ASSERTIONS_WARNING,
    STATUS_ERROR,
    TOKEN_VARIABLE,
    InputFile,
    NewmanRunRequest,
    NewmanRunResult,
    RunStats,
)
from src.test_execution.newman.preflight import run_preflight, sha256_of
from src.test_execution.newman.process import ProcessOutcome, ProcessRunner, run_process
from src.test_execution.newman.redaction import (
    SanitizationError,
    sanitize_summary,
    scrub_text,
    summary_secrets,
    variable_secrets,
    verify_clean,
    verify_clean_json,
)
from src.test_execution.newman.report import (
    Events,
    ReportError,
    build_requests,
    event_secrets,
    inconsistency,
    read_events,
    read_summary,
    stats_of,
)
from src.test_execution.newman.scratch import (
    DEFAULT_ROOT,
    PrivateDirectoryError,
    create_private_dir,
    remove_private_dir,
    sweep_stale,
)

# htmlextra 1.23.1 — گزینه‌هایی که در lib/index.js و قالب‌ها بررسی شده‌اند.
HTMLEXTRA_OPTIONS = {
    "omitHeaders": True,
    "omitRequestBodies": True,
    "omitResponseBodies": True,
    "skipSensitiveData": True,
    "showEnvironmentData": False,
    "showGlobalData": False,
    "logs": False,
}
_TAIL_CHARS = 4000
# نامِ متغیرِ محیطیِ پروسه‌ی runner.js برای متغیرهای زمانِ اجرا (JSON) — runner.js آن را
# بلافاصله پس از خواندن از process.env حذف می‌کند.
RUNTIME_ENV_VARS = "TESTAGENT_NEWMAN_ENV_VARS"
_NO_ASSERTIONS_BANNER = (
    '<div role="alert" style="padding:12px;margin:12px;border:2px solid #b45309;'
    'background:#fef3c7;color:#78350f;font-weight:bold">' + NO_ASSERTIONS_WARNING + "</div>"
)


def _bearer(token: str) -> str:
    """تنها جایی که «Bearer » اضافه می‌شود."""
    return f"Bearer {token}"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class NewmanExecutionEngine:
    """پارامترها برای تست قابلِ جایگزینی‌اند: اجرای پروسه و یافتنِ نصب."""

    name = "newman_engine"

    def __init__(
        self,
        debug_config: DebugConfig | None = None,
        process_runner: ProcessRunner = run_process,
        installation: NewmanInstallation | None = None,
        scratch_root: Path = DEFAULT_ROOT,
    ) -> None:
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
        self._run_process = process_runner
        self._installation = installation
        # ریشه‌ی پوشه‌های خصوصیِ گزارشِ خام (scratch.py)
        self._scratch_root = Path(scratch_root)

    # ── اجرا ────────────────────────────────────────────────────────────────
    def run(
        self, request: NewmanRunRequest, cancel_event: threading.Event | None = None
    ) -> NewmanRunResult:
        """یک اجرای کامل؛ با cancel_event، لغو درختِ پروسه را می‌بندد و نتایجِ جزئی می‌مانند.

        لغوِ بعد از پایانِ Newman هیچ اثری ندارد — PASS/FAIL بازنویسی نمی‌شود.
        """
        started = _now()
        result = NewmanRunResult(
            run_id=f"{started:%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}",
            started_at=_iso(started),
            collection=InputFile(str(request.collection_path)),
            environment=InputFile(str(request.environment_path)),
            globals=InputFile(str(request.globals_path)),
        )
        # همه‌ی مقادیرِ محرمانه‌ی دیده‌شده — result.json هم پیش از نوشتن با آن‌ها سنجیده می‌شود.
        secrets: set[str] = set()
        try:
            self._run(request, result, secrets, cancel_event)
        except Exception as exc:  # noqa: BLE001 — موتور هرگز استثنا بیرون نمی‌دهد
            # پیامِ استثنا ممکن است داده داشته باشد؛ فقط نوعش ثبت می‌شود.
            self._fail(result, ERROR_INTERNAL, f"Unexpected engine failure ({type(exc).__name__}).")
        finished = _now()
        result.finished_at = _iso(finished)
        result.duration_ms = int((finished - started).total_seconds() * 1000)
        self._write_result(result, secrets)
        self._log.info(
            "اجرای Newman تمام شد",
            run_id=result.run_id,
            status=result.status,
            error_kind=result.error_kind,
            requests=result.stats.requests_executed,
            assertions_failed=result.stats.assertions_failed,
        )
        return result

    @staticmethod
    def _fail(result: NewmanRunResult, kind: str, message: str) -> None:
        result.status, result.error_kind, result.error_message = STATUS_ERROR, kind, message

    def _run(
        self,
        request: NewmanRunRequest,
        result: NewmanRunResult,
        secrets: set[str],
        cancel_event: threading.Event | None = None,
    ) -> None:
        options = request.options
        if (
            options.request_timeout_ms <= 0
            or options.process_timeout_s <= 0
            or options.delay_request_ms < 0
        ):
            self._fail(
                result,
                ERROR_INVALID_OPTIONS,
                "Timeouts must be positive and the delay non-negative.",
            )
            return
        token = request.access_token
        if token is not None:
            if not token.strip() or any(ch.isspace() for ch in token):
                # هرگز Newman با توکنِ خالی یا نامعتبر اجرا نمی‌شود
                self._fail(
                    result,
                    ERROR_ACCESS_TOKEN_INVALID,
                    "The access token is empty or invalid; Newman was not run.",
                )
                return
            secrets.update({token, _bearer(token)})

        install = self._installation or locate()
        result.node_version, result.newman_version = install.node_version, install.newman_version
        result.htmlextra_version = install.htmlextra_version
        if not install.ok:
            self._fail(result, install.error_kind, install.error_message)
            return

        preflight = run_preflight(
            request.collection_path, request.environment_path, request.globals_path
        )
        result.collection.sha256_before = preflight.hashes.get("collection", "")
        result.environment.sha256_before = preflight.hashes.get("environment", "")
        result.globals.sha256_before = preflight.hashes.get("globals", "")
        result.host_variables = preflight.host_variables
        if not preflight.ok:
            self._fail(result, preflight.error_kind, preflight.error_message)
            return

        try:
            run_dir = Path(request.output_dir) / result.run_id
            run_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            self._fail(
                result,
                ERROR_OUTPUT_DIRECTORY,
                f"The run directory cannot be created ({type(exc).__name__}).",
            )
            return
        result.artifacts.run_dir = str(run_dir)

        secrets.update(
            variable_secrets(preflight.environment)
            | variable_secrets(preflight.globals)
            | variable_secrets(preflight.collection)
        )
        # پوشه‌های رهاشده‌ی اجراهای قطع‌شده‌ی قبلی (فقط پوشه‌های نشان‌دارِ خودِ موتور).
        sweep_stale(self._scratch_root)
        try:
            private = create_private_dir(self._scratch_root)
        except PrivateDirectoryError as exc:
            self._fail(result, ERROR_PRIVATE_DIRECTORY, f"Newman was not run: {exc}")
            return
        try:
            self._execute(
                request,
                result,
                install,
                private,
                run_dir,
                secrets,
                preflight.request_count,
                cancel_event,
            )
        finally:
            if not remove_private_dir(private):
                result.warnings.append(
                    "The private temporary directory could not be removed; it is removed by a "
                    "later run once it is stale."
                )

        self._check_inputs_unchanged(request, result)

    def _execute(
        self,
        request: NewmanRunRequest,
        result: NewmanRunResult,
        install: NewmanInstallation,
        private: Path,
        run_dir: Path,
        secrets: set[str],
        request_count: int,
        cancel_event: threading.Event | None = None,
    ) -> None:
        options = request.options
        paths = {
            "json": private / "newman-report.json",
            "html": private / "newman-report.html",
            "events": private / "events.ndjson",
        }
        config = {
            "newman_module": install.newman_module,
            "collection": str(Path(request.collection_path).resolve()),
            "environment": str(Path(request.environment_path).resolve()),
            "globals": str(Path(request.globals_path).resolve()),
            "timeout_request_ms": options.request_timeout_ms,
            "delay_request_ms": options.delay_request_ms,
            "bail": options.bail,
            "insecure": options.insecure,
            "json_report": str(paths["json"]),
            "html_report": str(paths["html"]),
            "events_path": str(paths["events"]),
            "htmlextra": HTMLEXTRA_OPTIONS,
            # runner.js خودش را می‌بندد اگر این پروسه از بین برود (بدونِ فرزندِ یتیم).
            "parent_pid": os.getpid(),
        }
        config_path = private / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        argv = [install.node, "--no-deprecation", install.runner, str(config_path)]
        result.command = ["node", "--no-deprecation", Path(install.runner).name, "<private config>"]

        if cancel_event is not None and cancel_event.is_set():
            # لغو پیش از شروعِ پروسه: Newman اصلاً اجرا نمی‌شود.
            outcome = ProcessOutcome(started=False, cancelled=True)
        else:
            # cancel_event و extra_env فقط وقتی لازم باشند فرستاده می‌شوند — فراخوانی‌های قبلی
            # دقیقاً همان‌اند.
            extra: dict = {"cancel_event": cancel_event} if cancel_event is not None else {}
            if request.access_token is not None:
                runtime = [{"key": TOKEN_VARIABLE, "value": _bearer(request.access_token)}]
                extra["extra_env"] = {RUNTIME_ENV_VARS: json.dumps(runtime)}
            outcome = self._run_process(argv, options.process_timeout_s, str(REPO_ROOT), **extra)
        result.exit_code, result.timed_out, result.process_id = (
            outcome.exit_code,
            outcome.timed_out,
            outcome.pid,
        )
        result.cancelled = outcome.cancelled

        events, events_error = Events(), None
        try:
            events = read_events(paths["events"])
        except ReportError as exc:
            events_error = (exc.kind, str(exc))
        summary, summary_error = None, None
        try:
            summary = read_summary(paths["json"])
        except ReportError as exc:
            summary_error = (exc.kind, str(exc))

        secrets.update(event_secrets(events) | (summary_secrets(summary) if summary else set()))
        try:
            published = self._sanitize(outcome, events, summary, paths["html"], secrets, result)
        except SanitizationError as exc:
            result.requests = []
            result.stats = self._safe_stats(
                outcome, events, events_error, summary, secrets, request_count
            )
            self._fail(
                result,
                ERROR_SANITIZATION_FAILED,
                f"Sanitization failed: {exc} No report was published.",
            )
            return

        result.stats = stats_of(result.requests)
        if not outcome.timed_out:
            result.stats.items_not_started = max(0, request_count - len(result.requests))
        verdict = classify(
            outcome,
            done=events.done,
            events_error=events_error,
            summary_error=summary_error,
            inconsistency=inconsistency(summary, result.stats)
            if summary and not events_error
            else "",
            requests=result.requests,
            stats=result.stats,
            bail=options.bail,
        )
        result.status, result.error_kind, result.error_message = (
            verdict.status,
            verdict.error_kind,
            verdict.message,
        )
        result.no_assertions = verdict.no_assertions
        if verdict.no_assertions:
            result.warnings.append(NO_ASSERTIONS_WARNING)
            published = self._mark_no_assertions(published)

        self._publish(published, run_dir, result)

    @staticmethod
    def _safe_stats(
        outcome: ProcessOutcome,
        events: Events,
        events_error: tuple[str, str] | None,
        summary: dict | None,
        secrets: set[str],
        request_count: int,
    ) -> RunStats:
        """شمارش‌ها وقتی پاک‌سازی شکست خورد — فقط از رخدادهای ساختاریافته و فقط اگر
        قابلِ اتکا باشند؛ وگرنه «نامعلوم» (None)، هرگز صفرِ ساختگی.

        قابلِ اتکا یعنی: پروسه کامل تمام شد (نه timeout، نه لغو)، فایلِ رخداد سالم و
        کامل است، و گزارشِ JSONِ Newman هست و همان شمارش‌ها را می‌گوید. فقط عدد
        برمی‌گردد؛ هیچ متنی از رخدادها یا گزارش منتشر نمی‌شود و حکمی صادر نمی‌شود.
        """
        if outcome.timed_out or outcome.cancelled or events_error or not events.done:
            return RunStats.unknown()
        if summary is None:
            return RunStats.unknown()
        try:
            requests = build_requests(events, secrets)
            stats = stats_of(requests)
            if inconsistency(summary, stats):
                return RunStats.unknown()
        except Exception:  # noqa: BLE001 — هر خطا یعنی شمارشِ قابلِ اتکا نداریم
            return RunStats.unknown()
        stats.items_not_started = max(0, request_count - len(requests))
        return stats

    # ── پاک‌سازی ─────────────────────────────────────────────────────────────
    def _sanitize(
        self,
        outcome: ProcessOutcome,
        events: Events,
        summary: dict | None,
        html_path: Path,
        secrets: set[str],
        result: NewmanRunResult,
    ) -> dict[str, str]:
        """متنِ پاک‌شده‌ی هر خروجی — هر کدام پیش از برگشت دوباره بررسی می‌شود."""
        published: dict[str, str] = {}

        result.requests = build_requests(events, secrets)
        verify_clean_json([asdict(r) for r in result.requests], secrets, "the request results")

        stdout, stderr = scrub_text(outcome.stdout, secrets), scrub_text(outcome.stderr, secrets)
        verify_clean(stdout, secrets, "stdout")
        verify_clean(stderr, secrets, "stderr")
        published["stdout.log"], published["stderr.log"] = stdout, stderr
        result.stdout_tail, result.stderr_tail = stdout[-_TAIL_CHARS:], stderr[-_TAIL_CHARS:]

        if summary is not None:
            sanitized = sanitize_summary(summary, secrets)
            verify_clean_json(sanitized, secrets, "the JSON report")
            published["newman-report.json"] = json.dumps(sanitized, ensure_ascii=False, indent=2)

        if html_path.is_file():
            try:
                raw_html = html_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise SanitizationError(
                    "the HTML report could not be read for sanitization."
                ) from exc
            html = scrub_text(raw_html, secrets)
            verify_clean(html, secrets, "the HTML report")
            published["newman-report.html"] = html
        return published

    @staticmethod
    def _mark_no_assertions(published: dict[str, str]) -> dict[str, str]:
        marked = dict(published)
        if "newman-report.json" in marked:
            report = json.loads(marked["newman-report.json"])
            report["engine_warnings"] = [NO_ASSERTIONS_WARNING]
            marked["newman-report.json"] = json.dumps(report, ensure_ascii=False, indent=2)
        if "newman-report.html" in marked:
            html = marked["newman-report.html"]
            index = html.find(">", html.find("<body"))
            marked["newman-report.html"] = (
                html[: index + 1] + _NO_ASSERTIONS_BANNER + html[index + 1 :]
                if index > 0
                else _NO_ASSERTIONS_BANNER + html
            )
        return marked

    # ── انتشار ──────────────────────────────────────────────────────────────
    @staticmethod
    def _publish(published: dict[str, str], run_dir: Path, result: NewmanRunResult) -> None:
        fields = {
            "newman-report.json": "json_report",
            "newman-report.html": "html_report",
            "stdout.log": "stdout_log",
            "stderr.log": "stderr_log",
        }
        for name, text in published.items():
            target = run_dir / name
            with open(
                target, "x", encoding="utf-8", newline="\n"
            ) as handle:  # هرگز بازنویسی نمی‌شود
                handle.write(text)
            setattr(result.artifacts, fields[name], str(target))

    def _check_inputs_unchanged(self, request: NewmanRunRequest, result: NewmanRunResult) -> None:
        changed = []
        for label, path, record in (
            ("collection", request.collection_path, result.collection),
            ("environment", request.environment_path, result.environment),
            ("globals", request.globals_path, result.globals),
        ):
            try:
                record.sha256_after = sha256_of(Path(path))
            except OSError:
                record.sha256_after = ""
            if record.sha256_before and record.sha256_after != record.sha256_before:
                changed.append(label)
        if changed:
            self._fail(
                result,
                ERROR_INPUT_FILE_MODIFIED,
                "Input file(s) changed during the run: " + ", ".join(changed) + ".",
            )

    def _write_result(self, result: NewmanRunResult, secrets: set[str]) -> None:
        if not result.artifacts.run_dir:
            return
        target = Path(result.artifacts.run_dir) / "result.json"
        result.artifacts.result_json = str(target)
        try:
            verify_clean_json(result.to_dict(), secrets, "the structured result")
        except SanitizationError as exc:
            # آخرین سد: هیچ بخشِ قابلِ‌تردیدی منتشر نمی‌شود.
            result.requests, result.stdout_tail, result.stderr_tail, result.warnings = (
                [],
                "",
                "",
                [],
            )
            self._fail(
                result,
                ERROR_SANITIZATION_FAILED,
                f"Sanitization failed: {exc} No report was published.",
            )
        with open(target, "x", encoding="utf-8", newline="\n") as handle:
            json.dump(result.to_dict(), handle, ensure_ascii=False, indent=2)


__all__ = ["HTMLEXTRA_OPTIONS", "RUNTIME_ENV_VARS", "NewmanExecutionEngine"]
