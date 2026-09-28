"""
tests/unit/gitlab/test_commenter_sonar_wiring.py — تست وایرینگ sonar_enabled/
sonar_scan_mode از state به save_score در GitLabCommenterAgent

نکته: save_score/format_score_comment داخل __call__ به‌صورت local import
می‌شوند ("from src.agents.code_review.scoring import ...")، پس patch باید
روی خود ماژول src.agents.code_review.scoring اعمال شود، نه scorer.py.
"""

from unittest.mock import patch, MagicMock

from src.agents.code_review.gitlab.agent import GitLabCommenterAgent


def _base_state(**overrides) -> dict:
    state = {
        "mr_iid": 42,
        "user_id": "u1",
        "created_at": "",
        "review_comments": [],
        "existing_comments": [],
        "sonar_enabled": False,
        "sonar_scan_mode": None,
    }
    state.update(overrides)
    return state


def _setup_gitlab_mock(mock_gl_cls) -> MagicMock:
    instance = MagicMock()
    instance.get_mr.return_value = {"diff_refs": {"base_sha": "", "head_sha": "", "start_sha": ""}}
    instance._project_id = "123"
    mock_gl_cls.return_value = instance
    return instance


@patch("src.agents.code_review.scoring.format_score_comment", return_value="score comment")
@patch("src.agents.code_review.scoring.save_score")
@patch("src.agents.code_review.gitlab.agent.GitLabClient")
class TestCommenterSonarWiring:
    def test_passes_sonar_enabled_true_and_mode(self, mock_gl_cls, mock_save_score, mock_format):
        _setup_gitlab_mock(mock_gl_cls)
        mock_save_score.return_value = {"total_score": 0, "sonar": {"enabled": True}}

        commenter = GitLabCommenterAgent()
        commenter(_base_state(sonar_enabled=True, sonar_scan_mode="diff"))

        _, kwargs = mock_save_score.call_args
        assert kwargs["sonar_enabled"] is True
        assert kwargs["sonar_scan_mode"] == "diff"

    def test_passes_sonar_enabled_false_by_default(self, mock_gl_cls, mock_save_score, mock_format):
        _setup_gitlab_mock(mock_gl_cls)
        mock_save_score.return_value = {"total_score": 0, "sonar": {"enabled": False}}

        commenter = GitLabCommenterAgent()
        commenter(_base_state())

        _, kwargs = mock_save_score.call_args
        assert kwargs["sonar_enabled"] is False
        assert kwargs["sonar_scan_mode"] is None

    def test_passes_full_scan_mode(self, mock_gl_cls, mock_save_score, mock_format):
        _setup_gitlab_mock(mock_gl_cls)
        mock_save_score.return_value = {"total_score": 0, "sonar": {"enabled": True}}

        commenter = GitLabCommenterAgent()
        commenter(_base_state(sonar_enabled=True, sonar_scan_mode="full"))

        _, kwargs = mock_save_score.call_args
        assert kwargs["sonar_scan_mode"] == "full"

    def test_score_comment_posted_via_format_score_comment(self, mock_gl_cls, mock_save_score, mock_format):
        gl_instance = _setup_gitlab_mock(mock_gl_cls)
        mock_save_score.return_value = {"total_score": 0, "sonar": {"enabled": False}}

        commenter = GitLabCommenterAgent()
        commenter(_base_state())

        gl_instance.post_comment.assert_called_once_with(42, "score comment")