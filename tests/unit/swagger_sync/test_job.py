"""
tests/unit/swagger_sync/test_job.py — موتورِ همگام‌سازی: retry تا موفقیت، گزارش، قفل و CLI

نشستِ HTTP، ساعت و sleep ساختگی‌اند (retry ِ نامحدود بی‌هیچ انتظارِ واقعی)؛ snapshotها در
tmp_path. هیچ شبکه یا فایلِ واقعی. شماره‌ی سناریوهای الزامی در docstring ِ هر تست آمده است.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from src.agents.test_case_generator import swagger_locks
from src.agents.test_case_generator import swagger_snapshots as snaps
from src.execution_gate import OsLock
from src.swagger_settings import SwaggerSettingsError, SyncSettings, load_sync_settings
from src.swagger_sync import __main__ as cli
from src.swagger_sync import job as sync_job
from src.swagger_sync import report as sync_report
from tests.unit.swagger_sync.swagger_fakes import FakeSession, Response, ok, raw, spec

REPO_ROOT = Path(__file__).resolve().parents[3]
KEYS = [s.key for s in snaps.SWAGGER_SOURCES]
URL = {s.key: s.remote_url for s in snaps.SWAGGER_SOURCES}
SECRET_BODY = "SECRET-BODY-0123456789"
SECRET_HEADER = "Bearer SECRET-TOKEN-abcdef"


def settings(tmp_path: Path, **overrides) -> SyncSettings:
    values = dict(
        enabled=True,
        daily_time="03:00",
        sources=(),
        max_attempts=0,
        retry_interval_s=30,
        backoff_multiplier=2,
        max_retry_interval_s=300,
        max_runtime_s=0,
        log_dir=tmp_path / "logs",
        log_backups=2,
        connect_timeout_s=10,
        read_timeout_s=120,
    )
    values.update(overrides)
    return SyncSettings(**values)


class Clock:
    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.sleeps: list[float] = []
        self.on_sleep = on_sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))


@pytest.fixture
def folder(tmp_path):
    directory = tmp_path / "swagger"
    directory.mkdir()
    for key in KEYS:
        (directory / f"{key}.json").write_bytes(raw(spec(10, prefix=f"/{key}")))
    (directory / "README.md").write_text("readme", encoding="utf-8")
    (directory / "other.json").write_text("{}", encoding="utf-8")
    return directory


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}


def _job(tmp_path, folder, by_url, clock=None, progress=None, **overrides):
    clock = clock or Clock()
    session = FakeSession(by_url=by_url)
    job = sync_job.SyncJob(
        settings(tmp_path, **overrides),
        directory=folder,
        session_factory=lambda: session,
        sleep=clock.sleep,
        clock=clock,
        progress=progress,
    )
    return job, session, clock


def _new(key, operations=11):
    return ok(spec(operations, prefix=f"/{key}"))


def _same(key):
    return ok(spec(10, prefix=f"/{key}"))


def _all_same():
    return {URL[k]: [_same(k)] for k in KEYS}


def _by(report):
    return {s.key: s for s in report.sources}


def _calls(session, key):
    return sum(1 for c in session.calls if c["url"] == URL[key])


class TestSuccess:
    def test_all_five_sources_succeed(self, tmp_path, folder):
        """۱ — هر پنج سرویس موفق؛ فقط فایل‌های ثبت‌شده."""
        by_url = {URL[k]: [_new(k) if k in ("admin", "crm") else _same(k)] for k in KEYS}
        job, session, clock = _job(tmp_path, folder, by_url)
        report = job.run()
        assert (report.result, report.exit_code) == (sync_job.RESULT_SUCCESS, 0)
        assert report.succeeded == KEYS and report.pending == report.failed == []
        outcomes = {k: s.outcome for k, s in _by(report).items()}
        assert outcomes["admin"] == outcomes["crm"] == sync_job.OUTCOME_UPDATED
        assert outcomes["customer"] == outcomes["provider"] == sync_job.OUTCOME_UNCHANGED
        assert [c["url"] for c in session.calls] == [URL[k] for k in KEYS]
        assert all(c["timeout"] == (10, 120) for c in session.calls)
        assert clock.sleeps == []

    def test_identical_content_is_not_rewritten(self, tmp_path, folder, monkeypatch):
        """۲ — محتوای یکسان: هیچ بازنویسی (نه os.replace، نه تغییرِ mtime)."""
        replaced: list[str] = []
        real = snaps._replace_atomically
        monkeypatch.setattr(
            snaps,
            "_replace_atomically",
            lambda source, path, content: replaced.append(path.name) or real(source, path, content),
        )
        mtimes = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
        by_url = _all_same()
        by_url[URL["customer"]] = [ok(json.loads(raw(spec(10, prefix="/customer"))))]
        report = job_run(tmp_path, folder, by_url)
        assert replaced == []
        assert {p.name: p.stat().st_mtime_ns for p in folder.iterdir()} == mtimes
        customer = _by(report)["customer"]
        assert (customer.comparison, customer.update, customer.shrink_guard) == (
            sync_job.COMPARE_IDENTICAL,
            sync_job.UPDATE_NOT_WRITTEN,
            sync_job.SHRINK_NOT_APPLICABLE,
        )

    def test_changed_content_is_replaced_atomically(self, tmp_path, folder, monkeypatch):
        """۳ — محتوای تغییرکرده: فایلِ موقت در همان پوشه، سپس os.replace."""
        replaces: list[tuple[Path, Path]] = []
        real_replace = os.replace

        def spy(src, dst):
            replaces.append((Path(src), Path(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr(snaps.os, "replace", spy)
        by_url = _all_same()
        by_url[URL["admin"]] = [_new("admin", operations=12)]
        report = job_run(tmp_path, folder, by_url)
        ((src, dst),) = replaces
        assert dst == folder / "admin.json" and src.parent == folder
        assert src.name.startswith(".admin.json.") and src.suffix == ".tmp" and not src.exists()
        assert (folder / "admin.json").read_bytes() == raw(spec(12, prefix="/admin"))
        admin = _by(report)["admin"]
        assert (admin.comparison, admin.update, admin.shrink_guard, admin.validation) == (
            sync_job.COMPARE_DIFFERENT,
            sync_job.UPDATE_REPLACED,
            sync_job.SHRINK_PASSED,
            sync_job.VALIDATION_PASSED,
        )
        assert (admin.operations, admin.previous_operations) == (12, 10)
        assert admin.size_bytes == len(raw(spec(12, prefix="/admin")))
        assert admin.previous_size_bytes == len(raw(spec(10, prefix="/admin")))


def job_run(tmp_path, folder, by_url, **overrides):
    job, _session, _clock = _job(tmp_path, folder, by_url, **overrides)
    return job.run()


class TestRetryUntilSuccess:
    def test_first_attempt_fails_and_the_second_succeeds(self, tmp_path, folder):
        """۴ — همان الگوی سرورِ واقعی: 504 در تلاشِ اول، 200 در تلاشِ دوم."""
        by_url = _all_same()
        by_url[URL["admin"]] = [Response(504, b"", "Gateway Time-out"), _new("admin")]
        job, _, clock = _job(tmp_path, folder, by_url)
        report = job.run()
        admin = _by(report)["admin"]
        assert (report.result, admin.outcome, admin.attempts) == (
            sync_job.RESULT_SUCCESS,
            sync_job.OUTCOME_UPDATED,
            2,
        )
        first, second = admin.attempt_history
        assert (first.http_status, first.error_kind, first.retry_in_s) == (504, "http_status", 30)
        assert (second.http_status, second.error_kind, second.retry_in_s) == (200, "", None)
        assert clock.sleeps == [30] and admin.error_kind == "" and admin.next_retry_at == ""

    def test_several_consecutive_failures_then_success_with_backoff(self, tmp_path, folder):
        """۵ — شکستِ متوالی: فاصله‌ها 30، 60، 120، 240 و سپس سقفِ 300."""
        by_url = _all_same()
        by_url[URL["crm"]] = [Response(503, b"")] * 5 + [_new("crm")]
        job, _, clock = _job(tmp_path, folder, by_url)
        report = job.run()
        assert _by(report)["crm"].attempts == 6 and report.result == sync_job.RESULT_SUCCESS
        assert clock.sleeps == [30, 60, 120, 240, 300]

    def test_a_persistent_failure_keeps_retrying_without_stopping(self, tmp_path, folder):
        """۶ — خطای دائمی (۱۵۰ بار 404): بی‌سقف retry، فاصله هرگز بیش از سقف، بقیه یک‌بار."""
        by_url = _all_same()
        by_url[URL["site"]] = [Response(404, b"", "Not Found")] * 150 + [_new("site")]
        job, session, clock = _job(tmp_path, folder, by_url)
        report = job.run()
        site = _by(report)["site"]
        assert (site.outcome, site.attempts) == (sync_job.OUTCOME_UPDATED, 151)
        assert max(clock.sleeps) == 300 and len(clock.sleeps) == 150
        assert all(_calls(session, k) == 1 for k in KEYS if k != "site")
        assert len(site.attempt_history) == sync_job.MAX_ATTEMPT_HISTORY
        assert site.attempts_omitted == 151 - sync_job.MAX_ATTEMPT_HISTORY
        assert site.attempt_history[-1].number == 151
        assert report.result == sync_job.RESULT_SUCCESS

    def test_four_succeed_and_only_the_fifth_is_retried(self, tmp_path, folder):
        """۷ و ۸ — چهار موفق حفظ می‌شوند و دوباره درخواست نمی‌گیرند؛ فقط پنجمی retry می‌شود."""
        seen: list[dict] = []
        by_url = {URL[k]: [_new(k)] for k in KEYS}
        by_url[URL["provider"]] = [requests.exceptions.ConnectionError("x")] * 3 + [
            _new("provider")
        ]

        def progress(report):
            seen.append(
                {
                    "succeeded": list(report.succeeded),
                    "pending": list(report.pending),
                    "result": report.result,
                    "files": {k: (folder / f"{k}.json").read_bytes() for k in KEYS},
                }
            )

        job, session, _ = _job(tmp_path, folder, by_url, progress=progress)
        report = job.run()
        after_round = next(s for s in seen if len(s["succeeded"]) == 4)
        assert after_round["pending"] == ["provider"]
        assert after_round["result"] == sync_job.RESULT_IN_PROGRESS
        for k in KEYS:
            if k != "provider":  # چهار snapshot ِ موفق پیش از موفقیتِ پنجمی نوشته شده‌اند
                assert after_round["files"][k] == raw(spec(11, prefix=f"/{k}"))
        assert all(_calls(session, k) == 1 for k in KEYS if k != "provider")
        assert _calls(session, "provider") == 4
        assert report.result == sync_job.RESULT_SUCCESS and report.succeeded == KEYS

    def test_failing_sources_have_independent_schedules(self, tmp_path, folder):
        by_url = _all_same()
        by_url[URL["admin"]] = [Response(500, b"")] * 2 + [_same("admin")]
        by_url[URL["crm"]] = [Response(500, b""), _same("crm")]
        job, session, clock = _job(tmp_path, folder, by_url)
        report = job.run()
        assert report.result == sync_job.RESULT_SUCCESS
        # t=0: همه؛ t=30: admin و crm (هر دو سرِ موعد)؛ t=90: admin (فاصله‌ی دوم 60)
        assert clock.sleeps == [30, 60]
        assert [c["url"] for c in session.calls][5:] == [URL["admin"], URL["crm"], URL["admin"]]

    @pytest.mark.parametrize(
        ("retry_after", "expected"),
        [("100", 100), ("9999", 300), ("5", 30)],
    )
    def test_429_honours_retry_after_within_the_cap(self, tmp_path, folder, retry_after, expected):
        """۹ — Retry-After رعایت می‌شود، ولی هرگز بیش از سقف و هرگز کمتر از backoff."""
        by_url = _all_same()
        by_url[URL["admin"]] = [
            Response(429, b"", "Too Many Requests", {"Retry-After": retry_after}),
            _same("admin"),
        ]
        job, _, clock = _job(tmp_path, folder, by_url)
        admin = _by(job.run())["admin"]
        assert clock.sleeps == [expected] and admin.attempt_history[0].http_status == 429

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 500, 502, 503, 504])
    def test_http_4xx_and_5xx_are_retried(self, tmp_path, folder, status):
        """۱۰"""
        by_url = _all_same()
        by_url[URL["admin"]] = [Response(status, b""), _new("admin")]
        admin = _by(job_run(tmp_path, folder, by_url))["admin"]
        assert admin.outcome == sync_job.OUTCOME_UPDATED
        assert admin.attempt_history[0].http_status == status
        assert admin.attempt_history[0].error_kind == snaps.KIND_HTTP_STATUS

    @pytest.mark.parametrize(
        ("error", "kind"),
        [
            (requests.exceptions.ConnectTimeout("x"), snaps.KIND_CONNECT_TIMEOUT),
            (requests.exceptions.ReadTimeout("x"), snaps.KIND_READ_TIMEOUT),
            (requests.exceptions.ConnectionError("x"), snaps.KIND_CONNECTION),
            (requests.exceptions.TooManyRedirects("x"), snaps.KIND_REQUEST),
        ],
    )
    def test_timeouts_and_network_errors_are_retried(self, tmp_path, folder, error, kind):
        """۱۱"""
        by_url = _all_same()
        by_url[URL["admin"]] = [error, _new("admin")]
        admin = _by(job_run(tmp_path, folder, by_url))["admin"]
        assert admin.outcome == sync_job.OUTCOME_UPDATED
        record = admin.attempt_history[0]
        assert (record.error_kind, record.http_status) == (kind, None)

    @pytest.mark.parametrize(
        ("response", "kind"),
        [
            (Response(200, b""), snaps.KIND_EMPTY),
            (Response(200, b"   "), snaps.KIND_EMPTY),
            (Response(200, b"<html>gateway</html>"), snaps.KIND_INVALID_JSON),
            (Response(200, b'{"openapi": '), snaps.KIND_INVALID_JSON),
        ],
    )
    def test_empty_and_invalid_json_are_retried(self, tmp_path, folder, response, kind):
        """۱۲"""
        before = (folder / "admin.json").read_bytes()
        by_url = _all_same()
        by_url[URL["admin"]] = [response, _same("admin")]
        admin = _by(job_run(tmp_path, folder, by_url))["admin"]
        record = admin.attempt_history[0]
        assert (record.error_kind, record.http_status) == (kind, 200)
        assert admin.outcome == sync_job.OUTCOME_UNCHANGED
        assert (folder / "admin.json").read_bytes() == before

    @pytest.mark.parametrize(
        ("document", "kind"),
        [
            ({"hello": "world"}, snaps.KIND_NOT_OPENAPI),
            (spec(0), snaps.KIND_UNUSABLE),
            (
                {"openapi": "3.0.1", "info": {"title": "t", "version": "1"}, "paths": {"/x": {}}},
                snaps.KIND_NO_OPERATIONS,
            ),
        ],
    )
    def test_invalid_openapi_is_retried(self, tmp_path, folder, document, kind):
        """۱۳"""
        by_url = _all_same()
        by_url[URL["admin"]] = [ok(document), _new("admin")]
        admin = _by(job_run(tmp_path, folder, by_url))["admin"]
        assert admin.attempt_history[0].error_kind in (kind, snaps.KIND_UNUSABLE)
        assert admin.outcome == sync_job.OUTCOME_UPDATED

    def test_the_shrink_guard_keeps_the_file_and_is_retried(self, tmp_path, folder):
        """۱۴ — سندِ ناقص (۲ از ۱۰) رد و فایل حفظ؛ retry تا سندِ کامل."""
        seen: list[tuple[str, str, bytes]] = []
        by_url = _all_same()
        by_url[URL["admin"]] = [_new("admin", operations=2), _new("admin", operations=10 + 1)]

        def progress(report):
            admin = _by(report)["admin"]
            if admin.attempts == 1:
                seen.append(
                    (admin.shrink_guard, admin.validation, (folder / "admin.json").read_bytes())
                )

        job, _, clock = _job(tmp_path, folder, by_url, progress=progress)
        report = job.run()
        assert seen[-1] == (
            sync_job.SHRINK_BLOCKED,
            sync_job.VALIDATION_PASSED,
            raw(spec(10, prefix="/admin")),
        )
        admin = _by(report)["admin"]
        assert admin.attempt_history[0].error_kind == snaps.KIND_SHRINK and clock.sleeps == [30]
        assert admin.outcome == sync_job.OUTCOME_UPDATED

    def test_a_permission_error_while_saving_is_retried(self, tmp_path, folder, monkeypatch):
        """۱۵ — PermissionError هنگامِ جایگزینی (خواننده‌ای فایل را باز دارد) → retry."""
        failures = {"left": 2}
        real_replace = os.replace

        def flaky(src, dst):
            if failures["left"]:
                failures["left"] -= 1
                raise PermissionError(13, "Access is denied")
            return real_replace(src, dst)

        monkeypatch.setattr(snaps.os, "replace", flaky)
        by_url = _all_same()
        by_url[URL["admin"]] = [_new("admin")] * 3
        report = job_run(tmp_path, folder, by_url)
        admin = _by(report)["admin"]
        assert [a.error_kind for a in admin.attempt_history] == ["permission", "permission", ""]
        assert admin.outcome == sync_job.OUTCOME_UPDATED
        assert not [p for p in folder.iterdir() if p.suffix == ".tmp"]

    def test_an_unexpected_error_is_retried_and_the_file_kept(self, tmp_path, folder):
        by_url = _all_same()
        by_url[URL["admin"]] = [RuntimeError("boom"), _same("admin")]
        admin = _by(job_run(tmp_path, folder, by_url))["admin"]
        assert admin.attempt_history[0].error_kind == sync_job.KIND_UNEXPECTED
        assert "RuntimeError" in admin.attempt_history[0].error
        assert admin.outcome == sync_job.OUTCOME_UNCHANGED

    def test_a_configuration_error_fails_closed_without_retry(self, tmp_path, folder, monkeypatch):
        def broken(*args, **kwargs):
            raise snaps.SwaggerRefreshError("Admin", "bad ratio", kind=snaps.KIND_CONFIGURATION)

        monkeypatch.setattr(snaps, "write_snapshot", broken)
        job, session, clock = _job(tmp_path, folder, _all_same())
        report = job.run()
        assert (report.result, report.exit_code) == (sync_job.RESULT_CONFIG_ERROR, 3)
        assert clock.sleeps == [] and len(session.calls) == len(KEYS)

    def test_the_backoff_never_overflows(self, tmp_path):
        s = settings(tmp_path, backoff_multiplier=10)
        assert s.retry_delay(1) == 30 and s.retry_delay(100_000) == 300


class TestOptionalCaps:
    def test_a_configured_attempt_cap_ends_failed_and_never_success(self, tmp_path, folder):
        by_url = _all_same()
        by_url[URL["admin"]] = [requests.exceptions.ConnectionError("x")] * 3
        job, _, clock = _job(tmp_path, folder, by_url, max_attempts=3, retry_interval_s=200)
        report = job.run()
        admin = _by(report)["admin"]
        assert (admin.outcome, admin.attempts) == (sync_job.OUTCOME_FAILED, 3)
        assert clock.sleeps == [200, 300]
        assert (report.result, report.exit_code) == (sync_job.RESULT_PARTIAL, 1)
        assert report.failed == ["admin"]
        assert any("SWAGGER_SYNC_MAX_ATTEMPTS" in w for w in report.warnings)

    def test_a_configured_runtime_cap_stops_retries(self, tmp_path, folder):
        by_url = {URL[k]: [requests.exceptions.ConnectTimeout("x")] * 10 for k in KEYS}
        job, _, clock = _job(tmp_path, folder, by_url, max_runtime_s=100)
        report = job.run()
        assert report.exit_code == sync_job.EXIT_FAILED and sum(clock.sleeps) < 100
        assert {s.outcome for s in report.sources} == {sync_job.OUTCOME_FAILED}

    def test_unlimited_is_the_default(self, monkeypatch):
        s = load_sync_settings()
        assert s.unlimited_attempts and s.unlimited_runtime

    @pytest.mark.parametrize(
        "name", ["SWAGGER_SYNC_RETRY_INTERVAL_SECONDS", "SWAGGER_SYNC_MAX_RETRY_INTERVAL_SECONDS"]
    )
    def test_retry_intervals_below_one_second_are_refused(self, monkeypatch, name):
        monkeypatch.setenv(name, "0")
        with pytest.raises(SwaggerSettingsError, match=name):
            load_sync_settings()


class TestCrashSafetyAndLocks:
    def test_a_crash_mid_write_keeps_the_file_and_the_next_run_recovers(
        self, tmp_path, folder, monkeypatch
    ):
        """۱۶ — پروسه وسطِ نوشتن «کشته» می‌شود: فایلِ قبلی سالم، قفل آزاد، اجرای بعدی موفق."""
        before = _snapshot(folder)

        def killed(src, dst):
            raise SystemExit("killed")  # BaseException: هیچ except ِ OSError آن را نمی‌گیرد

        with monkeypatch.context() as patch:
            patch.setattr(snaps.os, "replace", killed)
            by_url = _all_same()
            by_url[URL["admin"]] = [_new("admin")]
            job, _, _ = _job(tmp_path, folder, by_url)
            with pytest.raises(SystemExit):
                job.run()
        leftovers = [p.name for p in folder.iterdir() if p.name.startswith(".admin.json.")]
        assert leftovers  # فایلِ موقتِ جامانده، دقیقاً مثلِ kill ِ واقعی
        assert {k: v for k, v in _snapshot(folder).items() if k in before} == before
        report = job_run(tmp_path, folder, {URL[k]: [_new(k)] for k in KEYS})
        assert report.result == sync_job.RESULT_SUCCESS
        assert report.cleaned_temp_files == leftovers
        assert sorted(p.name for p in folder.iterdir()) == sorted(before)

    def test_an_interrupted_run_releases_the_lock_and_reports_it(self, tmp_path, folder):
        def interrupt(count):
            raise KeyboardInterrupt

        by_url = _all_same()
        by_url[URL["admin"]] = [Response(503, b"")]
        clock = Clock(on_sleep=interrupt)
        job, _, _ = _job(tmp_path, folder, by_url, clock=clock)
        report = job.run()
        assert (report.result, report.exit_code) == (sync_job.RESULT_INTERRUPTED, 5)
        assert _by(report)["admin"].outcome == sync_job.OUTCOME_INTERRUPTED
        assert report.failed == ["admin"] and len(report.succeeded) == 4
        held = swagger_locks.try_run_lock(folder)
        assert held is not None
        held.release()

    def test_a_job_retries_while_the_update_button_holds_the_write_lock(
        self, tmp_path, folder, monkeypatch
    ):
        """۱۷ — دکمه‌ی Update قفلِ نوشتن را دارد: job خطای locked می‌گیرد، فایل را دست نمی‌زند و
        پس از آزاد شدنِ قفل موفق می‌شود."""
        real_lock = swagger_locks.write_lock
        monkeypatch.setattr(snaps, "write_lock", lambda d, **kw: real_lock(d, wait_s=0.2))
        lock_dir = swagger_locks.lock_dir_for(folder)
        lock_dir.mkdir(parents=True, exist_ok=True)
        holder = OsLock(lock_dir / swagger_locks.WRITE_LOCK_NAME)
        assert holder.try_acquire()
        released = []

        def release(count):
            if not released:
                holder.release()
                released.append(True)

        by_url = {URL[k]: [_new(k), _new(k)] for k in KEYS}
        job, _, _ = _job(tmp_path, folder, by_url, clock=Clock(on_sleep=release))
        try:
            report = job.run()
        finally:
            if not released:
                holder.release()
        admin = _by(report)["admin"]
        assert admin.attempt_history[0].error_kind == snaps.KIND_LOCKED
        assert report.result == sync_job.RESULT_SUCCESS
        assert (folder / "admin.json").read_bytes() == raw(spec(11, prefix="/admin"))

    def test_a_concurrent_job_is_refused_without_any_request(self, tmp_path, folder):
        """۱۸ — اجرای هم‌زمانِ دو Job (همان پروسه)."""
        held = swagger_locks.try_run_lock(folder)
        try:
            job, session, _ = _job(tmp_path, folder, {})
            report = job.run()
        finally:
            held.release()
        assert (report.result, report.exit_code) == (sync_job.RESULT_BUSY, 4)
        assert session.calls == []

    def test_the_run_lock_of_a_killed_process_is_released_by_the_os(self, tmp_path, folder):
        """۱۸ و ۱۶ — پروسه‌ی دیگری قفلِ اجرا را دارد → BUSY؛ kill → قفل بی‌هیچ پاک‌سازی آزاد."""
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import sys, time
                    sys.path.insert(0, {str(REPO_ROOT)!r})
                    from src.agents.test_case_generator.swagger_locks import try_run_lock
                    lock = try_run_lock({str(folder)!r})
                    assert lock is not None
                    print("HELD", flush=True)
                    time.sleep(60)
                    """
                ),
            ],
            stdout=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "ENV_FILE": str(tmp_path / "missing.env"),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        try:
            assert child.stdout.readline().strip() == "HELD"
            job, session, _ = _job(tmp_path, folder, {})
            assert job.run().result == sync_job.RESULT_BUSY and session.calls == []
        finally:
            child.kill()
            child.wait(30)
            child.stdout.close()
        assert job_run(tmp_path, folder, _all_same()).result == sync_job.RESULT_SUCCESS


class TestSafety:
    def test_no_secret_reaches_the_log_or_report(self, tmp_path, folder, monkeypatch, capsys):
        """۲۱ — بدنه، header و مقدارِ محیط هرگز در log یا گزارش نمی‌آیند."""
        monkeypatch.setenv("JIRA_API_TOKEN", SECRET_HEADER)
        by_url = _all_same()
        by_url[URL["admin"]] = [
            Response(
                200,
                f"<html>{SECRET_BODY}</html>".encode(),
                headers={"Set-Cookie": SECRET_HEADER, "Authorization": SECRET_HEADER},
            ),
            Response(500, SECRET_BODY.encode(), headers={"WWW-Authenticate": SECRET_HEADER}),
            _same("admin"),
        ]
        logger = sync_report.setup_logging(tmp_path / "logs", 1)
        try:
            job, _, _ = _job(tmp_path, folder, by_url)
            job.log = logger
            report = job.run()
        finally:
            sync_report.close_logging()
        text = (tmp_path / "logs" / sync_report.LOG_FILE).read_text(encoding="utf-8")
        dumped = json.dumps(report.to_dict())
        err = capsys.readouterr().err
        assert "Admin attempt 1 failed" in text and "HTTP 500" in text
        for secret in (SECRET_BODY, "SECRET-TOKEN"):
            assert secret not in text and secret not in dumped and secret not in err

    def test_only_registered_snapshots_change(self, tmp_path, folder):
        """۲۲ — README، other.json و فایل‌های موقتِ غیرِ ثبت‌شده هرگز تغییر یا حذف نمی‌شوند."""
        for name in (".README.md.x.tmp", "notes.tmp", ".other.json.y.tmp", ".admin.json.z.txt"):
            (folder / name).write_text("keep", encoding="utf-8")
        (folder / ".admin.json.abc.tmp").write_text("stale", encoding="utf-8")
        report = job_run(tmp_path, folder, {URL[k]: [_new(k)] for k in KEYS})
        assert report.cleaned_temp_files == [".admin.json.abc.tmp"]
        names = sorted(p.name for p in folder.iterdir())
        assert names == sorted(
            [f"{k}.json" for k in KEYS]
            + ["README.md", "other.json", ".README.md.x.tmp", "notes.tmp", ".other.json.y.tmp"]
            + [".admin.json.z.txt"]
        )
        assert (folder / "README.md").read_text(encoding="utf-8") == "readme"
        assert (folder / "other.json").read_text(encoding="utf-8") == "{}"

    def test_only_the_selected_sources_run(self, tmp_path, folder):
        job, session, _ = _job(
            tmp_path,
            folder,
            {URL["customer"]: [_same("customer")], URL["admin"]: [_same("admin")]},
            sources=("customer", "admin"),
        )
        report = job.run()
        assert [s.key for s in report.sources] == ["admin", "customer"]  # ترتیبِ رجیستری

    @pytest.mark.parametrize("sources", [("acl",), ("admin", "../etc")])
    def test_unknown_sources_are_a_configuration_error(self, tmp_path, folder, sources):
        job, session, _ = _job(tmp_path, folder, {}, sources=sources)
        report = job.run()
        assert (report.result, report.exit_code) == (sync_job.RESULT_CONFIG_ERROR, 3)
        assert session.calls == []

    def test_a_disabled_job_does_nothing(self, tmp_path, folder):
        job, session, _ = _job(tmp_path, folder, {}, enabled=False)
        report = job.run()
        assert (report.result, report.exit_code, session.calls) == (sync_job.RESULT_DISABLED, 0, [])

    def test_a_failing_progress_writer_never_stops_the_run(self, tmp_path, folder):
        def broken(report):
            raise OSError("disk full")

        assert job_run(tmp_path, folder, _all_same(), progress=broken).result == "SUCCESS"


class TestCheckAndDryRun:
    def test_check_fetches_and_validates_but_never_writes(self, tmp_path, folder):
        before = _snapshot(folder)
        by_url = {URL[k]: [_new(k) if k == "admin" else _same(k)] for k in KEYS}
        job, session, _ = _job(tmp_path, folder, by_url)
        report = job.run(sync_job.MODE_CHECK)
        admin, customer = _by(report)["admin"], _by(report)["customer"]
        assert (admin.outcome, admin.update) == (
            sync_job.OUTCOME_WOULD_UPDATE,
            sync_job.UPDATE_WOULD_REPLACE,
        )
        assert (customer.outcome, customer.update) == (
            sync_job.OUTCOME_UNCHANGED,
            sync_job.UPDATE_NOT_NEEDED,
        )
        assert _snapshot(folder) == before and len(session.calls) == len(KEYS)

    def test_check_retries_like_a_run_and_never_writes(self, tmp_path, folder):
        before = _snapshot(folder)
        by_url = _all_same()
        by_url[URL["admin"]] = [_new("admin", operations=1), Response(504, b""), _new("admin")]
        job, _, clock = _job(tmp_path, folder, by_url)
        report = job.run(sync_job.MODE_CHECK)
        admin = _by(report)["admin"]
        assert [a.error_kind for a in admin.attempt_history] == ["shrink", "http_status", ""]
        assert admin.outcome == sync_job.OUTCOME_WOULD_UPDATE and clock.sleeps == [30, 60]
        assert _snapshot(folder) == before

    def test_probe_https_tries_the_https_variant_once(self, tmp_path, folder):
        by_url = _all_same()
        for k in KEYS:
            by_url[sync_job.https_variant(URL[k])] = [requests.exceptions.ConnectionError("x")]
        by_url[sync_job.https_variant(URL["admin"])] = [_new("admin")]
        job, session, clock = _job(tmp_path, folder, by_url)
        report = job.run(sync_job.MODE_CHECK, probe_https=True)
        assert _by(report)["admin"].https_probe == {
            "url": sync_job.https_variant(URL["admin"]),
            "result": "valid",
            "operations": 11,
        }
        assert _by(report)["crm"].https_probe["result"] == "failed" and clock.sleeps == []
        assert report.warnings and "http://" in report.warnings[0]

    def test_probe_https_is_refused_outside_check(self, tmp_path, folder):
        job, _, _ = _job(tmp_path, folder, {})
        for mode in (sync_job.MODE_RUN, sync_job.MODE_DRY_RUN):
            with pytest.raises(ValueError):
                job.run(mode, probe_https=True)

    def test_dry_run_uses_no_network_no_lock_and_writes_nothing(self, tmp_path, folder):
        before = _snapshot(folder)

        def no_session():
            raise AssertionError("dry-run must not create an HTTP session")

        job = sync_job.SyncJob(settings(tmp_path), directory=folder, session_factory=no_session)
        report = job.run(sync_job.MODE_DRY_RUN)
        assert report.exit_code == 0
        assert {s.outcome for s in report.sources} == {sync_job.OUTCOME_PLANNED}
        assert {s.previous_operations for s in report.sources} == {10}
        assert report.settings["max_attempts"] == "unlimited"
        assert report.settings["max_runtime_s"] == "unlimited"
        assert report.settings["retry_schedule_s"] == [30, 60, 120, 240, 300, 300, 300, 300]
        assert _snapshot(folder) == before
        assert not swagger_locks.lock_dir_for(folder).exists()
        assert not (tmp_path / "logs").exists()


class TestDue:
    LOCAL = timezone(timedelta(hours=3, minutes=30))

    def _report(self, result, finished: datetime | None):
        return {
            "result": result,
            "finished_at": finished.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if finished
            else "",
        }

    def test_the_last_boundary_is_never_in_the_future(self):
        now = datetime(2026, 11, 8, 2, 0, tzinfo=self.LOCAL)
        assert sync_job.last_boundary("03:00", now) == datetime(
            2026, 11, 7, 3, 0, tzinfo=self.LOCAL
        )
        assert sync_job.last_boundary("01:00", now) == datetime(
            2026, 11, 8, 1, 0, tzinfo=self.LOCAL
        )

    def test_due_cases(self):
        now = datetime(2026, 11, 8, 9, 0, tzinfo=self.LOCAL)
        today_3 = datetime(2026, 11, 8, 3, 5, tzinfo=self.LOCAL)
        yesterday = datetime(2026, 11, 7, 3, 5, tzinfo=self.LOCAL)
        assert sync_job.due_reason(None, "03:00", now) == "no previous run report"
        assert "PARTIAL_FAILURE" in sync_job.due_reason(
            self._report("PARTIAL_FAILURE", today_3), "03:00", now
        )
        assert "IN_PROGRESS" in sync_job.due_reason(self._report("IN_PROGRESS", None), "03:00", now)
        assert "since" in sync_job.due_reason(self._report("SUCCESS", yesterday), "03:00", now)
        assert sync_job.due_reason(self._report("SUCCESS", today_3), "03:00", now) is None
        assert "finish time" in sync_job.due_reason({"result": "SUCCESS"}, "03:00", now)


class TestCli:
    @pytest.fixture
    def cli_folder(self, folder, monkeypatch):
        monkeypatch.setattr(snaps, "SNAPSHOT_DIR", folder)
        return folder

    @pytest.fixture
    def fake_session(self, monkeypatch):
        holder = {"session": FakeSession(by_url=_all_same())}
        real = sync_job.SyncJob

        def fake_job(settings, **kwargs):
            return real(settings, session_factory=lambda: holder["session"], **kwargs)

        monkeypatch.setattr(sync_job, "SyncJob", fake_job)
        return holder

    def test_dry_run_prints_json_and_writes_no_file(self, tmp_path, cli_folder, capsys):
        before = _snapshot(cli_folder)
        assert cli.main(["run", "--dry-run"]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["mode"] == "dry-run" and len(output["sources"]) == len(KEYS)
        assert _snapshot(cli_folder) == before and not (tmp_path / "logs").exists()

    def test_a_configuration_error_exits_3(self, monkeypatch, cli_folder, capsys):
        monkeypatch.setenv("SWAGGER_SYNC_MAX_ATTEMPTS", "-1")
        assert cli.main(["run", "--dry-run"]) == 3
        assert "SWAGGER_SYNC_MAX_ATTEMPTS" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            ["run", "--probe-https"],
            ["run", "--if-due", "--check"],
            ["run", "--if-due", "--dry-run"],
        ],
    )
    def test_invalid_flag_combinations(self, cli_folder, argv):
        assert cli.main(argv) == 3

    def test_run_writes_progress_and_the_final_report(
        self, tmp_path, cli_folder, fake_session, monkeypatch
    ):
        written: list[str] = []
        real_write = sync_report.write_report

        def spy(path, report):
            written.append(report["result"])
            real_write(path, report)

        monkeypatch.setattr(sync_report, "write_report", spy)
        assert cli.main(["run"]) == 0
        report = json.loads((tmp_path / "logs" / "last_run.json").read_text(encoding="utf-8"))
        assert report["result"] == "SUCCESS" and report["succeeded"] == KEYS
        assert written[0] == "IN_PROGRESS" and written[-1] == "SUCCESS"
        assert (tmp_path / "logs" / sync_report.LOG_FILE).is_file()

    def test_if_due_skips_when_already_succeeded_today(self, tmp_path, cli_folder, fake_session):
        logs = tmp_path / "logs"
        logs.mkdir()
        last = {"result": "SUCCESS", "finished_at": sync_job._utc_now()}
        (logs / "last_run.json").write_text(json.dumps(last), encoding="utf-8")
        fake_session["session"] = FakeSession()  # هر درخواستی AssertionError است
        assert cli.main(["run", "--if-due"]) == 0
        assert json.loads((logs / "last_run.json").read_text(encoding="utf-8")) == last
        assert "not due" in (logs / sync_report.LOG_FILE).read_text(encoding="utf-8")

    def test_if_due_runs_after_a_failed_or_missing_run(self, tmp_path, cli_folder, fake_session):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "last_run.json").write_text('{"result": "IN_PROGRESS"}', encoding="utf-8")
        assert cli.main(["run", "--if-due"]) == 0
        report = json.loads((logs / "last_run.json").read_text(encoding="utf-8"))
        assert report["result"] == "SUCCESS"
        assert "due: the last run ended IN_PROGRESS" in (logs / sync_report.LOG_FILE).read_text(
            encoding="utf-8"
        )

    def test_a_busy_run_does_not_overwrite_the_last_report(self, tmp_path, cli_folder):
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs" / "last_run.json").write_text('{"result": "SUCCESS"}', encoding="utf-8")
        held = swagger_locks.try_run_lock(cli_folder)
        try:
            assert cli.main(["run"]) == 4
        finally:
            held.release()
        assert json.loads((tmp_path / "logs" / "last_run.json").read_text(encoding="utf-8")) == {
            "result": "SUCCESS"
        }
