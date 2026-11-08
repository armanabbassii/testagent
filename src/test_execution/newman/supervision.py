"""
supervision.py — پروسه‌ی Newman هرگز پس از مرگِ پروسه‌ی Python زنده نمی‌ماند (Windows Job Object)

مسئله: runner.js فقط وقتی خودش را می‌بندد که حلقه‌ی رخدادِ خودش هنوز کار کند و نبودِ والد را
ببیند؛ Node ِ هنگ‌کرده یا Newman ِ گیرکرده پس از crash ِ پروسه‌ی Python می‌توانست زنده بماند —
در حالی که قفلِ سیستم‌عاملِ Execution Gate با همان مرگ آزاد می‌شد و اجرای تازه‌ای (Batch یا Box)
می‌توانست کنارِ آن شروع شود.

راه‌حل (فقط Windows) — مهار پیش از اجرا:

  1. Job Object ساخته و با JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE پیکربندی می‌شود (ProcessJob()).
  2. پروسه با CREATE_SUSPENDED ساخته می‌شود (CREATION_FLAGS): threadِ اصلی‌اش هنوز هیچ کدِ
     user-mode ای (نه loader، نه Node، نه runner.js) اجرا نکرده است.
  3. assign(): پروسه به job سپرده و عضویتش با IsProcessInJob تأیید می‌شود.
  4. start(): فقط پس از تأییدِ عضویت، تنها threadِ پروسه (با Toolhelp32 از روی pid پیدا می‌شود،
     چون Popen handle ِ thread را می‌بندد) resume می‌شود.

پس پروسه پیش از مهارشدن هیچ کدی اجرا نمی‌کند و هر فرزندی که بعداً بسازد خودکار عضوِ job است
(BREAKAWAY مجاز نیست). handle ِ job ارث‌بردنی نیست و فقط در همین پروسه‌ی Python است؛ وقتی بسته شود
— با close() یا با مرگِ پروسه‌ی Python به هر شکلی — سیستم‌عامل همه‌ی اعضا را می‌کشد، مستقل از اینکه
Node پاسخ‌گو باشد یا نه. شکستِ هر گام → SupervisionError و پروسه‌ی معلق کشته می‌شود (process.py).

محدودیتِ صادقانه: اگر پروسه‌ی Python درست بینِ ساختِ پروسه‌ی معلق و assign بمیرد، آن پروسه معلق باقی
می‌ماند (هیچ کدی اجرا نکرده و هیچ درخواستی نفرستاده؛ کسی آن را resume نمی‌کند) تا دستی بسته شود.
روی POSIX این ماژول کاری نمی‌کند (session ِ جدا + watchdog ِ runner.js، همان رفتارِ قبلی؛ Linux جزوِ
محیطِ پذیرشِ این پروژه نیست).
"""

from __future__ import annotations

import sys

SUPPORTED = sys.platform == "win32"
# Popen(creationflags=...) — پروسه تا start() هیچ کدی اجرا نمی‌کند
CREATION_FLAGS = 0x00000004 if SUPPORTED else 0  # CREATE_SUSPENDED


class SupervisionError(OSError):
    """job ساخته یا پروسه به آن سپرده نشد — پیام هیچ داده‌ای ندارد."""


if SUPPORTED:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    _kernel32.IsProcessInJob.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    _RESUME_FAILED = 0xFFFFFFFF

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    _kernel32.Thread32First.restype = wintypes.BOOL
    _kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    _kernel32.Thread32Next.restype = wintypes.BOOL
    _kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.ResumeThread.restype = wintypes.DWORD

    def _thread_ids(pid: int) -> list[int]:
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
            raise SupervisionError("The process threads could not be listed.")
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found: list[int] = []
            more = _kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    found.append(entry.th32ThreadID)
                entry.dwSize = ctypes.sizeof(entry)
                more = _kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            return found
        finally:
            _kernel32.CloseHandle(snapshot)


class ProcessJob:
    """یک job برای یک پروسه‌ی Newman. close() (یا مرگِ پروسه‌ی Python) همه‌ی اعضا را می‌کشد."""

    def __init__(self) -> None:
        self._handle = None
        self._assigned_pid: int | None = None
        if not SUPPORTED:
            return
        handle = _kernel32.CreateJobObjectW(None, None)  # امنیتِ پیش‌فرض: ارث‌بردنی نیست
        if not handle:
            raise SupervisionError("The process supervision job could not be created.")
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _kernel32.CloseHandle(handle)
            raise SupervisionError("The process supervision job could not be configured.")
        self._handle = handle

    def assign(self, process) -> None:
        """پروسه‌ی تازه‌ساخته (subprocess.Popen) را به job می‌سپارد و عضویت را تأیید می‌کند."""
        if not SUPPORTED:
            return
        process_handle = int(process._handle)
        if not _kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise SupervisionError("The process could not be placed under supervision.")
        member = wintypes.BOOL(False)
        if not _kernel32.IsProcessInJob(process_handle, self._handle, ctypes.byref(member)):
            raise SupervisionError("Process supervision could not be verified.")
        if not member.value:
            raise SupervisionError("The process is not under supervision.")
        self._assigned_pid = process.pid

    def start(self, process) -> None:
        """پروسه‌ی معلق را فقط پس از assign ِ تأییدشده اجرا می‌کند (fail-closed)."""
        if not SUPPORTED:
            return
        if self._handle is None or self._assigned_pid != process.pid:
            raise SupervisionError(
                "The process was not placed under supervision; it was not started."
            )
        threads = _thread_ids(process.pid)
        if len(threads) != 1:  # پروسه‌ی تازه‌ی معلق دقیقاً یک thread دارد
            raise SupervisionError("The suspended process is not in the expected state.")
        thread = _kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, threads[0])
        if not thread:
            raise SupervisionError("The suspended process could not be resumed.")
        try:
            previous = _kernel32.ResumeThread(thread)
        finally:
            _kernel32.CloseHandle(thread)
        if previous != 1:  # 1 → 0: حالا در حالِ اجراست؛ هر مقدارِ دیگر یعنی وضعیتِ ناشناخته
            raise SupervisionError("The suspended process could not be resumed.")

    def close(self) -> None:
        """idempotent؛ با KILL_ON_JOB_CLOSE هر عضوِ هنوز زنده کشته می‌شود."""
        handle, self._handle = self._handle, None
        if handle and SUPPORTED:
            _kernel32.CloseHandle(handle)


__all__ = ["CREATION_FLAGS", "SUPPORTED", "ProcessJob", "SupervisionError"]
