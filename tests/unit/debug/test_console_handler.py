"""تست‌های ConsoleHandler."""

import sys
import pytest
from io import StringIO
from unittest.mock import patch
from src.debug.levels import DebugLevel
from src.debug.base_handler import LogRecord
from src.debug.handlers.console_handler import ConsoleHandler


def _record(message: str, level: DebugLevel = DebugLevel.INFO,
            agent: str = "test_agent", **extra) -> LogRecord:
    return LogRecord(
        level=level,
        agent_name=agent,
        message=message,
        timestamp="2024-01-01T12:00:00+00:00",
        extra=extra,
    )


@pytest.fixture
def handler() -> ConsoleHandler:
    return ConsoleHandler(min_level=DebugLevel.TRACE, colorize=False)


class TestConsoleHandlerOutput:
    def test_info_goes_to_stdout(self, handler, capsys):
        handler.emit(_record("hello", DebugLevel.INFO))
        out, err = capsys.readouterr()
        assert "hello" in out
        assert err == ""

    def test_warning_goes_to_stderr(self, handler, capsys):
        handler.emit(_record("warn msg", DebugLevel.WARNING))
        out, err = capsys.readouterr()
        assert "warn msg" in err

    def test_error_goes_to_stderr(self, handler, capsys):
        handler.emit(_record("err msg", DebugLevel.ERROR))
        out, err = capsys.readouterr()
        assert "err msg" in err

    def test_debug_goes_to_stdout(self, handler, capsys):
        handler.emit(_record("debug msg", DebugLevel.DEBUG))
        out, _ = capsys.readouterr()
        assert "debug msg" in out

    def test_trace_goes_to_stdout(self, handler, capsys):
        handler.emit(_record("trace msg", DebugLevel.TRACE))
        out, _ = capsys.readouterr()
        assert "trace msg" in out


class TestConsoleHandlerFormat:
    def test_level_label_in_output(self, handler, capsys):
        handler.emit(_record("msg", DebugLevel.INFO))
        out, _ = capsys.readouterr()
        assert "INFO" in out

    def test_agent_name_in_output(self, handler, capsys):
        handler.emit(_record("msg", agent="my_agent"))
        out, _ = capsys.readouterr()
        assert "my_agent" in out

    def test_message_in_output(self, handler, capsys):
        handler.emit(_record("specific message text"))
        out, _ = capsys.readouterr()
        assert "specific message text" in out

    def test_extra_fields_in_output(self, handler, capsys):
        handler.emit(_record("msg", count=42, user="u1"))
        out, _ = capsys.readouterr()
        assert "count=42" in out
        assert "user='u1'" in out

    def test_no_timestamp_by_default(self, handler, capsys):
        handler.emit(_record("msg"))
        out, _ = capsys.readouterr()
        assert "2024-01-01" not in out

    def test_show_time_includes_timestamp(self, capsys):
        h = ConsoleHandler(colorize=False, show_time=True)
        h.emit(_record("msg"))
        out, _ = capsys.readouterr()
        assert "2024-01-01" in out


class TestConsoleHandlerJsonlFormat:
    def test_jsonl_output_is_valid_json(self, capsys):
        import json
        h = ConsoleHandler(colorize=False, fmt="jsonl")
        h.emit(_record("test"))
        out, _ = capsys.readouterr()
        data = json.loads(out.strip())
        assert data["message"] == "test"

    def test_jsonl_has_required_fields(self, capsys):
        import json
        h = ConsoleHandler(colorize=False, fmt="jsonl")
        h.emit(_record("test", count=5))
        out, _ = capsys.readouterr()
        data = json.loads(out.strip())
        for field in ["timestamp", "level", "agent_name", "message"]:
            assert field in data
        assert data["count"] == 5


class TestConsoleHandlerMinLevel:
    def test_below_min_level_not_printed(self, capsys):
        h = ConsoleHandler(min_level=DebugLevel.WARNING, colorize=False)
        h.handle(_record("ignored", DebugLevel.DEBUG))
        out, err = capsys.readouterr()
        assert "ignored" not in out
        assert "ignored" not in err

    def test_at_min_level_printed(self, capsys):
        h = ConsoleHandler(min_level=DebugLevel.WARNING, colorize=False)
        h.handle(_record("shown", DebugLevel.WARNING))
        _, err = capsys.readouterr()
        assert "shown" in err


class TestConsoleHandlerColorize:
    def test_colorize_true_adds_ansi(self, capsys):
        h = ConsoleHandler(colorize=True)
        h.emit(_record("colored"))
        out, _ = capsys.readouterr()
        assert "\033[" in out

    def test_colorize_false_no_ansi(self, handler, capsys):
        handler.emit(_record("plain"))
        out, _ = capsys.readouterr()
        assert "\033[" not in out

    def test_close_does_not_raise(self, handler):
        handler.close()   # نباید خطا بدهد


class TestConsoleHandlerFromEnv:
    def test_console_driver_from_env(self, tmp_path):
        from unittest.mock import patch
        with patch.dict("os.environ", {
            "LOGGING_ENABLED": "true",
            "LOGGING_LEVEL": "info",
            "LOGGING_DRIVER": "console",
            "LOGGING_DRIVER_CONSOLE_FORMAT": "text",
            "LOGGING_DRIVER_CONSOLE_SHOW_TIME": "false",
        }):
            from src.debug.config import DebugConfig
            config = DebugConfig.from_env()
        assert len(config.handlers) == 1
        from src.debug.handlers.console_handler import ConsoleHandler
        assert isinstance(config.handlers[0], ConsoleHandler)