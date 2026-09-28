"""
tests/unit/gitlab/test_get_project.py — تست متد GitLabClient.get_project

این متد برای SonarAnalyzerAgent اضافه شد تا http_url_to_repo برای clone
در دسترس باشد.
"""

from unittest.mock import patch, MagicMock

from src.agents.code_review.gitlab.agent import GitLabClient


class TestGetProject:
    @patch("src.agents.code_review.gitlab.agent.requests.get")
    def test_returns_project_payload(self, mock_get):
        mock_get.return_value = MagicMock(
            json=lambda: {"http_url_to_repo": "https://gitlab.example.com/group/repo.git"},
        )
        mock_get.return_value.raise_for_status = lambda: None

        client = GitLabClient()
        project = client.get_project()

        assert project["http_url_to_repo"] == "https://gitlab.example.com/group/repo.git"

    @patch("src.agents.code_review.gitlab.agent.requests.get")
    def test_calls_correct_endpoint(self, mock_get):
        mock_get.return_value = MagicMock(json=lambda: {})
        mock_get.return_value.raise_for_status = lambda: None

        client = GitLabClient()
        client.get_project()

        called_url = mock_get.call_args.args[0]
        assert called_url.endswith(f"/projects/{client._project_id}")