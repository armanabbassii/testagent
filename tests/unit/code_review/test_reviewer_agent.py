"""تست‌های CodeReviewerAgent."""

import json
import pytest
from unittest.mock import patch, MagicMock
from src.agents.code_review.state import CodeReviewState, ReviewComment
from src.agents.code_review.reviewer.agent import _parse_comments, _build_system_prompt


def _make_state(**kwargs) -> CodeReviewState:
    defaults = {
        "mr_iid": 1, "user_id": "u1", "created_at": "2024-01-01",
        "mr_title": "Test MR", "mr_description": "desc",
        "diff": "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n+new line",
        "existing_comments": [], "review_comments": [],
        "score_record": {}, "decision": "", "decision_reason": "", "messages": [],
    }
    defaults.update(kwargs)
    return defaults


class TestParseComments:
    def test_valid_json_array(self):
        raw = json.dumps([{
            "file_path": "src/auth.py", "line": 42,
            "severity": "critical", "category": "security",
            "body": "SQL injection",
        }])
        result = _parse_comments(raw)
        assert len(result) == 1
        assert result[0]["severity"] == "critical"
        assert result[0]["file_path"] == "src/auth.py"

    def test_empty_array_returns_empty(self):
        result = _parse_comments("[]")
        assert result == []

    def test_strips_markdown_fences(self):
        raw = "```json\n[]\n```"
        result = _parse_comments(raw)
        assert result == []

    def test_invalid_json_returns_fallback_comment(self):
        result = _parse_comments("not valid json {{{")
        assert len(result) == 1
        assert result[0]["severity"] == "minor"
        assert "Raw:" in result[0]["body"]

    def test_multiple_comments_parsed(self):
        raw = json.dumps([
            {"file_path": "a.py", "line": 1, "severity": "major",
             "category": "correctness", "body": "b1"},
            {"file_path": "b.py", "line": None, "severity": "minor",
             "category": "style", "body": "b2"},
        ])
        result = _parse_comments(raw)
        assert len(result) == 2

    def test_missing_fields_use_defaults(self):
        raw = json.dumps([{"body": "just a body"}])
        result = _parse_comments(raw)
        assert result[0]["file_path"] == "unknown"
        assert result[0]["severity"] == "minor"

    def test_null_line_preserved(self):
        raw = json.dumps([{
            "file_path": "f.py", "line": None,
            "severity": "minor", "category": "style", "body": "b",
        }])
        result = _parse_comments(raw)
        assert result[0]["line"] is None


class TestBuildSystemPrompt:
    def test_returns_string(self):
        prompt = _build_system_prompt()
        assert isinstance(prompt, str)

    def test_contains_system_md_content(self):
        prompt = _build_system_prompt()
        assert len(prompt) > 100

    def test_contains_rule_sections(self):
        prompt = _build_system_prompt()
        # باید حداقل یکی از rule ها حضور داشته باشد
        assert any(kw in prompt.lower() for kw in ["security", "performance", "style", "correctness"])


class TestCodeReviewerAgentRun:
    def test_returns_review_comments(self):
        from src.agents.code_review.reviewer.agent import CodeReviewerAgent
        from src.debug import DebugConfig

        agent = CodeReviewerAgent(debug_config=DebugConfig.off())
        state = _make_state()

        mock_response = json.dumps([{
            "file_path": "src/main.py", "line": 5,
            "severity": "minor", "category": "style",
            "body": "Variable name unclear",
        }])

        with patch("src.llm_client.LLMClient.chat", return_value=mock_response):
            result = agent(state)

        assert "review_comments" in result
        assert len(result["review_comments"]) == 1
        assert result["review_comments"][0]["severity"] == "minor"

    def test_returns_ai_message_with_summary(self):
        from src.agents.code_review.reviewer.agent import CodeReviewerAgent
        from src.debug import DebugConfig

        agent = CodeReviewerAgent(debug_config=DebugConfig.off())
        state = _make_state()

        with patch("src.llm_client.LLMClient.chat", return_value="[]"):
            result = agent(state)

        assert "messages" in result
        assert len(result["messages"]) == 1

    def test_empty_diff_handled(self):
        from src.agents.code_review.reviewer.agent import CodeReviewerAgent
        from src.debug import DebugConfig

        agent = CodeReviewerAgent(debug_config=DebugConfig.off())
        state = _make_state(diff="")

        with patch("src.llm_client.LLMClient.chat", return_value="[]"):
            result = agent(state)

        assert result["review_comments"] == []
