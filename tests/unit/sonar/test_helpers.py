"""
tests/unit/sonar/test_helpers.py — تست توابع کمکی ماژول sonar

شامل: _parse_task_id، _map_issue، _filter_to_diff، _clone_branch، _run_scanner
هیچ subprocess یا git واقعی اجرا نمی‌شود — همه با @patch mock می‌شوند.
"""

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from src.agents.code_review.sonar.agent import (
    _parse_task_id,
    _map_issue,
    _filter_to_diff,
    _clone_branch,
    _run_scanner,
    SonarScanError,
)


# ── _parse_task_id ───────────────────────────────────────────────────────

class TestParseTaskId:
    def test_extracts_task_id_from_report_file(self, tmp_path):
        scannerwork = tmp_path / ".scannerwork"
        scannerwork.mkdir()
        (scannerwork / "report-task.txt").write_text(
            "projectKey=my-proj\nceTaskId=AYabc123\nserverUrl=http://sonar.test\n",
            encoding="utf-8",
        )
        assert _parse_task_id(scannerwork) == "AYabc123"

    def test_missing_file_raises(self, tmp_path):
        scannerwork = tmp_path / ".scannerwork"
        scannerwork.mkdir()
        with pytest.raises(SonarScanError):
            _parse_task_id(scannerwork)

    def test_missing_field_raises(self, tmp_path):
        scannerwork = tmp_path / ".scannerwork"
        scannerwork.mkdir()
        (scannerwork / "report-task.txt").write_text("projectKey=my-proj\n", encoding="utf-8")
        with pytest.raises(SonarScanError):
            _parse_task_id(scannerwork)


# ── _map_issue ───────────────────────────────────────────────────────────

class TestMapIssue:
    def test_maps_blocker_to_critical_security(self, sonar_issue_factory):
        issue = sonar_issue_factory(severity="BLOCKER", issue_type="VULNERABILITY")
        comment = _map_issue(issue)
        assert comment["severity"] == "critical"
        assert comment["category"] == "security"

    def test_maps_major_bug_to_major_correctness(self, sonar_issue_factory):
        issue = sonar_issue_factory(severity="MAJOR", issue_type="BUG")
        comment = _map_issue(issue)
        assert comment["severity"] == "major"
        assert comment["category"] == "correctness"

    def test_maps_minor_code_smell_to_minor_style(self, sonar_issue_factory):
        issue = sonar_issue_factory(severity="MINOR", issue_type="CODE_SMELL")
        comment = _map_issue(issue)
        assert comment["severity"] == "minor"
        assert comment["category"] == "style"

    def test_unknown_severity_falls_back_to_minor(self, sonar_issue_factory):
        issue = sonar_issue_factory(severity="UNKNOWN_LEVEL")
        comment = _map_issue(issue)
        assert comment["severity"] == "minor"

    def test_extracts_file_path_from_component(self, sonar_issue_factory):
        issue = sonar_issue_factory(component="cr-mr-42:src/agents/foo.py")
        comment = _map_issue(issue)
        assert comment["file_path"] == "src/agents/foo.py"

    def test_extracts_line_from_text_range(self, sonar_issue_factory):
        issue = sonar_issue_factory(line=77)
        comment = _map_issue(issue)
        assert comment["line"] == 77

    def test_body_contains_rule_and_message(self, sonar_issue_factory):
        issue = sonar_issue_factory(message="مشکل نمونه", rule="python:S999")
        comment = _map_issue(issue)
        assert "مشکل نمونه" in comment["body"]
        assert "python:S999" in comment["body"]

    def test_source_is_tagged_as_sonar(self, sonar_issue_factory):
        issue = sonar_issue_factory()
        comment = _map_issue(issue)
        assert comment["source"] == "sonar"


# ── _filter_to_diff ──────────────────────────────────────────────────────

def _comment(file_path: str, line: int | None) -> dict:
    return {
        "file_path": file_path, "line": line, "severity": "major",
        "category": "correctness", "body": "x", "source": "sonar",
    }


class TestFilterToDiff:
    def test_keeps_issue_on_changed_line(self):
        comments = [_comment("src/foo.py", 11)]
        added_lines = {"src/foo.py": {11, 12}}
        result = _filter_to_diff(comments, added_lines)
        assert result == comments

    def test_drops_issue_on_unchanged_line_of_touched_file(self):
        comments = [_comment("src/foo.py", 99)]
        added_lines = {"src/foo.py": {11, 12}}
        result = _filter_to_diff(comments, added_lines)
        assert result == []

    def test_drops_issue_on_file_not_present_in_diff(self):
        comments = [_comment("src/untouched.py", 5)]
        added_lines = {"src/foo.py": {11, 12}}
        result = _filter_to_diff(comments, added_lines)
        assert result == []

    def test_keeps_file_level_issue_when_file_touched(self):
        comments = [_comment("src/foo.py", None)]
        added_lines = {"src/foo.py": {11, 12}}
        result = _filter_to_diff(comments, added_lines)
        assert result == comments

    def test_drops_file_level_issue_when_file_not_touched(self):
        comments = [_comment("src/untouched.py", None)]
        added_lines = {"src/foo.py": {11, 12}}
        result = _filter_to_diff(comments, added_lines)
        assert result == []

    def test_mixed_list_filters_independently(self):
        comments = [
            _comment("src/foo.py", 11),      # نگه داشته می‌شود
            _comment("src/foo.py", 500),     # حذف می‌شود
            _comment("src/other.py", 1),     # حذف می‌شود
        ]
        added_lines = {"src/foo.py": {11, 12}}
        result = _filter_to_diff(comments, added_lines)
        assert result == [comments[0]]

    def test_empty_added_lines_drops_everything(self):
        comments = [_comment("src/foo.py", 11)]
        result = _filter_to_diff(comments, {})
        assert result == []


# ── _clone_branch ────────────────────────────────────────────────────────

class TestCloneBranch:
    @patch("src.agents.code_review.sonar.agent.subprocess.run")
    def test_calls_git_with_correct_args(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        dest = tmp_path / "clone-dest"

        _clone_branch("https://gitlab.example.com/repo.git", "feature/x", dest)

        args = mock_run.call_args.args[0]
        assert args[:3] == ["git", "clone", "--depth"]
        assert "feature/x" in args
        assert str(dest) in args

    @patch("src.agents.code_review.sonar.agent.subprocess.run")
    def test_failure_raises_sonar_scan_error(self, mock_run, tmp_path):
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=128, cmd=["git"], stderr="fatal: repository not found",
        )
        with pytest.raises(SonarScanError):
            _clone_branch("https://gitlab.example.com/repo.git", "missing-branch", tmp_path / "d")


# ── _run_scanner ─────────────────────────────────────────────────────────

class TestRunScanner:
    @patch("src.agents.code_review.sonar.agent.subprocess.run")
    def test_success_returns_scannerwork_path(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        result = _run_scanner(tmp_path, project_key="cr-mr-42")
        assert result == tmp_path / ".scannerwork"

    @patch("src.agents.code_review.sonar.agent.subprocess.run")
    def test_process_error_raises_with_stderr(self, mock_run, tmp_path):
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd=["sonar-scanner"], stderr="ERROR: analysis failed",
        )
        with pytest.raises(SonarScanError, match="analysis failed"):
            _run_scanner(tmp_path, project_key="cr-mr-42")

    @patch("src.agents.code_review.sonar.agent.subprocess.run")
    def test_missing_binary_raises_helpful_error(self, mock_run, tmp_path):
        mock_run.side_effect = FileNotFoundError()
        with pytest.raises(SonarScanError, match="sonar-scanner"):
            _run_scanner(tmp_path, project_key="cr-mr-42", scanner_bin="sonar-scanner")

    @patch("src.agents.code_review.sonar.agent.subprocess.run")
    def test_uses_custom_scanner_binary(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        _run_scanner(tmp_path, project_key="cr-mr-42", scanner_bin="/opt/sonar/bin/sonar-scanner")
        args = mock_run.call_args.args[0]
        assert args[0] == "/opt/sonar/bin/sonar-scanner"