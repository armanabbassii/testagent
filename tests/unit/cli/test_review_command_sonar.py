"""
tests/unit/cli/test_review_command_sonar.py — تست وایرینگ فلگ --sonar/--sonar-mode

بررسی می‌کند:
  ۱. --sonar به‌صورت store_true پارس می‌شود (نه type=bool)
  ۲. --sonar-mode مقادیر مجاز full/diff را می‌پذیرد و پیش‌فرض دارد
  ۳. مقادیر درست به build_code_review_graph پاس داده می‌شوند
"""

import argparse
from unittest.mock import patch, MagicMock

import pytest

from src.cli.commands.review import add_parser, run
from src.cli.i18n import Translator
from src.config import SONAR_SCAN_MODE


@pytest.fixture
def parser():
    p = argparse.ArgumentParser()
    subparsers = p.add_subparsers(dest="command")
    lang_parent = argparse.ArgumentParser(add_help=False)
    add_parser(subparsers, Translator(lang="fa"), lang_parent)
    return p


class TestSonarFlagParsing:
    def test_sonar_flag_defaults_to_false(self, parser):
        args = parser.parse_args(["review", "--mr", "42"])
        assert args.sonar is False

    def test_sonar_flag_true_when_passed(self, parser):
        args = parser.parse_args(["review", "--mr", "42", "--sonar"])
        assert args.sonar is True

    def test_sonar_flag_does_not_consume_extra_argument(self, parser):
        args = parser.parse_args(["review", "--sonar", "--mr", "42"])
        assert args.sonar is True
        assert args.mr == 42


class TestSonarModeParsing:
    def test_sonar_mode_defaults_to_config_value(self, parser):
        args = parser.parse_args(["review", "--mr", "42"])
        assert args.sonar_mode == SONAR_SCAN_MODE

    def test_sonar_mode_accepts_full(self, parser):
        args = parser.parse_args(["review", "--mr", "42", "--sonar-mode", "full"])
        assert args.sonar_mode == "full"

    def test_sonar_mode_accepts_diff(self, parser):
        args = parser.parse_args(["review", "--mr", "42", "--sonar-mode", "diff"])
        assert args.sonar_mode == "diff"

    def test_sonar_mode_rejects_invalid_value(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["review", "--mr", "42", "--sonar-mode", "new-code"])


class TestSonarFlagWiring:
    @patch("src.cli.commands.review.make_checkpointer")
    @patch("src.cli.commands.review.build_code_review_graph")
    def test_enable_sonar_and_mode_passed_to_graph_builder(self, mock_build_graph, mock_make_cp):
        mock_make_cp.return_value.__enter__.return_value = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"review_comments": [], "score_record": {}, "decision": ""}
        mock_build_graph.return_value = mock_graph

        args = argparse.Namespace(mr=42, hitl=False, sonar=True, sonar_mode="full")
        run(args, Translator(lang="fa"))

        _, kwargs = mock_build_graph.call_args
        assert kwargs["enable_sonar"] is True
        assert kwargs["sonar_scan_mode"] == "full"

    @patch("src.cli.commands.review.make_checkpointer")
    @patch("src.cli.commands.review.build_code_review_graph")
    def test_diff_mode_passed_to_graph_builder(self, mock_build_graph, mock_make_cp):
        mock_make_cp.return_value.__enter__.return_value = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"review_comments": [], "score_record": {}, "decision": ""}
        mock_build_graph.return_value = mock_graph

        args = argparse.Namespace(mr=42, hitl=False, sonar=True, sonar_mode="diff")
        run(args, Translator(lang="fa"))

        _, kwargs = mock_build_graph.call_args
        assert kwargs["sonar_scan_mode"] == "diff"

    @patch("src.cli.commands.review.make_checkpointer")
    @patch("src.cli.commands.review.build_code_review_graph")
    def test_enable_sonar_false_still_passed(self, mock_build_graph, mock_make_cp):
        mock_make_cp.return_value.__enter__.return_value = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"review_comments": [], "score_record": {}, "decision": ""}
        mock_build_graph.return_value = mock_graph

        args = argparse.Namespace(mr=42, hitl=False, sonar=False, sonar_mode="diff")
        run(args, Translator(lang="fa"))

        _, kwargs = mock_build_graph.call_args
        assert kwargs["enable_sonar"] is False