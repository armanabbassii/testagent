"""
تست‌های یکپارچه‌ی انتخابِ میزبان — از تحلیلِ تسک تا بازبینیِ نهایی.

مولدهای واقعیِ قدم ۱، ۲، ۴ و ۵ اجرا می‌شوند؛ فقط LLM، بارگذاریِ snapshot و
عاملِ نگاشت ساختگی‌اند. همچنین مسیرِ قدیمیِ agent.py باید همان قراردادِ URL را
بدهد.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.agents.test_case_generator.agent import TestCaseGeneratorAgent as LegacyAgent
from src.agents.test_case_generator.api_discovery import DiscoveredApi, DiscoveredService
from src.agents.test_case_generator.api_mapping import Step2ApiMappingGenerator
from src.agents.test_case_generator.final_review import (
    STATUS_BLOCKED,
    STATUS_NEEDS_REVIEW,
    STATUS_READY,
    FinalReviewGenerator,
)
from src.agents.test_case_generator.postman_builder import PostmanBuilder
from src.agents.test_case_generator.postman_generation import Step4PostmanGenerator
from src.agents.test_case_generator.scenario_parser import OperationRef, ScenarioStep
from src.agents.test_case_generator.swagger_analyzer import ApiSpec, EndpointSpec
from src.agents.test_case_generator.task_analysis import TaskAnalysisGenerator
from src.agents.test_case_generator.testcase_generator import (
    ResolvedStep,
    _build_scenario_baseline,
)
from src.llm_client import LLMResponse
from src.task_sources.sources import sources_from_task_input
from src.task_sources.task_input import TaskComment, TaskInput

POSSIBILITY = "/admin/serviceSet/{id}/requestPossibility"
SERVICE_SET = "/admin/serviceSet/{id}"
REPORT = "/report/voucher/{id}"
VDI_SENTENCE = "The request possibility service must be tested in VDI."
NORMAL_SENTENCE = "Run the request possibility service in the normal environment."


def _task(*comments: str) -> TaskInput:
    return TaskInput(
        title="Service set request possibility",
        description=(
            "Implement POST /api/admin/serviceSet/{id}/requestPossibility and check the "
            "voucher report."
        ),
        comments=tuple(
            TaskComment("Lead", f"2026-01-0{n}", body, id=f"9000{n}")
            for n, body in enumerate(comments, start=1)
        ),
        source="jira",
        source_reference="PODIUM-1",
    )


def _case(case_id: str, method: str, path: str, title: str) -> dict:
    return {
        "id": case_id,
        "title": title,
        "type": "positive",
        "priority": "high",
        "preconditions": [],
        "steps": [f"Send {method} {path}"],
        "expected_result": "Succeeds.",
        "related_service": {"method": method, "path": path},
        "auth_intent": "inherit",
    }


def _vdi(reference: str = "Comment 1", sentence: str = VDI_SENTENCE, env: str = "vdi") -> dict:
    return {
        "scope": "service",
        "service": {"method": "POST", "path": POSSIBILITY, "name": "request possibility"},
        "environment": env,
        "evidence": sentence,
        "source_type": "comment",
        "source_reference": reference,
    }


def _step1(task: TaskInput, decisions: list[dict]) -> dict:
    payload = {
        "task_summary": "Service set request possibility.",
        "identified_requirements": [],
        "test_cases": [
            _case("TC-001", "POST", POSSIBILITY, "Request possibility"),
            _case("TC-002", "GET", SERVICE_SET, "Read the service set"),
            _case("TC-003", "GET", REPORT, "Read the voucher report"),
        ],
        "clarifications": [],
        "variables": [],
        "environment_decisions": decisions,
    }
    response = LLMResponse(json.dumps(payload), finish_reason="stop")
    with patch("src.agents.test_case_generator.task_analysis.LLMClient") as client:
        client.return_value.chat.return_value = response
        return TaskAnalysisGenerator().generate(
            task.to_task_description(), "u", sources=sources_from_task_input(task)
        )


def _services() -> list[DiscoveredService]:
    api = "http://podium-back.devpod.ir/api"
    return [
        DiscoveredService(
            name="Admin",
            source_url=f"{api}/v3/api-docs/Admin",
            base_url=api,
            apis=[
                DiscoveredApi(
                    "POST", POSSIBILITY, "requestPossibility", responses={"200": {"fields": ["id"]}}
                ),
                DiscoveredApi(
                    "GET", SERVICE_SET, "getServiceSet", responses={"200": {"fields": ["id"]}}
                ),
            ],
        ),
        DiscoveredService(
            name="Report",
            source_url=f"{api}/v3/api-docs/Report",
            base_url=api,
            apis=[
                DiscoveredApi("GET", REPORT, "voucherReport", responses={"200": {"fields": ["id"]}})
            ],
        ),
    ]


def _step2(step1: dict) -> dict:
    mappings = [
        {
            "test_case_id": case_id,
            "api": {"method": method, "path": path, "operation_id": ""},
            "confidence": "high",
            "reason": "matches",
            "clarification": "",
        }
        for case_id, method, path in (
            ("TC-001", "POST", POSSIBILITY),
            ("TC-002", "GET", SERVICE_SET),
            ("TC-003", "GET", REPORT),
        )
    ]
    with (
        patch(
            "src.agents.test_case_generator.api_mapping.load_snapshots",
            return_value=(_services(), []),
        ),
        patch(
            "src.agents.test_case_generator.api_mapping.ApiMappingAgent.map_test_cases",
            return_value=(mappings, []),
        ),
    ):
        return Step2ApiMappingGenerator().generate(step1, [], "u", snapshot_keys=["admin"])


STEP3 = {
    "scenarios": [
        {
            "id": "SC-001",
            "title": "Flow",
            "test_case_ids": ["TC-001", "TC-002", "TC-003"],
            "reason": "one flow",
        }
    ],
    "execution_order": [
        {"test_case_id": "TC-001", "order": 1, "depends_on": []},
        {"test_case_id": "TC-002", "order": 2, "depends_on": []},
        {"test_case_id": "TC-003", "order": 3, "depends_on": ["TC-001"]},
    ],
    "data_dependencies": [
        {
            "variable_name": "possibilityId",
            "source": {"test_case_id": "TC-001", "location": "response.body", "path": "$.id"},
            "targets": [{"test_case_id": "TC-003", "location": "query", "parameter": "ref"}],
        }
    ],
    "clarifications": [],
}


def _pipeline(*comments: str, decisions: list[dict]) -> dict:
    task = _task(*comments)
    step1 = _step1(task, decisions)
    step2 = _step2(step1)
    step4 = Step4PostmanGenerator().generate(step1, step2, STEP3)
    step5 = FinalReviewGenerator().generate(step1, step2, STEP3, step4)
    return {"step1": step1, "step2": step2, "step4": step4, "step5": step5}


def _url(step4: dict, case_id: str) -> dict:
    for folder in step4["collection"]["item"]:
        for item in folder["item"]:
            if item["name"].startswith(case_id):
                return item["request"]["url"]
    raise AssertionError(f"no request for {case_id}")


@pytest.fixture(scope="module")
def run():
    return _pipeline(VDI_SENTENCE, decisions=[_vdi()])


class TestPropagationThroughAllSteps:
    def test_step1_keeps_the_decision_with_its_jira_comment_provenance(self, run):
        (decision,) = run["step1"]["environment_decisions"]
        assert decision["source"] == {
            "type": "comment",
            "reference": "90001",
            "label": "Comment 1",
            "sha256": sources_from_task_input(_task(VDI_SENTENCE))[1].sha256,
        }

    def test_step2_persists_one_selection_per_case(self, run):
        selections = {s["test_case_id"]: s for s in run["step2"]["host_resolution"]["selections"]}
        assert selections["TC-001"]["variable"] == "base_url_vdi"
        assert selections["TC-002"]["variable"] == "base_url"
        assert selections["TC-003"]["basis"] == "default"

    def test_step4_urls_use_the_selected_host_and_the_api_base_path(self, run):
        assert (
            _url(run["step4"], "TC-001")["raw"]
            == "{{base_url_vdi}}/api/admin/serviceSet/:id/requestPossibility"
        )
        assert _url(run["step4"], "TC-002")["raw"] == "{{base_url}}/api/admin/serviceSet/:id"

    def test_path_variables_and_query_parameters_are_preserved(self, run):
        url = _url(run["step4"], "TC-003")
        assert url["raw"] == "{{base_url}}/api/report/voucher/:id?ref={{possibilityId}}"
        assert url["path"] == ["api", "report", "voucher", ":id"]
        assert url["query"] == [{"key": "ref", "value": "{{possibilityId}}"}]
        assert url["variable"] == [{"key": "id", "value": ""}]

    def test_no_swagger_host_is_written_into_the_collection(self, run):
        collection = run["step4"]["collection"]
        assert [v["key"] for v in collection["variable"]] == ["token"]
        assert "podium-back" not in json.dumps(collection)
        assert "devpod" not in json.dumps(collection)

    def test_step4_rows_record_the_host_and_base_path(self, run):
        rows = {row["test_case_id"]: row for row in run["step4"]["requests"]}
        assert rows["TC-001"]["host_variable"] == "base_url_vdi"
        assert rows["TC-001"]["base_path"] == "/api"

    def test_step5_matches_paths_despite_the_api_prefix(self, run):
        assert run["step5"]["review"]["issues"] == []

    def test_step5_blocks_a_request_whose_host_was_changed(self, run):
        step4 = json.loads(json.dumps(run["step4"]))
        url = _url(step4, "TC-001")
        url["host"] = ["{{base_url}}"]
        url["raw"] = url["raw"].replace("{{base_url_vdi}}", "{{base_url}}")
        review = FinalReviewGenerator().generate(run["step1"], run["step2"], STEP3, step4)["review"]
        assert review["status"] == STATUS_BLOCKED
        assert [issue["code"] for issue in review["issues"]] == ["host_mismatch"]


class TestPartialProcessing:
    def test_a_conflicting_case_gets_no_request_and_the_others_still_build(self):
        run = _pipeline(
            VDI_SENTENCE,
            NORMAL_SENTENCE,
            decisions=[_vdi(), _vdi("Comment 2", NORMAL_SENTENCE, "normal")],
        )
        names = [
            item["name"] for folder in run["step4"]["collection"]["item"] for item in folder["item"]
        ]
        assert not any(name.startswith("TC-001") for name in names)
        assert len(names) == 2
        (row,) = run["step4"]["unresolved"]
        assert row["test_case_id"] == "TC-001"
        assert row["host_status"] == "CONFLICT"
        assert row["reason"].startswith("Environment conflict")
        review = run["step5"]["review"]
        assert review["issues"] == []
        assert any(item["test_case_id"] == "TC-001" for item in review["unresolved_clarifications"])

    def test_step5_blocks_a_request_built_for_a_case_whose_host_is_unresolved(self, run):
        """پشتیبانِ قدمِ ۴: اگر درخواستی برای تست‌کیسِ CONFLICT در کالکشن باشد، قدمِ ۵
        blocked می‌دهد — و Batch برای blocked هرگز Newman را اجرا نمی‌کند."""
        conflict = _pipeline(
            VDI_SENTENCE,
            NORMAL_SENTENCE,
            decisions=[_vdi(), _vdi("Comment 2", NORMAL_SENTENCE, "normal")],
        )
        step4 = json.loads(json.dumps(conflict["step4"]))
        smuggled = next(
            item
            for folder in run["step4"]["collection"]["item"]
            for item in folder["item"]
            if item["name"].startswith("TC-001")
        )
        step4["collection"]["item"][0]["item"].append(json.loads(json.dumps(smuggled)))
        # بدونِ ردیفِ قدمِ ۴ هم blocked است (extra_request)
        review = FinalReviewGenerator().generate(
            conflict["step1"], conflict["step2"], STEP3, step4
        )["review"]
        assert review["status"] == STATUS_BLOCKED

        # با ردیفِ قدمِ ۴، خودِ قاعده‌ی میزبان آن را می‌گیرد
        step4["requests"].append(
            next(row for row in run["step4"]["requests"] if row["test_case_id"] == "TC-001")
        )
        review = FinalReviewGenerator().generate(
            conflict["step1"], conflict["step2"], STEP3, step4
        )["review"]
        assert review["status"] == STATUS_BLOCKED
        assert "unresolved_host_request" in [issue["code"] for issue in review["issues"]]

    def test_a_rejected_statement_puts_its_case_on_hold(self):
        run = _pipeline(
            VDI_SENTENCE, decisions=[_vdi(sentence="The possibility service runs on VDI.")]
        )
        (row,) = run["step4"]["unresolved"]
        assert row["test_case_id"] == "TC-001"
        assert row["host_status"] == "NEEDS_REVIEW"
        assert run["step2"]["host_resolution"]["issue_review"]["status"] == "NEEDS_REVIEW"

    def test_an_unmatched_statement_needs_review_without_blocking_any_case(self):
        unmatched = _vdi() | {"service": {"method": "GET", "path": "/billing/x", "name": ""}}
        run = _pipeline(VDI_SENTENCE, decisions=[unmatched])
        assert run["step4"]["unresolved"] == []
        assert _url(run["step4"], "TC-001")["host"] == ["{{base_url}}"]
        kinds = [item["kind"] for item in run["step5"]["review"]["unresolved_clarifications"]]
        assert "environment_review" in kinds


class TestInformationalWarnings:
    """یک ذکرِ اطلاعاتیِ VDI نه بازبینیِ issue می‌سازد و نه چیزی را متوقف می‌کند."""

    def test_a_standalone_vdi_mention_is_information_not_review(self):
        run = _pipeline("Some admins open the panel from a VDI session.", decisions=[])

        assert any("mentions VDI" in w for w in run["step1"]["environment_warnings"])
        assert run["step2"]["host_resolution"]["issue_review"]["status"] == "NONE"
        assert run["step4"]["unresolved"] == []
        review = run["step5"]["review"]
        assert review["status"] == STATUS_READY
        assert any("mentions VDI" in note for note in review["information"])
        assert not any("VDI" in warning for warning in review["warnings"])

    def test_a_normal_non_vdi_decision_produces_no_warning_at_all(self):
        sentence = "All APIs should use the normal environment (non-VDI)."
        decision = {
            "scope": "task",
            "environment": "normal",
            "evidence": sentence,
            "source_type": "comment",
            "source_reference": "Comment 1",
        }
        run = _pipeline(sentence, decisions=[decision])

        assert run["step1"]["environment_warnings"] == []
        assert run["step5"]["review"]["information"] == []
        assert {s["variable"] for s in run["step2"]["host_resolution"]["selections"]} == {
            "base_url"
        }

    def test_a_conflict_still_needs_review_and_spares_unrelated_cases(self):
        run = _pipeline(
            VDI_SENTENCE,
            NORMAL_SENTENCE,
            decisions=[_vdi(), _vdi("Comment 2", NORMAL_SENTENCE, "normal")],
        )
        assert run["step5"]["review"]["status"] == STATUS_NEEDS_REVIEW
        assert run["step2"]["host_resolution"]["issue_review"]["status"] == "NEEDS_REVIEW"
        assert _url(run["step4"], "TC-002")["host"] == ["{{base_url}}"]
        assert _url(run["step4"], "TC-003")["host"] == ["{{base_url}}"]

    def test_a_rejected_decision_still_needs_review_and_spares_unrelated_cases(self):
        run = _pipeline(
            VDI_SENTENCE, decisions=[_vdi(sentence="The possibility service runs on VDI.")]
        )
        assert run["step5"]["review"]["status"] == STATUS_NEEDS_REVIEW
        assert [row["host_status"] for row in run["step4"]["unresolved"]] == ["NEEDS_REVIEW"]
        assert _url(run["step4"], "TC-002")["host"] == ["{{base_url}}"]


class TestSameInputSameResult:
    def test_identical_task_content_gives_identical_decisions_and_hosts(self):
        first = _pipeline(VDI_SENTENCE, decisions=[_vdi()])
        second = _pipeline(VDI_SENTENCE, decisions=[_vdi()])
        for key in ("environment_decisions", "rejected_environment_decisions"):
            assert first["step1"][key] == second["step1"][key]
        assert first["step2"]["host_resolution"] == second["step2"]["host_resolution"]


class TestLegacyAgent:
    """agent.py توصیفِ تسک ندارد → میزبانِ پیش‌فرض، همان قراردادِ URL، بدونِ میزبانِ Swagger."""

    @staticmethod
    def _spec() -> ApiSpec:
        return ApiSpec(
            title="Admin",
            version="1",
            base_url="http://podium-back.devpod.ir/api",
            endpoints=[
                EndpointSpec(method="GET", path="/admin/voucher/{id}", operation_id="getVoucher")
            ],
        )

    def test_swagger_mode_uses_base_url_and_the_server_base_path(self, monkeypatch):
        spec = self._spec()
        suite = {
            "controllers": [
                {
                    "name": "Voucher",
                    "test_cases": [
                        {
                            "name": "GET voucher",
                            "type": "positive",
                            "method": "GET",
                            "path": "/admin/voucher/{id}",
                            "description": "",
                            "headers": {},
                            "query_params": {},
                            "path_params": {"id": "1"},
                            "body": None,
                            "expected_status": 200,
                            "assertions": [],
                            "save_variables": [],
                            "assumptions": [],
                        }
                    ],
                }
            ]
        }
        captured: list[dict] = []
        agent = LegacyAgent(
            analyzer=SimpleNamespace(analyze=lambda url: spec),
            testcase_generator=SimpleNamespace(generate=lambda spec, user_id: suite),
        )
        monkeypatch.setattr(
            agent,
            "_save_collection",
            lambda collection, title: captured.append(collection) or "out.json",
        )

        agent._run_swagger_mode("http://podium-back.devpod.ir/v3/api-docs/Admin", {"user_id": "u"})

        (collection,) = captured
        url = collection["item"][0]["item"][0]["request"]["url"]
        assert url["raw"] == "{{base_url}}/api/admin/voucher/:id"
        assert [v["key"] for v in collection["variable"]] == ["token"]
        assert "podium-back" not in json.dumps(collection)

    def test_scenario_steps_carry_the_default_host_and_their_base_path(self):
        spec = self._spec()
        step = SimpleNamespace(
            name="read",
            swagger="admin",
            description="",
            operation=SimpleNamespace(method="GET", path="/admin/voucher/{id}", operation_id=""),
        )
        agent = LegacyAgent()
        resolved, errors = agent._resolve_steps(SimpleNamespace(steps=[step]), {"admin": spec})
        assert errors == []
        assert (resolved[0].base_url_var, resolved[0].base_path) == ("base_url", "/api")

    def test_scenario_baseline_builds_the_shared_url_contract(self):
        endpoint = EndpointSpec(method="GET", path="/admin/voucher/{id}", operation_id="getVoucher")
        step = ScenarioStep(
            name="read", swagger="admin", operation=OperationRef("GET", "/admin/voucher/{id}")
        )
        resolved = ResolvedStep(
            step=step,
            endpoint=endpoint,
            source_name="admin",
            base_url="http://podium-back.devpod.ir/api",
            base_url_var="base_url",
            base_path="/api",
        )
        suite = _build_scenario_baseline([resolved])
        collection = PostmanBuilder().build(suite=suite, collection_name="scenario")
        raws = [
            item["request"]["url"]["raw"]
            for folder in collection["item"]
            for item in folder["item"]
        ]
        assert raws and all(raw.startswith("{{base_url}}/api/admin/voucher/") for raw in raws)
