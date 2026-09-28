"""
tests/unit/sonar/test_agent.py — تست‌های SonarAnalyzerAgent

استراتژی mock:
  - GitLabClient در سطح ماژول patch می‌شود
  - _clone_branch / _run_scanner / _parse_task_id در سطح ماژول patch می‌شوند
  - SonarClient به‌صورت MagicMock مستقیم به سازنده پاس داده می‌شود
"""

from unittest.mock import patch, MagicMock

import pytest

from src.agents.code_review.sonar.agent import SonarAnalyzerAgent, SonarScanError


# diff نمونه‌ای که فقط خط ۱۱ و ۱۲ فایل src/foo.py را تغییرکرده می‌داند
_SAMPLE_DIFF = (
    "### src/foo.py\n"
    "@@ -10,3 +10,4 @@\n"
    " def foo():\n"
    "-    return 1\n"
    "+    return 2\n"
    "+    return 3\n"
    " \n"
)


def _base_state(**overrides) -> dict:
    state = {
        "mr_iid": 42,
        "user_id": "u1",
        "mr_source_branch": "feature/x",
        "diff": _SAMPLE_DIFF,
        "review_comments": [],
    }
    state.update(overrides)
    return state


@pytest.fixture
def mock_sonar_client() -> MagicMock:
    client = MagicMock()
    client.poll_task.return_value = {"status": "SUCCESS"}
    client.fetch_issues.return_value = []
    return client


@pytest.fixture
def mock_gitlab_client_cls():
    with patch("src.agents.code_review.sonar.agent.GitLabClient") as mock_cls:
        instance = MagicMock()
        instance.get_project.return_value = {"http_url_to_repo": "https://gitlab.example.com/repo.git"}
        mock_cls.return_value = instance
        yield mock_cls


class TestSonarAnalyzerAgentConstruction:
    def test_invalid_scan_mode_raises(self, mock_sonar_client, mock_gitlab_client_cls):
        with pytest.raises(ValueError):
            SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="new-code")

    def test_valid_scan_modes_accepted(self, mock_sonar_client, mock_gitlab_client_cls):
        SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="full")
        SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="diff")


class TestSonarAnalyzerAgentMissingBranch:
    def test_skips_scan_and_returns_empty_issues(self, mock_sonar_client, mock_gitlab_client_cls):
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="full")
        result = agent(_base_state(mr_source_branch=""))
        assert result["sonar_issues"] == []

    def test_still_reports_sonar_enabled_and_mode(self, mock_sonar_client, mock_gitlab_client_cls):
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="diff")
        result = agent(_base_state(mr_source_branch=""))
        assert result["sonar_enabled"] is True
        assert result["sonar_scan_mode"] == "diff"

    def test_does_not_call_poll_task_when_branch_missing(self, mock_sonar_client, mock_gitlab_client_cls):
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="full")
        agent(_base_state(mr_source_branch=""))
        mock_sonar_client.poll_task.assert_not_called()


@patch("src.agents.code_review.sonar.agent._parse_task_id", return_value="task-1")
@patch("src.agents.code_review.sonar.agent._run_scanner")
@patch("src.agents.code_review.sonar.agent._clone_branch")
class TestSonarAnalyzerAgentFullMode:
    def test_returns_all_issues_unfiltered(
        self, mock_clone, mock_run_scanner, mock_parse_task_id,
        mock_sonar_client, mock_gitlab_client_cls, sonar_issue_factory,
    ):
        mock_sonar_client.fetch_issues.return_value = [
            sonar_issue_factory(component="proj:src/foo.py", line=11),   # روی diff
            sonar_issue_factory(component="proj:src/foo.py", line=500),  # خارج از diff
            sonar_issue_factory(component="proj:src/untouched.py", line=1),  # فایل دست‌نخورده
        ]
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="full")

        result = agent(_base_state())

        assert len(result["sonar_issues"]) == 3

    def test_sonar_scan_mode_reported_as_full(
        self, mock_clone, mock_run_scanner, mock_parse_task_id,
        mock_sonar_client, mock_gitlab_client_cls,
    ):
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="full")
        result = agent(_base_state())
        assert result["sonar_scan_mode"] == "full"


@patch("src.agents.code_review.sonar.agent._parse_task_id", return_value="task-1")
@patch("src.agents.code_review.sonar.agent._run_scanner")
@patch("src.agents.code_review.sonar.agent._clone_branch")
class TestSonarAnalyzerAgentDiffMode:
    def test_filters_out_issues_outside_diff(
        self, mock_clone, mock_run_scanner, mock_parse_task_id,
        mock_sonar_client, mock_gitlab_client_cls, sonar_issue_factory,
    ):
        mock_sonar_client.fetch_issues.return_value = [
            sonar_issue_factory(component="proj:src/foo.py", line=11),       # روی diff — می‌ماند
            sonar_issue_factory(component="proj:src/foo.py", line=500),      # خارج از diff — حذف
            sonar_issue_factory(component="proj:src/untouched.py", line=1),  # فایل دست‌نخورده — حذف
        ]
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="diff")

        result = agent(_base_state())

        assert len(result["sonar_issues"]) == 1
        assert result["sonar_issues"][0]["line"] == 11

    def test_sonar_scan_mode_reported_as_diff(
        self, mock_clone, mock_run_scanner, mock_parse_task_id,
        mock_sonar_client, mock_gitlab_client_cls,
    ):
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="diff")
        result = agent(_base_state())
        assert result["sonar_scan_mode"] == "diff"

    def test_empty_diff_filters_everything(
        self, mock_clone, mock_run_scanner, mock_parse_task_id,
        mock_sonar_client, mock_gitlab_client_cls, sonar_issue_factory,
    ):
        mock_sonar_client.fetch_issues.return_value = [
            sonar_issue_factory(component="proj:src/foo.py", line=11),
        ]
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="diff")

        result = agent(_base_state(diff=""))

        assert result["sonar_issues"] == []


@patch("src.agents.code_review.sonar.agent._parse_task_id", return_value="task-1")
@patch("src.agents.code_review.sonar.agent._run_scanner")
@patch("src.agents.code_review.sonar.agent._clone_branch")
class TestSonarAnalyzerAgentSuccessPath:
    def test_no_issues_returns_empty_list(
        self, mock_clone, mock_run_scanner, mock_parse_task_id,
        mock_sonar_client, mock_gitlab_client_cls,
    ):
        mock_sonar_client.fetch_issues.return_value = []
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="full")

        result = agent(_base_state())

        assert result["sonar_issues"] == []

    def test_clone_called_with_correct_branch(
        self, mock_clone, mock_run_scanner, mock_parse_task_id,
        mock_sonar_client, mock_gitlab_client_cls,
    ):
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="full")
        agent(_base_state(mr_source_branch="feature/payments"))

        called_branch = mock_clone.call_args.args[1]
        assert called_branch == "feature/payments"


class TestSonarAnalyzerAgentFailurePath:
    @patch("src.agents.code_review.sonar.agent._clone_branch", side_effect=SonarScanError("clone failed"))
    def test_clone_failure_returns_empty_issues(self, mock_clone, mock_sonar_client, mock_gitlab_client_cls):
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="full")
        result = agent(_base_state())
        assert result["sonar_issues"] == []

    @patch("src.agents.code_review.sonar.agent._clone_branch", side_effect=SonarScanError("clone failed"))
    def test_clone_failure_message_mentions_error(self, mock_clone, mock_sonar_client, mock_gitlab_client_cls):
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="full")
        result = agent(_base_state())
        assert "ناموفق" in result["messages"][0].content

    @patch("src.agents.code_review.sonar.agent._clone_branch", side_effect=SonarScanError("clone failed"))
    def test_failure_still_reports_sonar_enabled_and_mode(self, mock_clone, mock_sonar_client, mock_gitlab_client_cls):
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="diff")
        result = agent(_base_state())
        assert result["sonar_enabled"] is True
        assert result["sonar_scan_mode"] == "diff"

    @patch("src.agents.code_review.sonar.agent._parse_task_id", return_value="task-1")
    @patch("src.agents.code_review.sonar.agent._run_scanner")
    @patch("src.agents.code_review.sonar.agent._clone_branch")
    def test_task_not_success_returns_empty_issues(
        self, mock_clone, mock_run_scanner, mock_parse_task_id,
        mock_sonar_client, mock_gitlab_client_cls,
    ):
        mock_sonar_client.poll_task.return_value = {"status": "FAILED"}
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="full")

        result = agent(_base_state())

        assert result["sonar_issues"] == []


class TestSonarAnalyzerAgentCleanup:
    @patch("src.agents.code_review.sonar.agent._parse_task_id", return_value="task-1")
    @patch("src.agents.code_review.sonar.agent._run_scanner")
    @patch("src.agents.code_review.sonar.agent._clone_branch")
    @patch("src.agents.code_review.sonar.agent.SONAR_DELETE_PROJECT_AFTER_SCAN", True)
    def test_deletes_project_when_configured(
        self, mock_clone, mock_run_scanner, mock_parse_task_id,
        mock_sonar_client, mock_gitlab_client_cls,
    ):
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="full")
        agent(_base_state())
        mock_sonar_client.delete_project.assert_called_once()

    @patch("src.agents.code_review.sonar.agent._parse_task_id", return_value="task-1")
    @patch("src.agents.code_review.sonar.agent._run_scanner")
    @patch("src.agents.code_review.sonar.agent._clone_branch")
    @patch("src.agents.code_review.sonar.agent.SONAR_DELETE_PROJECT_AFTER_SCAN", False)
    def test_does_not_delete_project_when_disabled(
        self, mock_clone, mock_run_scanner, mock_parse_task_id,
        mock_sonar_client, mock_gitlab_client_cls,
    ):
        agent = SonarAnalyzerAgent(sonar_client=mock_sonar_client, scan_mode="full")
        agent(_base_state())
        mock_sonar_client.delete_project.assert_not_called()