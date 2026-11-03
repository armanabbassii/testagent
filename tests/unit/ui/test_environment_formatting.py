"""
تست‌های نمایشِ تصمیم‌های محیط و میزبان — بدونِ Streamlit.

UI باید بگوید هر تست‌کیس چرا {{base_url}} یا {{base_url_vdi}} گرفته (یا چرا
اجرا نمی‌شود) و شاهدش از کدام منبع آمده است.
"""

from __future__ import annotations

from src.agents.test_case_generator.host_selection import resolve_hosts
from ui.formatting import (
    environment_decision_rows,
    environment_issue_review,
    environment_warnings,
    host_explanations,
    host_selection_rows,
    unmatched_decision_rows,
)

ACCEPTED = {
    "id": "ENV-1",
    "status": "ACCEPTED",
    "scope": "service",
    "service": {"method": "POST", "path": "/admin/x", "name": "request possibility"},
    "test_case_ids": [],
    "environment": "vdi",
    "evidence": {"original": "This service must be tested in VDI.", "as_returned": "x"},
    "source": {"type": "comment", "reference": "90001", "label": "Comment 1", "sha256": "ab" * 32},
    "warnings": [],
}
REJECTED = {
    "id": "ENV-2",
    "status": "REJECTED",
    "scope": "task",
    "service": {},
    "test_case_ids": [],
    "environment": "normal",
    "evidence": {"original": "", "as_returned": "Everything is normal."},
    "source": {"type": "description", "reference": "", "label": "", "sha256": ""},
    "warnings": [],
    "rejection": {"code": "EVIDENCE_NOT_FOUND", "message": "the sentence was not found"},
}
UNMATCHED = {
    **ACCEPTED,
    "id": "ENV-3",
    "service": {"method": "GET", "path": "/billing/x", "name": ""},
    "environment": "normal",
}


def _step1(*decisions) -> dict:
    return {
        "test_cases": [{"id": "TC-001"}, {"id": "TC-002"}],
        "environment_decisions": [d for d in decisions if d["status"] == "ACCEPTED"],
        "rejected_environment_decisions": [d for d in decisions if d["status"] == "REJECTED"],
        "environment_warnings": ["ENV-1: REFERENCE_CORRECTED: corrected"],
    }


def _step2(step1: dict) -> dict:
    mappings = [
        {"test_case_id": "TC-001", "api": {"method": "POST", "path": "/admin/x"}},
        {"test_case_id": "TC-002", "api": {"method": "GET", "path": "/admin/y"}},
    ]
    return {"host_resolution": resolve_hosts(step1, mappings, [])}


class TestDecisionRows:
    def test_accepted_and_rejected_decisions_are_both_shown(self):
        rows = environment_decision_rows(_step1(REJECTED, ACCEPTED))
        assert [row["Decision"] for row in rows] == ["ENV-1", "ENV-2"]
        assert rows[0]["Evidence"] == "This service must be tested in VDI."
        assert rows[0]["Source"] == "Comment 1 (comment 90001)"
        assert rows[0]["Service"] == "request possibility (POST /admin/x)"
        assert rows[1]["Status"] == "REJECTED (EVIDENCE_NOT_FOUND)"
        assert rows[1]["Evidence"] == "Everything is normal."
        assert rows[1]["Note"] == "the sentence was not found"

    def test_no_decision_gives_no_rows(self):
        assert environment_decision_rows({"test_cases": []}) == []
        assert environment_decision_rows(None) == []

    def test_warnings_are_listed(self):
        assert environment_warnings(_step1()) == ["ENV-1: REFERENCE_CORRECTED: corrected"]


class TestHostRows:
    def test_each_case_shows_its_host_or_its_review_status(self):
        step1 = _step1(ACCEPTED, REJECTED)
        rows = host_selection_rows(_step2(step1))
        assert rows[0] == {
            "Test case": "TC-001",
            "Host": "{{base_url_vdi}}",
            "Status": "RESOLVED",
            "Basis": "service",
            "Decisions": "ENV-1",
        }
        assert rows[1]["Host"] == "—"
        assert rows[1]["Status"] == "NEEDS_REVIEW"

    def test_old_results_without_a_resolution_show_nothing(self):
        assert host_selection_rows({"mappings": []}) == []


class TestExplanations:
    def test_the_explanation_names_the_evidence_and_its_source(self):
        step1 = _step1(ACCEPTED)
        explanations = {
            item["test_case_id"]: item for item in host_explanations(step1, _step2(step1))
        }
        resolved = explanations["TC-001"]
        assert resolved["heading"] == "TC-001 → {{base_url_vdi}}"
        assert any(
            '"This service must be tested in VDI."' in line and "Comment 1 (comment 90001)" in line
            for line in resolved["lines"]
        )
        assert explanations["TC-002"]["lines"] == [
            "No environment statement applies, so the default host is used."
        ]

    def test_a_held_case_explains_the_rejected_statement(self):
        step1 = _step1(REJECTED)
        explanation = host_explanations(step1, _step2(step1))[0]
        assert explanation["heading"] == "TC-001 → NEEDS_REVIEW"
        assert any("ENV-2" in line and "REJECTED" in line for line in explanation["lines"])


class TestIssueReview:
    def test_unmatched_decisions_and_review_status(self):
        step1 = _step1(UNMATCHED)
        step2 = _step2(step1)
        assert unmatched_decision_rows(step2)[0]["Decision"] == "ENV-3"
        review = environment_issue_review(step2)
        assert review["status"] == "NEEDS_REVIEW"
        assert review["reasons"]
        assert review["clarifications"]

    def test_no_resolution_means_no_review(self):
        assert environment_issue_review({})["status"] == "NONE"


class TestPanelsRender:
    def test_both_panels_render_with_decisions_and_review(self):
        from streamlit.testing.v1 import AppTest

        def page(step1, step2):
            from ui.environment_view import render_environment_decisions, render_host_resolution

            render_environment_decisions(step1)
            render_host_resolution(step1, step2)

        step1 = _step1(ACCEPTED, REJECTED, UNMATCHED)
        app = AppTest.from_function(page, args=(step1, _step2(step1)), default_timeout=120).run()

        assert not app.exception
        assert [header.value for header in app.subheader] == ["Environment Decisions", "Hosts"]
        assert any("Environment review needed" in w.value for w in app.warning)
        assert len(app.dataframe) == 3
