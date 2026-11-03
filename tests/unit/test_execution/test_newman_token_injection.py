"""
تزریقِ توکنِ زمانِ اجرا با Newmanِ واقعی — سرورِ HTTPِ محلی، بدونِ هیچ API یا توکنِ واقعی.

ثابت می‌کند که «Bearer <token>» ِ request.access_token بر token ِ خالیِ Environment،
Globals و متغیرِ کالکشن مقدم است (envVarِ Newman پس از بارکردنِ فایلِ Environment
اعمال می‌شود)، و فایل‌های ورودی بایت‌به‌بایت دست نمی‌خورند. اگر Node یا Newman نصب
نباشد، رد می‌شوند.
"""

from __future__ import annotations

import json
import threading
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

ACCESS_TOKEN = "SYNTHETIC-INJECTED-ACCESS-4d71b0"  # ساختگی


class _Recorder(BaseHTTPRequestHandler):
    seen: list[str | None] = []
    lock = threading.Lock()

    def do_GET(self):  # noqa: N802 — نامِ BaseHTTPRequestHandler
        with _Recorder.lock:
            _Recorder.seen.append(self.headers.get("Authorization"))
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Recorder.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _inputs(tmp_path: Path, base_url: str, *, env_token: str = "") -> tuple[Path, Path, Path]:
    case = {
        "name": "TC-001 — read",
        "type": "positive",
        "method": "GET",
        "path": "/ok",
        "description": "",
        "headers": {},
        "query_params": {},
        "path_params": {},
        "body": None,
        "expected_status": 200,
        "assertions": [{"type": "status_code", "expected": 200}],
        "save_variables": [],
        "assumptions": [],
        "base_url_var": "base_url",
        "base_path": "/api",
        "required_variables": [],
    }
    collection = PostmanBuilder().build(
        suite={"controllers": [{"name": "Token", "test_cases": [case]}]},
        collection_name="token-injection",
    )
    # همان شکلِ فایل‌های واقعی: token در کالکشن، Environment و Globals خالی است
    assert {"key": "token", "value": ""} in collection["variable"]
    environment = {
        "name": "dev",
        "_postman_variable_scope": "environment",
        "values": [
            {"key": "base_url", "value": base_url, "enabled": True},
            {"key": "base_url_vdi", "value": base_url, "enabled": True},
            {"key": "token", "value": env_token, "type": "any", "enabled": True},
        ],
    }
    globals_doc = {
        "name": "Globals",
        "_postman_variable_scope": "globals",
        "values": [{"key": "token", "value": "", "enabled": True}],
    }
    paths = (tmp_path / "collection.json", tmp_path / "dev.json", tmp_path / "globals.json")
    for path, document in zip(paths, (collection, environment, globals_doc)):
        path.write_text(json.dumps(document), encoding="utf-8")
    return paths


def _run(tmp_path: Path, base_url: str, token: str | None, *, env_token: str = ""):
    paths = _inputs(tmp_path, base_url, env_token=env_token)
    before = {path: path.read_bytes() for path in paths}
    request = NewmanRunRequest(
        *paths, tmp_path / "out", NewmanOptions(process_timeout_s=120), access_token=token
    )
    result = NewmanExecutionEngine(scratch_root=tmp_path / "scratch").run(request)
    for path, content in before.items():
        assert path.read_bytes() == content  # Environment/Globals/collection دست‌نخورده
    return result


def _output_text(tmp_path: Path) -> str:
    return "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (tmp_path / "out").rglob("*")
        if path.is_file()
    )


def test_newman_version_is_the_one_the_precedence_was_proven_for():
    assert INSTALL.newman_version == "6.2.3"


def test_without_injection_the_empty_environment_token_is_sent(tmp_path, server):
    result = _run(tmp_path, server, None)
    assert result.status == "PASS", result.error_message
    assert _Recorder.seen in ([""], [None])  # کنترل: فایل‌ها واقعاً token ِ خالی دارند


def test_the_runtime_token_overrides_the_empty_environment_token(tmp_path, server):
    result = _run(tmp_path, server, ACCESS_TOKEN)
    assert result.status == "PASS", result.error_message
    assert _Recorder.seen == [f"Bearer {ACCESS_TOKEN}"]  # «Bearer » دقیقاً یک بار
    assert ACCESS_TOKEN not in _output_text(tmp_path)
    assert list((tmp_path / "scratch").iterdir()) == []


def test_the_runtime_token_also_overrides_a_non_empty_environment_value(tmp_path, server):
    result = _run(tmp_path, server, ACCESS_TOKEN, env_token="SYNTHETIC-STALE-FILE-VALUE")
    assert result.status == "PASS", result.error_message
    assert _Recorder.seen == [f"Bearer {ACCESS_TOKEN}"]
