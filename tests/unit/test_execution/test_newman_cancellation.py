"""
تست‌های لغوِ همکارانه‌ی موتورِ Newman (Milestone 4.2).

سه لایه:
  * run_process با پروسه‌های واقعیِ Python (بدونِ Newman) — تقدمِ پایان/لغو/timeout،
    بستنِ درختِ پروسه (نوه هم)، reap، خروجیِ جزئی؛
  * موتور با runnerِ ساختگی — طبقه‌بندی، نتایجِ جزئی، پاک‌سازی، سازگاریِ قبلی؛
  * Newmanِ واقعی روی سرورِ HTTPِ محلی (در test_newman_integration.py).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src.test_execution.newman import NewmanExecutionEngine, NewmanOptions, NewmanRunRequest
from src.test_execution.newman.process import run_process
from tests.unit.test_execution.newman_fakes import (
    INSTALLATION,
    QUERY_SECRET,
    TOKEN,
    FakeRunner,
    item_events,
    write_inputs,
)

PASSING = item_events(0, "TC-001 — Read", assertions=[("Status code is 200", None)])
PARTIAL = PASSING + item_events(1, "TC-002 — Slow", sent=False, done=False)


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        output = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
        ).stdout
        return re.search(rf"\b{pid}\b", output) is not None
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _python(code: str, *args: str) -> list[str]:
    return [sys.executable, "-c", code, *args]


def _set_later(event: threading.Event, delay: float, times: int = 1) -> threading.Thread:
    def fire():
        time.sleep(delay)
        for _ in range(times):
            event.set()

    thread = threading.Thread(target=fire, daemon=True)
    thread.start()
    return thread


def _engine_run(tmp_path: Path, runner: FakeRunner, event: threading.Event | None = None, **inputs):
    collection, environment, globals_path = write_inputs(tmp_path, **inputs)
    request = NewmanRunRequest(
        collection, environment, globals_path, tmp_path / "out", NewmanOptions()
    )
    engine = NewmanExecutionEngine(
        process_runner=runner, installation=INSTALLATION, scratch_root=tmp_path / "scratch"
    )
    return engine.run(request, cancel_event=event) if event is not None else engine.run(request)


def _texts(result) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in Path(result.artifacts.run_dir).iterdir()}


# ── run_process: پروسه‌های واقعی، بدونِ Newman ─────────────────────────────


class TestProcessCancellation:
    def test_cancel_during_a_slow_process_kills_it_and_keeps_its_output(self, tmp_path):
        event = threading.Event()
        _set_later(event, 1.0)
        started = time.monotonic()
        outcome = run_process(
            _python("import sys, time; print('partial', flush=True); time.sleep(60)"),
            timeout_s=60,
            cwd=str(tmp_path),
            cancel_event=event,
        )
        assert (outcome.cancelled, outcome.timed_out) == (True, False)
        assert time.monotonic() - started < 15
        assert "partial" in outcome.stdout
        assert outcome.exit_code is not None  # reaped
        assert not _alive(outcome.pid)

    def test_the_whole_process_tree_is_terminated(self, tmp_path):
        pid_file = tmp_path / "grandchild.pid"
        code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "time.sleep(120)\n"
        )
        event = threading.Event()

        def cancel_when_grandchild_exists():
            deadline = time.monotonic() + 30
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            time.sleep(0.3)
            event.set()

        threading.Thread(target=cancel_when_grandchild_exists, daemon=True).start()
        outcome = run_process(_python(code, str(pid_file)), 120, str(tmp_path), cancel_event=event)
        grandchild = int(pid_file.read_text())
        time.sleep(1)
        assert outcome.cancelled
        assert not _alive(outcome.pid)
        assert not _alive(grandchild)

    def test_cancel_immediately_after_startup(self, tmp_path):
        event = threading.Event()
        event.set()
        outcome = run_process(
            _python("import time; time.sleep(60)"), 60, str(tmp_path), cancel_event=event
        )
        assert outcome.started and outcome.cancelled and not outcome.timed_out
        assert not _alive(outcome.pid)

    def test_cancel_and_timeout_together_resolve_to_cancel(self, tmp_path):
        event = threading.Event()
        event.set()
        outcome = run_process(
            _python("import time; time.sleep(60)"), 0.0, str(tmp_path), cancel_event=event
        )
        assert (outcome.cancelled, outcome.timed_out) == (True, False)

    def test_a_timeout_without_cancel_is_still_a_timeout(self, tmp_path):
        outcome = run_process(
            _python("import time; time.sleep(60)"),
            0.5,
            str(tmp_path),
            cancel_event=threading.Event(),
        )
        assert (outcome.cancelled, outcome.timed_out) == (False, True)
        assert not _alive(outcome.pid)

    def test_a_process_that_already_finished_is_not_rewritten(self, tmp_path):
        event = threading.Event()
        _set_later(event, 3.0)
        outcome = run_process(_python("print('done')"), 60, str(tmp_path), cancel_event=event)
        assert (outcome.cancelled, outcome.timed_out, outcome.exit_code) == (False, False, 0)
        assert outcome.stdout.strip() == "done"

    def test_cancel_at_nearly_the_same_time_as_completion_is_consistent(self, tmp_path):
        event = threading.Event()
        _set_later(event, 0.5)
        outcome = run_process(
            _python("import time; time.sleep(0.5); print('done', flush=True)"),
            60,
            str(tmp_path),
            cancel_event=event,
        )
        # whichever the loop observes first — never both, never neither, always reaped
        if outcome.cancelled:
            assert not outcome.timed_out and not _alive(outcome.pid)
        else:
            assert outcome.exit_code == 0 and outcome.stdout.strip() == "done"

    def test_repeated_signals_while_terminating_are_harmless(self, tmp_path):
        event = threading.Event()
        _set_later(event, 0.5, times=50)
        outcome = run_process(
            _python("import time; time.sleep(60)"), 60, str(tmp_path), cancel_event=event
        )
        assert outcome.cancelled and not _alive(outcome.pid)

    def test_without_a_cancel_event_the_previous_path_runs(self, tmp_path):
        outcome = run_process(_python("print('x')"), 60, str(tmp_path))
        assert (outcome.cancelled, outcome.exit_code, outcome.stdout.strip()) == (False, 0, "x")


# ── موتور با runnerِ ساختگی ──────────────────────────────────────────────


class TestEngineCancellation:
    def test_cancel_before_the_process_starts(self, tmp_path):
        event = threading.Event()
        event.set()
        runner = FakeRunner(events=PASSING)
        result = _engine_run(tmp_path, runner, event)
        assert runner.calls == []
        assert (result.status, result.error_kind, result.cancelled, result.timed_out) == (
            "ERROR",
            "PROCESS_CANCELLED",
            True,
            False,
        )
        assert "before it started" in result.error_message
        assert result.requests == []
        assert "result.json" in _texts(result)
        assert "newman-report.json" not in _texts(result)

    def test_partial_results_survive_cancellation(self, tmp_path):
        runner = FakeRunner(
            events=PARTIAL,
            cancelled=True,
            finish=False,
            summary=None,
            html="<html><body><p>partial</p></body></html>",
        )
        result = _engine_run(tmp_path, runner, threading.Event())
        assert (result.status, result.error_kind, result.cancelled, result.timed_out) == (
            "ERROR",
            "PROCESS_CANCELLED",
            True,
            False,
        )
        assert "after 1 request(s) had run" in result.error_message
        assert result.requests[0].status_code == 200 and result.requests[0].completed
        assert result.requests[0].assertions[0].passed is True
        assert result.requests[1].completed is False
        assert (result.stats.requests_executed, result.stats.assertions_total) == (1, 1)
        texts = _texts(result)
        assert "newman-report.json" not in texts  # Newman never wrote it
        assert "partial" in texts["newman-report.html"]
        assert json.loads(texts["result.json"])["error_kind"] == "PROCESS_CANCELLED"
        assert result.finished_at >= result.started_at and result.duration_ms >= 0

    def test_cancel_during_report_generation_is_cancel_not_a_broken_report(self, tmp_path):
        runner = FakeRunner(events=PASSING, cancelled=True, finish=False, summary='{"run": {"st')
        result = _engine_run(tmp_path, runner, threading.Event())
        assert result.error_kind == "PROCESS_CANCELLED"
        assert "newman-report.json" not in _texts(result)

    def test_cancel_wins_over_a_timeout_reported_together(self, tmp_path):
        runner = FakeRunner(
            events=PARTIAL, cancelled=True, timed_out=True, finish=False, summary=None
        )
        result = _engine_run(tmp_path, runner, threading.Event())
        assert result.error_kind == "PROCESS_CANCELLED"

    @pytest.mark.parametrize(
        ("events", "expected"),
        [
            (PASSING, "PASS"),
            (
                item_events(
                    0, "TC-001", status=500, assertions=[("Status code is 200", "got 500")]
                ),
                "FAIL",
            ),
        ],
    )
    def test_cancel_after_completion_does_not_rewrite_the_result(self, tmp_path, events, expected):
        event = threading.Event()
        runner = FakeRunner(events=events, side_effect=lambda config: event.set())
        result = _engine_run(tmp_path, runner, event)
        assert event.is_set()
        assert (result.status, result.error_kind, result.cancelled) == (expected, "", False)

    def test_without_a_cancel_event_the_runner_is_called_exactly_as_before(self, tmp_path):
        runner = FakeRunner(events=PASSING)
        result = _engine_run(tmp_path, runner)
        assert runner.calls[0]["kwargs"] == {}
        assert (result.status, result.cancelled) == ("PASS", False)

    def test_the_cancel_event_reaches_the_process_runner(self, tmp_path):
        event = threading.Event()
        runner = FakeRunner(events=PASSING)
        _engine_run(tmp_path, runner, event)
        assert runner.calls[0]["kwargs"]["cancel_event"] is event

    def test_a_cancelled_run_is_still_sanitized(self, tmp_path):
        events = item_events(
            0,
            "TC-001",
            assertions=[("Status code is 200", None)],
            url=f"http://podium.test/api/voucher?api_key={QUERY_SECRET}&token={TOKEN}",
        ) + item_events(1, "TC-002", sent=False, done=False)
        runner = FakeRunner(
            events=events,
            cancelled=True,
            finish=False,
            summary=None,
            html=f"<html><body>?token&#x3D;{TOKEN} {QUERY_SECRET}</body></html>",
            stdout=f"token={TOKEN}\n",
            stderr=f"cookie {QUERY_SECRET}\n",
        )
        result = _engine_run(tmp_path, runner, threading.Event())
        assert result.error_kind == "PROCESS_CANCELLED"
        for name, text in _texts(result).items():
            assert TOKEN not in text and QUERY_SECRET not in text, name
        assert result.requests[0].url.endswith("api_key=***&token=***")

    def test_a_sanitization_failure_still_fails_closed(self, tmp_path):
        runner = FakeRunner(
            events=PARTIAL, cancelled=True, finish=False, summary=None, stderr="value k7Z leaked\n"
        )
        env = [
            {"key": "base_url", "value": "http://podium.test", "enabled": True},
            {"key": "token", "value": "k7Z", "enabled": True},
        ]
        result = _engine_run(tmp_path, runner, threading.Event(), env_values=env)
        assert (result.status, result.error_kind) == ("ERROR", "SANITIZATION_FAILED")
        assert set(_texts(result)) == {"result.json"}

    def test_the_private_directory_is_removed_after_cancellation(self, tmp_path):
        runner = FakeRunner(events=PARTIAL, cancelled=True, finish=False, summary=None)
        _engine_run(tmp_path, runner, threading.Event())
        assert not Path(runner.calls[0]["config"]["events_path"]).parent.exists()
