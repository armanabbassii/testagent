"""تست‌های FileHandler."""

import json
import pytest
from pathlib import Path
from src.debug.levels import DebugLevel
from src.debug.base_handler import LogRecord
from src.debug.handlers.file_handler import FileHandler


def _make_record(message: str, level: DebugLevel = DebugLevel.INFO,
                 agent: str = "test_agent", **extra) -> LogRecord:
    return LogRecord(
        level=level,
        agent_name=agent,
        message=message,
        timestamp="2024-01-01T00:00:00+00:00",
        extra=extra,
    )


@pytest.fixture
def text_handler(tmp_path) -> FileHandler:
    h = FileHandler(path=tmp_path / "test.log", fmt="text", max_bytes=0)
    yield h
    h.close()


@pytest.fixture
def jsonl_handler(tmp_path) -> FileHandler:
    h = FileHandler(path=tmp_path / "test.jsonl", fmt="jsonl", max_bytes=0)
    yield h
    h.close()


class TestFileHandlerTextFormat:
    def test_writes_message_to_file(self, text_handler, tmp_path):
        text_handler.emit(_make_record("hello world"))
        content = (tmp_path / "test.log").read_text()
        assert "hello world" in content

    def test_level_in_output(self, text_handler, tmp_path):
        text_handler.emit(_make_record("msg", level=DebugLevel.WARNING))
        content = (tmp_path / "test.log").read_text()
        assert "WARNING" in content

    def test_agent_name_in_output(self, text_handler, tmp_path):
        text_handler.emit(_make_record("msg", agent="my_agent"))
        content = (tmp_path / "test.log").read_text()
        assert "my_agent" in content

    def test_extra_fields_in_output(self, text_handler, tmp_path):
        text_handler.emit(_make_record("msg", count=42))
        content = (tmp_path / "test.log").read_text()
        assert "count=42" in content

    def test_multiple_records_appended(self, text_handler, tmp_path):
        text_handler.emit(_make_record("first"))
        text_handler.emit(_make_record("second"))
        lines = (tmp_path / "test.log").read_text().strip().splitlines()
        assert len(lines) == 2
        assert "first" in lines[0]
        assert "second" in lines[1]


class TestFileHandlerJsonlFormat:
    def test_valid_json_per_line(self, jsonl_handler, tmp_path):
        jsonl_handler.emit(_make_record("test msg", count=5))
        lines = (tmp_path / "test.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["message"] == "test msg"
        assert data["count"] == 5

    def test_json_has_required_fields(self, jsonl_handler, tmp_path):
        jsonl_handler.emit(_make_record("msg"))
        data = json.loads((tmp_path / "test.jsonl").read_text())
        assert "timestamp" in data
        assert "level" in data
        assert "agent_name" in data
        assert "message" in data

    def test_multiple_lines_all_valid_json(self, jsonl_handler, tmp_path):
        for i in range(5):
            jsonl_handler.emit(_make_record(f"msg {i}"))
        lines = (tmp_path / "test.jsonl").read_text().strip().splitlines()
        assert len(lines) == 5
        for line in lines:
            json.loads(line)   # نباید exception بدهد


class TestFileHandlerMinLevel:
    def test_record_below_min_level_not_written(self, tmp_path):
        handler = FileHandler(tmp_path / "out.log", min_level=DebugLevel.WARNING, max_bytes=0)
        handler.handle(_make_record("ignored", level=DebugLevel.DEBUG))
        handler.close()
        assert not (tmp_path / "out.log").exists() or \
               (tmp_path / "out.log").read_text() == ""

    def test_record_at_min_level_written(self, tmp_path):
        handler = FileHandler(tmp_path / "out.log", min_level=DebugLevel.WARNING, max_bytes=0)
        handler.handle(_make_record("written", level=DebugLevel.WARNING))
        handler.close()
        assert "written" in (tmp_path / "out.log").read_text()


class TestFileHandlerRotation:
    def test_rotation_creates_backup(self, tmp_path):
        handler = FileHandler(
            tmp_path / "rotating.log",
            max_bytes=50,
            backup_count=3,
        )
        # بنویس تا rotation ایجاد شود
        for i in range(20):
            handler.emit(_make_record(f"message number {i:03d}"))
        handler.close()

        # باید حداقل یک فایل backup وجود داشته باشد
        backups = list(tmp_path.glob("rotating.log.*"))
        assert len(backups) >= 1

    def test_invalid_format_raises(self, tmp_path):
        with pytest.raises(ValueError, match="نامعتبر"):
            FileHandler(tmp_path / "out.log", fmt="xml")

    def test_creates_parent_directories(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c" / "test.log"
        handler = FileHandler(nested, max_bytes=0)
        handler.emit(_make_record("msg"))
        handler.close()
        assert nested.exists()