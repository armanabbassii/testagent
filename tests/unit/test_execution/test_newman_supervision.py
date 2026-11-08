"""
tests/unit/test_execution/test_newman_supervision.py — هیچ پروسه‌ی Newman از مرگِ والد جان سالم به در نمی‌برد

فقط Windows (محیطِ پذیرش). فرزندها پروسه‌های ساده‌ی Python اند که مدام در فایل می‌نویسند (هرگز
Node یا Newman). «زنده بودن» با دو شاهدِ مستقل سنجیده می‌شود: GetExitCodeProcess و رشدِ فایلِ tick.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src.execution_gate import ExecutionGate
from src.test_execution.newman import process
from src.test_execution.newman import supervision
from tests.unit.execution_gate.support import acquire_eventually

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")

REPO_ROOT = Path(__file__).resolve().parents[3]
PARENT = Path(__file__).with_name("supervision_parent.py")
BASE_PYTHON = getattr(sys, "_base_executable", None) or sys.executable
WAIT_S = 30.0


def _alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _terminate(pid: int) -> None:
    """کشتنِ ناگهانی (مثلِ crash) — هیچ پاک‌سازی‌ای در پروسه اجرا نمی‌شود."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    assert handle, "the parent process could not be opened"
    try:
        assert kernel32.TerminateProcess(handle, 9)
    finally:
        kernel32.CloseHandle(handle)


def _wait(predicate, timeout: float = WAIT_S) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("The condition was not reached in time.")
        time.sleep(0.02)


def _stopped_ticking(tick_file: Path) -> bool:
    before = tick_file.stat().st_size
    time.sleep(0.3)
    return tick_file.stat().st_size == before


class _Parent:
    def __init__(
        self, tmp_path: Path, *, gate_dir: Path | None = None, mode: str | None = None
    ) -> None:
        self.tick_file = tmp_path / "ticks.bin"
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(PARENT),
                str(REPO_ROOT),
                str(self.tick_file),
                str(gate_dir) if gate_dir else "-",
                BASE_PYTHON,
                *([mode] if mode else []),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(tmp_path),
        )
        self.lines: list[str] = []

    def expect(self, prefix: str) -> str:
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline().strip()
            if line:
                self.lines.append(line)
                if line.startswith(prefix):
                    return line
            elif self.proc.poll() is not None:
                break
        raise AssertionError(
            f"parent never printed {prefix!r}: {self.lines} {self.proc.stderr.read()[-2000:]}"
        )

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=WAIT_S)
        for stream in (self.proc.stdout, self.proc.stderr):
            stream.close()


@pytest.fixture
def parents():
    started: list[_Parent] = []
    children: list[int] = []
    yield started, children
    for parent in started:
        parent.close()
    for pid in children:  # پاک‌سازیِ تست — اگر نظارت شکست خورده بود
        if _alive(pid):
            _terminate(pid)


def test_a_child_is_supervised_and_killed_when_its_job_closes(tmp_path):
    tick_file = tmp_path / "ticks.bin"
    job = supervision.ProcessJob()
    child = subprocess.Popen([BASE_PYTHON, "-I", "-c", _ticker(), str(tick_file)])
    try:
        job.assign(child)
        _wait(lambda: tick_file.exists() and tick_file.stat().st_size > 0)
        job.close()
        child.wait(timeout=WAIT_S)
        assert child.returncode is not None and _stopped_ticking(tick_file)
    finally:
        job.close()
        if child.poll() is None:
            child.kill()
            child.wait()


def _ticker() -> str:
    from tests.unit.test_execution.supervision_parent import TICKER

    return TICKER


def test_an_abruptly_killed_parent_leaves_no_child_running(tmp_path, parents):
    started, children = parents
    parent = _Parent(tmp_path)
    started.append(parent)
    parent_pid = int(parent.expect("PARENT").split()[1])
    child_pid = int(parent.expect("CHILD").split()[1])
    children.append(child_pid)
    _wait(lambda: parent.tick_file.exists() and parent.tick_file.stat().st_size > 0)
    assert _alive(child_pid)
    _terminate(parent_pid)  # crash: هیچ finally ای در والد اجرا نمی‌شود
    _wait(lambda: not _alive(child_pid))
    assert _stopped_ticking(parent.tick_file)


def test_a_new_gate_owner_never_coexists_with_the_old_child(tmp_path, parents):
    started, children = parents
    gate_dir = tmp_path / "gate"
    parent = _Parent(tmp_path, gate_dir=gate_dir)
    started.append(parent)
    parent_pid = int(parent.expect("PARENT").split()[1])
    parent.expect("GATE")
    child_pid = int(parent.expect("CHILD").split()[1])
    children.append(child_pid)
    _wait(lambda: parent.tick_file.exists() and parent.tick_file.stat().st_size > 0)
    with pytest.raises(Exception):  # noqa: B017 — GateBusy: والدِ زنده Gate را دارد
        ExecutionGate(gate_dir).acquire("BATCH", "probe")
    _terminate(parent_pid)
    lease = acquire_eventually(ExecutionGate(gate_dir))  # فقط پس از مرگِ واقعیِ والد
    try:
        _wait(lambda: not _alive(child_pid), timeout=5.0)
        assert _stopped_ticking(parent.tick_file)
    finally:
        lease.release()


def test_a_kill_timeout_still_leaves_no_survivor(tmp_path, monkeypatch):
    tick_file = tmp_path / "ticks.bin"
    seen: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        seen.append(child)  # مرجع نگه داشته می‌شود تا پس از اثبات، reap شود
        return child

    def hung_taskkill(proc):
        raise subprocess.TimeoutExpired(["taskkill"], 30)  # taskkill خودش گیر کرد

    monkeypatch.setattr(process.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(process, "_kill_tree", hung_taskkill)
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    with pytest.raises(subprocess.TimeoutExpired):
        process.run_process(
            [BASE_PYTHON, "-I", "-c", _ticker(), str(tick_file)],
            600.0,
            str(tmp_path),
            cancel_event=cancel,
        )
    (child,) = seen
    _wait(lambda: not _alive(child.pid))  # job در finally بسته شد → عضوِ زنده کشته شد
    assert _stopped_ticking(tick_file)
    child.wait(timeout=WAIT_S)  # فقط reap ِ handle ِ Python — پروسه پیش از این مرده بود
    for stream in (child.stdout, child.stderr):
        stream.close()


def test_a_hung_child_is_killed_with_its_whole_tree(tmp_path):
    marker = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([{BASE_PYTHON!r}, '-I', '-c', 'import time; time.sleep(600)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "time.sleep(600)\n"  # والدِ هنگ‌کرده (مثلِ Node ای که منتظرِ Newman است)
    )
    cancel = threading.Event()

    def cancel_when_ready():
        _wait(lambda: marker.exists() and marker.read_text())
        cancel.set()

    threading.Thread(target=cancel_when_ready, daemon=True).start()
    outcome = process.run_process(
        [BASE_PYTHON, "-I", "-c", code, str(marker)], 120.0, str(tmp_path), cancel_event=cancel
    )
    assert outcome.cancelled and outcome.exit_code is not None
    grandchild = int(marker.read_text())
    _wait(lambda: not _alive(grandchild))  # فرزندِ فرزند هم داخلِ job بود


def test_supervision_failure_prevents_unsupervised_execution(tmp_path, monkeypatch):
    def broken(self):
        raise supervision.SupervisionError("The process supervision job could not be created.")

    monkeypatch.setattr(supervision.ProcessJob, "__init__", broken)
    outcome = process.run_process([BASE_PYTHON, "-I", "-c", "print('x')"], 30.0, str(tmp_path))
    assert not outcome.started and "not started" in outcome.error


def test_an_assignment_failure_terminates_the_process(tmp_path, monkeypatch):
    def refuse(self, proc):
        raise supervision.SupervisionError("The process could not be placed under supervision.")

    monkeypatch.setattr(supervision.ProcessJob, "assign", refuse)
    outcome = process.run_process(
        [BASE_PYTHON, "-I", "-c", "import time; time.sleep(600)"], 30.0, str(tmp_path)
    )
    assert outcome.started and outcome.exit_code is not None and "terminated" in outcome.error


# ── مهار پیش از اجرا (پنجره‌ی بینِ CreateProcess و AssignProcessToJobObject) ──────────────────

_MARK = "import sys; open(sys.argv[1], 'w').write('ran')\n"
_MARK_THEN_SLEEP = _MARK + "import time; time.sleep(600)\n"
WINDOW_S = 1.5  # بسیار بیشتر از زمانِ شروعِ «python -I» — اگر فرزند اجرا می‌شد، نشانه را نوشته بود


def _job_flags(handle) -> int:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    info = supervision._ExtendedLimit()
    ok = kernel32.QueryInformationJobObject(
        ctypes.c_void_p(handle), 9, ctypes.byref(info), ctypes.sizeof(info), None
    )
    assert ok, "the job could not be queried"
    return info.BasicLimitInformation.LimitFlags


def _handle_count() -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    count = wintypes.DWORD()
    assert kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count))
    return count.value


class _Recorder:
    """ترتیبِ رخدادهای چرخه‌ی پروسه و حالِ نشانه‌ی فرزند در هر گام را ثبت می‌کند."""

    def __init__(self, monkeypatch, marker: Path, *, window: float = 0.0) -> None:
        self.events: list[tuple[str, object]] = []
        self.children: list[subprocess.Popen] = []
        self.marker = marker
        real_init = supervision.ProcessJob.__init__
        real_assign = supervision.ProcessJob.assign
        real_start = supervision.ProcessJob.start
        real_close = supervision.ProcessJob.close
        real_popen = process.subprocess.Popen
        recorder = self

        def init(job):
            real_init(job)
            recorder.events.append(("job-configured", _job_flags(job._handle)))

        def popen(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            recorder.children.append(child)
            recorder.events.append(("process-created", kwargs.get("creationflags", 0)))
            return child

        def assign(job, proc):
            time.sleep(window)  # پنجره‌ی گزارش‌شده، عمداً بزرگ شده
            recorder.events.append(("before-assign", (marker.exists(), _alive(proc.pid))))
            real_assign(job, proc)
            recorder.events.append(("assigned", None))

        def start(job, proc):
            time.sleep(window)  # پس از assign و پیش از resume هم نباید اجرا شود
            recorder.events.append(("before-start", marker.exists()))
            real_start(job, proc)
            recorder.events.append(("started", None))

        def close(job):
            recorder.events.append(("job-closed", None))
            real_close(job)

        monkeypatch.setattr(supervision.ProcessJob, "__init__", init)
        monkeypatch.setattr(supervision.ProcessJob, "assign", assign)
        monkeypatch.setattr(supervision.ProcessJob, "start", start)
        monkeypatch.setattr(supervision.ProcessJob, "close", close)
        monkeypatch.setattr(process.subprocess, "Popen", popen)

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def value(self, name: str):
        return next(value for event, value in self.events if event == name)


def test_the_job_is_configured_before_the_child_exists_and_the_child_starts_suspended(
    tmp_path, monkeypatch
):
    marker = tmp_path / "ran.txt"
    recorder = _Recorder(monkeypatch, marker)
    outcome = process.run_process(
        [BASE_PYTHON, "-I", "-c", _MARK, str(marker)], 30.0, str(tmp_path)
    )
    assert recorder.names() == [
        "job-configured",
        "process-created",
        "before-assign",
        "assigned",
        "before-start",
        "started",
        "job-closed",
    ]
    assert recorder.value("job-configured") & 0x2000  # KILL_ON_JOB_CLOSE پیش از ساختِ پروسه
    assert recorder.value("process-created") & 0x4  # CREATE_SUSPENDED
    assert outcome.exit_code == 0 and marker.read_text() == "ran"


def test_the_child_cannot_execute_inside_the_reported_race_window(tmp_path, monkeypatch):
    """پنجره‌ی بینِ ساختِ پروسه و سپردن به job (و بینِ سپردن و resume) عمداً ۱.۵ ثانیه کش می‌آید:
    فرزند زنده است ولی هیچ خطی اجرا نکرده؛ پیش از اصلاح، نشانه در همین پنجره نوشته می‌شد."""
    marker = tmp_path / "ran.txt"
    recorder = _Recorder(monkeypatch, marker, window=WINDOW_S)
    outcome = process.run_process(
        [BASE_PYTHON, "-I", "-c", _MARK, str(marker)], 30.0, str(tmp_path)
    )
    executed, alive = recorder.value("before-assign")
    assert alive and not executed  # پروسه وجود دارد، اما معلق است
    assert recorder.value("before-start") is False  # در job است، هنوز resume نشده
    assert outcome.exit_code == 0 and marker.read_text() == "ran"  # پس از resume اجرا شد


def test_successful_assignment_permits_execution(tmp_path):
    outcome = process.run_process(
        [BASE_PYTHON, "-I", "-c", "print('contained')"], 30.0, str(tmp_path)
    )
    assert outcome.started and outcome.exit_code == 0 and outcome.stdout.strip() == "contained"
    assert not outcome.error


@pytest.mark.parametrize("step", ["assign", "start"])
def test_a_supervision_failure_never_lets_the_child_execute(tmp_path, monkeypatch, step):
    marker = tmp_path / "ran.txt"
    seen: list[subprocess.Popen] = []
    real_popen = process.subprocess.Popen
    closed: list[bool] = []
    real_close = supervision.ProcessJob.close

    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        seen.append(child)
        return child

    def refuse(self, proc):
        time.sleep(0.5)
        raise supervision.SupervisionError("The process could not be placed under supervision.")

    def close(self):
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(process.subprocess, "Popen", popen)
    monkeypatch.setattr(supervision.ProcessJob, step, refuse)
    monkeypatch.setattr(supervision.ProcessJob, "close", close)
    outcome = process.run_process(
        [BASE_PYTHON, "-I", "-c", _MARK_THEN_SLEEP, str(marker)], 30.0, str(tmp_path)
    )
    child = seen[0]  # بعدی taskkill است (خودش با Popen اجرا می‌شود)
    assert outcome.started and outcome.exit_code is not None and "terminated" in outcome.error
    assert not _alive(child.pid) and child.returncode is not None  # کشته و reap شد
    time.sleep(WINDOW_S)
    assert not marker.exists()  # هرگز هیچ کدی اجرا نکرد
    assert closed  # handle ِ job بسته شد


def test_start_refuses_a_process_that_was_not_assigned(tmp_path):
    marker = tmp_path / "ran.txt"
    job = supervision.ProcessJob()
    child = subprocess.Popen(
        [BASE_PYTHON, "-I", "-c", _MARK, str(marker)], creationflags=supervision.CREATION_FLAGS
    )
    try:
        with pytest.raises(supervision.SupervisionError, match="not started"):
            job.start(child)
        time.sleep(WINDOW_S)
        assert _alive(child.pid) and not marker.exists()  # هنوز معلق
    finally:
        child.kill()
        child.wait(timeout=WAIT_S)
        job.close()
    assert not marker.exists()


def test_an_unexpected_error_before_resume_kills_the_suspended_child(tmp_path, monkeypatch):
    marker = tmp_path / "ran.txt"
    seen: list[subprocess.Popen] = []
    real_popen = process.subprocess.Popen

    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        seen.append(child)
        return child

    def explode(self, proc):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(process.subprocess, "Popen", popen)
    monkeypatch.setattr(supervision.ProcessJob, "assign", explode)
    with pytest.raises(RuntimeError):
        process.run_process([BASE_PYTHON, "-I", "-c", _MARK, str(marker)], 30.0, str(tmp_path))
    (child,) = seen
    assert child.returncode is not None and not _alive(child.pid)
    for stream in (child.stdout, child.stderr):
        stream.close()
    time.sleep(WINDOW_S)
    assert not marker.exists()


def test_a_parent_killed_inside_the_window_leaves_a_child_that_never_executed(tmp_path, parents):
    """والد پس از ساختِ پروسه و پیش از سپردن به job ناگهانی می‌میرد. فرزند معلق می‌ماند و هرگز
    هیچ خطی اجرا نمی‌کند (پیش از اصلاح، فرزند بیرونِ job اجرا می‌شد و ادامه می‌داد)."""
    started, children = parents
    parent = _Parent(tmp_path, mode="window")
    started.append(parent)
    parent_pid = int(parent.expect("PARENT").split()[1])
    child_pid = int(parent.expect("CHILD").split()[1])
    children.append(child_pid)  # پاک‌سازیِ تست: فرزندِ معلق را fixture می‌بندد
    parent.expect("WINDOW")
    _wait(lambda: not _alive(parent_pid))
    time.sleep(WINDOW_S)
    assert not parent.tick_file.exists()  # هیچ tick ای — فرزند هرگز اجرا نشد


def test_a_process_timeout_still_kills_the_whole_tree(tmp_path):
    marker = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([{BASE_PYTHON!r}, '-I', '-c', 'import time; time.sleep(600)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "time.sleep(600)\n"
    )
    outcome = process.run_process(
        [BASE_PYTHON, "-I", "-c", code, str(marker)],
        5.0,
        str(tmp_path),
        cancel_event=threading.Event(),
    )
    assert outcome.timed_out and outcome.exit_code is not None
    _wait(lambda: not _alive(int(marker.read_text())))


def test_no_handles_leak_on_normal_and_failed_paths(tmp_path, monkeypatch):
    def run_once():
        outcome = process.run_process([BASE_PYTHON, "-I", "-c", "pass"], 30.0, str(tmp_path))
        assert outcome.exit_code == 0

    run_once()  # گرم‌کردن (DLLها، threadهای داخلیِ subprocess)
    before = _handle_count()
    for _ in range(10):
        run_once()
    monkeypatch.setattr(
        supervision.ProcessJob,
        "start",
        lambda self, proc: (_ for _ in ()).throw(supervision.SupervisionError("refused")),
    )
    for _ in range(5):
        outcome = process.run_process([BASE_PYTHON, "-I", "-c", "pass"], 30.0, str(tmp_path))
        assert "terminated" in outcome.error
    assert _handle_count() - before < 10  # هر اجرا چند handle دارد؛ نشت → دست‌کم ۱۵ handle
