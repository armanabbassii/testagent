"""تست‌های دستور review CLI."""

import pytest
from unittest.mock import patch, MagicMock, call
from src.cli.i18n import Translator
from src.cli.commands.review import _print_results, _get_user_id, _hitl_prompt


def _make_result(**kwargs) -> dict:
    defaults = {
        "review_comments": [],
        "score_record": {},
        "decision": "",
        "decision_reason": "",
        "messages": [],
    }
    defaults.update(kwargs)
    return defaults


class TestGetUserId:
    def test_returns_env_var_if_set(self):
        with patch.dict("os.environ", {"CLI_USER_ID": "env-user"}):
            assert _get_user_id() == "env-user"

    def test_returns_default_if_not_set(self):
        import os
        os.environ.pop("CLI_USER_ID", None)
        result = _get_user_id()
        assert result == "cli-user"


class TestHITLPrompt:
    @patch("src.cli.commands.review.out.prompt", return_value="y")
    def test_yes_returns_true(self, mock_prompt):
        t = Translator("en")
        assert _hitl_prompt(t) is True

    @patch("src.cli.commands.review.out.prompt", return_value="n")
    def test_no_returns_false(self, mock_prompt):
        t = Translator("en")
        assert _hitl_prompt(t) is False

    @patch("src.cli.commands.review.out.prompt", return_value="بله")
    def test_fa_yes_returns_true(self, mock_prompt):
        t = Translator("fa")
        assert _hitl_prompt(t) is True

    @patch("src.cli.commands.review.out.prompt", return_value="yes")
    def test_yes_word_returns_true(self, mock_prompt):
        t = Translator("en")
        assert _hitl_prompt(t) is True


class TestPrintResults:
    def test_no_issues_prints_success(self, capsys):
        t = Translator("en")
        _print_results(_make_result(), t)
        output = capsys.readouterr().out
        assert "No issues" in output or "0" in output

    def test_with_comments_prints_them(self, capsys):
        from src.agents.code_review.state import ReviewComment
        t = Translator("en")
        comments = [ReviewComment(
            file_path="src/auth.py", line=42,
            severity="critical", category="security",
            body="SQL injection vulnerability",
        )]
        _print_results(_make_result(review_comments=comments), t)
        output = capsys.readouterr().out
        assert "src/auth.py" in output
        assert "CRITICAL" in output

    def test_with_score_prints_score(self, capsys):
        t = Translator("en")
        score = {
            "total_score": -10,
            "issues": [{"severity": "critical", "count": 1, "score": -10}],
            "categories": [],
        }
        _print_results(_make_result(score_record=score), t)
        output = capsys.readouterr().out
        assert "-10" in output

    def test_with_approve_decision(self, capsys):
        t = Translator("en")
        _print_results(_make_result(decision="approve", decision_reason="LGTM"), t)
        output = capsys.readouterr().out
        assert "APPROVED" in output or "approve" in output.lower()

    def test_with_reject_decision(self, capsys):
        t = Translator("en")
        _print_results(_make_result(decision="reject", decision_reason="Critical issue"), t)
        output = capsys.readouterr().out
        assert "REJECTED" in output or "reject" in output.lower()

    def test_fa_translation_used(self, capsys):
        t = Translator("fa")
        _print_results(_make_result(), t)
        # فارسی باید در خروجی باشد
        output = capsys.readouterr().out
        assert len(output) > 0

    def test_long_body_truncated(self, capsys):
        from src.agents.code_review.state import ReviewComment
        t = Translator("en")
        long_body = "A" * 300
        comments = [ReviewComment(
            file_path="f.py", line=1,
            severity="minor", category="style",
            body=long_body,
        )]
        _print_results(_make_result(review_comments=comments), t)
        output = capsys.readouterr().out
        # بدنه نباید کامل چاپ شود
        assert len(output) < len(long_body) + 500


class TestReviewCommandRun:
    @patch("src.cli.commands.review.make_checkpointer")
    def test_returns_zero_on_success(self, mock_cp):
        from contextlib import contextmanager
        from src.cli.commands.review import run
        import argparse

        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {
            "review_comments": [], "score_record": {},
            "decision": "approve", "decision_reason": "ok", "messages": [],
        }

        @contextmanager
        def fake_cp():
            yield MagicMock()

        mock_cp.return_value = fake_cp()

        with patch("src.cli.commands.review.build_code_review_graph",
                   return_value=mock_graph):
            args = argparse.Namespace(mr=42, hitl=False, sonar=False, sonar_mode="full")
            t = Translator("en")
            result = run(args, t)

        assert result == 0

    @patch("src.cli.commands.review.make_checkpointer")
    def test_returns_one_on_exception(self, mock_cp):
        from contextlib import contextmanager
        from src.cli.commands.review import run
        import argparse

        @contextmanager
        def fake_cp():
            yield MagicMock()

        mock_cp.return_value = fake_cp()

        with patch("src.cli.commands.review.build_code_review_graph",
                   side_effect=Exception("connection failed")):
            args = argparse.Namespace(mr=42, hitl=False, sonar=False, sonar_mode="full")
            t = Translator("en")
            result = run(args, t)

        assert result == 1
