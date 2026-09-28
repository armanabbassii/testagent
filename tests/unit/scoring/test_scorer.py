"""تست‌های scoring/scorer.py."""

import json
import pytest
from src.agents.code_review.scoring.scorer import (
    calculate_score,
    save_score,
    format_score_comment,
    SEVERITY_SCORES,
)
from src.agents.code_review.state import ReviewComment


# ── Helpers ───────────────────────────────────────────────────────────────────

def _comment(severity: str, category: str = "general",
             file_path: str = "f.py", line: int | None = 1) -> ReviewComment:
    return ReviewComment(
        file_path=file_path, line=line, severity=severity,
        category=category, body="test body",
    )


# ── calculate_score ───────────────────────────────────────────────────────────

class TestCalculateScore:
    def test_empty_list_returns_zero(self):
        result = calculate_score([])
        assert result["total_score"] == 0
        assert result["issues"] == []
        assert result["categories"] == []

    def test_single_critical(self):
        result = calculate_score([_comment("critical", "security")])
        assert result["total_score"] == -10
        assert len(result["issues"]) == 1
        assert result["issues"][0] == {"severity": "critical", "count": 1, "score": -10}

    def test_single_major(self):
        result = calculate_score([_comment("major", "correctness")])
        assert result["total_score"] == -7

    def test_single_minor(self):
        result = calculate_score([_comment("minor", "style")])
        assert result["total_score"] == -3

    def test_single_suggestion(self):
        result = calculate_score([_comment("suggestion", "performance")])
        assert result["total_score"] == 0

    def test_mixed_severity_total(self, mixed_comments):
        # critical(-10) + major(-7) + minor(-3) + suggestion(0) = -21
        result = calculate_score(mixed_comments)
        assert result["total_score"] == -20

    def test_multiple_same_severity(self):
        comments = [_comment("major"), _comment("major"), _comment("major")]
        result = calculate_score(comments)
        issue = next(i for i in result["issues"] if i["severity"] == "major")
        assert issue["count"] == 3
        assert issue["score"] == -21

    def test_severity_issues_sorted_by_severity_order(self):
        comments = [
            _comment("suggestion"), _comment("critical"), _comment("minor")
        ]
        result = calculate_score(comments)
        order = [i["severity"] for i in result["issues"]]
        assert order == ["critical", "minor", "suggestion"]

    def test_only_nonzero_severities_in_issues(self):
        result = calculate_score([_comment("critical")])
        severities = [i["severity"] for i in result["issues"]]
        assert "major" not in severities
        assert "minor" not in severities

    def test_category_count_correct(self):
        comments = [
            _comment("critical", "security"),
            _comment("major", "security"),
            _comment("minor", "style"),
        ]
        result = calculate_score(comments)
        security = next(c for c in result["categories"] if c["category"] == "security")
        assert security["count"] == 2
        assert security["score"] == -17   # -10 + -7

    def test_unknown_severity_not_counted_in_issues_list(self):
        """unknown severity در SEVERITY_ORDER نیست — در issues ظاهر نمی‌شود."""
        comment = ReviewComment(
            file_path="f.py", line=1, severity="unknown",
            category="general", body="body"
        )
        result = calculate_score([comment])
        issue_severities = [i["severity"] for i in result["issues"]]
        assert "unknown" not in issue_severities
        # total_score از issues محاسبه می‌شود که unknown ندارد
        assert result["total_score"] == 0

    def test_severity_scores_constants(self):
        assert SEVERITY_SCORES["critical"] == -10
        assert SEVERITY_SCORES["major"] == -7
        assert SEVERITY_SCORES["minor"] == -3
        assert SEVERITY_SCORES["suggestion"] == 0


# ── save_score ────────────────────────────────────────────────────────────────

class TestSaveScore:
    def test_creates_jsonl_file(self, tmp_path, mixed_comments):
        path = tmp_path / "scores.jsonl"
        save_score(
            project_id=1, mr_iid=42, user_id="u1",
            comments=mixed_comments, created_at="2024-01-01T00:00:00",
            output_path=path,
        )
        assert path.exists()

    def test_output_is_valid_json(self, tmp_path, mixed_comments):
        path = tmp_path / "scores.jsonl"
        save_score(1, 42, "u1", mixed_comments, "2024-01-01", output_path=path)
        record = json.loads(path.read_text())
        assert isinstance(record, dict)

    def test_record_has_required_fields(self, tmp_path, mixed_comments):
        path = tmp_path / "scores.jsonl"
        record = save_score(1, 42, "u1", mixed_comments, "2024-01-01", output_path=path)
        for field in ["project_id", "merger_request_id", "user_id",
                      "issues", "categories", "total_score",
                      "created_at", "reviewed_at"]:
            assert field in record

    def test_project_id_stored_as_int(self, tmp_path):
        path = tmp_path / "scores.jsonl"
        record = save_score("999", 1, "u1", [], "2024-01-01", output_path=path)
        assert isinstance(record["project_id"], int)
        assert record["project_id"] == 999

    def test_appends_multiple_records(self, tmp_path, mixed_comments):
        path = tmp_path / "scores.jsonl"
        save_score(1, 1, "u1", mixed_comments, "2024-01-01", output_path=path)
        save_score(1, 2, "u1", mixed_comments, "2024-01-01", output_path=path)
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "a" / "b" / "scores.jsonl"
        save_score(1, 1, "u1", [], "2024-01-01", output_path=path)
        assert path.exists()

    def test_total_score_in_record_matches_calculate(self, tmp_path, mixed_comments):
        path = tmp_path / "scores.jsonl"
        record = save_score(1, 1, "u1", mixed_comments, "2024-01-01", output_path=path)
        expected = calculate_score(mixed_comments)["total_score"]
        assert record["total_score"] == expected


# ── format_score_comment ──────────────────────────────────────────────────────

class TestFormatScoreComment:
    def _make_record(self, total: int, issues=None, categories=None):
        return {
            "total_score": total,
            "issues": issues or [],
            "categories": categories or [],
            "reviewed_at": "2024-01-01T00:00:00+00:00",
        }

    def test_zero_score_clean_code_message(self):
        comment = format_score_comment(self._make_record(0))
        assert "Clean code" in comment
        assert "Score: 0" in comment

    def test_minor_score_range(self):
        comment = format_score_comment(self._make_record(-3))
        assert "Minor issues" in comment

    def test_moderate_score_range(self):
        comment = format_score_comment(self._make_record(-10))
        assert "Several issues" in comment

    def test_severe_score_range(self):
        comment = format_score_comment(self._make_record(-20))
        assert "Significant issues" in comment

    def test_issues_table_present(self):
        issues = [{"severity": "critical", "count": 1, "score": -10}]
        comment = format_score_comment(self._make_record(-10, issues=issues))
        assert "| Severity | Count | Score |" in comment
        assert "Critical" in comment

    def test_categories_table_present(self):
        cats = [{"category": "security", "count": 1, "score": -10}]
        comment = format_score_comment(self._make_record(-10, categories=cats))
        assert "| Category | Count | Score |" in comment
        assert "Security" in comment

    def test_reviewed_at_in_comment(self):
        comment = format_score_comment(self._make_record(0))
        assert "2024-01-01T00:00:00" in comment

    def test_returns_string(self):
        assert isinstance(format_score_comment(self._make_record(0)), str)