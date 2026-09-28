"""
tests/unit/scoring/test_scorer_sonar.py — تست breakdown مستقل Sonar در scorer.py

پوشش:
  - _build_sonar_summary (خصوصی، مثل بقیه تست‌های scorer مستقیم تست می‌شود)
  - save_score با/بدون sonar_enabled
  - format_score_comment با/بدون بخش Sonar
"""

import json
from pathlib import Path

from src.agents.code_review.scoring.scorer import (
    _build_sonar_summary,
    save_score,
    format_score_comment,
)


def _comment(severity: str, category: str, source: str) -> dict:
    return {
        "file_path": "f.py", "line": 1, "severity": severity,
        "category": category, "body": "x", "source": source,
    }


# ── _build_sonar_summary ─────────────────────────────────────────────────

class TestBuildSonarSummary:
    def test_disabled_returns_zeroed_summary(self):
        summary = _build_sonar_summary([_comment("major", "correctness", "sonar")], False, None)
        assert summary == {
            "enabled": False, "scan_mode": None, "issue_count": 0,
            "score": 0, "issues": [], "categories": [],
        }

    def test_enabled_counts_only_sonar_sourced_comments(self):
        comments = [
            _comment("critical", "security", "llm"),
            _comment("major", "correctness", "sonar"),
            _comment("minor", "style", "sonar"),
        ]
        summary = _build_sonar_summary(comments, True, "diff")
        assert summary["issue_count"] == 2

    def test_enabled_no_sonar_comments_returns_empty_breakdown(self):
        comments = [_comment("critical", "security", "llm")]
        summary = _build_sonar_summary(comments, True, "full")
        assert summary["issue_count"] == 0
        assert summary["score"] == 0
        assert summary["issues"] == []

    def test_score_matches_severity_scores(self):
        comments = [_comment("major", "correctness", "sonar")]  # -7
        summary = _build_sonar_summary(comments, True, "diff")
        assert summary["score"] == -7

    def test_scan_mode_included_in_summary(self):
        summary = _build_sonar_summary([], True, "full")
        assert summary["scan_mode"] == "full"


# ── save_score ───────────────────────────────────────────────────────────

class TestSaveScoreSonar:
    def test_default_sonar_disabled(self, tmp_path):
        output = tmp_path / "scores.jsonl"
        record = save_score(
            project_id=1, mr_iid=42, user_id="u1",
            comments=[_comment("major", "correctness", "llm")],
            created_at="2024-01-01T00:00:00Z", output_path=output,
        )
        assert record["sonar"]["enabled"] is False

    def test_sonar_enabled_records_breakdown(self, tmp_path):
        output = tmp_path / "scores.jsonl"
        comments = [
            _comment("major", "correctness", "llm"),
            _comment("critical", "security", "sonar"),
        ]
        record = save_score(
            project_id=1, mr_iid=42, user_id="u1", comments=comments,
            created_at="2024-01-01T00:00:00Z", output_path=output,
            sonar_enabled=True, sonar_scan_mode="diff",
        )
        assert record["sonar"]["enabled"] is True
        assert record["sonar"]["scan_mode"] == "diff"
        assert record["sonar"]["issue_count"] == 1

    def test_total_score_includes_both_sources(self, tmp_path):
        output = tmp_path / "scores.jsonl"
        comments = [
            _comment("major", "correctness", "llm"),      # -7
            _comment("critical", "security", "sonar"),    # -10
        ]
        record = save_score(
            project_id=1, mr_iid=42, user_id="u1", comments=comments,
            created_at="2024-01-01T00:00:00Z", output_path=output,
            sonar_enabled=True, sonar_scan_mode="full",
        )
        assert record["total_score"] == -17

    def test_written_jsonl_line_is_valid_json_with_sonar_key(self, tmp_path):
        output = tmp_path / "scores.jsonl"
        save_score(
            project_id=1, mr_iid=42, user_id="u1",
            comments=[_comment("minor", "style", "sonar")],
            created_at="2024-01-01T00:00:00Z", output_path=output,
            sonar_enabled=True, sonar_scan_mode="diff",
        )
        line = Path(output).read_text(encoding="utf-8").strip()
        parsed = json.loads(line)
        assert "sonar" in parsed
        assert parsed["sonar"]["scan_mode"] == "diff"


# ── format_score_comment ─────────────────────────────────────────────────

class TestFormatScoreCommentSonar:
    def _record(self, sonar_enabled: bool, scan_mode: str | None = None) -> dict:
        return {
            "total_score": -7,
            "issues": [{"severity": "major", "count": 1, "score": -7}],
            "categories": [{"category": "correctness", "count": 1, "score": -7}],
            "sonar": {
                "enabled": sonar_enabled, "scan_mode": scan_mode,
                "issue_count": 1 if sonar_enabled else 0,
                "score": -7 if sonar_enabled else 0,
                "issues": [{"severity": "major", "count": 1, "score": -7}] if sonar_enabled else [],
                "categories": [],
            },
            "reviewed_at": "2024-01-01T00:00:00Z",
        }

    def test_no_sonar_section_when_disabled(self):
        text = format_score_comment(self._record(sonar_enabled=False))
        assert "SonarQube" not in text

    def test_sonar_section_present_when_enabled(self):
        text = format_score_comment(self._record(sonar_enabled=True, scan_mode="diff"))
        assert "SonarQube" in text

    def test_diff_mode_label_shown(self):
        text = format_score_comment(self._record(sonar_enabled=True, scan_mode="diff"))
        assert "فقط خطوط تغییرکرده" in text

    def test_full_mode_label_shown(self):
        text = format_score_comment(self._record(sonar_enabled=True, scan_mode="full"))
        assert "اسکن کامل پروژه" in text

    def test_missing_sonar_key_does_not_crash(self):
        # سازگاری با رکوردهای قدیمی که هنوز فیلد "sonar" ندارند
        record = {
            "total_score": 0, "issues": [], "categories": [],
            "reviewed_at": "2024-01-01T00:00:00Z",
        }
        text = format_score_comment(record)
        assert "SonarQube" not in text