"""تست‌های GitLabClient، GitLabFetcherAgent، GitLabCommenterAgent."""

import pytest
from unittest.mock import patch, MagicMock, call
from src.agents.code_review.gitlab.agent import GitLabClient, _comment_fingerprint
from src.agents.code_review.state import CodeReviewState, ReviewComment


def _make_state(**kwargs) -> CodeReviewState:
    defaults = {
        "mr_iid": 42, "user_id": "u1", "created_at": "",
        "mr_title": "", "mr_description": "", "diff": "",
        "existing_comments": [], "review_comments": [],
        "score_record": {}, "decision": "", "decision_reason": "", "messages": [],
    }
    defaults.update(kwargs)
    return defaults


def _make_comment(severity="minor", file_path="src/f.py", line=5) -> ReviewComment:
    return ReviewComment(
        file_path=file_path, line=line,
        severity=severity, category="style", body="test body",
    )


# ── GitLabClient ─────────────────────────────────────────────────────────────

class TestGitLabClientGet:
    @patch("src.agents.code_review.gitlab.agent.requests.get")
    def test_get_calls_correct_url(self, mock_get):
        mock_get.return_value.json.return_value = {"id": 1}
        mock_get.return_value.raise_for_status = MagicMock()

        client = GitLabClient()
        client.get_mr(42)

        called_url = mock_get.call_args[0][0]
        assert "merge_requests/42" in called_url

    @patch("src.agents.code_review.gitlab.agent.requests.get")
    def test_get_includes_auth_header(self, mock_get):
        mock_get.return_value.json.return_value = {}
        mock_get.return_value.raise_for_status = MagicMock()

        client = GitLabClient()
        client.get_mr(1)

        headers = mock_get.call_args[1]["headers"]
        assert "PRIVATE-TOKEN" in headers

    @patch("src.agents.code_review.gitlab.agent.requests.get")
    def test_get_mr_diff_concatenates_changes(self, mock_get):
        mock_get.return_value.json.return_value = {
            "changes": [
                {"new_path": "a.py", "diff": "@@ -1 +1 @@\n+new"},
                {"new_path": "b.py", "diff": "@@ -1 +1 @@\n+other"},
            ]
        }
        mock_get.return_value.raise_for_status = MagicMock()

        client = GitLabClient()
        diff = client.get_mr_diff(1)

        assert "a.py" in diff
        assert "b.py" in diff

    @patch("src.agents.code_review.gitlab.agent.requests.get")
    def test_get_mr_diff_empty_changes(self, mock_get):
        mock_get.return_value.json.return_value = {"changes": []}
        mock_get.return_value.raise_for_status = MagicMock()

        client = GitLabClient()
        diff = client.get_mr_diff(1)
        assert diff == ""


class TestGitLabClientPost:
    @patch("src.agents.code_review.gitlab.agent.requests.post")
    def test_post_comment_sends_body(self, mock_post):
        mock_post.return_value.json.return_value = {}
        mock_post.return_value.raise_for_status = MagicMock()

        client = GitLabClient()
        client.post_comment(42, "test comment body")

        payload = mock_post.call_args[1]["json"]
        assert payload["body"] == "test comment body"

    @patch("src.agents.code_review.gitlab.agent.requests.post")
    def test_approve_mr_calls_correct_endpoint(self, mock_post):
        mock_post.return_value.json.return_value = {}
        mock_post.return_value.raise_for_status = MagicMock()

        client = GitLabClient()
        client.approve_mr(42)

        url = mock_post.call_args[0][0]
        assert "approve" in url
        assert "42" in url


# ── GitLabFetcherAgent ────────────────────────────────────────────────────────

class TestGitLabFetcherAgent:
    @patch("src.agents.code_review.gitlab.agent.requests.get")
    def test_fetches_mr_info(self, mock_get):
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.side_effect = [
            {"title": "My MR", "description": "desc", "diff_refs": {}},
            {"changes": []},
            [],   # notes
            [],   # discussions
        ]

        from src.agents.code_review.gitlab.agent import GitLabFetcherAgent
        agent = GitLabFetcherAgent()
        state = _make_state()
        result = agent(state)

        assert result["mr_title"] == "My MR"
        assert result["mr_description"] == "desc"

    @patch("src.agents.code_review.gitlab.agent.requests.get")
    def test_existing_comments_fingerprinted(self, mock_get):
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.side_effect = [
            {"title": "MR", "description": "", "diff_refs": {}},
            {"changes": []},
            [{"body": "existing comment", "position": None}],  # notes
            [],  # discussions
        ]

        from src.agents.code_review.gitlab.agent import GitLabFetcherAgent
        agent = GitLabFetcherAgent()
        result = agent(_make_state())

        assert len(result["existing_comments"]) == 1

    @patch("src.agents.code_review.gitlab.agent.requests.get")
    def test_created_at_set(self, mock_get):
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.side_effect = [
            {"title": "MR", "description": "", "diff_refs": {}},
            {"changes": []},
            [], [],
        ]

        from src.agents.code_review.gitlab.agent import GitLabFetcherAgent
        agent = GitLabFetcherAgent()
        result = agent(_make_state())

        assert result["created_at"] != ""


# ── GitLabCommenterAgent ──────────────────────────────────────────────────────

class TestGitLabCommenterAgent:
    def _make_commenter(self, tmp_path):
        from src.agents.code_review.gitlab.agent import GitLabCommenterAgent
        return GitLabCommenterAgent(score_output_path=str(tmp_path / "scores.jsonl"))

    @patch("src.agents.code_review.gitlab.agent.requests.get")
    @patch("src.agents.code_review.gitlab.agent.requests.post")
    def test_posts_new_comment(self, mock_post, mock_get, tmp_path):
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.return_value = {
            "diff_refs": {"base_sha": "", "head_sha": "", "start_sha": ""}
        }
        mock_post.return_value.raise_for_status = MagicMock()
        mock_post.return_value.json.return_value = {}

        commenter = self._make_commenter(tmp_path)
        state = _make_state(
            review_comments=[_make_comment(line=None)],
            existing_comments=[],
        )
        result = commenter(state)

        # باید پست کرده باشد (comment + score comment)
        assert mock_post.call_count >= 1

    @patch("src.agents.code_review.gitlab.agent.requests.get")
    @patch("src.agents.code_review.gitlab.agent.requests.post")
    def test_skips_duplicate_comment(self, mock_post, mock_get, tmp_path):
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.return_value = {"diff_refs": {}}
        mock_post.return_value.raise_for_status = MagicMock()
        mock_post.return_value.json.return_value = {}

        commenter = self._make_commenter(tmp_path)
        comment = _make_comment(line=None)

        # fingerprint همان comment را از قبل می‌سازیم
        body = f"**[{comment['severity'].upper()}]** `{comment['file_path']}`\n\n{comment['body']}"
        existing_fp = _comment_fingerprint("__note__", None, body)

        state = _make_state(
            review_comments=[comment],
            existing_comments=[existing_fp],
        )
        result = commenter(state)

        # فقط score comment باید پست شده باشد نه review comment
        msg_content = result["messages"][0].content
        assert "تکراری" in msg_content or "skipped" in msg_content.lower() or "1 تکراری" in msg_content

    @patch("src.agents.code_review.gitlab.agent.requests.get")
    @patch("src.agents.code_review.gitlab.agent.requests.post")
    def test_saves_score_record(self, mock_post, mock_get, tmp_path):
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.return_value = {"diff_refs": {}}
        mock_post.return_value.raise_for_status = MagicMock()
        mock_post.return_value.json.return_value = {}

        commenter = self._make_commenter(tmp_path)
        state = _make_state(review_comments=[_make_comment()])
        result = commenter(state)

        assert "score_record" in result
        assert "total_score" in result["score_record"]

    @patch("src.agents.code_review.gitlab.agent.requests.get")
    @patch("src.agents.code_review.gitlab.agent.requests.post")
    def test_score_jsonl_written(self, mock_post, mock_get, tmp_path):
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json.return_value = {"diff_refs": {}}
        mock_post.return_value.raise_for_status = MagicMock()
        mock_post.return_value.json.return_value = {}

        score_path = tmp_path / "scores.jsonl"
        from src.agents.code_review.gitlab.agent import GitLabCommenterAgent
        commenter = GitLabCommenterAgent(score_output_path=str(score_path))
        commenter(state=_make_state())

        assert score_path.exists()
