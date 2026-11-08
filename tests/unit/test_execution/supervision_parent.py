"""
tests/unit/test_execution/supervision_parent.py — پروسه‌ی «والد» برای تست‌های نظارتِ پروسه (ماژولِ تست نیست)

    python supervision_parent.py <repo_root> <tick_file> <gate_dir|-> <base_python> [window]

با «window» والد درست پس از ساختِ پروسه و پیش از سپردنش به job خودش را ناگهانی می‌کشد.

اگر gate_dir داده شده باشد اول Execution Gate را می‌گیرد، سپس با run_process ِ واقعی یک فرزندِ
Python (نه Node/Newman) اجرا می‌کند که هر ۱۰ms یک بایت به tick_file اضافه می‌کند و هرگز خودش تمام
نمی‌شود. خط‌های خروجی: «PARENT <pid>»، «GATE» و «CHILD <pid>». تست این پروسه را ناگهانی می‌کشد.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

TICKER = (
    "import sys, time\n"
    "with open(sys.argv[1], 'ab', buffering=0) as f:\n"
    "    while True:\n"
    "        f.write(b'.')\n"
    "        time.sleep(0.01)\n"
)


def main() -> None:
    repo, tick_file, gate_dir, base_python = sys.argv[1:5]
    sys.path.insert(0, repo)
    import os

    from src.test_execution.newman import process

    print(f"PARENT {os.getpid()}", flush=True)
    lease = None
    if gate_dir != "-":
        from src.execution_gate import ExecutionGate

        lease = ExecutionGate(gate_dir).acquire("BOX", "supervision-parent")
        print("GATE", flush=True)

    real_popen = process.subprocess.Popen

    def announcing_popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        print(f"CHILD {child.pid}", flush=True)
        return child

    process.subprocess.Popen = announcing_popen
    if len(sys.argv) > 5 and sys.argv[5] == "window":
        # والد درست در پنجره‌ی بینِ ساختِ پروسه و سپردن به job ناگهانی می‌میرد
        import ctypes

        from src.test_execution.newman import supervision

        def die_before_assign(self, proc):
            print("WINDOW", flush=True)
            time.sleep(0.5)  # اگر فرزند اجرا می‌شد، تا این‌جا شروع کرده بود
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.TerminateProcess(ctypes.c_void_p(-1), 9)  # خودِ والد — بی finally

        supervision.ProcessJob.assign = die_before_assign
    worker = threading.Thread(
        target=process.run_process,
        args=([base_python, "-I", "-c", TICKER, tick_file], 600.0, str(Path(tick_file).parent)),
        kwargs={"cancel_event": threading.Event()},
        daemon=True,
    )
    worker.start()
    while True:  # تا وقتی کشته شود — هرگز lease را آزاد نمی‌کند
        time.sleep(1)
    _ = lease


if __name__ == "__main__":
    main()
