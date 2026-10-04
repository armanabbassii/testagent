"""
تست‌های واحدِ کمک‌تابع‌های نمایشیِ قدم پنجم — ui/formatting.py

این توابع هیچ منطقِ کسب‌وکاری ندارند: فقط گزارشِ FinalReviewGenerator را به شکلِ
قابلِ نمایش تبدیل می‌کنند. تست‌ها همان چیزی را می‌سنجند که صفحه‌ی Streamlit
مصرف می‌کند — از جمله اینکه صادرات دقیقاً همان کالکشنِ قدم چهارم است و هیچ
فراداده‌ی قدم پنجم به آن راه پیدا نمی‌کند.
"""

from __future__ import annotations

import json

from ui.formatting import (
    collection_filename,
    collection_json,
    collection_name,
    postman_collection,
    step5_issue_rows,
    step5_metrics,
    step5_review_item_rows,
    step5_status,
    step5_status_label,
    step5_summary,
    step5_warnings,
)


def _collection() -> dict:
    return {
        "info": {
            "name": "Voucher management API Tests",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [{"key": "baseUrl", "value": "https://admin.example.com"}],
        "item": [
            {
                "name": "SC-001 — Voucher lifecycle",
                "item": [{"name": "TC-001 — Create a valid voucher"}],
            }
        ],
    }


def _result() -> dict:
    return {
        "review": {
            "status": "needs_review",
            "summary": "Step 5 review completed.\n\n4 test cases analyzed.",
            "issues": [
                {
                    "code": "missing_request",
                    "step": 4,
                    "test_case_id": "TC-004",
                    "message": "Test case 'TC-004' has no generated request.",
                },
                {
                    "code": "invalid_collection",
                    "step": 4,
                    "test_case_id": "",
                    "message": "'info.schema' must be the v2.1 schema.",
                },
            ],
            "warnings": ["TC-003 runs before its source.", "   "],
            "unresolved_clarifications": [
                {
                    "kind": "clarification",
                    "source_step": 1,
                    "test_case_id": "",
                    "message": "The expected response fields are not defined.",
                },
                {
                    "kind": "missing_data",
                    "source_step": 3,
                    "test_case_id": "TC-004",
                    "message": "No test case provisions a voucher id.",
                },
                {
                    "kind": "unresolved_test_case",
                    "source_step": 4,
                    "test_case_id": "TC-004",
                    "message": "Step 2 did not resolve an API for this test case.",
                },
            ],
        },
        "metrics": {
            "test_cases": 4,
            "mapped_test_cases": 3,
            "generated_requests": 3,
            "scenarios": 2,
            "data_dependencies": 1,
            "clarifications": 3,
            "issues": 2,
            "warnings": 1,
        },
        "artifacts": {"postman_collection": _collection()},
    }


# ── وضعیت ────────────────────────────────────────────────────────────────────

class TestStep5Status:
    def test_every_known_status_gets_a_label(self):
        for status, label in (
            ("ready", "Ready"),
            ("needs_review", "Needs review"),
            ("blocked", "Blocked"),
        ):
            result = {"review": {"status": status}}
            assert step5_status(result) == status
            assert step5_status_label(result) == label

    def test_an_unknown_status_is_shown_as_is(self):
        assert step5_status_label({"review": {"status": "waiting"}}) == "waiting"

    def test_a_missing_review_section_does_not_raise(self):
        for result in (None, {}, {"review": None}, {"review": "nope"}):
            assert step5_status(result) == ""
            assert step5_status_label(result) == "—"

    def test_a_blank_status_is_trimmed(self):
        assert step5_status({"review": {"status": "  ready  "}}) == "ready"


class TestStep5Summary:
    def test_the_summary_is_passed_through(self):
        assert step5_summary(_result()) == "Step 5 review completed.\n\n4 test cases analyzed."

    def test_a_missing_summary_is_empty(self):
        assert step5_summary({}) == ""
        assert step5_summary(None) == ""


# ── متریک‌ها ─────────────────────────────────────────────────────────────────

class TestStep5Metrics:
    def test_every_metric_is_exposed(self):
        assert step5_metrics(_result()) == {
            "test_cases": 4,
            "mapped_test_cases": 3,
            "generated_requests": 3,
            "scenarios": 2,
            "data_dependencies": 1,
            "clarifications": 3,
            "issues": 2,
            "warnings": 1,
        }

    def test_missing_metrics_fall_back_to_zero(self):
        assert step5_metrics({"metrics": {"test_cases": 2}})["test_cases"] == 2
        assert step5_metrics({"metrics": {"test_cases": 2}})["issues"] == 0

    def test_a_missing_or_invalid_metrics_section_is_all_zeros(self):
        for result in (None, {}, {"metrics": None}, {"metrics": []}):
            assert set(step5_metrics(result).values()) == {0}

    def test_invalid_counts_fall_back_to_zero(self):
        metrics = step5_metrics({"metrics": {"issues": "many", "warnings": -1}})
        assert metrics["issues"] == 0
        assert metrics["warnings"] == 0


# ── issue ها ─────────────────────────────────────────────────────────────────

class TestStep5IssueRows:
    def test_every_issue_becomes_a_row(self):
        rows = step5_issue_rows(_result())
        assert rows[0] == {
            "Step": "Step 4",
            "Code": "missing_request",
            "Test case": "TC-004",
            "Message": "Test case 'TC-004' has no generated request.",
        }
        assert len(rows) == 2

    def test_an_issue_without_a_test_case_shows_a_placeholder(self):
        assert step5_issue_rows(_result())[1]["Test case"] == "—"

    def test_no_issues_gives_no_rows(self):
        assert step5_issue_rows({}) == []
        assert step5_issue_rows({"review": {"issues": None}}) == []

    def test_non_object_entries_are_skipped(self):
        assert step5_issue_rows({"review": {"issues": ["nope", None]}}) == []

    def test_an_unknown_step_number_shows_a_placeholder(self):
        rows = step5_issue_rows(
            {"review": {"issues": [{"code": "x", "step": 0, "message": "m"}]}}
        )
        assert rows[0]["Step"] == "—"


# ── مواردِ حل‌نشده ──────────────────────────────────────────────────────────

class TestStep5ReviewItemRows:
    def test_every_item_keeps_its_source_step_and_test_case(self):
        rows = step5_review_item_rows(_result())
        assert rows[0] == {
            "Step": "Step 1",
            "Kind": "clarification",
            "Test case": "—",
            "Message": "The expected response fields are not defined.",
        }
        assert rows[2]["Step"] == "Step 4"
        assert rows[2]["Kind"] == "unresolved_test_case"
        assert rows[2]["Test case"] == "TC-004"
        assert len(rows) == 3

    def test_no_items_gives_no_rows(self):
        assert step5_review_item_rows({}) == []
        assert step5_review_item_rows({"review": {"unresolved_clarifications": []}}) == []

    def test_non_object_entries_are_skipped(self):
        rows = step5_review_item_rows(
            {"review": {"unresolved_clarifications": ["nope", {"message": "m"}]}}
        )
        assert len(rows) == 1
        assert rows[0]["Kind"] == "—"
        assert rows[0]["Step"] == "—"


# ── هشدارها ─────────────────────────────────────────────────────────────────

class TestStep5Warnings:
    def test_warnings_are_kept_in_order(self):
        assert step5_warnings(_result()) == ["TC-003 runs before its source."]

    def test_blank_warnings_are_dropped(self):
        assert step5_warnings({"review": {"warnings": ["", "  ", None]}}) == []

    def test_missing_warnings_gives_an_empty_list(self):
        assert step5_warnings({}) == []
        assert step5_warnings(None) == []


# ── صادرات ───────────────────────────────────────────────────────────────────

class TestStep5Export:
    def test_the_collection_comes_from_the_artifacts(self):
        assert postman_collection(_result()) == _collection()

    def test_a_missing_artifact_is_an_empty_collection(self):
        for result in (None, {}, {"artifacts": None}, {"artifacts": {"postman_collection": 5}}):
            assert postman_collection(result) == {}

    def test_the_name_comes_from_the_collection_itself(self):
        assert collection_name(_collection()) == "Voucher management API Tests"
        assert collection_name(None) == ""
        assert collection_name({"info": {}}) == ""

    def test_the_exported_filename_matches_the_step4_convention(self):
        result = _result()
        filename = collection_filename(collection_name(postman_collection(result)))
        assert filename == "voucher_management_api_tests.postman_collection.json"

    def test_no_step5_metadata_reaches_the_exported_json(self):
        payload = json.loads(collection_json(postman_collection(_result())))
        assert set(payload) == {"info", "item", "variable"}
        assert "review" not in payload
        assert "metrics" not in payload
        assert "artifacts" not in payload
