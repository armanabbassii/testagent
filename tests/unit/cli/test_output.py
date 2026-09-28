"""تست‌های cli/output.py."""

import pytest
from unittest.mock import patch
from src.cli import output as out


class TestColorFunctions:
    def test_bold_returns_string(self):
        result = out.bold("test")
        assert "test" in result

    def test_dim_returns_string(self):
        result = out.dim("test")
        assert "test" in result

    def test_severity_color_critical(self):
        result = out.severity_color("critical", "CRITICAL")
        assert "CRITICAL" in result

    def test_severity_color_unknown_still_returns_text(self):
        result = out.severity_color("unknown_sev", "TEXT")
        assert "TEXT" in result

    def test_decision_color_approve(self):
        result = out.decision_color("approve", "APPROVED")
        assert "APPROVED" in result

    def test_decision_color_reject(self):
        result = out.decision_color("reject", "REJECTED")
        assert "REJECTED" in result


class TestPrintFunctions:
    def test_header_prints_text(self, capsys):
        out.header("Test Header")
        output = capsys.readouterr().out
        assert "Test Header" in output

    def test_section_prints_text(self, capsys):
        out.section("Test Section")
        output = capsys.readouterr().out
        assert "Test Section" in output

    def test_info_prints_text(self, capsys):
        out.info("info message")
        output = capsys.readouterr().out
        assert "info message" in output

    def test_success_prints_text(self, capsys):
        out.success("success message")
        output = capsys.readouterr().out
        assert "success message" in output

    def test_warning_prints_text(self, capsys):
        out.warning("warning message")
        output = capsys.readouterr().out
        assert "warning message" in output

    def test_step_prints_text(self, capsys):
        out.step("step message")
        output = capsys.readouterr().out
        assert "step message" in output

    def test_score_row_prints_label_and_value(self, capsys):
        out.score_row("Total Score", -10)
        output = capsys.readouterr().out
        assert "Total Score" in output
        assert "-10" in output

    def test_review_comment_prints_severity(self, capsys):
        out.review_comment("critical", "security", "src/auth.py", 42, "SQL injection")
        output = capsys.readouterr().out
        assert "CRITICAL" in output
        assert "src/auth.py" in output

    def test_review_comment_no_line(self, capsys):
        out.review_comment("minor", "style", "src/f.py", None, "style issue")
        output = capsys.readouterr().out
        assert "src/f.py" in output
        assert "None" not in output
