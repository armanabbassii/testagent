"""
process.py — اجرای پروسه‌ی Newman با سقفِ زمان و پایان‌دادنِ مطمئن به درختِ پروسه

روی Windows پروسه در گروهِ تازه ساخته می‌شود و در timeout با
«taskkill /T /F» همراهِ همه‌ی فرزندانش بسته می‌شود؛ روی POSIX کلِ session با
SIGKILL. خروجیِ گرفته‌شده تا لحظه‌ی پایان نگه داشته می‌شود.

لغوِ همکارانه (cancel_event): پروسه هر POLL_INTERVAL_S بررسی می‌شود و تقدم قطعی است:

  1. پروسه تمام شده → همان نتیجه (لغوِ بعدی هیچ چیزی را بازنویسی نمی‌کند)؛
  2. cancel_event ست شده → درختِ پروسه بسته می‌شود (cancelled=True)؛
  3. سقفِ زمانِ کل گذشته → درختِ پروسه بسته می‌شود (timed_out=True).

لغو و timeout در یک بررسیِ واحد → لغو برنده است (درخواستِ صریحِ کاربر).
بدونِ cancel_event همان مسیرِ قبلی (یک communicate با سقفِ زمان) اجرا می‌شود.

پروسه فقط متغیرهای محیطیِ لازم را می‌گیرد — اعتبارنامه‌های پروسه‌ی Python
(LLM، Jira، …) به Newman نمی‌رسند. extra_env (مثلاً متغیرهای زمانِ اجرای Newman)
فقط به همین پروسه داده می‌شود، نه به محیطِ پروسه‌ی Python.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

# متغیرهای محیطی‌ای که Node/Newman برای اجرا و شبکه لازم دارند.
_INHERITED = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "LANG",
    "LC_ALL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
)


@dataclass
class ProcessOutcome:
    started: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    pid: int | None = None
    error: str = ""
    cancelled: bool = False


# فراخوانیِ (argv، سقفِ زمان، cwd) — و cancel_event به‌صورتِ کلیدواژه فقط وقتی داده شده باشد.
ProcessRunner = Callable[..., ProcessOutcome]

POLL_INTERVAL_S = 0.2
# پس از بستنِ درخت: صبر برای جمع‌کردنِ خروجی و برداشتنِ پروسه (reap)
_REAP_TIMEOUT_S = 30.0


def child_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {key: os.environ[key] for key in _INHERITED if key in os.environ}
    environment.update(extra or {})
    return environment


def _kill_tree(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if process.poll() is None:
        process.kill()


def _terminate(process: subprocess.Popen) -> tuple[bytes, bytes]:
    """درخت را می‌بندد، خروجیِ باقی‌مانده را می‌گیرد و پروسه را reap می‌کند — با سقفِ زمان."""
    _kill_tree(process)
    try:
        return process.communicate(timeout=_REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return b"", b""


def run_process(
    argv: list[str],
    timeout_s: float,
    cwd: str,
    *,
    cancel_event: threading.Event | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> ProcessOutcome:
    """argv بدونِ shell اجرا می‌شود؛ هیچ استثنایی بیرون نمی‌آید."""
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=child_environment(extra_env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )
    except OSError as exc:
        return ProcessOutcome(
            started=False, error=f"{type(exc).__name__}: the process could not be started."
        )

    timed_out = cancelled = False
    if cancel_event is None:
        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            stdout, stderr = _terminate(process)
            timed_out = True
    else:
        deadline = time.monotonic() + timeout_s
        while True:
            if cancel_event.is_set():
                stdout, stderr = _terminate(process)
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stdout, stderr = _terminate(process)
                timed_out = True
                break
            try:
                # تکرارِ communicate بعد از TimeoutExpired خروجی را از دست نمی‌دهد.
                stdout, stderr = process.communicate(timeout=min(POLL_INTERVAL_S, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    return ProcessOutcome(
        started=True,
        exit_code=process.returncode,
        stdout=(stdout or b"").decode("utf-8", errors="replace"),
        stderr=(stderr or b"").decode("utf-8", errors="replace"),
        timed_out=timed_out,
        pid=process.pid,
        cancelled=cancelled,
    )


__all__ = ["POLL_INTERVAL_S", "ProcessOutcome", "ProcessRunner", "child_environment", "run_process"]
