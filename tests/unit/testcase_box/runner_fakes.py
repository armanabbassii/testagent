"""
tests/unit/testcase_box/runner_fakes.py — موتورِ ساختگیِ Newman، منبعِ توکنِ ساختگی و پیکربندیِ موقت

موتورِ ساختگی هیچ پروسه، Node یا شبکه‌ای ندارد: فقط NewmanRunResult می‌سازد و مثلِ موتورِ واقعی
فایل‌های «منتشرشده» را زیرِ request.output_dir می‌نویسد.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.auth import AccessToken, AuthenticationError
from src.test_execution.newman.models import (
    ERROR_PROCESS_CANCELLED,
    Artifacts,
    NewmanRunResult,
    RequestResult,
    RunStats,
)

TOKEN = "synthetic-access-token-0123456789abcdef"
ENV_SECRET = "env-password-value-0123456789"
WAIT_S = 20.0


def write_configuration(directory: Path, *, values: list[dict] | None = None) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    environment, globals_path = directory / "dev.json", directory / "globals.json"
    environment.write_text(
        json.dumps(
            {
                "name": "dev",
                "_postman_variable_scope": "environment",
                "values": values
                if values is not None
                else [
                    {"key": "base_url", "value": "http://podium.test", "enabled": True},
                    {"key": "base_url_vdi", "value": "http://podium-vdi.test", "enabled": True},
                    {"key": "userId", "value": "42", "enabled": True},
                    {"key": "password", "value": ENV_SECRET, "enabled": True},
                    {"key": "token", "value": "", "enabled": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    globals_path.write_text(
        json.dumps({"name": "Globals", "_postman_variable_scope": "globals", "values": []}),
        encoding="utf-8",
    )
    return environment, globals_path


def published(
    request,
    *,
    status: str = "PASS",
    error_kind: str = "",
    no_assertions: bool = True,
    exit_code: int | None = 0,
    cancelled: bool = False,
    timed_out: bool = False,
    run_id: str | None = None,
    artifacts: bool = True,
    status_code: int | None = 200,
) -> NewmanRunResult:
    run_id = run_id or f"20261107T000000Z_{uuid.uuid4().hex[:8]}"
    result_artifacts = Artifacts()
    if artifacts:
        run_dir = Path(request.output_dir) / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text("{}", encoding="utf-8")
        result_artifacts = Artifacts(run_dir=str(run_dir), result_json=str(run_dir / "result.json"))
    return NewmanRunResult(
        run_id=run_id,
        status=status,
        error_kind=error_kind,
        no_assertions=no_assertions and status == "PASS",
        exit_code=exit_code,
        process_id=4242,
        cancelled=cancelled,
        timed_out=timed_out,
        stats=RunStats(
            requests_executed=1,
            assertions_total=0 if no_assertions else 1,
            assertions_failed=1 if status == "FAIL" else 0,
        ),
        requests=[RequestResult(index=0, name="BX", status_code=status_code, executed=True)],
        artifacts=result_artifacts,
    )


def outcome(**kwargs: Any) -> Callable:
    return lambda request, cancel: published(request, **kwargs)


@dataclass
class Call:
    request: Any
    cancel_event: threading.Event | None
    collection: dict


class FakeEngine:
    """رفتارِ هر فراخوانی از behaviours (به ترتیب)؛ پیش‌فرض PASS ِ بی‌assertion."""

    def __init__(self, behaviours: list | None = None, on_call: Callable | None = None) -> None:
        self.behaviours = list(behaviours or [])
        self.on_call = on_call
        self.calls: list[Call] = []
        self.active = self.max_active = 0
        self._lock = threading.Lock()

    def run(self, request, cancel_event=None) -> NewmanRunResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            collection = json.loads(Path(request.collection_path).read_text(encoding="utf-8"))
            self.calls.append(Call(request, cancel_event, collection))
            if self.on_call is not None:
                self.on_call(request)
            behaviour = self.behaviours.pop(0) if self.behaviours else outcome()
            return behaviour(request, cancel_event)
        finally:
            with self._lock:
                self.active -= 1


def blocking(
    started: threading.Event, release: threading.Event, *, confirmed: bool = True
) -> Callable:
    """تا آزادسازی (→ PASS) یا لغو (→ نتیجه‌ی لغوِ موتور) منتظر می‌ماند — بی‌sleepِ دلبخواه."""

    def run(request, cancel):
        started.set()
        deadline = time.monotonic() + WAIT_S
        while not cancel.wait(0.005):
            if release.is_set():
                return published(request)
            if time.monotonic() > deadline:
                raise TimeoutError("the blocking fake was never released")
        return published(
            request,
            status="ERROR",
            error_kind=ERROR_PROCESS_CANCELLED,
            cancelled=True,
            exit_code=1 if confirmed else None,
        )

    return run


class FakeTokens:
    def __init__(self, failure: Exception | None = None, on_fetch: Callable | None = None) -> None:
        self.failure, self.on_fetch, self.calls = failure, on_fetch, 0

    def fetch(self) -> AccessToken:
        self.calls += 1
        if self.on_fetch is not None:
            self.on_fetch()
        if self.failure is not None:
            raise self.failure
        return AccessToken(TOKEN)


AUTH_FAILURE = AuthenticationError("rejected", "The token endpoint rejected the refresh token.")


def wait_for(predicate: Callable[[], bool], timeout: float = WAIT_S) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("The condition was not reached in time.")
        time.sleep(0.005)
