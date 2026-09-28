"""تست‌های CLI entrypoint و argument parsing."""

import pytest
import json
from unittest.mock import patch, MagicMock
from src.cli.__main__ import main


class TestCLIHelp:
    def test_no_command_returns_zero(self):
        result = main([])
        assert result == 0

    def test_help_flag_exits(self):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0

    def test_review_help_exits(self):
        with pytest.raises(SystemExit) as exc:
            main(["review", "--help"])
        assert exc.value.code == 0

    def test_scores_help_exits(self):
        with pytest.raises(SystemExit) as exc:
            main(["scores", "--help"])
        assert exc.value.code == 0


class TestCLILangOption:
    def test_lang_before_command(self):
        result = main(["--lang", "en"])
        assert result == 0

    def test_lang_after_command_scores(self, tmp_path):
        """--lang بعد از command scores باید قبول شود."""
        scores_file = tmp_path / "scores.jsonl"
        scores_file.write_text("")
        result = main(["scores", "--file", str(scores_file), "--lang", "fa"])
        assert result == 0

    def test_lang_after_command_en(self, tmp_path):
        scores_file = tmp_path / "scores.jsonl"
        scores_file.write_text("")
        result = main(["scores", "--file", str(scores_file), "--lang", "en"])
        assert result == 0

    def test_lang_fa_before_scores(self, tmp_path):
        scores_file = tmp_path / "scores.jsonl"
        scores_file.write_text("")
        result = main(["--lang", "fa", "scores", "--file", str(scores_file)])
        assert result == 0

    def test_invalid_lang_exits_with_error(self):
        with pytest.raises(SystemExit) as exc:
            main(["--lang", "de"])
        assert exc.value.code != 0


class TestCLIKeyboardInterrupt:
    def test_keyboard_interrupt_returns_130(self):
        with patch("src.cli.commands.review.run", side_effect=KeyboardInterrupt):
            result = main(["review", "--mr", "1"])
        assert result == 130


class TestScoresCommand:
    def test_scores_missing_file_returns_one(self, tmp_path):
        result = main(["scores", "--file", str(tmp_path / "nonexistent.jsonl")])
        assert result == 1

    def test_scores_empty_file_returns_zero(self, tmp_path):
        empty = tmp_path / "scores.jsonl"
        empty.write_text("")
        result = main(["scores", "--file", str(empty)])
        assert result == 0

    def test_scores_valid_file_returns_zero(self, tmp_path):
        scores_file = tmp_path / "scores.jsonl"
        record = {
            "project_id": 1, "merger_request_id": 42, "user_id": "u1",
            "issues": [{"severity": "minor", "count": 1, "score": -3}],
            "categories": [], "total_score": -3,
            "created_at": "2024-01-01T00:00:00",
            "reviewed_at": "2024-01-01T00:01:00",
        }
        scores_file.write_text(json.dumps(record) + "\n")
        result = main(["scores", "--file", str(scores_file)])
        assert result == 0

    def test_scores_lang_after_options(self, tmp_path):
        """--lang بعد از همه options باید کار کند."""
        scores_file = tmp_path / "scores.jsonl"
        scores_file.write_text("")
        result = main(["scores", "--last", "5", "--file", str(scores_file), "--lang", "fa"])
        assert result == 0

    def test_scores_filter_by_mr(self, tmp_path):
        scores_file = tmp_path / "scores.jsonl"
        records = [
            {"project_id": 1, "merger_request_id": 42, "user_id": "u",
             "issues": [], "categories": [], "total_score": 0,
             "created_at": "2024-01-01", "reviewed_at": "2024-01-01"},
            {"project_id": 1, "merger_request_id": 99, "user_id": "u",
             "issues": [], "categories": [], "total_score": 0,
             "created_at": "2024-01-01", "reviewed_at": "2024-01-01"},
        ]
        scores_file.write_text("\n".join(json.dumps(r) for r in records))
        result = main(["scores", "--file", str(scores_file), "--mr", "42"])
        assert result == 0

    def test_scores_last_n(self, tmp_path):
        scores_file = tmp_path / "scores.jsonl"
        records = [
            {"project_id": 1, "merger_request_id": i, "user_id": "u",
             "issues": [], "categories": [], "total_score": -i,
             "created_at": "2024-01-01", "reviewed_at": "2024-01-01"}
            for i in range(10)
        ]
        scores_file.write_text("\n".join(json.dumps(r) for r in records))
        result = main(["scores", "--file", str(scores_file), "--last", "3"])
        assert result == 0