"""
tests/unit/execution_gate/gate_child.py — پروسه‌ی فرزند برای تست‌های بین‌پروسه‌ای (ماژولِ تست نیست)

    python -I gate_child.py <src_dir> <gate_dir> <mode>

بسته مستقیم از src/ بار می‌شود (import execution_gate) تا src/__init__.py و هیچ چیزِ دیگری از
برنامه بار نشود. خروجی یک خط برای هر رخداد است: ACQUIRED <generation>، BUSY، UNAVAILABLE،
LOCKED، SPAWNED <pid>، RELEASED <outcome>.

حالت‌ها:
  try     — یک بار تلاش؛ اگر گرفت، بلافاصله آزاد می‌کند
  hold    — می‌گیرد، heartbeat می‌زند تا خطی در stdin بیاید، بعد آزاد می‌کند
  hang    — می‌گیرد و دیگر هیچ کاری نمی‌کند (زنده ولی بی‌heartbeat)
  exit    — می‌گیرد و بی‌آزادسازی os._exit می‌کند (crash پس از commitِ رکورد)
  oslock  — فقط قفلِ سیستم‌عامل را می‌گیرد (crash بینِ قفل و رکورد)
  cleared — رکورد را پاک می‌کند و پیش از آزاد کردنِ قفل os._exit می‌کند
  spawn   — می‌گیرد، یک فرزندِ ماندگار می‌سازد و هنگ می‌کند (ارثِ handle)
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main() -> None:
    src, directory, mode = sys.argv[1:4]
    sys.path.insert(0, src)
    import execution_gate as eg
    from execution_gate import _oslock

    gate = eg.ExecutionGate(directory)
    if mode == "oslock":
        lock = _oslock.OsLock(gate.lock_path)
        _out("LOCKED" if lock.try_acquire() else "BUSY")
        time.sleep(600)
        return
    try:
        lease = gate.acquire("BOX", f"child-{os.getpid()}")
    except eg.GateBusy:
        _out("BUSY")
        return
    except eg.GateUnavailable:
        _out("UNAVAILABLE")
        return
    _out(f"ACQUIRED {lease.generation}")
    if mode == "try":
        _out(f"RELEASED {lease.release().value}")
    elif mode == "exit":
        os._exit(0)
    elif mode == "cleared":
        lease._clear_record()
        os._exit(0)
    elif mode == "hang":
        time.sleep(600)
    elif mode == "spawn":
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(120)"])
        _out(f"SPAWNED {child.pid}")
        time.sleep(600)
    elif mode == "hold":
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(0.2):
                try:
                    lease.heartbeat()
                except eg.GateError:
                    return

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        sys.stdin.readline()
        stop.set()
        thread.join()
        _out(f"RELEASED {lease.release().value}")


if __name__ == "__main__":
    main()
