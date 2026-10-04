"""تست‌های کمک‌تابع‌های نمایشیِ UI قدم اول (بدون Streamlit)."""

from src.agents.test_case_generator.task_analysis import REQUIRES_MAPPING
from ui.formatting import (
    TYPE_ORDER,
    case_details,
    case_heading,
    case_meta,
    case_overview,
    case_sections,
    format_related_service,
    markdown_bullets,
    type_counts,
)


def _case(**overrides) -> dict:
    case = {
        "id": "TC-001",
        "title": "Create a valid percentage voucher",
        "type": "positive",
        "priority": "high",
        "preconditions": ["An admin is signed in"],
        "steps": ["Submit the request"],
        "expected_result": "The voucher is created.",
        "related_service": {"method": "POST", "path": "/admin/voucher", "service": ""},
    }
    case.update(overrides)
    return case


class TestFormatRelatedService:
    def test_mapped_service_without_name(self):
        service = {"method": "POST", "path": "/admin/voucher/percent", "service": ""}
        assert format_related_service(service) == "POST /admin/voucher/percent"

    def test_mapped_service_with_name(self):
        service = {
            "method": "POST",
            "path": "/admin/voucher/percent",
            "service": "Admin API",
        }
        assert (
            format_related_service(service) == "POST /admin/voucher/percent (Admin API)"
        )

    def test_unmapped_service_returns_sentinel(self):
        service = {"method": REQUIRES_MAPPING, "path": REQUIRES_MAPPING, "service": ""}
        assert format_related_service(service) == REQUIRES_MAPPING

    def test_sentinel_method_only(self):
        assert format_related_service({"method": REQUIRES_MAPPING, "path": "/x"}) == (
            REQUIRES_MAPPING
        )

    def test_missing_method(self):
        assert format_related_service({"path": "/x"}) == REQUIRES_MAPPING

    def test_missing_path(self):
        assert format_related_service({"method": "GET"}) == REQUIRES_MAPPING

    def test_blank_values(self):
        assert format_related_service({"method": "  ", "path": "  "}) == REQUIRES_MAPPING

    def test_empty_object(self):
        assert format_related_service({}) == REQUIRES_MAPPING

    def test_none(self):
        assert format_related_service(None) == REQUIRES_MAPPING

    def test_non_dict(self):
        assert format_related_service("POST /x") == REQUIRES_MAPPING


class TestMarkdownBullets:
    def test_list_becomes_bullets(self):
        assert markdown_bullets(["first", "second"]) == "- first\n- second"

    def test_empty_list_uses_placeholder(self):
        assert markdown_bullets([]) == "—"

    def test_custom_placeholder(self):
        assert markdown_bullets([], empty="None") == "None"

    def test_blank_entries_are_dropped(self):
        assert markdown_bullets(["", "  ", "real"]) == "- real"

    def test_values_are_trimmed(self):
        assert markdown_bullets(["  spaced  "]) == "- spaced"

    def test_non_list_uses_placeholder(self):
        assert markdown_bullets(None) == "—"

    def test_non_string_entries_are_stringified(self):
        assert markdown_bullets([42]) == "- 42"


class TestTypeCounts:
    def test_counts_by_type(self):
        result = {
            "test_cases": [
                _case(type="positive"),
                _case(id="TC-002", type="positive"),
                _case(id="TC-003", type="negative"),
                _case(id="TC-004", type="boundary"),
            ]
        }
        counts = type_counts(result)

        assert counts["positive"] == 2
        assert counts["negative"] == 1
        assert counts["boundary"] == 1
        assert counts["state_transition"] == 0

    def test_all_type_keys_are_always_present(self):
        assert set(type_counts({"test_cases": []})) == set(TYPE_ORDER)

    def test_unknown_type_is_ignored(self):
        counts = type_counts({"test_cases": [_case(type="exploratory")]})
        assert sum(counts.values()) == 0

    def test_empty_result(self):
        assert set(type_counts({})) == set(TYPE_ORDER)

    def test_none_result(self):
        assert sum(type_counts(None).values()) == 0


class TestTestCaseOverview:
    def test_one_row_per_case(self):
        result = {"test_cases": [_case(), _case(id="TC-002", title="Second")]}
        rows = case_overview(result)

        assert len(rows) == 2
        assert rows[0]["ID"] == "TC-001"
        assert rows[1]["Title"] == "Second"

    def test_row_has_all_review_columns(self):
        rows = case_overview({"test_cases": [_case()]})

        assert list(rows[0]) == ["ID", "Title", "Type", "Priority", "Related Service"]

    def test_related_service_is_formatted(self):
        case = _case(
            related_service={"method": "POST", "path": "/v", "service": "Admin API"}
        )
        rows = case_overview({"test_cases": [case]})

        assert rows[0]["Related Service"] == "POST /v (Admin API)"

    def test_unmapped_service_is_shown_as_sentinel(self):
        case = _case(related_service={"method": REQUIRES_MAPPING, "path": REQUIRES_MAPPING})
        rows = case_overview({"test_cases": [case]})

        assert rows[0]["Related Service"] == REQUIRES_MAPPING

    def test_empty_result(self):
        assert case_overview({}) == []

    def test_none_result(self):
        assert case_overview(None) == []


class TestCaseHeading:
    def test_id_and_title_are_joined(self):
        assert case_heading(_case()) == "TC-001 — Create a valid percentage voucher"

    def test_missing_title_falls_back_to_id(self):
        assert case_heading(_case(title="")) == "TC-001"

    def test_missing_id_falls_back_to_title(self):
        assert case_heading(_case(id="", title="Only a title")) == "Only a title"

    def test_non_dict_case_does_not_break(self):
        assert case_heading(None) == ""


class TestCaseMeta:
    def test_type_and_priority(self):
        assert case_meta(_case(type="negative", priority="low")) == (
            "Type: negative\nPriority: low"
        )

    def test_missing_values_use_placeholder(self):
        assert case_meta(_case(type="", priority="")) == "Type: —\nPriority: —"


class TestCaseSections:
    def test_sections_are_present_in_fixed_order(self):
        labels = [section["label"] for section in case_sections(_case())]

        assert labels == [
            "Preconditions",
            "Steps",
            "Expected Result",
            "Related Service",
        ]

    def test_bodies_come_from_the_case(self):
        sections = {section["label"]: section["body"] for section in case_sections(_case())}

        assert sections["Preconditions"] == "- An admin is signed in"
        assert sections["Steps"] == "- Submit the request"
        assert sections["Expected Result"] == "The voucher is created."
        assert sections["Related Service"] == "POST /admin/voucher"

    def test_empty_optional_fields_do_not_break(self):
        case = _case(preconditions=[], steps=[], related_service=None)
        sections = {section["label"]: section["body"] for section in case_sections(case)}

        assert sections["Preconditions"] == "None"
        assert sections["Steps"] == "—"
        assert sections["Related Service"] == REQUIRES_MAPPING

    def test_missing_fields_do_not_break(self):
        sections = {section["label"]: section["body"] for section in case_sections({})}

        assert sections["Preconditions"] == "None"
        assert sections["Steps"] == "—"
        assert sections["Expected Result"] == "—"
        assert sections["Related Service"] == REQUIRES_MAPPING

    def test_non_dict_case_does_not_break(self):
        assert len(case_sections(None)) == 4


class TestCaseDetails:
    def test_one_block_per_case_in_order(self):
        result = {"test_cases": [_case(), _case(id="TC-002", title="Second")]}
        details = case_details(result)

        assert len(details) == 2
        assert details[0]["heading"] == "TC-001 — Create a valid percentage voucher"
        assert details[1]["heading"] == "TC-002 — Second"

    def test_every_case_gets_the_same_complete_structure(self):
        """هیچ تست‌کیسی — صرف‌نظر از نوعش — خلاصه‌تر از بقیه نمایش داده نمی‌شود."""
        result = {
            "test_cases": [
                _case(type="positive"),
                _case(id="TC-002", type="negative"),
                _case(id="TC-003", type="boundary"),
                _case(id="TC-004", type="state_transition"),
            ]
        }
        details = case_details(result)

        assert len(details) == 4
        for detail in details:
            assert set(detail) == {"heading", "meta", "sections"}
            assert [section["label"] for section in detail["sections"]] == [
                "Preconditions",
                "Steps",
                "Expected Result",
                "Related Service",
            ]

    def test_type_and_priority_are_shown_per_case(self):
        result = {
            "test_cases": [_case(type="positive"), _case(id="TC-002", type="negative")]
        }
        metas = [detail["meta"] for detail in case_details(result)]

        assert metas[0] == "Type: positive\nPriority: high"
        assert metas[1] == "Type: negative\nPriority: high"

    def test_case_with_only_required_fields_still_renders(self):
        result = {
            "test_cases": [
                {"id": "TC-009", "title": "Bare", "type": "boundary", "priority": "low"}
            ]
        }
        details = case_details(result)

        assert len(details) == 1
        assert details[0]["heading"] == "TC-009 — Bare"
        assert len(details[0]["sections"]) == 4

    def test_empty_result(self):
        assert case_details({}) == []

    def test_none_result(self):
        assert case_details(None) == []

    def test_result_without_test_cases_key(self):
        assert case_details({"task_summary": "no cases here"}) == []
