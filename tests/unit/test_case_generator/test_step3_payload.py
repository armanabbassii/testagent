"""قراردادِ فشرده‌ی قدم ۲ → ۳: اندازه‌ی payload و بقای هویتِ تست‌کیس‌ها.

این فایل سه چیز را قفل می‌کند:

  1. **اندازه‌ی پیامِ قدم سوم** — کاتالوگِ کاملِ Swagger هرگز نباید وارد پرامپتِ
     قدم سوم شود. رگرسیونِ اصلی همان است: اگر روزی کسی دوباره نتیجه‌ی خامِ
     قدم دوم را به قدم سوم وصل کند، تست‌های این کلاس می‌شکنند.
  2. **بعدِ امنیتیِ همان چیز** — نگاشتِ حل‌نشده نباید حذف شود و نباید API ای
     حدس زده شود.
  3. **بقای هویت** — هر تست‌کیسِ قدم اول باید تا قدم چهارم قابلِ ردیابی بماند.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import src.agents.test_case_generator.scenario_analysis as scenario_analysis
from src.agents.test_case_generator.api_mapping import (
    ApiMappingError,
    compact_step2_result,
)
from src.agents.test_case_generator.postman_generation import _plan
from src.agents.test_case_generator.scenario_analysis import (
    ScenarioAnalysisError,
    render_prompt,
    validate_scenario_payload,
)
from src.agents.test_case_generator.swagger_snapshots import (
    SELECTION_ADMIN,
    SELECTION_BOTH,
    SELECTION_CUSTOMER,
    expand_selection,
)

# مسیرِ فایلِ پرامپت از خودِ ماژول مشتق می‌شود تا تست به ساختارِ پوشه گره نخورد.
STEP3_PROMPT_PATH = (
    Path(scenario_analysis.__file__).parent / "prompts" / "step3_scenario_analysis.md"
)
STEP3_TEMPLATE = scenario_analysis.split_prompt(
    STEP3_PROMPT_PATH.read_text(encoding="utf-8")
)[1]

STEP1_IDS = ("TC-001", "TC-002", "TC-003")

# نشانه‌ی نشتِ کاتالوگ: این متن فقط روی عملیات‌هایی می‌آید که هیچ نگاشتی به
# آن‌ها اشاره نکرده است.
UNMAPPED_SUMMARY = "operation-no-test-case-references"

# کاتالوگِ عمداً بزرگ — نزدیک به همان چیزی که خطای 413 را می‌سازد.
UNMAPPED_PER_SERVICE = 40


# ── fixtureها ────────────────────────────────────────────────────────────────

def _test_case(case_id: str, title: str) -> dict:
    return {
        "id": case_id,
        "title": title,
        "type": "positive",
        "priority": "high",
        "preconditions": [],
        "steps": ["do the thing"],
        "expected_result": f"{title} succeeds",
        "related_service": "admin",
    }


def _step1_result() -> dict:
    return {
        "task_summary": "Voucher lifecycle",
        "identified_requirements": ["create vouchers"],
        "test_cases": [
            _test_case("TC-001", "Get voucher details"),
            _test_case("TC-002", "Activate voucher"),
            _test_case("TC-003", "Export voucher report"),
        ],
        "clarifications": [],
    }


def _operation(method: str, path: str, operation_id: str, summary: str) -> dict:
    return {
        "method": method,
        "path": path,
        "operation_id": operation_id,
        "summary": summary,
        "parameters": [
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
        "request_body": {"content": {"application/json": {"schema": {"type": "object"}}}},
        "responses": {"200": {"description": "OK"}, "404": {"description": "Not found"}},
    }


def _services() -> list[dict]:
    """دو سرویس با یک کاتالوگِ بزرگ؛ فقط دو عملیاتِ اول نگاشت دارند."""
    admin_apis = [
        _operation("GET", "/admin/voucher/{id}", "getVoucherDetails", "Get voucher details"),
        _operation("PUT", "/admin/voucher/{id}/active", "activateVoucher", "Activate voucher"),
    ]
    admin_apis += [
        _operation(
            "GET",
            f"/admin/bulk/{index}",
            f"bulkOperation{index}",
            UNMAPPED_SUMMARY,
        )
        for index in range(UNMAPPED_PER_SERVICE)
    ]

    customer_apis = [
        _operation(
            "GET",
            f"/customer/report/{index}",
            f"customerReport{index}",
            UNMAPPED_SUMMARY,
        )
        for index in range(UNMAPPED_PER_SERVICE)
    ]

    return [
        {
            "name": "Admin",
            "source_url": "https://podium-admin.sandpod.ir",
            "base_url": "https://podium-admin.sandpod.ir",
            "apis": admin_apis,
            "authorization": None,
            "unresolved": [],
        },
        {
            "name": "Customer",
            "source_url": "http://podium-back.devpod.ir",
            "base_url": "http://podium-back.devpod.ir",
            "apis": customer_apis,
            "authorization": None,
            "unresolved": [],
        },
    ]


def _mapping(case_id: str, api: dict | None, *, confidence: str = "high") -> dict:
    return {
        "test_case_id": case_id,
        "api": api,
        "confidence": confidence if api else "low",
        "reason": f"{case_id} maps here",
        "clarification": "" if api else "No operation matched this test case.",
    }


def _step2_result() -> dict:
    """شکلِ کاملِ نتیجه‌ی قدم دوم — همان چیزی که در وضعیتِ جریان ذخیره می‌شود."""
    return {
        "services": _services(),
        "mappings": [
            _mapping(
                "TC-001",
                {
                    "method": "GET",
                    "path": "/admin/voucher/{id}",
                    "operation_id": "getVoucherDetails",
                },
            ),
            _mapping(
                "TC-002",
                {
                    "method": "PUT",
                    "path": "/admin/voucher/{id}/active",
                    "operation_id": "activateVoucher",
                },
            ),
            # نگاشتِ حل‌نشده — باید حل‌نشده بماند، نه اینکه حذف شود.
            _mapping("TC-003", None),
        ],
        "clarifications": ["Which environment is this for?"],
    }


def _snapshot_spec(operation_id: str) -> dict:
    """یک سندِ OpenAPI کوچک ولی معتبر — برای تستِ خواندنِ snapshot."""
    return {
        "openapi": "3.0.1",
        "info": {"title": "Snapshot", "version": "1.0.0"},
        "servers": [{"url": "https://podium-admin.sandpod.ir"}],
        "paths": {
            "/snapshot": {
                "get": {
                    "operationId": operation_id,
                    "summary": "Snapshot operation",
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }


def _render(step2_result: dict) -> str:
    """همان کاری که قدم سوم پیش از فرستادن به مدل می‌کند."""
    return render_prompt(STEP3_TEMPLATE, _step1_result(), step2_result)


# ── قراردادِ فشرده ───────────────────────────────────────────────────────────

class TestCompactContract:
    def test_the_contract_carries_only_the_documented_keys(self):
        contract = compact_step2_result(_step2_result())

        assert set(contract) == {"mappings", "clarifications"}

    def test_the_catalog_is_not_part_of_the_contract(self):
        contract = compact_step2_result(_step2_result())

        assert "services" not in contract
        assert '"services"' not in json.dumps(contract)

    def test_every_mapping_survives_with_its_test_case_id(self):
        contract = compact_step2_result(_step2_result())

        assert [m["test_case_id"] for m in contract["mappings"]] == list(STEP1_IDS)

    def test_no_test_case_id_is_dropped(self):
        contract = compact_step2_result(_step2_result())

        assert {m["test_case_id"] for m in contract["mappings"]} == set(STEP1_IDS)

    def test_the_mapping_order_of_step_two_is_preserved(self):
        contract = compact_step2_result(_step2_result())

        assert [m["test_case_id"] for m in contract["mappings"]] == ["TC-001", "TC-002", "TC-003"]

    def test_only_the_operations_a_mapping_references_are_carried(self):
        contract = compact_step2_result(_step2_result())

        carried = [m["operation_id"] for m in contract["mappings"] if m["operation"]]
        assert carried == ["getVoucherDetails", "activateVoucher"]

    def test_the_carried_operations_are_the_full_definitions(self):
        contract = compact_step2_result(_step2_result())

        operation = contract["mappings"][0]["operation"]

        assert operation is not None
        assert operation["parameters"]
        assert operation["responses"]

    def test_operation_definitions_never_outnumber_mappings(self):
        contract = compact_step2_result(_step2_result())

        assert sum(1 for m in contract["mappings"] if m["operation"]) <= len(
            contract["mappings"]
        )

    def test_an_unresolved_mapping_stays_unresolved(self):
        contract = compact_step2_result(_step2_result())
        unresolved = contract["mappings"][2]

        assert unresolved["test_case_id"] == "TC-003"
        assert unresolved["operation"] is None
        assert unresolved["method"] == ""
        assert unresolved["path"] == ""

    def test_no_operation_is_carried_that_the_catalog_does_not_contain(self):
        contract = compact_step2_result(_step2_result())
        catalog = {
            api["operation_id"] for service in _services() for api in service["apis"]
        }

        carried = {
            m["operation"]["operation_id"]
            for m in contract["mappings"]
            if m["operation"]
        }

        assert carried <= catalog

    def test_the_service_is_kept_for_provenance(self):
        contract = compact_step2_result(_step2_result())

        assert contract["mappings"][0]["service"] == "Admin"

    def test_clarifications_are_carried_forward(self):
        contract = compact_step2_result(_step2_result())

        assert contract["clarifications"] == ["Which environment is this for?"]

    def test_a_missing_services_key_does_not_stop_the_compaction(self):
        step2 = {"mappings": [_mapping("TC-001", None)], "clarifications": []}

        contract = compact_step2_result(step2)

        assert contract["mappings"][0]["operation"] is None

    def test_a_non_object_step_two_result_is_rejected(self):
        with pytest.raises(ApiMappingError, match="must be a JSON object"):
            compact_step2_result(["not", "an", "object"])


# ── payloadِ قدم سوم ─────────────────────────────────────────────────────────

class TestStep3Payload:
    def test_the_payload_carries_every_step_one_test_case_id(self):
        prompt = _render(compact_step2_result(_step2_result()))

        for case_id in STEP1_IDS:
            assert case_id in prompt

    def test_the_payload_carries_the_mapped_operations(self):
        prompt = _render(compact_step2_result(_step2_result()))

        assert "getVoucherDetails" in prompt
        assert "activateVoucher" in prompt

    def test_the_payload_never_mentions_an_unreferenced_operation(self):
        prompt = _render(compact_step2_result(_step2_result()))

        assert UNMAPPED_SUMMARY not in prompt

    def test_the_payload_has_no_services_key(self):
        contract = compact_step2_result(_step2_result())

        assert '"services"' not in json.dumps(contract, ensure_ascii=False)

    def test_operation_definitions_do_not_outnumber_mappings(self):
        contract = compact_step2_result(_step2_result())

        assert '"operation_id"' in json.dumps(contract)
        assert json.dumps(contract).count('"operation_id"') <= 2 * len(
            contract["mappings"]
        )

    def test_the_compact_payload_is_far_smaller_than_the_full_result(self):
        full = json.dumps(_step2_result(), ensure_ascii=False)
        compact = json.dumps(compact_step2_result(_step2_result()), ensure_ascii=False)

        # کاتالوگِ ۸۰ عملیاتی حذف شده؛ فشرده باید چند مرتبه‌ی بزرگی کوچک‌تر باشد.
        assert len(compact) * 4 < len(full)

    def test_the_rendered_prompt_is_far_smaller_than_a_catalog_carrying_one(self):
        compact_prompt = _render(compact_step2_result(_step2_result()))
        full_prompt = _render(_step2_result())

        assert len(compact_prompt) * 4 < len(full_prompt)

    def test_the_step_three_input_validator_accepts_the_compact_contract(self):
        # قراردادِ فشرده باید همان چیزی باشد که قدم سوم به‌عنوان ورودی می‌پذیرد.
        assert scenario_analysis.extract_api_mappings(
            compact_step2_result(_step2_result())
        )

    def test_an_empty_mapping_list_is_still_rejected(self):
        with pytest.raises(ScenarioAnalysisError, match="any test case mapping"):
            scenario_analysis.extract_api_mappings({"mappings": [], "clarifications": []})


# ── همان چیز، از مسیرِ واقعیِ ویزارد ────────────────────────────────────────

class TestStep3UiNeverSendsTheCatalog:
    """قوی‌ترین نگهبانِ رگرسیون: خودِ ورودیِ ui/step3 را بررسی می‌کند."""

    def _captured_step2_result(self) -> dict:
        streamlit = pytest.importorskip("streamlit")
        from ui import step3_scenario_analysis as step3_ui  # noqa: E402

        generator = MagicMock()
        generator.generate.return_value = {
            "scenarios": [],
            "execution_order": [],
            "data_dependencies": [],
            "clarifications": [],
        }

        with patch.object(
            step3_ui, "Step3ScenarioAnalysisGenerator", return_value=generator
        ), patch.object(streamlit, "spinner", MagicMock()):
            result, error = step3_ui._run(_step1_result(), _step2_result())

        assert error is None
        assert result is not None
        return generator.generate.call_args.kwargs["step2_result"]

    def test_the_generator_never_receives_the_discovered_catalog(self):
        payload = self._captured_step2_result()

        assert "services" not in payload
        assert '"services"' not in json.dumps(payload)

    def test_the_generator_never_receives_an_unreferenced_operation(self):
        payload = self._captured_step2_result()

        assert UNMAPPED_SUMMARY not in json.dumps(payload)

    def test_the_generator_receives_every_step_one_test_case_id(self):
        payload = self._captured_step2_result()

        assert {m["test_case_id"] for m in payload["mappings"]} == set(STEP1_IDS)

    def test_the_generator_receives_the_mapped_operations(self):
        payload = self._captured_step2_result()

        assert [
            m["operation_id"] for m in payload["mappings"] if m["operation"]
        ] == ["getVoucherDetails", "activateVoucher"]

    def test_a_step_two_result_that_is_not_an_object_fails_the_step(self):
        streamlit = pytest.importorskip("streamlit")
        from ui import step3_scenario_analysis as step3_ui  # noqa: E402

        with patch.object(streamlit, "spinner", MagicMock()):
            result, error = step3_ui._run(_step1_result(), ["not", "an", "object"])

        assert result is None
        assert error is not None
        assert "must be a JSON object" in error


# ── بقای هویت تا قدم چهارم ──────────────────────────────────────────────────

class TestIdentitySurvivesToStepFour:
    """هر تست‌کیسِ قدم اول باید در خروجیِ قدم چهارم حساب شود — نه ناپدید شود."""

    def _outcome(self):
        # _plan همان نیمه‌ی بدونِ LLMِ قدم چهارم است؛ عمومی نیست، ولی تنها
        # مسیری است که نتیجه‌ی قدم چهارم را بدونِ فراخوانیِ مدل می‌سازد.
        return _plan(
            _step1_result()["test_cases"],
            scenarios=[
                {
                    "id": "SC-001",
                    "title": "Voucher lifecycle",
                    "reason": "same resource",
                    "test_case_ids": ["TC-001", "TC-002"],
                }
            ],
            execution_order=[
                {"test_case_id": "TC-001", "order": 1, "depends_on": []},
                {"test_case_id": "TC-002", "order": 2, "depends_on": []},
                {"test_case_id": "TC-003", "order": 3, "depends_on": []},
            ],
            dependencies=[],
            clarifications=[],
            services=_services(),
            mappings=_step2_result()["mappings"],
        )

    def test_every_step_one_id_is_either_a_request_or_unresolved(self):
        outcome = self._outcome()

        accounted = {request["test_case_id"] for request in outcome.requests}
        accounted |= {item["test_case_id"] for item in outcome.unresolved}

        assert accounted == set(STEP1_IDS)

    def test_nothing_is_silently_dropped(self):
        outcome = self._outcome()

        assert len(outcome.requests) + len(outcome.unresolved) == len(STEP1_IDS)

    def test_the_unmapped_test_case_is_reported_rather_than_omitted(self):
        outcome = self._outcome()

        assert [item["test_case_id"] for item in outcome.unresolved] == ["TC-003"]

    def test_no_request_is_invented_for_the_unmapped_test_case(self):
        outcome = self._outcome()

        assert "TC-003" not in {request["test_case_id"] for request in outcome.requests}

    def test_the_requests_keep_the_step_one_order(self):
        outcome = self._outcome()

        assert [request["test_case_id"] for request in outcome.requests] == [
            "TC-001",
            "TC-002",
        ]

    def test_every_request_carries_the_step_one_title(self):
        outcome = self._outcome()
        titles = {case["id"]: case["title"] for case in _step1_result()["test_cases"]}

        assert all(
            request["title"] == titles[request["test_case_id"]]
            for request in outcome.requests
        )


class TestScenarioMembershipKeepsIndividualIds:
    def _payload(self) -> dict:
        return {
            "scenarios": [
                {
                    "id": "SC-001",
                    "title": "Voucher lifecycle",
                    "reason": "same resource",
                    "test_case_ids": ["TC-001", "TC-002"],
                }
            ],
            "execution_order": [
                {"test_case_id": "TC-001", "order": 1, "depends_on": []},
                {"test_case_id": "TC-002", "order": 2, "depends_on": []},
            ],
            "data_dependencies": [],
            "clarifications": [],
        }

    def test_a_scenario_keeps_the_individual_test_case_ids(self):
        scenarios, _, _, _ = validate_scenario_payload(
            self._payload(),
            test_cases=_step1_result()["test_cases"],
            step2_result=compact_step2_result(_step2_result()),
        )

        assert scenarios[0]["test_case_ids"] == ["TC-001", "TC-002"]

    def test_a_scenario_title_does_not_replace_the_test_case_ids(self):
        scenarios, _, _, _ = validate_scenario_payload(
            self._payload(),
            test_cases=_step1_result()["test_cases"],
            step2_result=compact_step2_result(_step2_result()),
        )

        # گروه‌بندی نباید تست‌کیس‌ها را در عنوانِ سناریو حل کند.
        assert all(case_id not in scenarios[0]["title"] for case_id in STEP1_IDS)

    def test_an_unknown_test_case_id_in_a_scenario_is_rejected(self):
        payload = self._payload()
        payload["scenarios"][0]["test_case_ids"] = ["TC-999"]

        with pytest.raises(ScenarioAnalysisError, match="TC-999"):
            validate_scenario_payload(
                payload,
                test_cases=_step1_result()["test_cases"],
                step2_result=compact_step2_result(_step2_result()),
            )

    def test_the_compact_contract_is_enough_for_validation(self):
        # همان قراردادی که ویزارد می‌فرستد باید برای اعتبارسنجیِ کامل کافی باشد.
        scenarios, steps, dependencies, clarifications = validate_scenario_payload(
            self._payload(),
            test_cases=_step1_result()["test_cases"],
            step2_result=compact_step2_result(_step2_result()),
        )

        assert scenarios
        assert steps
        assert dependencies == []
        assert clarifications == []


# ── انتخابِ منبع در زمانِ کشف ───────────────────────────────────────────────

class TestSnapshotSelectionDrivesDiscovery:
    """انتخابِ کاربر باید دقیقاً همان snapshotها را به کشف برساند."""

    def _discover(self, selection: str, tmp_path) -> dict:
        from src.agents.test_case_generator import api_mapping as api_mapping_module

        for name, operation_id in (
            ("admin.json", "getVoucherDetails"),
            ("customer.json", "getMyVouchers"),
        ):
            (tmp_path / name).write_text(
                json.dumps(_snapshot_spec(operation_id)), encoding="utf-8"
            )

        keys = expand_selection(selection)
        real_loader = api_mapping_module.load_snapshots

        def loader(requested, **kwargs):
            assert requested == keys
            return real_loader(requested, directory=tmp_path)

        with patch.object(api_mapping_module, "load_snapshots", side_effect=loader), patch(
            "src.agents.test_case_generator.api_mapping.LLMClient"
        ) as mock_llm, patch.object(
            api_mapping_module, "discover_services"
        ) as mock_remote:
            mock_llm.return_value.chat.return_value = json.dumps(
                {
                    "mappings": [
                        {
                            "test_case_id": case_id,
                            "api": None,
                            "confidence": "low",
                            "reason": "No operation matched this test case.",
                        }
                        for case_id in STEP1_IDS
                    ],
                    "clarifications": [],
                }
            )
            result = api_mapping_module.Step2ApiMappingGenerator().generate(
                _step1_result(), [], "user-1", snapshot_keys=keys
            )

        # مسیرِ شبکه‌ای هرگز نباید صدا زده شود.
        assert not mock_remote.called
        return result

    def test_admin_selection_discovers_only_admin(self, tmp_path):
        result = self._discover(SELECTION_ADMIN, tmp_path)

        assert [service["name"] for service in result["services"]] == ["Admin"]

    def test_customer_selection_discovers_only_customer(self, tmp_path):
        result = self._discover(SELECTION_CUSTOMER, tmp_path)

        assert [service["name"] for service in result["services"]] == ["Customer"]

    def test_both_selection_discovers_both_in_order(self, tmp_path):
        result = self._discover(SELECTION_BOTH, tmp_path)

        assert [service["name"] for service in result["services"]] == [
            "Admin",
            "Customer",
        ]

    def test_the_selection_expands_to_the_expected_keys(self):
        assert expand_selection(SELECTION_ADMIN) == ["admin"]
        assert expand_selection(SELECTION_CUSTOMER) == ["customer"]
        assert expand_selection(SELECTION_BOTH) == ["admin", "customer"]
