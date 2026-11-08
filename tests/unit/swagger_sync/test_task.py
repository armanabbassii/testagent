"""
tests/unit/swagger_sync/test_task.py — XML ِ Task Scheduler و دستورهای install/uninstall/status (Phase 4)

schtasks هرگز واقعاً اجرا نمی‌شود: runner ِ ساختگی تزریق می‌شود و subprocess ِ ماژول مسدود است.
"""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest

from src.swagger_settings import REPO_ROOT, SyncSettings
from src.swagger_sync import __main__ as cli
from src.swagger_sync import task

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


@pytest.fixture(autouse=True)
def _no_real_schtasks(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("schtasks must never really run in tests")

    monkeypatch.setattr(task.subprocess, "run", forbidden)
    # مستقل از وجودِ .venv در محیطِ تست
    monkeypatch.setattr(task, "interpreter", lambda repo_root=REPO_ROOT: Path(sys.executable))


def _settings(tmp_path: Path, **overrides) -> SyncSettings:
    values = dict(
        enabled=True,
        daily_time="03:00",
        sources=(),
        max_attempts=0,
        retry_interval_s=30,
        backoff_multiplier=2,
        max_retry_interval_s=300,
        max_runtime_s=0,
        log_dir=tmp_path / "logs",
        log_backups=2,
        connect_timeout_s=10,
        read_timeout_s=120,
    )
    values.update(overrides)
    return SyncSettings(**values)


def _parse(xml: str) -> ET.Element:
    assert xml.startswith('<?xml version="1.0" encoding="UTF-16"?>')
    return ET.fromstring(xml.split("?>", 1)[1])


class FakeRunner:
    """schtasks ِ ساختگی؛ registered_xml خروجیِ «/Query /XML» است (None = Task ثبت نشده)."""

    def __init__(
        self, code: int = 0, stdout: str = "", stderr: str = "", registered_xml: str | None = None
    ):
        self.calls: list[list[str]] = []
        self.xml: list[str] = []
        self.code, self.stdout, self.stderr = code, stdout, stderr
        self.registered_xml = registered_xml

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if "/Create" in argv:
            path = Path(argv[argv.index("/XML") + 1])
            self.xml.append(path.read_bytes().decode("utf-16"))
            self.registered_xml = self.xml[-1]
        if "/Query" in argv and argv[-1] == "/XML":
            if self.registered_xml is None:
                return subprocess.CompletedProcess(argv, 1, "", "ERROR: not found")
            return subprocess.CompletedProcess(argv, 0, self.registered_xml, "")
        return subprocess.CompletedProcess(argv, self.code, self.stdout, self.stderr)


class TestXml:
    def test_the_task_definition_meets_every_requirement(self, tmp_path):
        root = _parse(
            task.task_xml(_settings(tmp_path), now=datetime(2026, 11, 8, 1, 0), user="DOM\\me")
        )
        trigger = root.find("t:Triggers/t:CalendarTrigger", NS)
        assert trigger.find("t:StartBoundary", NS).text == "2026-11-08T03:00:00"
        assert trigger.find("t:ScheduleByDay/t:DaysInterval", NS).text == "1"
        logon = root.find("t:Triggers/t:LogonTrigger", NS)
        assert logon.find("t:UserId", NS).text == "DOM\\me"
        assert logon.find("t:Delay", NS).text == task.LOGON_DELAY
        settings = root.find("t:Settings", NS)
        assert settings.find("t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
        assert settings.find("t:StartWhenAvailable", NS).text == "true"
        assert settings.find("t:DisallowStartIfOnBatteries", NS).text == "false"
        assert settings.find("t:StopIfGoingOnBatteries", NS).text == "false"
        # retry ِ نامحدود: Task هرگز job را پیش از موفقیتِ همه‌ی منابع نمی‌کشد
        assert settings.find("t:ExecutionTimeLimit", NS).text == "PT0S"
        principal = root.find("t:Principals/t:Principal", NS)
        assert principal.find("t:UserId", NS).text == "DOM\\me"
        assert principal.find("t:LogonType", NS).text == "InteractiveToken"
        assert principal.find("t:RunLevel", NS).text == "LeastPrivilege"
        action = root.find("t:Actions/t:Exec", NS)
        assert action.find("t:Arguments", NS).text == "-m src.swagger_sync run --if-due"
        assert action.find("t:WorkingDirectory", NS).text == str(REPO_ROOT)
        assert Path(action.find("t:Command", NS).text).name in ("pythonw.exe", "python.exe")

    @pytest.mark.parametrize(
        ("daily", "now", "expected"),
        [
            ("03:00", datetime(2026, 11, 8, 2, 59), "2026-11-08T03:00:00"),
            ("03:00", datetime(2026, 11, 8, 3, 0), "2026-11-09T03:00:00"),
            ("03:00", datetime(2026, 11, 8, 15, 0), "2026-11-09T03:00:00"),
            ("23:45", datetime(2026, 12, 31, 23, 50), "2027-01-01T23:45:00"),
        ],
    )
    def test_the_first_run_is_always_in_the_future(self, tmp_path, daily, now, expected):
        root = _parse(task.task_xml(_settings(tmp_path, daily_time=daily), now=now, user="u"))
        assert root.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", NS).text == expected

    def test_the_time_limit_follows_the_max_runtime(self, tmp_path):
        root = _parse(
            task.task_xml(
                _settings(tmp_path, max_runtime_s=1800), now=datetime(2026, 1, 1), user="u"
            )
        )
        assert root.find("t:Settings/t:ExecutionTimeLimit", NS).text == "PT40M"

    def test_paths_with_spaces_quote_only_the_command(self, tmp_path):
        """۲۰ — WorkingDirectory ِ درست و مسیرِ دارای فاصله."""
        repo = Path(r"C:\My Projects\agent repo")
        root = _parse(
            task.task_xml(
                _settings(tmp_path),
                now=datetime(2026, 1, 1),
                user="u",
                repo_root=repo,
                python=repo / ".venv" / "Scripts" / "pythonw.exe",
            )
        )
        action = root.find("t:Actions/t:Exec", NS)
        assert action.find("t:Command", NS).text == (
            '"C:\\My Projects\\agent repo\\.venv\\Scripts\\pythonw.exe"'
        )
        assert action.find("t:WorkingDirectory", NS).text == r"C:\My Projects\agent repo"

    def test_paths_and_users_are_xml_escaped(self, tmp_path):
        xml = task.task_xml(
            _settings(tmp_path),
            now=datetime(2026, 1, 1),
            user="A&B\\<x>",
            repo_root=Path(r"C:\a&b"),
            python=Path(r"C:\a&b\.venv\Scripts\pythonw.exe"),
        )
        root = _parse(xml)
        assert root.find("t:Principals/t:Principal/t:UserId", NS).text == "A&B\\<x>"
        assert root.find("t:Actions/t:Exec/t:WorkingDirectory", NS).text == r"C:\a&b"


class TestCommands:
    def test_install_registers_the_generated_xml_and_removes_the_temp_file(self, tmp_path, capsys):
        runner = FakeRunner()
        assert task.main("install-task", runner=runner, now=datetime(2026, 11, 8, 9, 0)) == 0
        (argv,) = runner.calls
        assert argv[:4] == ["schtasks", "/Create", "/TN", task.TASK_NAME] and argv[-1] == "/F"
        assert not Path(argv[argv.index("/XML") + 1]).exists()
        assert "2026-11-09T03:00:00" in runner.xml[0]
        assert "first run 2026-11-09 03:00" in capsys.readouterr().out

    def test_the_configured_time_is_used(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SWAGGER_SYNC_DAILY_TIME", "4:30")
        runner = FakeRunner()
        task.main("install-task", runner=runner, now=datetime(2026, 11, 8, 9, 0))
        assert "2026-11-09T04:30:00" in runner.xml[0]

    def test_an_invalid_time_registers_nothing(self, monkeypatch, capsys):
        monkeypatch.setenv("SWAGGER_SYNC_DAILY_TIME", "25:00")
        runner = FakeRunner()
        assert task.main("install-task", runner=runner) == 3 and runner.calls == []
        assert "SWAGGER_SYNC_DAILY_TIME" in capsys.readouterr().err

    def test_a_schtasks_failure_is_reported(self, capsys):
        runner = FakeRunner(code=1, stderr="ERROR: Access is denied.")
        assert task.main("install-task", runner=runner) == 1
        assert "Access is denied" in capsys.readouterr().err

    def test_update_task_re_registers_with_the_new_time(self, monkeypatch, capsys):
        """۱۹ — ساعت در .env تغییر می‌کند → status ناهماهنگی را می‌گوید → update-task."""
        runner = FakeRunner()
        task.main("install-task", runner=runner, now=datetime(2026, 11, 8, 9, 0))
        monkeypatch.setenv("SWAGGER_SYNC_DAILY_TIME", "05:15")
        capsys.readouterr()
        assert task.main("status", runner=runner) == 0
        out = capsys.readouterr().out
        assert "Registered: daily at 03:00" in out and "differs" in out and "update-task" in out
        assert task.main("update-task", runner=runner, now=datetime(2026, 11, 8, 9, 0)) == 0
        assert "2026-11-09T05:15:00" in runner.xml[-1]
        assert runner.calls[-1][:2] == ["schtasks", "/Create"] and runner.calls[-1][-1] == "/F"
        capsys.readouterr()
        task.main("status", runner=runner)
        assert "matches the current settings" in capsys.readouterr().out

    def test_update_task_requires_an_existing_task(self, capsys):
        runner = FakeRunner(code=1)
        assert task.main("update-task", runner=runner) == 1
        assert all("/Create" not in call for call in runner.calls)
        assert "install-task" in capsys.readouterr().err

    def test_a_missing_interpreter_registers_nothing(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(
            task, "interpreter", lambda repo_root=REPO_ROOT: tmp_path / "missing" / "pythonw.exe"
        )
        runner = FakeRunner()
        assert task.main("install-task", runner=runner) == 1 and runner.calls == []

    def test_a_finite_runtime_sets_a_finite_time_limit(self, tmp_path):
        assert task.time_limit(_settings(tmp_path, max_runtime_s=3600)) == "PT70M"

    def test_uninstall_deletes_only_this_task(self):
        runner = FakeRunner()
        assert task.main("uninstall-task", runner=runner) == 0
        assert runner.calls == [["schtasks", "/Delete", "/TN", task.TASK_NAME, "/F"]]

    def test_status_shows_the_last_run_and_the_task(self, tmp_path, capsys):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "last_run.json").write_text(
            json.dumps(
                {
                    "result": "PARTIAL_FAILURE",
                    "exit_code": 1,
                    "started_at": "a",
                    "finished_at": "b",
                    "sources": [
                        {"label": "Admin", "outcome": "FAILED", "error_kind": "http_status"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        runner = FakeRunner(stdout="Status: Ready", registered_xml="<Task/>")
        assert task.main("status", runner=runner) == 0
        out = capsys.readouterr().out
        assert (
            "PARTIAL_FAILURE" in out
            and "Admin: FAILED http_status" in out
            and "Status: Ready" in out
        )
        assert runner.calls == [
            ["schtasks", "/Query", "/TN", task.TASK_NAME, "/V", "/FO", "LIST"],
            ["schtasks", "/Query", "/TN", task.TASK_NAME, "/XML"],
        ]

    def test_status_without_a_task_or_report(self, capsys):
        assert task.main("status", runner=FakeRunner(code=1)) == 0
        out = capsys.readouterr().out
        assert "none recorded" in out and "not registered" in out

    def test_the_cli_routes_task_commands(self, monkeypatch):
        seen = []
        monkeypatch.setattr(task, "main", lambda command: seen.append(command) or 0)
        commands = ("install-task", "update-task", "uninstall-task", "status")
        for command in commands:
            assert cli.main([command]) == 0
        assert seen == list(commands)


def test_a_configuration_error_in_run_mode_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAGGER_SYNC_RETRY_INTERVAL_SECONDS", "soon")
    assert cli.main(["run"]) == 3
    report = json.loads((tmp_path / "default_logs" / "last_run.json").read_text(encoding="utf-8"))
    assert (
        report["result"] == "CONFIGURATION_ERROR"
        and "SWAGGER_SYNC_RETRY_INTERVAL_SECONDS" in report["error"]
    )
    assert "soon" not in report["error"]


def test_the_manual_script_runs_from_the_repository_root():
    script = (REPO_ROOT / "scripts" / "swagger_sync.cmd").read_bytes()
    assert b"\r\n" in script and b'cd /d "%~dp0.."' in script
    assert b"-m src.swagger_sync run %*" in script and b"exit /b %ERRORLEVEL%" in script
