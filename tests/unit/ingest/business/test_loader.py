"""تست‌های BusinessJsonlLoader."""

import json
import pytest
from src.ingest.business.loader import BusinessJsonlLoader


def _write_jsonl(path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")


class TestBusinessJsonlLoaderLoad:
    def test_loads_valid_records(self, tmp_path):
        path = tmp_path / "services.jsonl"
        _write_jsonl(path, [{"id": 1, "name": "پرداخت", "webservices": []}])
        records = BusinessJsonlLoader().load(path)
        assert len(records) == 1
        assert records[0]["id"] == 1

    def test_skips_invalid_json_line(self, tmp_path):
        path = tmp_path / "services.jsonl"
        path.write_text('{"id": 1, "name": "a"}\nnot json {{\n', encoding="utf-8")
        records = BusinessJsonlLoader().load(path)
        assert len(records) == 1

    def test_skips_record_missing_required_fields(self, tmp_path):
        path = tmp_path / "services.jsonl"
        _write_jsonl(path, [
            {"id": 1},
            {"name": "b"},
            {"id": 2, "name": "ok"},
        ])
        records = BusinessJsonlLoader().load(path)
        assert len(records) == 1
        assert records[0]["id"] == 2

    def test_id_zero_is_still_valid(self, tmp_path):
        """id=0 نباید به دلیل falsy بودن رد شود."""
        path = tmp_path / "services.jsonl"
        _write_jsonl(path, [{"id": 0, "name": "a"}])
        records = BusinessJsonlLoader().load(path)
        assert len(records) == 1

    def test_skips_empty_lines(self, tmp_path):
        path = tmp_path / "services.jsonl"
        path.write_text('{"id": 1, "name": "a"}\n\n\n', encoding="utf-8")
        records = BusinessJsonlLoader().load(path)
        assert len(records) == 1

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            BusinessJsonlLoader().load(tmp_path / "nonexistent.jsonl")

    def test_empty_file_returns_empty_list(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        assert BusinessJsonlLoader().load(path) == []