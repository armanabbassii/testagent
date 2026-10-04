"""تست‌های تحلیلِ سناریو، ترتیبِ اجرا و وابستگی‌های داده (قدم سوم)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agents.test_case_generator.scenario_analysis import (
    ScenarioAnalysisAgent,
    ScenarioAnalysisError,
    Step3ScenarioAnalysisGenerator,
    build_prompt,
    extract_api_mappings,
    extract_test_cases,
    find_cycle,
    split_prompt,
    validate_scenario_payload,
)

PROMPT_PATH = (
    Path(__file__).parents[3]
    / "src"
    / "agents"
    / "test_case_generator"
    / "prompts"
    / "step3_scenario_analysis.md"
)


# ── fixture ها ───────────────────────────────────────────────────────────────

def _step1_result() -> dict:
    """نتیجه‌ی قدم اول با چهار تست‌کیس."""
    titles = {
        "TC-001": "Create Voucher",
        "TC-002": "Get Voucher",
        "TC-003": "Activate Voucher",
        "TC-004": "Export Voucher Report",
    }
    return {
        "task_summary": "Voucher lifecycle management.",
        "identified_requirements": ["Create a voucher", "Activate a voucher"],
        "test_cases": [
            {
                "id": case_id,
                "title": title,
                "type": "positive",
                "priority": "high",
                "preconditions": [],
                "steps": [f"Perform {title}"],
                "expected_result": f"{title} succeeds.",
                "related_service": None,
            }
            for case_id, title in titles.items()
        ],
        "clarifications": [],
    }


def _id_param() -> dict:
    return {
        "name": "id",
        "in": "path",
        "required": True,
        "type": "string",
        "description": "Voucher identifier",
    }


def _step2_result() -> dict:
    """نتیجه‌ی قدم دوم: سه عملیاتِ کشف‌شده و چهار نگاشت (یکی حل‌نشده)."""
    return {
        "services": [
            {
                "name": "Admin",
                "source_url": "https://host/api/swagger-ui/index.html",
                "base_url": "https://podium-admin.sandpod.ir",
                "apis": [
                    {
                        "method": "POST",
                        "path": "/admin/voucher",
                        "operation_id": "createVoucher",
                        "summary": "Create voucher",
                        "parameters": [],
                        "request_body": {"amount": 0},
                        "responses": {"200": {"fields": ["id", "code"]}},
                    },
                    {
                        "method": "GET",
                        "path": "/admin/voucher/{id}",
                        "operation_id": "getVoucherDetails",
                        "summary": "Get voucher details",
                        "parameters": [_id_param()],
                        "request_body": None,
                        "responses": {"200": {"fields": ["id", "code", "status"]}},
                    },
                    {
                        "method": "PUT",
                        "path": "/admin/voucher/{id}/active",
                        "operation_id": "activateVoucher",
                        "summary": "Activate voucher",
                        "parameters": [_id_param()],
                        "request_body": None,
                        "responses": {"200": {"fields": []}},
                    },
                ],
                "authorization": {"header": "Authorization", "value": "Bearer {{token}}"},
                "unresolved": [],
            }
        ],
        "mappings": [
            _mapping("TC-001", "POST", "/admin/voucher", "createVoucher"),
            _mapping("TC-002", "GET", "/admin/voucher/{id}", "getVoucherDetails"),
            _mapping("TC-003", "PUT", "/admin/voucher/{id}/active", "activateVoucher"),
            _mapping("TC-004", None, None, None),
        ],
        "clarifications": [],
    }


def _mapping(case_id, method, path, operation_id, confidence="high") -> dict:
    api = (
        None
        if method is None
        else {"method": method, "path": path, "operation_id": operation_id}
    )
    return {
        "test_case_id": case_id,
        "api": api,
        "confidence": confidence,
        "reason": "Matched in Step 2.",
        "clarification": "" if api else "Which operation applies?",
    }


def _valid_payload() -> dict:
    return {
        "scenarios": [
            {
                "id": "SC-001",
                "title": "Voucher Lifecycle",
                "test_case_ids": ["TC-001", "TC-002", "TC-003"],
                "reason": "These test cases drive the same voucher through its lifecycle.",
            }
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
                    {"test_case_id": "TC-003", "location": "path", "parameter": "id"},
                ],
                "confidence": "high",
                "reason": "The create operation returns the identifier later operations need.",
            }
        ],
        "clarifications": [],
    }


def _validate(payload, step1=None, step2=None):
    return validate_scenario_payload(
        payload,
        test_cases=extract_test_cases(step1 if step1 is not None else _step1_result()),
        step2_result=step2 if step2 is not None else _step2_result(),
    )


def _failure(payload, step1=None, step2=None) -> str:
    """پیامِ خطای اعتبارسنجی را برمی‌گرداند."""
    with pytest.raises(ScenarioAnalysisError) as excinfo:
        _validate(payload, step1, step2)
    return str(excinfo.value)


def _with_dependency(dependency: dict) -> dict:
    payload = _valid_payload()
    payload["data_dependencies"] = [dependency]
    return payload


def _dependency(**overrides) -> dict:
    dependency = _valid_payload()["data_dependencies"][0]
    dependency = json.loads(json.dumps(dependency))
    dependency.update(overrides)
    return dependency


# ── سناریو ───────────────────────────────────────────────────────────────────

class TestScenarioValidation:
    def test_valid_scenario_is_accepted(self):
        scenarios, _, _, _ = _validate(_valid_payload())

        assert [scenario["id"] for scenario in scenarios] == ["SC-001"]
        assert scenarios[0]["test_case_ids"] == ["TC-001", "TC-002", "TC-003"]
        assert scenarios[0]["title"] == "Voucher Lifecycle"

    def test_unknown_test_case_id_is_rejected(self):
        payload = _valid_payload()
        payload["scenarios"][0]["test_case_ids"] = ["TC-001", "TC-999"]

        assert "unknown test case id" in _failure(payload)

    def test_duplicate_scenario_id_is_rejected(self):
        payload = _valid_payload()
        payload["scenarios"].append(
            {
                "id": "SC-001",
                "title": "Another Flow",
                "test_case_ids": ["TC-002"],
                "reason": "Different flow.",
            }
        )

        assert "duplicate scenario id" in _failure(payload)

    def test_empty_title_is_rejected(self):
        payload = _valid_payload()
        payload["scenarios"][0]["title"] = "   "

        assert "'title' is missing" in _failure(payload)

    def test_empty_reason_is_rejected(self):
        payload = _valid_payload()
        payload["scenarios"][0]["reason"] = ""

        assert "'reason' is missing" in _failure(payload)

    def test_missing_scenario_id_is_rejected(self):
        payload = _valid_payload()
        payload["scenarios"][0]["id"] = ""

        assert "'id' is missing" in _failure(payload)

    def test_empty_test_case_ids_is_rejected(self):
        payload = _valid_payload()
        payload["scenarios"][0]["test_case_ids"] = []

        assert "'test_case_ids' must be a non-empty array" in _failure(payload)

    def test_scenarios_must_be_a_non_empty_array(self):
        payload = _valid_payload()
        payload["scenarios"] = []

        assert "'scenarios' must be a non-empty array" in _failure(payload)

    def test_a_test_case_may_belong_to_several_scenarios(self):
        payload = _valid_payload()
        payload["scenarios"].append(
            {
                "id": "SC-002",
                "title": "Voucher Reporting",
                "test_case_ids": ["TC-002", "TC-003"],
                "reason": "Both feed the reporting flow.",
            }
        )

        scenarios, _, _, _ = _validate(payload)

        assert len(scenarios) == 2
        assert scenarios[1]["id"] == "SC-002"

    def test_scenario_with_a_non_object_entry_is_rejected(self):
        payload = _valid_payload()
        payload["scenarios"].append("SC-002")

        assert "expected an object" in _failure(payload)


# ── ترتیبِ اجرا ──────────────────────────────────────────────────────────────

class TestExecutionOrderValidation:
    def test_valid_order_is_accepted(self):
        _, steps, _, _ = _validate(_valid_payload())

        assert [(step["test_case_id"], step["order"]) for step in steps] == [
            ("TC-001", 1),
            ("TC-002", 2),
            ("TC-003", 3),
        ]
        assert steps[2]["depends_on"] == ["TC-001", "TC-002"]

    def test_independent_test_case_has_no_dependencies(self):
        _, steps, _, _ = _validate(_valid_payload())

        assert steps[0]["depends_on"] == []

    def test_multiple_dependencies_are_all_kept(self):
        _, steps, _, _ = _validate(_valid_payload())

        assert steps[2]["depends_on"] == ["TC-001", "TC-002"]

    def test_unknown_dependency_is_rejected(self):
        payload = _valid_payload()
        payload["execution_order"][1]["depends_on"] = ["TC-999"]

        assert "unknown dependency" in _failure(payload)

    def test_dependency_without_an_entry_in_execution_order_is_rejected(self):
        payload = _valid_payload()
        payload["execution_order"] = payload["execution_order"][1:]

        assert "has no entry in 'execution_order'" in _failure(payload)

    def test_self_dependency_is_rejected(self):
        payload = _valid_payload()
        payload["execution_order"][1]["depends_on"] = ["TC-002"]

        assert "depends on itself" in _failure(payload)

    def test_duplicate_order_value_is_rejected(self):
        payload = _valid_payload()
        payload["execution_order"][2]["order"] = 2

        assert "duplicate order" in _failure(payload)

    def test_duplicate_test_case_entry_is_rejected(self):
        payload = _valid_payload()
        payload["execution_order"].append(
            {"test_case_id": "TC-001", "order": 4, "depends_on": []}
        )

        assert "duplicate entry" in _failure(payload)

    def test_non_integer_order_is_rejected(self):
        payload = _valid_payload()
        payload["execution_order"][0]["order"] = "1"

        assert "'order' must be a positive integer" in _failure(payload)

    def test_boolean_order_is_rejected(self):
        payload = _valid_payload()
        payload["execution_order"][0]["order"] = True

        assert "'order' must be a positive integer" in _failure(payload)

    def test_zero_order_is_rejected(self):
        payload = _valid_payload()
        payload["execution_order"][0]["order"] = 0

        assert "'order' must be >= 1" in _failure(payload)

    def test_unknown_test_case_in_order_is_rejected(self):
        payload = _valid_payload()
        payload["execution_order"][0]["test_case_id"] = "TC-999"

        assert "unknown test_case_id" in _failure(payload)

    def test_order_contradicting_a_dependency_is_rejected(self):
        payload = _valid_payload()
        payload["execution_order"][0]["order"] = 9

        assert "must be executed before" in _failure(payload)

    def test_two_node_cycle_is_rejected(self):
        payload = _valid_payload()
        payload["execution_order"] = [
            {"test_case_id": "TC-001", "order": 1, "depends_on": ["TC-002"]},
            {"test_case_id": "TC-002", "order": 2, "depends_on": ["TC-001"]},
        ]
        payload["data_dependencies"] = []

        assert "circular dependency detected" in _failure(payload)

    def test_three_node_cycle_is_rejected(self):
        payload = _valid_payload()
        payload["execution_order"] = [
            {"test_case_id": "TC-001", "order": 1, "depends_on": ["TC-003"]},
            {"test_case_id": "TC-002", "order": 2, "depends_on": ["TC-001"]},
            {"test_case_id": "TC-003", "order": 3, "depends_on": ["TC-002"]},
        ]
        payload["data_dependencies"] = []

        assert "circular dependency detected" in _failure(payload)

    def test_execution_order_must_be_a_non_empty_array(self):
        payload = _valid_payload()
        payload["execution_order"] = []

        assert "'execution_order' must be a non-empty array" in _failure(payload)

    def test_orders_are_returned_sorted(self):
        payload = _valid_payload()
        payload["execution_order"] = [
            {"test_case_id": "TC-003", "order": 3, "depends_on": ["TC-001", "TC-002"]},
            {"test_case_id": "TC-001", "order": 1, "depends_on": []},
            {"test_case_id": "TC-002", "order": 2, "depends_on": ["TC-001"]},
        ]

        _, steps, _, _ = _validate(payload)

        assert [step["order"] for step in steps] == [1, 2, 3]


class TestFindCycle:
    def test_dag_has_no_cycle(self):
        assert find_cycle({"TC-002": {"TC-001"}, "TC-001": set()}) is None

    def test_two_node_cycle_is_found(self):
        cycle = find_cycle({"TC-001": {"TC-002"}, "TC-002": {"TC-001"}})

        assert cycle is not None
        assert cycle[0] == cycle[-1]
        assert set(cycle) == {"TC-001", "TC-002"}

    def test_three_node_cycle_is_found(self):
        cycle = find_cycle(
            {"TC-001": {"TC-003"}, "TC-002": {"TC-001"}, "TC-003": {"TC-002"}}
        )

        assert cycle is not None
        assert len(cycle) == 4


# ── وابستگیِ داده ────────────────────────────────────────────────────────────

class TestDataDependencyValidation:
    def test_valid_source_and_targets_are_accepted(self):
        _, _, dependencies, _ = _validate(_valid_payload())

        assert len(dependencies) == 1
        dependency = dependencies[0]
        assert dependency["variable_name"] == "voucherId"
        assert dependency["source"] == {
            "test_case_id": "TC-001",
            "location": "response.body",
            "path": "$.id",
        }
        assert [target["test_case_id"] for target in dependency["targets"]] == [
            "TC-002",
            "TC-003",
        ]
        assert dependency["confidence"] == "high"

    def test_empty_data_dependencies_is_valid(self):
        payload = _valid_payload()
        payload["data_dependencies"] = []

        _, _, dependencies, _ = _validate(payload)

        assert dependencies == []

    def test_unknown_source_test_case_is_rejected(self):
        payload = _with_dependency(
            _dependency(
                source={"test_case_id": "TC-999", "location": "response.body", "path": "$.id"}
            )
        )

        assert "unknown test case id" in _failure(payload)

    def test_unknown_target_test_case_is_rejected(self):
        payload = _with_dependency(
            _dependency(
                targets=[{"test_case_id": "TC-999", "location": "path", "parameter": "id"}]
            )
        )

        assert "unknown test case id" in _failure(payload)

    def test_source_without_a_resolved_mapping_is_rejected(self):
        payload = _with_dependency(
            _dependency(
                source={"test_case_id": "TC-004", "location": "response.body", "path": "$.id"}
            )
        )

        assert "has no resolved API mapping" in _failure(payload)

    def test_target_without_a_resolved_mapping_is_rejected(self):
        payload = _with_dependency(
            _dependency(
                targets=[{"test_case_id": "TC-004", "location": "path", "parameter": "id"}]
            )
        )

        assert "has no resolved API mapping" in _failure(payload)

    def test_invalid_source_location_is_rejected(self):
        payload = _with_dependency(
            _dependency(
                source={"test_case_id": "TC-001", "location": "request.body", "path": "$.id"}
            )
        )

        assert "'location' must be one of" in _failure(payload)

    def test_invalid_target_location_is_rejected(self):
        payload = _with_dependency(
            _dependency(
                targets=[{"test_case_id": "TC-002", "location": "cookie", "parameter": "id"}]
            )
        )

        assert "'location' must be one of" in _failure(payload)

    def test_invalid_confidence_is_rejected(self):
        payload = _with_dependency(_dependency(confidence="certain"))

        assert "'confidence' must be one of" in _failure(payload)

    def test_missing_reason_is_rejected(self):
        payload = _with_dependency(_dependency(reason=""))

        assert "'reason' is missing" in _failure(payload)

    def test_invented_response_field_is_rejected(self):
        payload = _with_dependency(
            _dependency(
                source={"test_case_id": "TC-001", "location": "response.body", "path": "$.secret"}
            )
        )

        assert "is not a response field" in _failure(payload)

    def test_parameter_not_on_the_target_operation_is_rejected(self):
        payload = _with_dependency(
            _dependency(
                targets=[{"test_case_id": "TC-002", "location": "path", "parameter": "voucherKey"}]
            )
        )

        assert "is not a path parameter" in _failure(payload)

    def test_target_parameter_may_be_left_unchecked_when_swagger_is_silent(self):
        # عملیاتِ TC-003 پارامترِ query ثبت نکرده است — پس ادعا تأیید یا رد نمی‌شود
        payload = _with_dependency(
            _dependency(
                targets=[{"test_case_id": "TC-003", "location": "query", "parameter": "flag"}]
            )
        )

        _, _, dependencies, _ = _validate(payload)

        assert dependencies[0]["targets"][0]["parameter"] == "flag"

    def test_target_must_not_be_the_source(self):
        payload = _with_dependency(
            _dependency(
                targets=[{"test_case_id": "TC-001", "location": "path", "parameter": "id"}]
            )
        )

        assert "a target must not be the source" in _failure(payload)

    def test_empty_targets_is_rejected(self):
        payload = _with_dependency(_dependency(targets=[]))

        assert "targets: must be a non-empty" in _failure(payload)

    def test_invalid_variable_name_is_rejected(self):
        payload = _with_dependency(_dependency(variable_name="voucher-id"))

        assert "cannot be used as a variable name" in _failure(payload)

    def test_empty_variable_name_is_rejected(self):
        payload = _with_dependency(_dependency(variable_name=""))

        assert "'variable_name' is missing" in _failure(payload)

    def test_duplicate_variable_name_is_rejected(self):
        payload = _valid_payload()
        payload["data_dependencies"] = [
            _dependency(),
            _dependency(
                source={"test_case_id": "TC-002", "location": "response.body", "path": "$.code"},
                targets=[{"test_case_id": "TC-003", "location": "path", "parameter": "id"}],
            ),
        ]
        payload["data_dependencies"][1]["variable_name"] = "voucherId"

        assert "duplicate variable_name" in _failure(payload)

    def test_invalid_json_path_is_rejected(self):
        payload = _with_dependency(
            _dependency(
                source={"test_case_id": "TC-001", "location": "response.body", "path": "id"}
            )
        )

        assert "is not a JSON path" in _failure(payload)

    def test_missing_path_is_rejected(self):
        payload = _with_dependency(
            _dependency(
                source={"test_case_id": "TC-001", "location": "response.body", "path": ""}
            )
        )

        assert "'path' is missing" in _failure(payload)

    def test_data_dependencies_must_be_an_array(self):
        payload = _valid_payload()
        payload["data_dependencies"] = None

        assert "'data_dependencies' must be an array" in _failure(payload)

    def test_dependencies_are_returned_sorted_by_source(self):
        payload = _valid_payload()
        payload["data_dependencies"] = [
            _dependency(
                variable_name="voucherStatus",
                source={"test_case_id": "TC-002", "location": "response.body", "path": "$.status"},
                targets=[{"test_case_id": "TC-003", "location": "path", "parameter": "id"}],
            ),
            _dependency(),
        ]

        _, _, dependencies, _ = _validate(payload)

        assert [dependency["variable_name"] for dependency in dependencies] == [
            "voucherId",
            "voucherStatus",
        ]


# ── ابهام‌ها ────────────────────────────────────────────────────────────────

class TestClarificationValidation:
    def test_valid_clarification_is_accepted(self):
        payload = _valid_payload()
        payload["clarifications"] = [
            {
                "type": "data_dependency",
                "test_case_id": "TC-002",
                "message": "The source field for the voucher identifier could not be determined.",
            }
        ]

        _, _, _, clarifications = _validate(payload)

        assert clarifications[0]["type"] == "data_dependency"
        assert clarifications[0]["test_case_id"] == "TC-002"

    def test_clarification_without_a_test_case_is_accepted(self):
        payload = _valid_payload()
        payload["clarifications"] = [
            {"type": "scenario", "test_case_id": "", "message": "Grouping is ambiguous."}
        ]

        _, _, _, clarifications = _validate(payload)

        assert clarifications[0]["test_case_id"] == ""

    def test_clarifications_must_be_an_array(self):
        payload = _valid_payload()
        payload["clarifications"] = {"type": "scenario"}

        assert "'clarifications' must be an array" in _failure(payload)

    def test_plain_string_clarification_is_rejected(self):
        payload = _valid_payload()
        payload["clarifications"] = ["this is not machine readable"]

        assert "expected an object with 'type' and 'message'" in _failure(payload)

    def test_clarification_without_message_is_rejected(self):
        payload = _valid_payload()
        payload["clarifications"] = [{"type": "scenario", "test_case_id": "TC-002"}]

        assert "'message' is missing" in _failure(payload)

    def test_clarification_without_type_is_rejected(self):
        payload = _valid_payload()
        payload["clarifications"] = [{"message": "Something is unclear."}]

        assert "'type' is missing" in _failure(payload)

    def test_clarification_with_unknown_test_case_is_rejected(self):
        payload = _valid_payload()
        payload["clarifications"] = [
            {"type": "scenario", "test_case_id": "TC-999", "message": "Unclear."}
        ]

        assert "unknown test_case_id" in _failure(payload)


# ── ساختارِ کلی ──────────────────────────────────────────────────────────────

class TestTopLevelShape:
    def test_non_object_payload_is_rejected(self):
        with pytest.raises(ScenarioAnalysisError, match="not a JSON object"):
            _validate(["not", "an", "object"])

    def test_scenarios_are_sorted_by_first_test_case(self):
        payload = _valid_payload()
        payload["scenarios"] = [
            {
                "id": "SC-002",
                "title": "Activation Flow",
                "test_case_ids": ["TC-003"],
                "reason": "Activation only.",
            },
            _valid_payload()["scenarios"][0],
        ]

        scenarios, _, _, _ = _validate(payload)

        assert [scenario["id"] for scenario in scenarios] == ["SC-001", "SC-002"]


# ── prompt ───────────────────────────────────────────────────────────────────

class TestPrompt:
    def test_shipped_prompt_splits(self):
        system_prompt, template = split_prompt(PROMPT_PATH.read_text(encoding="utf-8"))

        assert "SCENARIO & DEPENDENCY ANALYSIS" in system_prompt
        assert "{{step1_result}}" in template
        assert "{{step2_result}}" in template

    def test_missing_input_section_is_rejected(self):
        with pytest.raises(ScenarioAnalysisError, match="no INPUT section"):
            split_prompt("rules only, no input section")

    def test_missing_placeholder_is_rejected(self):
        with pytest.raises(ScenarioAnalysisError, match="missing placeholder"):
            split_prompt("rules\n====\nINPUT\n====\n{{step1_result}}\n")

    def test_static_rules_stay_in_the_system_prompt(self):
        system_prompt, user_message = build_prompt(
            PROMPT_PATH, _step1_result(), _step2_result()
        )

        assert "SCENARIO RULES" in system_prompt
        assert "EXECUTION ORDER RULES" in system_prompt
        assert "DATA DEPENDENCY RULES" in system_prompt
        assert "Never silently guess" in system_prompt
        assert "TC-001" not in system_prompt
        assert user_message.count("TC-001") > 0

    def test_no_literal_test_case_ids_in_the_static_instructions(self):
        system_prompt, _ = split_prompt(PROMPT_PATH.read_text(encoding="utf-8"))

        assert "TC-" not in system_prompt

    def test_runtime_step1_and_step2_data_is_supplied_as_input(self):
        _, user_message = build_prompt(PROMPT_PATH, _step1_result(), _step2_result())

        assert "Voucher lifecycle management." in user_message
        assert "Create Voucher" in user_message
        assert "getVoucherDetails" in user_message
        assert "/admin/voucher/{id}" in user_message
        assert "{{step1_result}}" not in user_message

    def test_swagger_runtime_data_never_reaches_the_system_prompt(self):
        system_prompt, _ = build_prompt(PROMPT_PATH, _step1_result(), _step2_result())

        assert "podium-admin.sandpod.ir" not in system_prompt
        assert "getVoucherDetails" not in system_prompt


# ── عاملِ تحلیل ──────────────────────────────────────────────────────────────

class TestScenarioAnalysisAgent:
    def _run(self, payload, **kwargs):
        with patch(
            "src.agents.test_case_generator.scenario_analysis.LLMClient"
        ) as mock_cls:
            llm = MagicMock()
            llm.chat.return_value = (
                payload if isinstance(payload, str) else json.dumps(payload)
            )
            mock_cls.return_value = llm
            result = ScenarioAnalysisAgent(**kwargs).analyze(
                _step1_result(), _step2_result(), "user-1"
            )
        return result, mock_cls, llm

    def test_returns_validated_sections(self):
        scenarios, steps, dependencies, clarifications = self._run(_valid_payload())[0]

        assert [scenario["id"] for scenario in scenarios] == ["SC-001"]
        assert [step["order"] for step in steps] == [1, 2, 3]
        assert dependencies[0]["variable_name"] == "voucherId"
        assert clarifications == []

    def test_llm_client_is_constructed_with_user_and_agent(self):
        _, mock_cls, _ = self._run(_valid_payload())

        assert mock_cls.call_args.kwargs == {
            "user_id": "user-1",
            "agent_name": ScenarioAnalysisAgent.name,
        }

    def test_llm_receives_the_complete_structured_input(self):
        _, _, llm = self._run(_valid_payload())
        kwargs = llm.chat.call_args.kwargs

        assert "TC-001" in kwargs["user_message"]
        assert "getVoucherDetails" in kwargs["user_message"]
        assert "TC-001" not in kwargs["system_prompt"]

    def test_hallucinated_dependency_is_rejected(self):
        payload = _valid_payload()
        payload["execution_order"][1]["depends_on"] = ["TC-999"]

        with pytest.raises(ScenarioAnalysisError, match="unknown dependency"):
            self._run(payload)

    def test_non_json_output_raises(self):
        with pytest.raises(ScenarioAnalysisError, match="did not return JSON"):
            self._run("not json at all")

    def test_invalid_step1_result_raises(self):
        with pytest.raises(ScenarioAnalysisError, match="does not contain any test case"):
            ScenarioAnalysisAgent().analyze({}, _step2_result(), "user-1")

    def test_custom_prompt_path_is_used(self, tmp_path):
        prompt_file = tmp_path / "custom.md"
        prompt_file.write_text(
            "Custom step 3 rules.\n====\nINPUT\n====\n"
            "ONE:\n{{step1_result}}\nTWO:\n{{step2_result}}\n",
            encoding="utf-8",
        )

        _, _, llm = self._run(_valid_payload(), prompt_path=prompt_file)

        assert llm.chat.call_args.kwargs["system_prompt"] == "Custom step 3 rules."


# ── نتیجه‌ی نهاییِ قدم سوم ───────────────────────────────────────────────────

class TestExtractApiMappings:
    def test_reads_mappings_from_a_step2_result(self):
        assert len(extract_api_mappings(_step2_result())) == 4

    def test_step2_result_without_mappings_is_rejected(self):
        with pytest.raises(ScenarioAnalysisError, match="does not contain any test case mapping"):
            extract_api_mappings({"services": []})

    def test_non_object_step2_result_is_rejected(self):
        with pytest.raises(ScenarioAnalysisError, match="must be a JSON object"):
            extract_api_mappings(["nope"])


class TestStep3ScenarioAnalysisGenerator:
    def _generate(self, payload, step1=None, step2=None):
        with patch(
            "src.agents.test_case_generator.scenario_analysis.LLMClient"
        ) as mock_cls:
            mock_cls.return_value.chat.return_value = json.dumps(payload)
            result = Step3ScenarioAnalysisGenerator().generate(
                step1 if step1 is not None else _step1_result(),
                step2 if step2 is not None else _step2_result(),
                "user-1",
            )
        return result

    def test_result_has_exactly_the_step3_sections(self):
        result = self._generate(_valid_payload())

        assert set(result) == {
            "scenarios",
            "execution_order",
            "data_dependencies",
            "clarifications",
        }

    def test_result_carries_the_validated_content(self):
        result = self._generate(_valid_payload())

        assert result["scenarios"][0]["title"] == "Voucher Lifecycle"
        assert result["execution_order"][1]["depends_on"] == ["TC-001"]
        assert result["data_dependencies"][0]["source"]["path"] == "$.id"

    def test_invalid_step1_result_raises_before_calling_the_llm(self):
        with patch(
            "src.agents.test_case_generator.scenario_analysis.LLMClient"
        ) as mock_cls:
            with pytest.raises(ScenarioAnalysisError, match="does not contain any test case"):
                Step3ScenarioAnalysisGenerator().generate({}, _step2_result(), "user-1")

        mock_cls.assert_not_called()

    def test_invalid_step2_result_raises_before_calling_the_llm(self):
        with patch(
            "src.agents.test_case_generator.scenario_analysis.LLMClient"
        ) as mock_cls:
            with pytest.raises(ScenarioAnalysisError, match="does not contain any test case mapping"):
                Step3ScenarioAnalysisGenerator().generate(
                    _step1_result(), {}, "user-1"
                )

        mock_cls.assert_not_called()
