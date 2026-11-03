"""
تست‌های یکپارچه‌ی موتورِ Newman — Node و Newmanِ واقعیِ نصب‌شده، سرورِ HTTPِ محلی.

کالکشن‌ها با PostmanBuilderِ واقعیِ پروژه ساخته می‌شوند ({{base_url}} + /api).
environment و globals فایل‌های موقتِ تست‌اند که به 127.0.0.1 اشاره می‌کنند — هیچ
API یا توکنِ واقعی‌ای به کار نمی‌رود. اگر Node یا Newman نصب نباشد، رد می‌شوند.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.agents.test_case_generator.postman_builder import PostmanBuilder
from src.test_execution.newman import NewmanExecutionEngine, NewmanOptions, NewmanRunRequest
from src.test_execution.newman.locator import locate

INSTALL = locate()
pytestmark = pytest.mark.skipif(
    not INSTALL.ok, reason=f"Newman is not available: {INSTALL.error_message}"
)

TOKEN = "INTEG-SECRET-TOKEN-91f0c2"
PASSWORD = "INTEG-SECRET-PASSWORD-77ab"
COOKIE = "INTEG-SECRET-COOKIE-3d3d"
SECRETS = (TOKEN, PASSWORD, COOKIE)


class _Handler(BaseHTTPRequestHandler):
    routes = {"/api/ok": 200, "/api/unauthorized": 401, "/api/forbidden": 403, "/api/broken": 500}
    # set when /api/slow arrives — proves every earlier request already completed
    slow_seen = threading.Event()

    def do_GET(self):  # noqa: N802 — نامِ BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0]
        if path == "/api/slow":
            _Handler.slow_seen.set()
            time.sleep(60)
        code = self.routes.get(path, 200 if path == "/api/slow" else 404)
        # پاسخ عمداً مقدارِ محرمانه را برمی‌گرداند: هدرِ Authorization و کوکی.
        body = json.dumps(
            {"id": 7, "echo": self.headers.get("Authorization"), "secret": PASSWORD}
        ).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", f"SESSION={COOKIE}; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _case(name, path, expected=None, *, host="base_url", query=None, required=None, saves=None):
    return {
        "name": name,
        "type": "positive",
        "method": "GET",
        "path": path,
        "description": "",
        "headers": {},
        "query_params": query or {},
        "path_params": {},
        "body": None,
        "expected_status": expected or 0,
        "assertions": [{"type": "status_code", "expected": expected}] if expected else [],
        "save_variables": saves or [],
        "assumptions": [],
        "base_url_var": host,
        "base_path": "/api",
        "required_variables": required or [],
    }


def _inputs(
    tmp_path: Path,
    cases: list[dict],
    base_url: str,
    vdi_url: str | None = None,
    *,
    token: str | None = TOKEN,
    edit=None,
):
    collection = PostmanBuilder().build(
        suite={"controllers": [{"name": "Integration", "test_cases": cases}]},
        collection_name="integration",
    )
    environment = {
        "name": "dev",
        "_postman_variable_scope": "environment",
        "values": [
            {"key": "base_url", "value": base_url, "enabled": True},
            {"key": "base_url_vdi", "value": vdi_url or base_url, "enabled": True},
        ]
        + ([{"key": "token", "value": token, "enabled": True}] if token is not None else []),
    }
    if edit is not None:
        edit(collection)
    globals_doc = {
        "name": "Globals",
        "_postman_variable_scope": "globals",
        "values": [{"key": "password", "value": PASSWORD, "enabled": True}],
    }
    # کالکشن token را خالی تعریف کرده (اسکوپِ collection بر globals مقدم است، نه بر
    # environment)؛ environment مقدارِ تست را می‌دهد.
    paths = []
    for name, document in (
        ("collection.json", collection),
        ("dev.json", environment),
        ("globals.json", globals_doc),
    ):
        path = tmp_path / name
        path.write_text(json.dumps(document), encoding="utf-8")
        paths.append(path)
    return paths


def _run(tmp_path, cases, base_url, *, vdi_url=None, options=None, token=TOKEN, edit=None):
    collection, environment, globals_path = _inputs(
        tmp_path, cases, base_url, vdi_url, token=token, edit=edit
    )
    request = NewmanRunRequest(
        collection, environment, globals_path, tmp_path / "out", options or NewmanOptions()
    )
    return NewmanExecutionEngine().run(request)


def _artifacts(result) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in Path(result.artifacts.run_dir).iterdir()
    }


def _process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        output = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
        ).stdout
        return str(pid) in output
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _children(pid: int) -> list[int]:
    if sys.platform != "win32":
        output = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False
        ).stdout
        return [int(line) for line in output.split()]
    command = f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}').ProcessId"
    output = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return [int(line) for line in output.split() if line.strip().isdigit()]


class TestRealNewman:
    def test_401_403_and_500_are_judged_only_by_assertions(self, tmp_path, server):
        result = _run(
            tmp_path,
            [
                _case("TC-001 — ok", "/ok", 200),
                _case("TC-002 — expected 401", "/unauthorized", 401),
                _case("TC-003 — expected 500", "/broken", 500),
            ],
            server,
        )
        assert (result.status, result.error_kind) == ("PASS", "")
        assert [r.status_code for r in result.requests] == [200, 401, 500]
        assert result.stats.assertions_total == 3

    def test_a_403_with_a_failing_assertion_is_fail(self, tmp_path, server):
        result = _run(
            tmp_path, [_case("TC-001", "/ok", 200), _case("TC-002", "/forbidden", 200)], server
        )
        assert result.status == "FAIL"
        failed = result.requests[1].assertions[0]
        assert failed.passed is False
        assert "403" in failed.message

    def test_a_refused_connection_with_assertion_failures_is_error(self, tmp_path, server):
        result = _run(
            tmp_path,
            [_case("TC-001", "/forbidden", 200), _case("TC-002", "/ok", 200, host="base_url_vdi")],
            server,
            vdi_url=f"http://127.0.0.1:{_closed_port()}",
        )
        assert (result.status, result.error_kind) == ("ERROR", "NETWORK_ERROR")
        assert result.requests[0].assertions[0].passed is False
        assert result.requests[1].status_code is None
        assert "ECONNREFUSED" in result.requests[1].errors[0]["message"]

    def test_a_collection_without_assertions_is_a_flagged_pass(self, tmp_path, server):
        result = _run(tmp_path, [_case("TC-001", "/ok")], server)
        assert (result.status, result.no_assertions) == ("PASS", True)
        assert "No assertions were executed." in _artifacts(result)["newman-report.html"]

    def test_a_missing_dependency_skips_the_request_and_fails(self, tmp_path, server):
        required = [{"variable": "voucherId", "scope": "collection", "producer": "TC-000"}]
        result = _run(tmp_path, [_case("TC-001", "/ok", 200, required=required)], server)
        assert result.status == "FAIL"
        assert result.requests[0].skipped is True
        assert "voucherId" in result.requests[0].skip_reason

    def test_no_secret_reaches_any_artifact(self, tmp_path, server):
        result = _run(
            tmp_path,
            [_case("TC-001", "/ok", 200, query={"token": "{{token}}", "page": "1"})],
            server,
        )
        assert result.status == "PASS"
        texts = _artifacts(result)
        assert set(texts) == {
            "newman-report.json",
            "newman-report.html",
            "stdout.log",
            "stderr.log",
            "result.json",
        }
        for name, text in texts.items():
            for secret in SECRETS:
                assert secret not in text, (name, secret[:12])
        assert result.requests[0].url.endswith("/api/ok?token=***&page=1")
        assert "INTEG-SECRET" not in json.dumps(result.to_dict())

    def test_a_timeout_kills_the_process_tree_and_keeps_partial_results(self, tmp_path, server):
        result = _run(
            tmp_path,
            [_case("TC-001", "/ok", 200), _case("TC-002", "/slow", 200)],
            server,
            options=NewmanOptions(process_timeout_s=8, request_timeout_ms=120_000),
        )
        assert (result.status, result.error_kind, result.timed_out) == (
            "ERROR",
            "PROCESS_TIMEOUT",
            True,
        )
        assert result.requests[0].status_code == 200 and result.requests[0].completed
        assert result.requests[0].assertions[0].passed is True
        assert result.requests[1].completed is False
        assert result.process_id
        time.sleep(1)
        assert not _process_alive(result.process_id)
        assert _children(result.process_id) == []
        texts = _artifacts(result)
        assert "newman-report.json" not in texts  # Newman never wrote it
        assert json.loads(texts["result.json"])["error_kind"] == "PROCESS_TIMEOUT"


def _whole_token(text: str, secret: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])", text) is not None


class TestAuditScenarios:
    def test_a_script_skip_without_a_failed_check_is_error(self, tmp_path, server):
        def skip_second(collection):
            item = collection["item"][0]["item"][1]
            item.setdefault("event", []).insert(
                0,
                {
                    "listen": "prerequest",
                    "script": {"type": "text/javascript", "exec": ["pm.execution.skipRequest();"]},
                },
            )

        result = _run(
            tmp_path,
            [_case("TC-001", "/ok", 200), _case("TC-002", "/ok", 200)],
            server,
            edit=skip_second,
        )
        assert (result.status, result.error_kind) == ("ERROR", "REQUEST_NOT_EXECUTED")
        assert result.requests[1].skip_kind == "unexplained"
        assert result.requests[0].assertions[0].passed is True

    def test_a_collection_variable_secret_never_reaches_artifacts(self, tmp_path, server):
        secret = "COLL-INTEG-SECRET-TOKEN-ab12"

        def collection_token(collection):
            for variable in collection["variable"]:
                if variable["key"] == "token":
                    variable["value"] = secret

        result = _run(
            tmp_path, [_case("TC-001", "/ok", 200)], server, token=None, edit=collection_token
        )
        assert result.status == "PASS"
        for name, text in _artifacts(result).items():
            assert secret not in text, name

    @pytest.mark.parametrize("secret", ["Q", "x9", "k7Z"])
    def test_short_secrets_never_reach_artifacts(self, tmp_path, server, secret):
        result = _run(
            tmp_path,
            [_case("TC-001", "/ok", 200, query={"token": "{{token}}"})],
            server,
            token=secret,
        )
        assert result.status == "PASS", result.error_message
        assert result.requests[0].url.endswith("/api/ok?token=***")
        for name, text in _artifacts(result).items():
            body = (
                json.dumps(json.loads(text), ensure_ascii=False) if name.endswith(".json") else text
            )
            assert not _whole_token(body, secret), name

    def test_newman_stops_when_its_parent_disappears(self, tmp_path, server):
        """Python-side crash: runner.js must not keep running as an orphan."""
        from src.test_execution.newman.scratch import create_private_dir, remove_private_dir

        collection, environment, globals_path = _inputs(
            tmp_path, [_case("TC-001", "/slow", 200)], server
        )
        private = create_private_dir(tmp_path / "scratch")
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            config = {
                "newman_module": INSTALL.newman_module,
                "collection": str(collection),
                "environment": str(environment),
                "globals": str(globals_path),
                "timeout_request_ms": 120_000,
                "delay_request_ms": 0,
                "bail": False,
                "insecure": False,
                "json_report": str(private / "r.json"),
                "html_report": str(private / "r.html"),
                "events_path": str(private / "events.ndjson"),
                "htmlextra": {},
                "parent_pid": parent.pid,
            }
            (private / "config.json").write_text(json.dumps(config), encoding="utf-8")
            runner = subprocess.Popen(
                [INSTALL.node, "--no-deprecation", INSTALL.runner, str(private / "config.json")],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(3)
            assert runner.poll() is None  # busy with the slow request
            parent.kill()
            parent.wait(timeout=30)
            assert runner.wait(timeout=20) == 5
        finally:
            if parent.poll() is None:
                parent.kill()
            if "runner" in locals() and runner.poll() is None:
                runner.kill()
            remove_private_dir(private)


class TestRealCancellation:
    """Milestone 4.2 — cancel_event against the real Newman process."""

    @staticmethod
    def _run_in_thread(tmp_path, cases, server, event, **kwargs):
        holder: dict = {}

        def target():
            collection, environment, globals_path = _inputs(tmp_path, cases, server)
            request = NewmanRunRequest(
                collection,
                environment,
                globals_path,
                tmp_path / "out",
                kwargs.get("options")
                or NewmanOptions(process_timeout_s=120, request_timeout_ms=120_000),
            )
            holder["result"] = NewmanExecutionEngine().run(request, cancel_event=event)

        thread = threading.Thread(target=target)
        thread.start()
        return thread, holder

    def test_cancel_during_a_slow_request_keeps_partial_results(self, tmp_path, server):
        event = threading.Event()
        _Handler.slow_seen.clear()
        started = time.monotonic()
        thread, holder = self._run_in_thread(
            tmp_path, [_case("TC-001", "/ok", 200), _case("TC-002", "/slow", 200)], server, event
        )
        # TC-001 is done once TC-002 reaches /slow (which would block for 60 s)
        assert _Handler.slow_seen.wait(timeout=60)
        time.sleep(0.5)
        event.set()
        thread.join(timeout=60)
        assert not thread.is_alive()
        result = holder["result"]
        assert (result.status, result.error_kind, result.cancelled, result.timed_out) == (
            "ERROR",
            "PROCESS_CANCELLED",
            True,
            False,
        )
        assert time.monotonic() - started < 40  # far below the 120 s process timeout
        assert result.requests[0].status_code == 200 and result.requests[0].completed
        assert result.requests[0].assertions[0].passed is True
        assert result.requests[1].completed is False
        time.sleep(1)
        assert not _process_alive(result.process_id)
        assert _children(result.process_id) == []
        texts = _artifacts(result)
        assert "newman-report.json" not in texts  # Newman never finished its report
        for name, text in texts.items():
            for secret in SECRETS:
                assert secret not in text, (name, secret[:12])

    def test_cancel_right_after_startup_leaves_no_process(self, tmp_path, server):
        event = threading.Event()
        thread, holder = self._run_in_thread(
            tmp_path, [_case("TC-001", "/slow", 200)], server, event
        )
        time.sleep(0.5)
        event.set()
        thread.join(timeout=60)
        result = holder["result"]
        assert result.error_kind == "PROCESS_CANCELLED"
        if result.process_id:
            time.sleep(1)
            assert not _process_alive(result.process_id)
            assert _children(result.process_id) == []

    def test_a_cancel_event_that_is_never_set_changes_nothing(self, tmp_path, server):
        event = threading.Event()
        thread, holder = self._run_in_thread(
            tmp_path,
            [_case("TC-001", "/ok", 200), _case("TC-002", "/forbidden", 200)],
            server,
            event,
        )
        thread.join(timeout=120)
        result = holder["result"]
        event.set()  # arriving after completion: the result is final
        assert (result.status, result.cancelled, result.timed_out) == ("FAIL", False, False)
        assert "newman-report.json" in _artifacts(result)
