"""
تست‌های واحدِ کمک‌تابع‌های نمایشیِ قدم چهارم — ui/formatting.py

این توابع هیچ منطقِ کسب‌وکاری ندارند: فقط نتیجه‌ی Step4PostmanGenerator را به
شکلِ قابلِ نمایش تبدیل می‌کنند. تست‌ها همان چیزی را می‌سنجند که صفحه‌ی
Streamlit مصرف می‌کند.
"""

from __future__ import annotations

import json

from ui.formatting import (
    collection_filename,
    collection_json,
    step4_counts,
    step4_request_rows,
    step4_unresolved_rows,
    step4_warnings,
)


def _result() -> dict:
    return {
        "collection_name": "Voucher management API Tests",
        "collection": {
            "info": {"name": "Voucher management API Tests"},
            "variable": [
                {"key": "baseUrl", "value": "https://admin.example.com"},
                {"key": "token", "value": ""},
            ],
            "item": [],
        },
        "scenarios": [
            {"id": "SC-001", "title": "Voucher lifecycle", "request_count": 3},
            {"id": "SC-002", "title": "Voucher validation", "request_count": 1},
        ],
        "requests": [
            {
                "test_case_id": "TC-001",
                "title": "Create a valid voucher",
                "name": "TC-001 — Create a valid voucher",
                "method": "POST",
                "path": "/admin/voucher",
                "scenario": "Voucher lifecycle",
            },
            {
                "test_case_id": "TC-002",
                "title": "Create an invalid voucher",
                "name": "TC-002 — Create an invalid voucher",
                "method": "POST",
                "path": "/admin/voucher",
                "scenario": "",
            },
        ],
        "unresolved": [
            {
                "test_case_id": "TC-004",
                "title": "Read a voucher",
                "reason": "Step 2 did not resolve an API for this test case.",
            },
            {
                "test_case_id": "TC-005",
                "title": "",
                "reason": "The service has no base URL in Step 2.",
            },
        ],
        "clarifications": [{"type": "open_question", "test_case_id": "", "message": "?"}],
        "warnings": ["TC-003 runs before its source.", "   "],
    }


# ── شمارش‌ها ─────────────────────────────────────────────────────────────────

class TestStep4Counts:
    def test_counts_every_section(self):
        counts = step4_counts(_result())
        assert counts == {
            "scenarios": 2,
            "requests": 2,
            "unresolved": 2,
            "clarifications": 1,
        }

    def test_missing_input_falls_back_to_zero(self):
        assert step4_counts(None) == {
            "scenarios": 0,
            "requests": 0,
            "unresolved": 0,
            "clarifications": 0,
        }

    def test_an_empty_result_does_not_raise(self):
        assert step4_counts({})["requests"] == 0


# ── ردیف‌های درخواست ─────────────────────────────────────────────────────────

class TestStep4RequestRows:
    def test_every_request_becomes_a_row(self):
        rows = step4_request_rows(_result())
        assert rows[0] == {
            "Request": "TC-001 — Create a valid voucher",
            "Method": "POST",
            "Path": "/admin/voucher",
            "Scenario": "Voucher lifecycle",
        }
        assert len(rows) == 2

    def test_a_request_outside_a_scenario_shows_a_placeholder(self):
        assert step4_request_rows(_result())[1]["Scenario"] == "—"

    def test_no_requests_gives_no_rows(self):
        assert step4_request_rows({}) == []

    def test_non_object_entries_are_skipped(self):
        assert step4_request_rows({"requests": ["nope", None]}) == []


# ── ردیف‌های حل‌نشده ─────────────────────────────────────────────────────────

class TestStep4UnresolvedRows:
    def test_a_row_keeps_the_case_id_and_reason(self):
        rows = step4_unresolved_rows(_result())
        assert rows[0]["Test case"] == "TC-004 — Read a voucher"
        assert rows[0]["Reason"] == "Step 2 did not resolve an API for this test case."

    def test_a_case_without_a_title_falls_back_to_the_id(self):
        assert step4_unresolved_rows(_result())[1]["Test case"] == "TC-005"

    def test_no_unresolved_entries_gives_no_rows(self):
        assert step4_unresolved_rows({}) == []


# ── هشدارها ─────────────────────────────────────────────────────────────────

class TestStep4Warnings:
    def test_warnings_are_kept_in_order(self):
        assert step4_warnings(_result()) == ["TC-003 runs before its source."]

    def test_blank_warnings_are_dropped(self):
        assert step4_warnings({"warnings": ["", "  ", None]}) == []

    def test_missing_warnings_gives_an_empty_list(self):
        assert step4_warnings({}) == []


# ── نامِ فایل و JSON ────────────────────────────────────────────────────────

class TestCollectionFilename:
    def test_the_name_is_slugified(self):
        assert collection_filename("Voucher management API Tests") == (
            "voucher_management_api_tests.postman_collection.json"
        )

    def test_separators_and_punctuation_are_collapsed(self):
        assert collection_filename("Admin — Voucher / v2") == (
            "admin_voucher_v2.postman_collection.json"
        )

    def test_an_empty_name_gets_a_fallback(self):
        assert collection_filename("") == "postman_collection.postman_collection.json"

    def test_a_non_string_name_does_not_raise(self):
        assert collection_filename(None) == "postman_collection.postman_collection.json"


class TestCollectionJson:
    def test_the_collection_is_pretty_printed(self):
        text = collection_json({"info": {"name": "X"}})
        assert "\n" in text
        assert json.loads(text) == {"info": {"name": "X"}}

    def test_non_ascii_is_preserved(self):
        assert "کالکشن" in collection_json({"info": {"name": "کالکشن"}})

    def test_an_unserialisable_collection_produces_a_valid_error_document(self):
        text = collection_json({"value": {1, 2}})
        payload = json.loads(text)
        assert "could not be serialized" in payload["error"]

    def test_an_empty_collection_is_still_valid_json(self):
        assert json.loads(collection_json({})) == {}
