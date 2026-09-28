"""تست‌های DecisionMakerAgent."""

import json
import pytest
from unittest.mock import patch, MagicMock
from src.agents.code_review.state import CodeReviewState, ReviewComment
from src.agents.code_review.decision.agent import (
    _parse_decision,
    _format_comments,
    _build_decision_comment,
)


def _make_state(**kwargs) -> CodeReviewState:
    defaults = {
        "mr_iid": 42, "user_id": "u1", "created_at": "2024-01-01",
        "mr_title": "Fix auth bug", "mr_description": "desc",
        "diff": "", "existing_comments": [], "review_comments": [],
        "score_record": {"total_score": -10, "issues": [], "categories": []},
        "decision": "", "decision_reason": "", "messages": [],
    }
    defaults.update(kwargs)
    return defaults


def _comment(severity: str, category: str = "security") -> ReviewComment:
    return ReviewComment(
        file_path="src/auth.py", line=1,
        severity=severity, category=category,
        body=f"{severity} issue body",
    )


class TestParseDecision:
    def test_valid_approve(self):
        raw = json.dumps({"decision": "approve", "reason": "Looks good!"})
        decision, reason = _parse_decision(raw)
        assert decision == "approve"
        assert reason == "Looks good!"

    def test_valid_reject(self):
        raw = json.dumps({"decision": "reject", "reason": "Critical issue found."})
        decision, reason = _parse_decision(raw)
        assert decision == "reject"

    def test_valid_needs_work(self):
        raw = json.dumps({"decision": "needs_work", "reason": "Please fix."})
        decision, reason = _parse_decision(raw)
        assert decision == "needs_work"

    def test_strips_markdown_fences(self):
        raw = '```json\n{"decision": "approve", "reason": "ok"}\n```'
        decision, reason = _parse_decision(raw)
        assert decision == "approve"

    def test_invalid_json_returns_needs_work(self):
        decision, reason = _parse_decision("not json {{")
        assert decision == "needs_work"
        assert "Raw:" in reason

    def test_missing_decision_defaults_to_needs_work(self):
        raw = json.dumps({"reason": "something"})
        decision, _ = _parse_decision(raw)
        assert decision == "needs_work"

    def test_missing_reason_returns_empty_string(self):
        raw = json.dumps({"decision": "approve"})
        _, reason = _parse_decision(raw)
        assert reason == ""


class TestFormatComments:
    def test_empty_list_returns_no_issues(self):
        result = _format_comments([])
        assert "No issues" in result

    def test_single_comment_formatted(self):
        comments = [_comment("critical")]
        result = _format_comments(comments)
        assert "CRITICAL" in result
        assert "src/auth.py" in result

    def test_multiple_comments_numbered(self):
        comments = [_comment("critical"), _comment("major", "correctness")]
        result = _format_comments(comments)
        assert "1." in result
        assert "2." in result

    def test_category_included(self):
        comments = [_comment("major", "performance")]
        result = _format_comments(comments)
        assert "PERFORMANCE" in result

    def test_line_info_included(self):
        comments = [_comment("minor")]
        result = _format_comments(comments)
        assert "line 1" in result

    def test_none_line_not_shown(self):
        comment = ReviewComment(
            file_path="f.py", line=None,
            severity="minor", category="style", body="body",
        )
        result = _format_comments([comment])
        assert "line None" not in result


class TestBuildDecisionComment:
    def _score(self, total: int, issues=None):
        return {"total_score": total, "issues": issues or []}

    def test_approve_shows_checkmark(self):
        comment = _build_decision_comment("approve", "LGTM", self._score(0))
        assert "✅" in comment or "APPROVED" in comment

    def test_reject_shows_x(self):
        comment = _build_decision_comment("reject", "Critical issue", self._score(-10))
        assert "❌" in comment or "REJECTED" in comment

    def test_needs_work_shows_refresh(self):
        comment = _build_decision_comment("needs_work", "Please fix", self._score(-7))
        assert "🔄" in comment or "NEEDS WORK" in comment

    def test_reason_included(self):
        comment = _build_decision_comment("approve", "All tests pass.", self._score(0))
        assert "All tests pass." in comment

    def test_total_score_included(self):
        comment = _build_decision_comment("reject", "reason", self._score(-17))
        assert "-17" in comment

    def test_issue_summary_included(self):
        issues = [{"severity": "critical", "count": 1, "score": -10}]
        comment = _build_decision_comment("reject", "reason", self._score(-10, issues))
        assert "Critical" in comment or "critical" in comment


class TestDecisionMakerAgentRun:
    def test_posts_comment_to_gitlab(self):
        from src.agents.code_review.decision.agent import DecisionMakerAgent
        from src.debug import DebugConfig

        with patch("src.agents.code_review.decision.agent.GitLabClient") as mock_gl_cls:
            mock_gl = MagicMock()
            mock_gl_cls.return_value = mock_gl
            agent = DecisionMakerAgent(debug_config=DebugConfig.off())

            state = _make_state(review_comments=[_comment("minor")])
            llm_response = json.dumps({"decision": "approve", "reason": "LGTM"})

            with patch("src.llm_client.LLMClient.chat", return_value=llm_response):
                result = agent(state)

        mock_gl.post_comment.assert_called_once()
        assert result["decision"] == "approve"

    def test_approve_calls_approve_api(self):
        from src.agents.code_review.decision.agent import DecisionMakerAgent
        from src.debug import DebugConfig

        with patch("src.agents.code_review.decision.agent.GitLabClient") as mock_gl_cls:
            mock_gl = MagicMock()
            mock_gl_cls.return_value = mock_gl
            agent = DecisionMakerAgent(debug_config=DebugConfig.off())

            state = _make_state()
            with patch("src.llm_client.LLMClient.chat",
                       return_value=json.dumps({"decision": "approve", "reason": "ok"})):
                agent(state)

        mock_gl.approve_mr.assert_called_once_with(42)

    def test_reject_calls_unapprove_api(self):
        from src.agents.code_review.decision.agent import DecisionMakerAgent
        from src.debug import DebugConfig

        with patch("src.agents.code_review.decision.agent.GitLabClient") as mock_gl_cls:
            mock_gl = MagicMock()
            mock_gl_cls.return_value = mock_gl
            agent = DecisionMakerAgent(debug_config=DebugConfig.off())

            state = _make_state()
            with patch("src.llm_client.LLMClient.chat",
                       return_value=json.dumps({"decision": "reject", "reason": "issue"})):
                agent(state)

        mock_gl.unapprove_mr.assert_called_once()

    def test_unapprove_exception_does_not_crash(self):
        from src.agents.code_review.decision.agent import DecisionMakerAgent
        from src.debug import DebugConfig

        with patch("src.agents.code_review.decision.agent.GitLabClient") as mock_gl_cls:
            mock_gl = MagicMock()
            mock_gl.unapprove_mr.side_effect = Exception("not approved yet")
            mock_gl_cls.return_value = mock_gl
            agent = DecisionMakerAgent(debug_config=DebugConfig.off())

            state = _make_state()
            with patch("src.llm_client.LLMClient.chat",
                       return_value=json.dumps({"decision": "reject", "reason": "issue"})):
                result = agent(state)   # نباید exception بدهد

        assert result["decision"] == "reject"

    def test_returns_ai_message(self):
        from src.agents.code_review.decision.agent import DecisionMakerAgent
        from src.debug import DebugConfig

        with patch("src.agents.code_review.decision.agent.GitLabClient"):
            agent = DecisionMakerAgent(debug_config=DebugConfig.off())
            state = _make_state()
            with patch("src.llm_client.LLMClient.chat",
                       return_value=json.dumps({"decision": "approve", "reason": "ok"})):
                result = agent(state)

        assert len(result["messages"]) == 1
