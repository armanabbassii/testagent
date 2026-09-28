"""
tests/unit/code_review/test_merge_findings.py — تست‌های MergeFindingsNode

این node کاملاً pure است (فقط ترکیب دو لیست) — نیازی به mock ندارد.
"""

from src.agents.code_review.merge_findings import MergeFindingsNode


def _comment(body: str) -> dict:
    return {"file_path": "f.py", "line": 1, "severity": "minor", "category": "general", "body": body}


class TestMergeFindingsNode:
    def test_combines_llm_and_sonar_comments(self):
        node = MergeFindingsNode()
        state = {
            "review_comments": [_comment("llm-1")],
            "sonar_issues": [_comment("sonar-1")],
        }
        result = node(state)
        assert len(result["review_comments"]) == 2

    def test_preserves_llm_comments_when_sonar_empty(self):
        node = MergeFindingsNode()
        state = {"review_comments": [_comment("llm-1"), _comment("llm-2")], "sonar_issues": []}
        result = node(state)
        assert result["review_comments"] == [_comment("llm-1"), _comment("llm-2")]

    def test_preserves_sonar_comments_when_llm_empty(self):
        node = MergeFindingsNode()
        state = {"review_comments": [], "sonar_issues": [_comment("sonar-1")]}
        result = node(state)
        assert result["review_comments"] == [_comment("sonar-1")]

    def test_both_empty_returns_empty_list(self):
        node = MergeFindingsNode()
        state = {"review_comments": [], "sonar_issues": []}
        result = node(state)
        assert result["review_comments"] == []

    def test_missing_keys_default_to_empty(self):
        node = MergeFindingsNode()
        result = node({})
        assert result["review_comments"] == []

    def test_message_content_contains_counts(self):
        node = MergeFindingsNode()
        state = {"review_comments": [_comment("a"), _comment("b")], "sonar_issues": [_comment("c")]}
        result = node(state)
        content = result["messages"][0].content
        assert "2" in content and "1" in content and "3" in content

    def test_message_has_correct_agent_name(self):
        node = MergeFindingsNode()
        result = node({"review_comments": [], "sonar_issues": []})
        assert result["messages"][0].name == "merge_findings"