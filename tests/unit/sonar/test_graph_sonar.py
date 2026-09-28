"""
tests/unit/code_review/test_graph_sonar.py — تست توپولوژی گراف با enable_sonar

این تست‌ها فقط ساختار گراف (node/edge) را بررسی می‌کنند، نه اجرای واقعی.

نکته: متغیرهای محیطی GITLAB_* از قبل توسط fixture مشترک پروژه (conftest
سطح بالا) فراهم شده‌اند.
"""

import pytest

from src.checkpointer import make_sqlite_checkpointer
from src.agents.code_review import build_code_review_graph


@pytest.fixture
def checkpointer():
    with make_sqlite_checkpointer(":memory:") as cp:
        yield cp


class TestGraphTopologyWithSonar:
    def test_enable_sonar_adds_sonar_analyzer_node(self, checkpointer):
        graph = build_code_review_graph(checkpointer=checkpointer, enable_sonar=True)
        assert "sonar_analyzer" in graph.get_graph().nodes

    def test_enable_sonar_adds_merge_findings_node(self, checkpointer):
        graph = build_code_review_graph(checkpointer=checkpointer, enable_sonar=True)
        assert "merge_findings" in graph.get_graph().nodes

    def test_disable_sonar_omits_sonar_analyzer_node(self, checkpointer):
        graph = build_code_review_graph(checkpointer=checkpointer, enable_sonar=False)
        assert "sonar_analyzer" not in graph.get_graph().nodes

    def test_disable_sonar_omits_merge_findings_node(self, checkpointer):
        graph = build_code_review_graph(checkpointer=checkpointer, enable_sonar=False)
        assert "merge_findings" not in graph.get_graph().nodes

    def test_default_enable_sonar_is_false(self, checkpointer):
        graph = build_code_review_graph(checkpointer=checkpointer)
        assert "sonar_analyzer" not in graph.get_graph().nodes

    def test_core_nodes_always_present(self, checkpointer):
        graph = build_code_review_graph(checkpointer=checkpointer, enable_sonar=True)
        nodes = graph.get_graph().nodes
        for expected in ("gitlab_fetcher", "code_reviewer", "gitlab_commenter", "decision_maker"):
            assert expected in nodes


class TestGraphSonarScanMode:
    def test_accepts_full_scan_mode(self, checkpointer):
        graph = build_code_review_graph(checkpointer=checkpointer, enable_sonar=True, sonar_scan_mode="full")
        assert "sonar_analyzer" in graph.get_graph().nodes

    def test_accepts_diff_scan_mode(self, checkpointer):
        graph = build_code_review_graph(checkpointer=checkpointer, enable_sonar=True, sonar_scan_mode="diff")
        assert "sonar_analyzer" in graph.get_graph().nodes

    def test_invalid_scan_mode_raises_when_sonar_enabled(self, checkpointer):
        with pytest.raises(ValueError):
            build_code_review_graph(checkpointer=checkpointer, enable_sonar=True, sonar_scan_mode="new-code")

    def test_invalid_scan_mode_ignored_when_sonar_disabled(self, checkpointer):
        # وقتی enable_sonar=False باشد، SonarAnalyzerAgent اصلاً ساخته نمی‌شود
        # پس scan_mode نامعتبر نباید خطا بدهد
        graph = build_code_review_graph(checkpointer=checkpointer, enable_sonar=False, sonar_scan_mode="new-code")
        assert "sonar_analyzer" not in graph.get_graph().nodes