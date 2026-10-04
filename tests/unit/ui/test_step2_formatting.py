"""تست‌های لایه‌ی نمایشیِ قدم دوم (کشفِ API و نگاشت)."""

from ui.formatting import (
    api_catalogue,
    api_label,
    mapping_counts,
    mapping_details,
    service_details,
)


def _result() -> dict:
    return {
        "services": [
            {
                "name": "Admin",
                "source_url": "https://host/api/swagger-ui/index.html?urls.primaryName=Admin",
                "base_url": "https://podium-admin.sandpod.ir",
                "title": "Admin API",
                "version": "2.1.0",
                "apis": [
                    {
                        "method": "GET",
                        "path": "/admin/voucher/{id}",
                        "operation_id": "getVoucherDetails",
                        "summary": "Get voucher details",
                        "parameters": [],
                        "request_body": None,
                        "responses": {},
                    },
                    {
                        "method": "PUT",
                        "path": "/admin/voucher/{id}/active",
                        "operation_id": "activateVoucher",
                        "summary": "Activate voucher",
                        "parameters": [],
                        "request_body": None,
                        "responses": {},
                    },
                ],
                "authorization": {"header": "Authorization", "value": "Bearer {{token}}"},
                "unresolved": [],
            }
        ],
        "mappings": [
            {
                "test_case_id": "TC-001",
                "api": {
                    "method": "GET",
                    "path": "/admin/voucher/{id}",
                    "operation_id": "getVoucherDetails",
                },
                "confidence": "high",
                "reason": "The operation returns voucher details by id.",
                "clarification": "",
            },
            {
                "test_case_id": "TC-002",
                "api": None,
                "confidence": "low",
                "reason": "Several operations could activate a voucher.",
                "clarification": "Should activation use PUT /active or POST /activate?",
            },
        ],
        "clarifications": ["The sandbox base URL differs from production."],
    }


def _test_cases() -> list[dict]:
    return [
        {"id": "TC-001", "title": "Verify successful retrieval of voucher details"},
        {"id": "TC-002", "title": "Activate a voucher"},
    ]


# ── برچسبِ عملیات ────────────────────────────────────────────────────────────

class TestApiLabel:
    def test_method_and_path(self):
        assert api_label({"method": "GET", "path": "/x"}) == "GET /x"

    def test_missing_path_falls_back_to_method(self):
        assert api_label({"method": "GET"}) == "GET"

    def test_non_dict_gives_empty_string(self):
        assert api_label(None) == ""


# ── سرویس‌های کشف‌شده ────────────────────────────────────────────────────────

class TestServiceDetails:
    def test_one_block_per_service(self):
        assert len(service_details(_result())) == 1

    def test_name_source_and_base_url_are_exposed(self):
        service = service_details(_result())[0]

        assert service["name"] == "Admin"
        assert service["source_url"].endswith("urls.primaryName=Admin")
        assert service["base_url"] == "https://podium-admin.sandpod.ir"

    def test_authorization_value_is_exposed(self):
        assert service_details(_result())[0]["authorization"] == "Bearer {{token}}"

    def test_missing_authorization_is_empty_not_invented(self):
        result = _result()
        result["services"][0]["authorization"] = None

        assert service_details(result)[0]["authorization"] == ""

    def test_unresolved_notes_are_exposed(self):
        result = _result()
        result["services"][0]["unresolved"] = ["Base URL could not be determined."]

        assert service_details(result)[0]["unresolved"] == [
            "Base URL could not be determined."
        ]

    def test_empty_result_gives_no_blocks(self):
        assert service_details({}) == []
        assert service_details(None) == []


class TestApiCatalogue:
    def test_every_operation_of_the_service_is_listed(self):
        catalogue = api_catalogue(_result()["services"][0])

        assert [item["label"] for item in catalogue] == [
            "GET /admin/voucher/{id}",
            "PUT /admin/voucher/{id}/active",
        ]

    def test_operation_id_and_summary_are_carried(self):
        first = api_catalogue(_result()["services"][0])[0]

        assert first["operation"] == "getVoucherDetails"
        assert first["summary"] == "Get voucher details"

    def test_service_without_operations_gives_empty_list(self):
        assert api_catalogue({"apis": []}) == []

    def test_non_dict_entries_are_skipped(self):
        assert api_catalogue({"apis": ["nope"]}) == []


# ── نگاشتِ تست‌کیس‌ها ─────────────────────────────────────────────────────────

class TestMappingDetails:
    def test_every_mapping_is_rendered(self):
        assert len(mapping_details(_result(), _test_cases())) == 2

    def test_title_of_the_step1_case_is_joined_in(self):
        details = mapping_details(_result(), _test_cases())

        assert details[0]["heading"] == (
            "TC-001 — Verify successful retrieval of voucher details"
        )

    def test_resolved_mapping_carries_api_and_operation(self):
        resolved = mapping_details(_result(), _test_cases())[0]

        assert resolved["resolved"] is True
        assert resolved["api"] == "GET /admin/voucher/{id}"
        assert resolved["operation"] == "getVoucherDetails"
        assert resolved["confidence"] == "high"
        assert resolved["reason"]

    def test_unresolved_mapping_exposes_its_clarification(self):
        unresolved = mapping_details(_result(), _test_cases())[1]

        assert unresolved["resolved"] is False
        assert unresolved["api"] == ""
        assert unresolved["operation"] == ""
        assert unresolved["confidence"] == "low"
        assert "PUT /active" in unresolved["clarification"]

    def test_heading_without_a_matching_test_case_is_still_built(self):
        details = mapping_details(_result())

        assert details[0]["heading"] == "TC-001"

    def test_empty_optional_fields_do_not_break_the_block(self):
        result = {
            "mappings": [
                {
                    "test_case_id": "TC-001",
                    "api": {"method": "GET", "path": "/x"},
                    "confidence": "medium",
                    "reason": "Best available match.",
                }
            ]
        }

        detail = mapping_details(result, _test_cases())[0]

        assert detail["operation"] == ""
        assert detail["clarification"] == ""
        assert detail["resolved"] is True

    def test_empty_result_gives_no_blocks(self):
        assert mapping_details({}, _test_cases()) == []


class TestMappingCounts:
    def test_resolved_and_unresolved_are_counted(self):
        assert mapping_counts(_result()) == {"resolved": 1, "unresolved": 1}

    def test_all_resolved(self):
        result = {"mappings": [{"test_case_id": "TC-1", "api": {"method": "GET", "path": "/x"}}]}

        assert mapping_counts(result) == {"resolved": 1, "unresolved": 0}

    def test_all_unresolved(self):
        result = {"mappings": [{"test_case_id": "TC-1", "api": None}]}

        assert mapping_counts(result) == {"resolved": 0, "unresolved": 1}

    def test_empty_result_counts_zero(self):
        assert mapping_counts({}) == {"resolved": 0, "unresolved": 0}
        assert mapping_counts(None) == {"resolved": 0, "unresolved": 0}
