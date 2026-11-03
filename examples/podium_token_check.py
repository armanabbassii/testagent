"""
examples/podium_token_check.py — بررسیِ دستیِ TokenProvider روی endpointِ واقعیِ Dev

فقط با اجرای صریحِ کاربر؛ pytest آن را جمع نمی‌کند (testpaths = tests). هیچ توکن، بدنه‌ی
پاسخ یا مقدارِ .env چاپ نمی‌شود — فقط بله/خیر، نوعِ خطا و هش‌ها.

    python examples/podium_token_check.py                 # خشک: فقط پیکربندی، هیچ درخواستی
    python examples/podium_token_check.py --send          # + یک POST به PODIUM_TOKEN_REFRESH_URL
    python examples/podium_token_check.py --send --newman # + Newmanِ واقعی روی سرورِ محلی

--newman هیچ APIِ کسب‌وکاری را صدا نمی‌زند: Newman با کپیِ موقتِ Environment/Globals اجرا
می‌شود که base_urlشان به یک سرورِ ضبط‌کننده روی 127.0.0.1 اشاره می‌کند؛ سرور فقط بررسی
می‌کند هدرِ Authorization دقیقاً «Bearer <همان توکن>» است. فایل‌های واقعیِ Environment و
Globals با SHA-256 پیش و پس مقایسه می‌شوند.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.auth import AuthenticationError, TokenProvider, TokenSettings  # noqa: E402
from src.test_execution.newman import (  # noqa: E402
    DEV_ENVIRONMENT_PATH,
    WORKSPACE_GLOBALS_PATH,
    NewmanExecutionEngine,
    NewmanOptions,
    NewmanRunRequest,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_configuration() -> TokenSettings | None:
    settings = TokenSettings.from_config()
    try:
        settings.validate()
    except AuthenticationError as exc:
        print(f"[config] FAILED ({exc.kind}): {exc}")
        return None
    scheme = urlsplit(settings.refresh_url).scheme
    print(f"[config] OK - scheme={scheme}, refresh token set=yes")
    if scheme == "http":
        print("[config] WARNING: http:// - credentials travel unencrypted (approved Dev only).")
    return settings


def _fetch() -> str | None:
    try:
        access = TokenProvider().fetch()
    except AuthenticationError as exc:
        # پیامِ AuthenticationError ثابت و بی‌راز است (وضعیتِ HTTP، نوعِ خطا)
        print(f"[refresh] FAILED ({exc.kind}): {exc}")
        return None
    print("[refresh] OK - HTTP 200, result.token is a non-empty string (value not shown)")
    return access.value


class _Recorder(BaseHTTPRequestHandler):
    seen: list[str | None] = []

    def do_GET(self):  # noqa: N802
        _Recorder.seen.append(self.headers.get("Authorization"))
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _local_copy(source: Path, target: Path, base_url: str) -> None:
    """کپیِ موقت با میزبانِ محلی؛ بقیه‌ی مقدارها (از جمله token ِ خالی) همان‌اند."""
    document = json.loads(source.read_text(encoding="utf-8-sig"))
    for entry in document.get("values", []):
        if entry.get("key") in ("base_url", "base_url_vdi"):
            entry["value"] = base_url
    target.write_text(json.dumps(document), encoding="utf-8")


def _check_newman(token: str) -> bool:
    from src.agents.test_case_generator.postman_builder import PostmanBuilder

    real_files = (DEV_ENVIRONMENT_PATH, WORKSPACE_GLOBALS_PATH)
    before = {path: _sha256(path) for path in real_files}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    ok = True
    try:
        with tempfile.TemporaryDirectory(prefix="podium-token-check-") as temp:
            work = Path(temp)
            case = {
                "name": "TC-001 — token check",
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
                suite={"controllers": [{"name": "Check", "test_cases": [case]}]},
                collection_name="token-check",
            )
            collection_path = work / "collection.json"
            collection_path.write_text(json.dumps(collection), encoding="utf-8")
            environment, globals_path = work / "dev.json", work / "globals.json"
            _local_copy(DEV_ENVIRONMENT_PATH, environment, base_url)
            _local_copy(WORKSPACE_GLOBALS_PATH, globals_path, base_url)
            inputs = (collection_path, environment, globals_path)
            inputs_before = {path: _sha256(path) for path in inputs}

            result = NewmanExecutionEngine(scratch_root=work / "scratch").run(
                NewmanRunRequest(
                    *inputs,
                    work / "out",
                    NewmanOptions(process_timeout_s=120),
                    access_token=token,
                )
            )
            print(f"[newman] status={result.status} error_kind={result.error_kind or '-'}")
            received = _Recorder.seen == [f"Bearer {token}"]
            print(f"[newman] server received exactly 'Bearer <token>': {received}")
            leaked = [
                path.name
                for path in (work / "out").rglob("*")
                if path.is_file() and token in path.read_text(encoding="utf-8", errors="ignore")
            ]
            print(f"[newman] token found in output files: {leaked or 'none'}")
            unchanged = all(_sha256(path) == digest for path, digest in inputs_before.items())
            print(f"[newman] run inputs unchanged: {unchanged}")
            ok = result.status == "PASS" and received and not leaked and unchanged
    finally:
        httpd.shutdown()
    untouched = all(_sha256(path) == digest for path, digest in before.items())
    print(f"[files] real Environment/Globals unchanged (SHA-256): {untouched}")
    return ok and untouched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--send", action="store_true", help="call the configured refresh URL")
    parser.add_argument("--newman", action="store_true", help="also run Newman locally")
    args = parser.parse_args()

    if _check_configuration() is None:
        return 1
    if not args.send:
        print("[refresh] dry run - no request sent (add --send to call the endpoint)")
        return 0
    token = _fetch()
    if token is None:
        return 1
    if args.newman and not _check_newman(token):
        return 1
    print("[done] all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
