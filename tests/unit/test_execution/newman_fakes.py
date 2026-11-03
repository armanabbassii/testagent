"""
newman_fakes.py — ورودی‌ها و runnerِ ساختگی برای تست‌های موتورِ Newman

FakeRunner همان کاری را می‌کند که runner.js می‌کند: config را از آخرین آرگومان
می‌خواند و فایلِ رخداد، گزارشِ JSON و HTML را در همان پوشه‌ی موقتِ خصوصی
می‌نویسد. هیچ Node یا شبکه‌ای لازم نیست.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.test_execution.newman.locator import NewmanInstallation
from src.test_execution.newman.process import ProcessOutcome

INSTALLATION = NewmanInstallation(
    node="node",
    node_version="v24.18.0",
    newman_module="node_modules/newman",
    newman_version="6.2.3",
    htmlextra_version="1.23.1",
)

TOKEN = "SECRET-ENV-TOKEN-7f3a91"
PASSWORD = "SECRET-GLOBAL-PASS-b2c4"
COOKIE = "SECRET-COOKIE-VALUE-55aa"
QUERY_SECRET = "SECRET-QUERY-API-KEY-0042"
ALL_SECRETS = (TOKEN, PASSWORD, COOKIE, QUERY_SECRET)


def request_item(name: str, path: str = "/voucher", host: str = "{{base_url}}") -> dict:
    return {
        "name": name,
        "request": {
            "method": "GET",
            "header": [{"key": "Authorization", "value": "{{token}}"}],
            "url": {
                "raw": f"{host}/api{path}",
                "host": [host],
                "path": ["api", *path.strip("/").split("/")],
            },
        },
    }


def write_inputs(
    directory: Path,
    *,
    items: list[dict] | None = None,
    env_values: list[dict] | None = None,
    globals_values: list[dict] | None = None,
    collection_variables: list[dict] | None = None,
) -> tuple[Path, Path, Path]:
    collection = {
        "info": {
            "name": "c",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": [{"name": "Folder", "item": items or [request_item("TC-001 — Read")]}],
        "variable": collection_variables
        if collection_variables is not None
        else [{"key": "token", "value": ""}],
    }
    environment = {
        "name": "dev",
        "_postman_variable_scope": "environment",
        "values": env_values
        if env_values is not None
        else [
            {"key": "base_url", "value": "http://podium.test", "enabled": True},
            {"key": "base_url_vdi", "value": "http://podium-vdi.test", "enabled": True},
            {"key": "token", "value": TOKEN, "enabled": True},
        ],
    }
    globals_doc = {
        "name": "Globals",
        "_postman_variable_scope": "globals",
        "values": globals_values
        if globals_values is not None
        else [{"key": "password", "value": PASSWORD, "enabled": True}],
    }
    paths = (directory / "collection.json", directory / "dev.json", directory / "globals.json")
    for path, document in zip(paths, (collection, environment, globals_doc)):
        path.write_text(json.dumps(document), encoding="utf-8")
    return paths


# ── رخدادها و گزارشِ خامِ ساختگی ────────────────────────────────────────────


def item_events(
    index: int,
    name: str,
    *,
    status: int | None = 200,
    assertions: list[tuple[str, str | None]] = (),
    network_error: str | None = None,
    sent: bool = True,
    done: bool = True,
    url: str = "http://podium.test/api/voucher",
) -> list[dict]:
    base = {"item_id": f"id-{index}", "item_name": name, "index": index}
    events = [{"type": "item_start", **base}]
    if sent:
        events.append(
            {
                "type": "request",
                **base,
                "method": "GET",
                "url": url,
                "status_code": None if network_error else status,
                "status_text": None
                if network_error
                else {200: "OK", 401: "Unauthorized"}.get(status, "X"),
                "response_time_ms": None if network_error else 12,
                "error": {"name": "Error", "code": "ECONNREFUSED", "message": network_error}
                if network_error
                else None,
            }
        )
    for assertion, failure in assertions:
        events.append(
            {
                "type": "assertion",
                **base,
                "assertion": assertion,
                "skipped": False,
                "error": {"name": "AssertionError", "code": "", "message": failure}
                if failure
                else None,
            }
        )
    if done:
        events.append({"type": "item_done", **base})
    return events


def summary_for(events: list[dict], executions: list[dict] | None = None) -> dict:
    assertions = [e for e in events if e["type"] == "assertion"]
    requests = [e for e in events if e["type"] == "request"]
    return {
        "collection": {
            "info": {"name": "c"},
            "item": [],
            "variable": [{"key": "token", "value": ""}],
        },
        "environment": {
            "values": [
                {"key": "token", "value": TOKEN},
                {"key": "base_url", "value": "http://podium.test"},
            ]
        },
        "globals": {"values": [{"key": "password", "value": PASSWORD}]},
        "run": {
            "stats": {
                "requests": {"total": len(requests), "failed": 0},
                "assertions": {
                    "total": len(assertions),
                    "failed": sum(1 for a in assertions if a["error"]),
                },
            },
            "executions": executions or [],
            "failures": [],
            "error": None,
        },
    }


def secret_execution() -> dict:
    """یک execution با همه‌ی جاهایی که Newman ممکن است مقدارِ محرمانه بگذارد."""
    return {
        "item": {"name": "TC-001"},
        "request": {
            "method": "GET",
            "header": [
                {"key": "Authorization", "value": TOKEN},
                {"key": "Cookie", "value": f"SESSION={COOKIE}"},
                {"key": "Accept", "value": "application/json"},
            ],
            "url": {
                "host": ["podium", "test"],
                "query": [{"key": "api_key", "value": QUERY_SECRET}],
            },
            "body": {"mode": "raw", "raw": json.dumps({"password": PASSWORD})},
        },
        "response": {
            "code": 200,
            "header": [{"key": "Set-Cookie", "value": f"SESSION={COOKIE}; Path=/"}],
            "stream": {"type": "Buffer", "data": list(f"echo {TOKEN}".encode())},
        },
        "assertions": [{"assertion": "Status code is 200"}],
    }


@dataclass
class FakeRunner:
    """جای run_process — رخدادها/گزارش‌ها را می‌نویسد و ProcessOutcome برمی‌گرداند."""

    events: list[dict] = field(default_factory=list)
    summary: Any = "auto"  # "auto" | None (ننویس) | dict | str (متنِ خام)
    html: str | None = "<html><body><h1>Newman</h1></body></html>"
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    started: bool = True
    write_events: bool = True
    finish: bool = True
    failures: int | None = None
    done_error: dict | None = None
    side_effect: Callable[[dict], None] | None = None
    # پروسه با cancel_event بسته شد (لغوِ همکارانه)
    cancelled: bool = False
    calls: list[dict] = field(default_factory=list)

    def __call__(
        self, argv: list[str], timeout_s: float, cwd: str, **kwargs: Any
    ) -> ProcessOutcome:
        config = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        self.calls.append(
            {"argv": argv, "timeout_s": timeout_s, "config": config, "kwargs": kwargs}
        )
        if not self.started:
            return ProcessOutcome(
                started=False, error="FileNotFoundError: the process could not be started."
            )
        failed = sum(1 for e in self.events if e["type"] == "assertion" and e["error"])
        failed += sum(1 for e in self.events if e["type"] == "request" and e["error"])
        failures = failed if self.failures is None else self.failures
        records = [{"type": "start", "pid": 4242}, *self.events]
        if self.finish:
            records.append(
                {"type": "done", "error": self.done_error, "run_error": None, "failures": failures}
            )
        if self.write_events:
            Path(config["events_path"]).write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
            )
        if self.summary == "auto":
            Path(config["json_report"]).write_text(
                json.dumps(summary_for(self.events)), encoding="utf-8"
            )
        elif isinstance(self.summary, dict):
            Path(config["json_report"]).write_text(json.dumps(self.summary), encoding="utf-8")
        elif isinstance(self.summary, str):
            Path(config["json_report"]).write_text(self.summary, encoding="utf-8")
        if self.html is not None:
            Path(config["html_report"]).write_text(self.html, encoding="utf-8")
        if self.side_effect:
            self.side_effect(config)
        exit_code = self.exit_code
        if exit_code is None:
            if self.cancelled:
                exit_code = 1  # taskkill /F
            else:
                exit_code = (
                    None if self.timed_out else (2 if self.done_error else (1 if failures else 0))
                )
        return ProcessOutcome(
            started=True,
            exit_code=exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
            pid=4242,
            cancelled=self.cancelled,
        )
