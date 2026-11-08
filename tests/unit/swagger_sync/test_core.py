"""
tests/unit/swagger_sync/test_core.py — هسته‌ی مشترکِ Swagger (Phase 2)

نوعِ خطاها، Base URL ِ قابلِ تنظیم، محافظِ کاهش (در دکمه‌ی Update هم)، تشخیصِ «بدونِ تغییر»
و قفلِ نوشتنِ مشترک. نشستِ HTTP ساختگی است؛ همه‌ی فایل‌ها در tmp_path اند (نه data/swagger).
"""

from __future__ import annotations

import functools
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
import requests

from src.agents.test_case_generator import swagger_locks
from src.agents.test_case_generator import swagger_snapshots as snaps
from src.execution_gate import OsLock
from tests.unit.swagger_sync.swagger_fakes import FakeSession, Response, ok, raw, spec
from ui import swagger_refresh

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _no_shrink_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("SWAGGER_SYNC_MAX_SHRINK_RATIO"):
            monkeypatch.delenv(key)


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "admin.json").write_bytes(raw(spec(10)))
    return tmp_path


def _refresh(session, folder, **kwargs):
    return snaps.refresh_snapshot(
        "admin", session=session, directory=folder, timeout=(1, 2), **kwargs
    )


class TestErrorKinds:
    @pytest.mark.parametrize(
        ("outcome", "kind", "status"),
        [
            (requests.exceptions.ConnectTimeout("x"), snaps.KIND_CONNECT_TIMEOUT, None),
            (requests.exceptions.ReadTimeout("x"), snaps.KIND_READ_TIMEOUT, None),
            (requests.exceptions.ConnectionError("x"), snaps.KIND_CONNECTION, None),
            (requests.exceptions.TooManyRedirects("x"), snaps.KIND_REQUEST, None),
            (Response(503, b"", "Service Unavailable"), snaps.KIND_HTTP_STATUS, 503),
            (Response(404, b"", "Not Found"), snaps.KIND_HTTP_STATUS, 404),
            (Response(200, b"  "), snaps.KIND_EMPTY, 200),
            (Response(200, b"<html>gateway</html>"), snaps.KIND_INVALID_JSON, 200),
            (ok({"hello": "world"}), snaps.KIND_NOT_OPENAPI, 200),
            (ok(spec(0)), snaps.KIND_UNUSABLE, 200),  # paths خالی
            (
                ok(
                    {
                        "openapi": "3.0.1",
                        "info": {"title": "t", "version": "1"},
                        "paths": {"/x": {}},
                    }
                ),
                snaps.KIND_NO_OPERATIONS,
                200,  # خطای اعتبارسنجی هم کدِ HTTP ِ پاسخ را دارد
            ),
        ],
    )
    def test_each_failure_has_a_kind_and_keeps_the_file(self, folder, outcome, kind, status):
        before = (folder / "admin.json").read_bytes()
        with pytest.raises(snaps.SwaggerRefreshError) as caught:
            _refresh(FakeSession(outcome), folder)
        assert caught.value.kind == kind and caught.value.status == status
        assert (folder / "admin.json").read_bytes() == before

    def test_retry_after_is_parsed_only_when_numeric(self, folder):
        for header, expected in (("12", 12.0), ("Wed, 21 Oct 2026 07:28:00 GMT", None)):
            response = Response(429, b"", "Too Many Requests", {"Retry-After": header})
            with pytest.raises(snaps.SwaggerRefreshError) as caught:
                _refresh(FakeSession(response), folder)
            assert caught.value.retry_after == expected

    def test_write_errors_have_kinds(self, folder, monkeypatch):
        for error, kind in (
            (PermissionError("x"), snaps.KIND_PERMISSION),
            (OSError(28, "full"), snaps.KIND_WRITE),
        ):

            def fail(src, dst, error=error):
                raise error

            monkeypatch.setattr(snaps.os, "replace", fail)
            with pytest.raises(snaps.SwaggerRefreshError) as caught:
                _refresh(FakeSession(ok(spec(11))), folder)
            assert caught.value.kind == kind
        assert sorted(p.name for p in folder.iterdir()) == ["admin.json"]

    def test_the_old_positional_signature_still_works(self):
        error = snaps.SwaggerRefreshError("Admin", "reason", "hint")
        assert (error.label, error.reason, error.hint, error.kind) == (
            "Admin",
            "reason",
            "hint",
            "error",
        )


class TestFetchAndValidate:
    def test_it_never_writes(self, folder):
        before = (folder / "admin.json").read_bytes()
        content, service = snaps.fetch_and_validate(
            "admin", session=FakeSession(ok(spec(3))), timeout=(1, 2)
        )
        assert len(service.apis) == 3 and content == raw(spec(3))
        assert (folder / "admin.json").read_bytes() == before

    def test_an_alternative_url_is_fetched_for_the_same_source(self):
        session = FakeSession(ok(spec(2)))
        snaps.fetch_and_validate(
            "admin", session=session, timeout=(1, 2), url="https://podium.test/v3/api-docs/Admin"
        )
        assert session.calls[0]["url"] == "https://podium.test/v3/api-docs/Admin"


class TestShrinkGuard:
    def test_an_abnormal_drop_is_refused_and_the_file_kept(self, folder):
        before = (folder / "admin.json").read_bytes()
        with pytest.raises(snaps.SwaggerRefreshError) as caught:
            _refresh(FakeSession(ok(spec(4))), folder)
        assert caught.value.kind == snaps.KIND_SHRINK and "shrink guard" in str(caught.value)
        assert (folder / "admin.json").read_bytes() == before

    def test_exactly_at_the_threshold_is_accepted(self, folder):
        result = _refresh(FakeSession(ok(spec(5))), folder)
        assert result.changed and result.previous_operations == 10 and result.operations == 5

    def test_the_default_ratio_comes_from_the_environment(self, folder, monkeypatch):
        monkeypatch.setenv("SWAGGER_SYNC_MAX_SHRINK_RATIO", "0.9")
        with pytest.raises(snaps.SwaggerRefreshError):
            _refresh(FakeSession(ok(spec(8))), folder)

    def test_a_per_service_ratio_overrides_the_default(self, folder, monkeypatch):
        monkeypatch.setenv("SWAGGER_SYNC_MAX_SHRINK_RATIO", "0.9")
        monkeypatch.setenv("SWAGGER_SYNC_MAX_SHRINK_RATIO_ADMIN", "0.2")
        assert _refresh(FakeSession(ok(spec(3))), folder).operations == 3

    def test_zero_disables_the_guard_for_that_service(self, folder, monkeypatch):
        monkeypatch.setenv("SWAGGER_SYNC_MAX_SHRINK_RATIO_ADMIN", "0")
        assert _refresh(FakeSession(ok(spec(1))), folder).operations == 1

    @pytest.mark.parametrize("value", ["1.5", "-0.1", "half"])
    def test_an_invalid_ratio_is_a_configuration_error_and_nothing_is_written(
        self, folder, monkeypatch, value
    ):
        monkeypatch.setenv("SWAGGER_SYNC_MAX_SHRINK_RATIO", value)
        before = (folder / "admin.json").read_bytes()
        with pytest.raises(snaps.SwaggerRefreshError) as caught:
            _refresh(FakeSession(ok(spec(20))), folder)
        assert caught.value.kind == snaps.KIND_CONFIGURATION and value not in str(caught.value)
        assert (folder / "admin.json").read_bytes() == before

    def test_a_missing_or_invalid_current_file_does_not_block(self, tmp_path):
        assert _refresh(FakeSession(ok(spec(1))), tmp_path).previous_operations is None
        (tmp_path / "admin.json").write_text("{broken", encoding="utf-8")
        assert _refresh(FakeSession(ok(spec(1))), tmp_path).changed

    def test_the_update_button_path_is_guarded_too(self, folder):
        state: dict = {}
        refresh = functools.partial(
            snaps.refresh_snapshot,
            session=FakeSession(ok(spec(2))),
            directory=folder,
            timeout=(1, 2),
        )
        swagger_refresh.start_refresh(state, "admin")
        status = swagger_refresh.run_pending_refresh(state, "admin", refresh=refresh)
        assert not status.ok and "shrink guard" in status.message
        assert (folder / "admin.json").read_bytes() == raw(spec(10))


class TestUnchanged:
    def test_identical_bytes_are_not_rewritten(self, folder):
        stat = (folder / "admin.json").stat()
        result = _refresh(FakeSession(ok(spec(10))), folder)
        assert not result.changed and (folder / "admin.json").stat().st_mtime_ns == stat.st_mtime_ns

    def test_equal_json_with_different_formatting_is_unchanged(self, folder):
        before = (folder / "admin.json").read_bytes()
        pretty = Response(200, __import__("json").dumps(spec(10), indent=2).encode())
        assert not _refresh(FakeSession(pretty), folder).changed
        assert (folder / "admin.json").read_bytes() == before

    def test_the_button_says_unchanged(self, folder):
        state: dict = {}
        refresh = functools.partial(
            snaps.refresh_snapshot,
            session=FakeSession(ok(spec(10))),
            directory=folder,
            timeout=(1, 2),
        )
        swagger_refresh.start_refresh(state, "admin")
        status = swagger_refresh.run_pending_refresh(state, "admin", refresh=refresh)
        assert status.ok and "left unchanged" in status.message


class TestWriteLock:
    def test_lock_paths_never_touch_the_snapshot_folder_or_the_repository(self, folder):
        assert (
            swagger_locks.lock_dir_for(swagger_locks.DEFAULT_SNAPSHOT_DIR)
            == REPO_ROOT / "logs" / "swagger_sync"
        )
        temp_dir = swagger_locks.lock_dir_for(folder)
        assert REPO_ROOT not in temp_dir.parents and folder not in temp_dir.parents
        _refresh(FakeSession(ok(spec(12))), folder)
        assert sorted(p.name for p in folder.iterdir()) == ["admin.json"]

    def test_a_held_lock_blocks_writing_and_keeps_the_file(self, folder):
        lock_dir = swagger_locks.lock_dir_for(folder)
        lock_dir.mkdir(parents=True, exist_ok=True)
        holder = OsLock(lock_dir / swagger_locks.WRITE_LOCK_NAME)
        assert holder.try_acquire()
        try:
            content, service = snaps.fetch_and_validate(
                "admin", session=FakeSession(ok(spec(12))), timeout=(1, 2)
            )
            with pytest.raises(snaps.SwaggerRefreshError) as caught:
                snaps.write_snapshot("admin", content, service, directory=folder, lock_wait_s=0.3)
            assert caught.value.kind == snaps.KIND_LOCKED
            assert (folder / "admin.json").read_bytes() == raw(spec(10))
        finally:
            holder.release()
        assert snaps.write_snapshot("admin", content, service, directory=folder).changed

    def test_the_writer_waits_for_a_short_lock(self, folder):
        lock_dir = swagger_locks.lock_dir_for(folder)
        lock_dir.mkdir(parents=True, exist_ok=True)
        holder = OsLock(lock_dir / swagger_locks.WRITE_LOCK_NAME)
        assert holder.try_acquire()
        threading.Timer(0.3, holder.release).start()
        content, service = snaps.fetch_and_validate(
            "admin", session=FakeSession(ok(spec(12))), timeout=(1, 2)
        )
        assert snaps.write_snapshot(
            "admin", content, service, directory=folder, lock_wait_s=10
        ).changed

    def test_a_lock_held_by_another_process_blocks_writing(self, folder):
        lock_file = swagger_locks.lock_dir_for(folder) / swagger_locks.WRITE_LOCK_NAME
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import sys, time
                    sys.path.insert(0, {str(REPO_ROOT)!r})
                    from src.execution_gate import OsLock
                    lock = OsLock({str(lock_file)!r})
                    assert lock.try_acquire()
                    print("HELD", flush=True)
                    time.sleep(60)
                    """
                ),
            ],
            stdout=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "ENV_FILE": str(folder / "missing.env"),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        try:
            assert child.stdout.readline().strip() == "HELD"
            content, service = snaps.fetch_and_validate(
                "admin", session=FakeSession(ok(spec(12))), timeout=(1, 2)
            )
            with pytest.raises(snaps.SwaggerRefreshError) as caught:
                snaps.write_snapshot("admin", content, service, directory=folder, lock_wait_s=0.3)
            assert caught.value.kind == snaps.KIND_LOCKED
        finally:
            child.kill()
            child.wait(30)
            child.stdout.close()
        assert snaps.write_snapshot(
            "admin", content, service, directory=folder, lock_wait_s=10
        ).changed


def test_the_base_url_is_configurable_and_url_matching_ignores_the_scheme(tmp_path):
    environment = {
        k: v for k, v in os.environ.items() if not k.startswith(("SWAGGER_", "LLM_", "EMBEDDING_"))
    }
    environment.update(
        ENV_FILE=str(tmp_path / "missing.env"),
        PYTHONDONTWRITEBYTECODE="1",
        SWAGGER_API_DOCS_BASE_URL="https://docs.podium.test/v3/api-docs/",
    )
    code = textwrap.dedent(
        """
        from src.agents.test_case_generator import swagger_snapshots as s
        assert s.source_for("admin").remote_url == "https://docs.podium.test/v3/api-docs/Admin"
        assert s.source_for("site").remote_url == "https://docs.podium.test/v3/api-docs/Site"
        assert s.source_for_url("http://docs.podium.test/v3/api-docs/Admin").key == "admin"
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr[-2000:]


def test_the_default_base_url_is_unchanged():
    assert (
        snaps.source_for("admin").remote_url == "http://podium-back.devpod.ir/api/v3/api-docs/Admin"
    )
