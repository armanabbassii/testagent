"""تست‌های لایه‌ی نمایشیِ قدم سوم (سناریو، ترتیبِ اجرا و وابستگی‌های داده)."""

from ui.formatting import (
    clarification_details,
    dependency_details,
    execution_arrow,
    execution_steps,
    scenario_details,
    step3_counts,
)


def _test_cases() -> list[dict]:
    return [
        {"id": "TC-001", "title": "Create Voucher"},
        {"id": "TC-002", "title": "Get Voucher"},
        {"id": "TC-003", "title": "Activate Voucher"},
    ]


def _result() -> dict:
    return {
        "scenarios": [
            {
                "id": "SC-001",
                "title": "Voucher Lifecycle",
                "test_case_ids": ["TC-001", "TC-002"],
                "reason": "Both drive the same voucher.",
            },
            {
                "id": "SC-002",
                "title": "Activation",
                "test_case_ids": ["TC-003"],
                "reason": "Activation is its own flow.",
            },
        ],
        "execution_order": [
            {"test_case_id": "TC-001", "order": 1, "depends_on": []},
            {"test_case_id": "TC-002", "order": 2, "depends_on": ["TC-001"]},
            {"test_case_id": "TC-003", "order": 3, "depends_on": ["TC-001", "TC-002"]},
        ],
        "data_dependencies": [
            {
                "variable_name": "voucherId",
                "source": {
                    "test_case_id": "TC-001",
                    "location": "response.body",
                    "path": "$.id",
                },
                "targets": [
                    {"test_case_id": "TC-002", "location": "path", "parameter": "id"},
                ],
                "confidence": "high",
                "reason": "The create operation returns the identifier.",
            }
        ],
        "clarifications": [
            {
                "type": "data_dependency",
                "test_case_id": "TC-003",
                "message": "The activation response fields are not documented.",
            },
            {
                "type": "scenario",
                "test_case_id": "",
                "message": "The report flow could belong to either scenario.",
            },
        ],
    }


# ── سناریوها ────────────────────────────────────────────────────────────────

class TestScenarioDetails:
    def test_builds_a_block_per_scenario(self):
        details = scenario_details(_result(), _test_cases())

        assert [detail["id"] for detail in details] == ["SC-001", "SC-002"]

    def test_heading_combines_id_and_title(self):
        details = scenario_details(_result(), _test_cases())

        assert details[0]["heading"] == "SC-001 — Voucher Lifecycle"
        assert details[0]["reason"] == "Both drive the same voucher."

    def test_test_cases_are_numbered_from_one(self):
        details = scenario_details(_result(), _test_cases())

        assert [(case["position"], case["test_case_id"]) for case in details[0]["cases"]] == [
            (1, "TC-001"),
            (2, "TC-002"),
        ]

    def test_case_headings_use_step1_titles(self):
        details = scenario_details(_result(), _test_cases())

        assert details[0]["cases"][0]["heading"] == "TC-001 — Create Voucher"

    def test_case_id_is_used_when_no_title_is_known(self):
        details = scenario_details(_result(), [])

        assert details[0]["cases"][0]["heading"] == "TC-001"

    def test_missing_optional_fields_do_not_break_the_block(self):
        details = scenario_details({"scenarios": [{"id": "SC-001"}]}, [])

        assert details[0]["title"] == ""
        assert details[0]["heading"] == "SC-001"
        assert details[0]["cases"] == []

    def test_empty_or_missing_result_yields_no_block(self):
        assert scenario_details({}, _test_cases()) == []
        assert scenario_details(None, _test_cases()) == []

    def test_non_object_scenario_is_skipped(self):
        assert scenario_details({"scenarios": ["SC-001"]}, []) == []


# ── ترتیبِ اجرا ──────────────────────────────────────────────────────────────

class TestExecutionSteps:
    def test_builds_a_block_per_ordered_test_case(self):
        steps = execution_steps(_result(), _test_cases())

        assert [(step["order"], step["test_case_id"]) for step in steps] == [
            (1, "TC-001"),
            (2, "TC-002"),
            (3, "TC-003"),
        ]

    def test_dependencies_are_labelled_with_titles(self):
        steps = execution_steps(_result(), _test_cases())

        assert steps[2]["depends_on_label"] == (
            "TC-001 — Create Voucher, TC-002 — Get Voucher"
        )
        assert [item["test_case_id"] for item in steps[2]["depends_on"]] == [
            "TC-001",
            "TC-002",
        ]

    def test_independent_step_has_an_empty_dependency_label(self):
        steps = execution_steps(_result(), _test_cases())

        assert steps[0]["depends_on"] == []
        assert steps[0]["depends_on_label"] == ""

    def test_arrow_joins_the_ordered_headings(self):
        arrow = execution_arrow(_result(), _test_cases())

        assert arrow == (
            "TC-001 — Create Voucher → TC-002 — Get Voucher → "
            "TC-003 — Activate Voucher"
        )

    def test_arrow_falls_back_to_ids_without_titles(self):
        arrow = execution_arrow(_result(), [])

        assert arrow == "TC-001 → TC-002 → TC-003"

    def test_arrow_is_empty_without_an_execution_order(self):
        assert execution_arrow({"execution_order": []}, _test_cases()) == ""
        assert execution_arrow(None, _test_cases()) == ""

    def test_non_object_step_is_skipped(self):
        assert execution_steps({"execution_order": ["TC-001"]}, []) == []


# ── وابستگی‌های داده ────────────────────────────────────────────────────────

class TestDependencyDetails:
    def test_builds_a_block_per_dependency(self):
        details = dependency_details(_result(), _test_cases())

        assert len(details) == 1
        assert details[0]["variable_name"] == "voucherId"
        assert details[0]["confidence"] == "high"
        assert details[0]["reason"] == "The create operation returns the identifier."

    def test_source_label_says_where_the_value_comes_from(self):
        source = dependency_details(_result(), _test_cases())[0]["source"]

        assert source["test_case_id"] == "TC-001"
        assert source["heading"] == "TC-001 — Create Voucher"
        assert source["location"] == "response.body"
        assert source["path"] == "id"
        assert source["label"] == "TC-001 → response.body.id"

    def test_target_label_says_where_the_value_is_consumed(self):
        targets = dependency_details(_result(), _test_cases())[0]["targets"]

        assert targets[0]["heading"] == "TC-002 — Get Voucher"
        assert targets[0]["location"] == "path"
        assert targets[0]["parameter"] == "id"
        assert targets[0]["label"] == "TC-002 → path.id"

    def test_missing_source_is_tolerated(self):
        details = dependency_details(
            {"data_dependencies": [{"variable_name": "voucherId"}]}, []
        )

        assert details[0]["source"]["label"] == ""
        assert details[0]["targets"] == []

    def test_non_object_target_is_skipped(self):
        details = dependency_details(
            {
                "data_dependencies": [
                    {"variable_name": "voucherId", "targets": ["TC-002"]}
                ]
            },
            [],
        )

        assert details[0]["targets"] == []

    def test_empty_or_missing_result_yields_no_block(self):
        assert dependency_details({}, _test_cases()) == []
        assert dependency_details(None, _test_cases()) == []


# ── ابهام‌ها ────────────────────────────────────────────────────────────────

class TestClarificationDetails:
    def test_builds_a_block_per_clarification(self):
        details = clarification_details(_result(), _test_cases())

        assert [detail["type"] for detail in details] == [
            "data_dependency",
            "scenario",
        ]

    def test_heading_uses_the_test_case_title_when_present(self):
        details = clarification_details(_result(), _test_cases())

        assert details[0]["heading"] == "TC-003 — Activate Voucher"
        assert details[0]["message"] == "The activation response fields are not documented."

    def test_general_clarification_has_no_heading(self):
        details = clarification_details(_result(), _test_cases())

        assert details[1]["test_case_id"] == ""
        assert details[1]["heading"] == ""

    def test_empty_or_missing_result_yields_no_block(self):
        assert clarification_details({}, _test_cases()) == []
        assert clarification_details(None, _test_cases()) == []

    def test_non_object_clarification_is_skipped(self):
        assert clarification_details({"clarifications": ["not structured"]}, []) == []


# ── شمارش ───────────────────────────────────────────────────────────────────

class TestStep3Counts:
    def test_counts_every_section(self):
        counts = step3_counts(_result())

        assert counts == {
            "scenarios": 2,
            "ordered": 3,
            "dependencies": 1,
            "clarifications": 2,
        }

    def test_empty_result_counts_zero(self):
        assert step3_counts({}) == {
            "scenarios": 0,
            "ordered": 0,
            "dependencies": 0,
            "clarifications": 0,
        }

    def test_none_result_counts_zero(self):
        assert step3_counts(None)["scenarios"] == 0
