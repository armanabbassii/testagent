"""
src/swagger_sync/task.py — ثبتِ همگام‌سازیِ روزانه در Windows Task Scheduler

    python -m src.swagger_sync install-task     # ثبت/جایگزینیِ Task (همان نام؛ /F)
    python -m src.swagger_sync update-task      # Task ِ موجود را با تنظیماتِ فعلیِ .env بازنویسی می‌کند
    python -m src.swagger_sync uninstall-task   # حذفِ Task
    python -m src.swagger_sync status           # آخرین گزارش + وضعیتِ Task + تطابقِ ساعت با .env

زمانِ اجرا فقط از SWAGGER_SYNC_DAILY_TIME می‌آید (هیچ زمانِ ثابتی در کد نیست)؛ پس از تغییرِ آن
update-task را اجرا کنید (status ناهماهنگی را نشان می‌دهد).

Task:
  * دو trigger: روزانه در ساعتِ تنظیم‌شده، و هنگامِ logon ِ کاربر (با چند دقیقه تأخیر). Action
    «run --if-due» است، پس logon فقط وقتی همگام‌سازی می‌کند که از آخرین ساعتِ روزانه اجرای موفقی
    ثبت نشده باشد — یعنی اجرای ازدست‌رفته (سیستمِ خاموش) یا قطع‌شده (restart، shutdown، logoff)
    در اولین فرصت انجام می‌شود. StartWhenAvailable همین را برای triggerِ روزانه هم تضمین می‌کند.
  * حسابِ کاربریِ فعلی، فقط وقتی logged on است (InteractiveToken، بدونِ ذخیره‌ی رمز)،
    LeastPrivilege، بدونِ پنجره‌ی کنسول (pythonw.exe ِ همین venv)، WorkingDirectory = ریشه‌ی مخزن.
  * MultipleInstancesPolicy=IgnoreNew: هرگز دو اجرای هم‌زمان (به‌علاوه‌ی قفلِ اجرای خودِ job).
  * ExecutionTimeLimit: با SWAGGER_SYNC_MAX_RUNTIME_MINUTES=0 (پیش‌فرض) PT0S یعنی بی‌سقف، تا Task
    هرگز job را پیش از موفقیتِ همه‌ی منابع نکشد؛ وگرنه max_runtime + ۱۰ دقیقه.
  * اجرا با باتری هم مجاز است و با رفتن روی باتری متوقف نمی‌شود.

StartBoundary اولین وقوعِ آینده‌ی همان ساعت است. اجرای schtasks تزریق‌پذیر است؛ تست‌ها هرگز آن
را واقعاً صدا نمی‌زنند.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from src.swagger_settings import REPO_ROOT, SwaggerSettingsError, SyncSettings, load_sync_settings
from src.swagger_sync import report as sync_report

TASK_NAME = "AgentSwaggerSync"
TASK_DESCRIPTION = (
    "Daily synchronisation of the Swagger snapshots in data/swagger "
    "(python -m src.swagger_sync run --if-due; daily and at logon). "
    "Managed by the agents repository."
)
TASK_ARGUMENTS = "-m src.swagger_sync run --if-due"
EXTRA_TIME_LIMIT_MIN = 10
LOGON_DELAY = "PT3M"
NO_TIME_LIMIT = "PT0S"
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=60, check=False)


def interpreter(repo_root: Path = REPO_ROOT) -> Path:
    scripts = repo_root / ".venv" / "Scripts"
    windowless = scripts / "pythonw.exe"
    return windowless if windowless.is_file() else scripts / "python.exe"


def next_boundary(daily_time: str, now: datetime) -> datetime:
    hour, minute = (int(part) for part in daily_time.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def time_limit(settings: SyncSettings) -> str:
    if settings.unlimited_runtime:
        return NO_TIME_LIMIT
    return f"PT{int(settings.max_runtime_s // 60) + EXTRA_TIME_LIMIT_MIN}M"


def _command(python: Path) -> str:
    # مسیرِ دارای فاصله در Command نقل‌قول می‌شود؛ WorkingDirectory هرگز نقل‌قول نمی‌گیرد
    text = str(python)
    return f'"{text}"' if " " in text else text


def task_xml(
    settings: SyncSettings,
    *,
    now: datetime,
    repo_root: Path = REPO_ROOT,
    python: Path | None = None,
    user: str | None = None,
) -> str:
    start = next_boundary(settings.daily_time, now).strftime("%Y-%m-%dT%H:%M:%S")
    python = python or interpreter(repo_root)
    account = escape(user or current_user())
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(TASK_DESCRIPTION)}</Description>
    <URI>\\{TASK_NAME}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{account}</UserId>
      <Delay>{LOGON_DELAY}</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{account}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{time_limit(settings)}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(_command(python))}</Command>
      <Arguments>{TASK_ARGUMENTS}</Arguments>
      <WorkingDirectory>{escape(str(repo_root))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install(
    settings: SyncSettings, *, runner: Runner = _default_runner, now: datetime | None = None
) -> subprocess.CompletedProcess:
    xml = task_xml(settings, now=now or datetime.now())
    fd, name = tempfile.mkstemp(prefix="swagger_sync_task_", suffix=".xml")
    os.close(fd)
    try:
        Path(name).write_text(xml, encoding="utf-16")
        return runner(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", name, "/F"])
    finally:
        Path(name).unlink(missing_ok=True)


def uninstall(*, runner: Runner = _default_runner) -> subprocess.CompletedProcess:
    return runner(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])


def query(*, runner: Runner = _default_runner) -> subprocess.CompletedProcess:
    return runner(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"])


def query_xml(*, runner: Runner = _default_runner) -> subprocess.CompletedProcess:
    return runner(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"])


def registered_schedule(xml: str) -> dict[str, str]:
    """مقدارهای کلیدیِ Task ِ ثبت‌شده (از خروجیِ schtasks /XML)."""

    def first(tag: str) -> str:
        match = re.search(rf"<{tag}>([^<]*)</{tag}>", xml)
        return match.group(1).strip() if match else ""

    start = first("StartBoundary")
    return {
        "daily_time": start[11:16] if len(start) >= 16 else "",
        "time_limit": first("ExecutionTimeLimit"),
        "arguments": first("Arguments"),
        "working_directory": first("WorkingDirectory"),
        "logon_trigger": "yes" if "<LogonTrigger" in xml else "no",
    }


def _print_result(action: str, result: subprocess.CompletedProcess) -> int:
    if result.returncode == 0:
        print(f"{action}: OK")
        return 0
    print(f"{action} failed (schtasks exit {result.returncode}).", file=sys.stderr)
    detail = (result.stderr or result.stdout or "").strip()
    if detail:
        print(detail, file=sys.stderr)
    return 1


def _install(action: str, settings: SyncSettings, runner: Runner, now: datetime | None) -> int:
    python = interpreter()
    if not python.is_file():
        print(f"{action} failed: the virtual environment interpreter is missing.", file=sys.stderr)
        return 1
    moment = now or datetime.now()
    code = _print_result(action, install(settings, runner=runner, now=moment))
    if code == 0:
        first = next_boundary(settings.daily_time, moment)
        print(
            f"Task {TASK_NAME!r}: daily at {settings.daily_time} (first run "
            f"{first:%Y-%m-%d %H:%M}) and at logon when a run is due; time limit "
            f"{time_limit(settings)}."
        )
    return code


def _status(settings: SyncSettings, runner: Runner) -> int:
    last = sync_report.read_report(settings.log_dir / "last_run.json")
    if last is None:
        print("Last run: none recorded.")
    else:
        print(
            f"Last run: {last.get('result')} (exit {last.get('exit_code')}), "
            f"{last.get('started_at')} -> {last.get('finished_at') or '(running)'}"
        )
        for name in ("succeeded", "pending", "failed"):
            if last.get(name):
                print(f"  {name}: {', '.join(last[name])}")
        for item in last.get("sources", []):
            print(
                f"  {item.get('label')}: {item.get('outcome')} {item.get('error_kind') or ''}".rstrip()
            )
    result = query(runner=runner)
    if result.returncode != 0:
        print(f"Task {TASK_NAME!r} is not registered (or cannot be queried).")
        return 0
    print(result.stdout.strip())
    definition = query_xml(runner=runner)
    if definition.returncode == 0:
        registered = registered_schedule(definition.stdout or "")
        print(
            f"Registered: daily at {registered['daily_time'] or '?'}, logon trigger "
            f"{registered['logon_trigger']}, time limit {registered['time_limit'] or '?'}, "
            f"arguments {registered['arguments'] or '?'!r}."
        )
        expected = {
            "daily_time": settings.daily_time,
            "time_limit": time_limit(settings),
            "arguments": TASK_ARGUMENTS,
            "logon_trigger": "yes",
        }
        differences = [k for k, v in expected.items() if registered.get(k) != v]
        if differences:
            print(
                "The registered task differs from the current settings "
                f"({', '.join(differences)}; SWAGGER_SYNC_DAILY_TIME={settings.daily_time}). "
                "Run: python -m src.swagger_sync update-task"
            )
        else:
            print("The registered task matches the current settings.")
    return 0


def main(command: str, *, runner: Runner = _default_runner, now: datetime | None = None) -> int:
    if sys.platform != "win32":
        print("Windows Task Scheduler is available only on Windows.", file=sys.stderr)
        return 3
    try:
        settings = load_sync_settings()
    except SwaggerSettingsError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 3
    if command == "install-task":
        return _install("install-task", settings, runner, now)
    if command == "update-task":
        if query(runner=runner).returncode != 0:
            print(f"Task {TASK_NAME!r} is not registered; run install-task first.", file=sys.stderr)
            return 1
        return _install("update-task", settings, runner, now)
    if command == "uninstall-task":
        return _print_result("uninstall-task", uninstall(runner=runner))
    if command == "status":
        return _status(settings, runner)
    print(f"Unknown command {command!r}.", file=sys.stderr)
    return 3


__all__ = [
    "LOGON_DELAY",
    "TASK_ARGUMENTS",
    "TASK_NAME",
    "current_user",
    "install",
    "interpreter",
    "main",
    "next_boundary",
    "query",
    "query_xml",
    "registered_schedule",
    "task_xml",
    "time_limit",
    "uninstall",
]
