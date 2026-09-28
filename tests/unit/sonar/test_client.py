"""
tests/unit/sonar/test_client.py — تست‌های SonarClient

قوانین این فایل:
  - هیچ اتصال شبکه واقعی برقرار نمی‌شود؛ requests.get/post همیشه mock می‌شوند
  - poll_task با interval=0 صدا زده می‌شود تا تست‌ها کند نشوند
"""

from unittest.mock import patch, MagicMock

import pytest

from src.agents.code_review.sonar.agent import SonarClient, SonarScanError


@pytest.fixture
def client() -> SonarClient:
    return SonarClient(base_url="http://sonar.test", token="dummy-token")


# ── poll_task ──────────────────────────────────────────────────────────────

class TestPollTask:
    @patch("src.agents.code_review.sonar.agent.requests.get")
    def test_returns_task_when_status_success(self, mock_get, client):
        mock_get.return_value = MagicMock(
            json=lambda: {"task": {"status": "SUCCESS", "id": "t1"}},
        )
        task = client.poll_task(task_id="t1", interval=0, timeout=5)
        assert task["status"] == "SUCCESS"

    @patch("src.agents.code_review.sonar.agent.requests.get")
    def test_returns_task_when_status_failed(self, mock_get, client):
        mock_get.return_value = MagicMock(
            json=lambda: {"task": {"status": "FAILED", "id": "t1"}},
        )
        task = client.poll_task(task_id="t1", interval=0, timeout=5)
        assert task["status"] == "FAILED"

    @patch("src.agents.code_review.sonar.agent.requests.get")
    def test_raises_on_timeout(self, mock_get, client):
        mock_get.return_value = MagicMock(
            json=lambda: {"task": {"status": "IN_PROGRESS", "id": "t1"}},
        )
        with pytest.raises(SonarScanError):
            client.poll_task(task_id="t1", interval=0, timeout=0)

    @patch("src.agents.code_review.sonar.agent.requests.get")
    def test_calls_correct_endpoint_with_task_id(self, mock_get, client):
        mock_get.return_value = MagicMock(
            json=lambda: {"task": {"status": "SUCCESS", "id": "t1"}},
        )
        client.poll_task(task_id="t1", interval=0, timeout=5)
        called_url = mock_get.call_args.args[0]
        called_params = mock_get.call_args.kwargs["params"]
        assert called_url == "http://sonar.test/api/ce/task"
        assert called_params == {"id": "t1"}


# ── fetch_issues ───────────────────────────────────────────────────────────

class TestFetchIssues:
    @patch("src.agents.code_review.sonar.agent.requests.get")
    def test_single_page_returns_all_issues(self, mock_get, client, sample_sonar_issue):
        mock_get.return_value = MagicMock(
            json=lambda: {"issues": [sample_sonar_issue, sample_sonar_issue], "total": 2},
        )
        issues = client.fetch_issues(project_key="proj-1")
        assert len(issues) == 2

    @patch("src.agents.code_review.sonar.agent.requests.get")
    def test_pagination_across_multiple_pages(self, mock_get, client, sonar_issue_factory):
        page1 = [sonar_issue_factory() for _ in range(100)]
        page2 = [sonar_issue_factory() for _ in range(1)]

        mock_get.side_effect = [
            MagicMock(json=lambda: {"issues": page1, "total": 101}),
            MagicMock(json=lambda: {"issues": page2, "total": 101}),
        ]
        issues = client.fetch_issues(project_key="proj-1")
        assert len(issues) == 101

    @patch("src.agents.code_review.sonar.agent.requests.get")
    def test_no_issues_returns_empty_list(self, mock_get, client):
        mock_get.return_value = MagicMock(json=lambda: {"issues": [], "total": 0})
        issues = client.fetch_issues(project_key="proj-empty")
        assert issues == []

    @patch("src.agents.code_review.sonar.agent.requests.get")
    def test_uses_component_keys_filter(self, mock_get, client):
        mock_get.return_value = MagicMock(json=lambda: {"issues": [], "total": 0})
        client.fetch_issues(project_key="my-proj")
        params = mock_get.call_args.kwargs["params"]
        assert params["componentKeys"] == "my-proj"
        assert params["resolved"] == "false"


# ── delete_project ───────────────────────────────────────────────────────

class TestDeleteProject:
    @patch("src.agents.code_review.sonar.agent.requests.post")
    def test_calls_correct_endpoint(self, mock_post, client):
        mock_post.return_value = MagicMock()
        client.delete_project(project_key="cr-mr-42")
        called_url = mock_post.call_args.args[0]
        called_params = mock_post.call_args.kwargs["params"]
        assert called_url == "http://sonar.test/api/projects/delete"
        assert called_params == {"project": "cr-mr-42"}

    @patch("src.agents.code_review.sonar.agent.requests.post")
    def test_swallows_exception_without_raising(self, mock_post, client):
        mock_post.side_effect = Exception("network down")
        # نباید exception بالا بره — فقط لاگ می‌شود (best-effort cleanup)
        client.delete_project(project_key="cr-mr-42")