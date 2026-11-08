"""
tests/unit/execution_gate/support.py — پروسه‌های فرزند با انتظارِ محدود و خواندنِ مستقیمِ پایگاه

همه چیز در پوشه‌ی موقتِ هر تست است؛ هیچ تابعی data/execution_gate/ ِ واقعی را نمی‌بیند.
"""

from __future__ import annotations

import queue
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

from src.execution_gate import ExecutionGate, GateBusy, GateLease

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"
CHILD = Path(__file__).with_name("gate_child.py")
WAIT_S = 30.0
# python.exe ِ venv روی Windows یک launcher است که مفسرِ واقعی را به‌صورتِ فرزندِ خودش اجرا
# می‌کند؛ مفسرِ پایه یعنی یک پروسه‌ی واقعی و pid ِ همان پروسه‌ای که قفل را می‌گیرد. فرزند فقط
# به کتابخانه‌ی استاندارد و src/ نیاز دارد.
PYTHON = getattr(sys, "_base_executable", None) or sys.executable


class Child:
    """یک پروسه‌ی فرزند؛ هر انتظار سقفِ زمان دارد و stderr برای تشخیص نگه داشته می‌شود."""

    def __init__(self, gate_dir: Path, mode: str) -> None:
        self.mode = mode
        self.proc = subprocess.Popen(
            [PYTHON, "-I", str(CHILD), str(SRC_DIR), str(gate_dir), mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(gate_dir.parent),
        )
        self._lines: queue.Queue[str] = queue.Queue()
        self._stderr: list[str] = []
        self._readers = [
            threading.Thread(target=self._pump, args=(self.proc.stdout, self._lines.put)),
            threading.Thread(target=self._pump, args=(self.proc.stderr, self._stderr.append)),
        ]
        for reader in self._readers:
            reader.daemon = True
            reader.start()

    @staticmethod
    def _pump(stream, sink) -> None:
        for line in stream:
            sink(line.rstrip("\n"))

    @property
    def pid(self) -> int:
        return self.proc.pid

    def expect(self, prefix: str, timeout: float = WAIT_S) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"child {self.mode!r} did not print {prefix!r} in {timeout}s; "
                    f"stderr: {' | '.join(self._stderr)[-2000:]}"
                )
            try:
                line = self._lines.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self.proc.poll() is not None and self._lines.empty():
                    self._readers[0].join(timeout=5)
                    if self._lines.empty():
                        raise AssertionError(
                            f"child {self.mode!r} exited ({self.proc.returncode}) without "
                            f"{prefix!r}; stderr: {' | '.join(self._stderr)[-2000:]}"
                        ) from None
                continue
            if line.startswith(prefix):
                return line
            raise AssertionError(f"child {self.mode!r} printed {line!r}, expected {prefix!r}")

    def send(self, text: str) -> None:
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def wait(self, timeout: float = WAIT_S) -> int:
        return self.proc.wait(timeout=timeout)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=WAIT_S)

    def close(self) -> None:
        try:
            self.kill()
        finally:
            for reader in self._readers:
                reader.join(timeout=5)
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


def acquire_eventually(gate: ExecutionGate, timeout: float = WAIT_S) -> GateLease:
    """پس از کشتنِ دارنده: سیستم‌عامل قفل را «به‌زودی» آزاد می‌کند (Windows زمانش را تضمین
    نمی‌کند) — تلاشِ دوباره تا سقفِ زمان، هرگز بی‌نهایت."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return gate.acquire("BATCH", "after-holder")
        except GateBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def events(gate: ExecutionGate) -> list[sqlite3.Row]:
    with closing(
        sqlite3.connect(f"{gate.database_path.resolve().as_uri()}?mode=ro", uri=True)
    ) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT * FROM gate_events ORDER BY id").fetchall()


def raw_owner(gate: ExecutionGate) -> sqlite3.Row:
    with closing(
        sqlite3.connect(f"{gate.database_path.resolve().as_uri()}?mode=ro", uri=True)
    ) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT * FROM gate_owner WHERE slot = 1").fetchone()


def write_owner(gate: ExecutionGate, sql: str, params: tuple = ()) -> None:
    """شبیه‌سازیِ وضعیت (مثلاً heartbeatِ خیلی کهنه یا رکوردِ دست‌کاری‌شده) — فقط در tmp."""
    with closing(sqlite3.connect(str(gate.database_path), isolation_level=None)) as c:
        c.execute(sql, params)
