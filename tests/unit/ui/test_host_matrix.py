"""
ماتریسِ مسیرهای اجرا — یک قراردادِ میزبان برای همه.

    دستی + قدم‌به‌قدم · دستی + Run All · Jira + قدم‌به‌قدم · Jira + Run All
    · Batch (همان وضعیت و همان run_all، بدونِ UI) · agent.py

همه از ui/pipeline.py (execute_step / run_all) و مولدهای واقعیِ قدم ۱ تا ۵
می‌گذرند؛ فقط کلاینت‌های LLM، محتوای snapshot و importerِ Jira ساختگی‌اند.
برای ورودیِ هم‌ارز، تصمیم‌ها، انتخابِ میزبان و URLها باید یکی باشند.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agents.test_case_generator import (
    api_mapping,
    scenario_analysis,
    swagger_snapshots,
    task_analysis,
)
from src.agents.test_case_generator.agent import TestCaseGeneratorAgent as LegacyAgent
from src.agents.test_case_generator.api_discovery import DiscoveredApi, DiscoveredService
from src.agents.test_case_generator.swagger_analyzer import ApiSpec, EndpointSpec
from src.llm_client import LLMResponse
from src.task_sources.jira import JiraImport, JiraSettings
from src.task_sources.task_input import TaskComment, TaskInput
from tests.unit.task_sources.fake_jira import adfs_settings
from tests.unit.test_case_generator.test_source_selection import write_snapshots
from tests.unit.test_case_generator.test_source_semantics import FakeLLM, route_by_task
from ui import pipeline
from ui.pipeline import RUN_SUCCEEDED, execute_step, load_jira_task, run_all
from ui.workflow import STEP_COUNT, INPUT_TASK_DESCRIPTION, WorkflowState

API = "http://podium-back.devpod.ir/api"
CREATE = ("POST", "/admin/voucher")
DETAILS = ("GET", "/admin/voucher/{id}")
REPORT = ("GET", "/report/voucher/{id}")
VDI_SENTENCE = "The voucher create service must be tested in VDI."
MANUAL_TEXT = f"Implement voucher management for admins.\n{VDI_SENTENCE}"


def _jira_task() -> TaskInput:
    return TaskInput(
        title="Voucher management",
        description="Implement voucher management for admins.",
        comments=(TaskComment("Lead", "2026-01-01", VDI_SENTENCE, id="7001"),),
        source="jira",
        source_reference="DEMO-7",
        source_url="https://jira.test/browse/DEMO-7",
    )


def _case(case_id: str, operation: tuple[str, str], title: str) -> dict:
    return {
        "id": case_id,
        "title": title,
        "type": "positive",
        "priority": "high",
        "preconditions": [],
        "steps": [f"Send {operation[0]} {operation[1]}"],
        "expected_result": "Succeeds.",
        "related_service": {"method": operation[0], "path": operation[1]},
        "auth_intent": "inherit",
    }


class _TaskAnalysisLLM:
    """پاسخِ قدم ۱ — منبعِ شاهد همان جایی است که جمله در متنِ تسک آمده."""

    def __init__(self, **kwargs) -> None:
        pass

    def chat(self, *, user_message, system_prompt, temperature, max_tokens):
        from_jira = "## Comments (1, oldest first)" in user_message
        payload = {
            "task_summary": "Voucher management.",
            "identified_requirements": [],
            "test_cases": [
                _case("TC-001", CREATE, "Create a voucher"),
                _case("TC-002", DETAILS, "Read voucher details"),
                _case("TC-003", REPORT, "Read the voucher report"),
            ],
            "clarifications": [],
            "variables": [],
            "environment_decisions": [
                {
                    "scope": "service",
                    "service": {
                        "method": "POST",
                        "path": "/admin/voucher",
                        "name": "voucher create",
                    },
                    "environment": "vdi",
                    "evidence": VDI_SENTENCE,
                    "source_type": "comment" if from_jira else "description",
                    "source_reference": "Comment 1" if from_jira else "",
                }
            ],
        }
        return LLMResponse(json.dumps(payload), finish_reason="stop")


class _ScenarioLLM:
    messages: list[str] = []

    def __init__(self, **kwargs) -> None:
        pass

    def chat(self, *, user_message, system_prompt, temperature, max_tokens):
        _ScenarioLLM.messages.append(user_message)
        payload = {
            "scenarios": [
                {
                    "id": "SC-001",
                    "title": "Vouchers",
                    "test_case_ids": ["TC-001", "TC-002", "TC-003"],
                    "reason": "one flow",
                }
            ],
            "execution_order": [
                {"test_case_id": "TC-001", "order": 1, "depends_on": []},
                {"test_case_id": "TC-002", "order": 2, "depends_on": []},
                {"test_case_id": "TC-003", "order": 3, "depends_on": []},
            ],
            "data_dependencies": [],
            "clarifications": [],
        }
        return LLMResponse(json.dumps(payload), finish_reason="stop")


def _services() -> list[DiscoveredService]:
    def api(operation, operation_id):
        return DiscoveredApi(
            operation[0], operation[1], operation_id, responses={"200": {"fields": ["id"]}}
        )

    return [
        DiscoveredService(
            "Admin",
            f"{API}/v3/api-docs/Admin",
            API,
            apis=[api(CREATE, "create"), api(DETAILS, "details")],
        ),
        DiscoveredService("Report", f"{API}/v3/api-docs/Report", API, apis=[api(REPORT, "report")]),
    ]


def _map(self, test_cases, services, user_id):
    operations = {"TC-001": CREATE, "TC-002": DETAILS, "TC-003": REPORT}
    return (
        [
            {
                "test_case_id": case["id"],
                "api": {
                    "method": operations[case["id"]][0],
                    "path": operations[case["id"]][1],
                    "operation_id": "",
                },
                "confidence": "high",
                "reason": "matches",
                "clarification": "",
            }
            for case in test_cases
        ],
        [],
    )


@pytest.fixture(autouse=True)
def fake_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(swagger_snapshots, "SNAPSHOT_DIR", write_snapshots(tmp_path))
    FakeLLM(monkeypatch, router=route_by_task)  # انتخابِ معناییِ منابع: ووچر → Admin
    monkeypatch.setattr(task_analysis, "LLMClient", _TaskAnalysisLLM)
    monkeypatch.setattr(scenario_analysis, "LLMClient", _ScenarioLLM)
    monkeypatch.setattr(api_mapping, "load_snapshots", lambda keys: (_services(), []))
    monkeypatch.setattr(api_mapping.ApiMappingAgent, "map_test_cases", _map)
    monkeypatch.setattr(
        pipeline, "import_issue", lambda reference, *, settings, client: _jira_import()
    )
    monkeypatch.setattr(JiraSettings, "from_config", classmethod(lambda cls: adfs_settings()))
    monkeypatch.setattr(pipeline, "_jira_clients", {})
    _ScenarioLLM.messages = []


def _jira_import() -> JiraImport:
    return JiraImport(
        reference="DEMO-7",
        issue_key="DEMO-7",
        task_input=_jira_task(),
        auth_method="ADFS",
        identity="Test User",
        authenticated=True,
        issue_found=True,
        comments_retrieved=True,
    )


# ── ساختِ وضعیت و اجرای هر مسیر ─────────────────────────────────────────────


def _manual_state() -> WorkflowState:
    state = WorkflowState()
    state.set_input(INPUT_TASK_DESCRIPTION, MANUAL_TEXT)
    return state


def _jira_state() -> WorkflowState:
    state = WorkflowState()
    assert load_jira_task(state, "DEMO-7").ok
    return state


def _batch_state() -> WorkflowState:
    """Batch بدونِ UI: issueِ خوانده‌شده + همان متنِ ساخته‌شده؛ بعد همان run_all."""
    state = WorkflowState()
    state.task_import = _jira_import()
    state.set_input(INPUT_TASK_DESCRIPTION, state.task_import.task_input.to_task_description())
    return state


def _step_by_step(state: WorkflowState) -> WorkflowState:
    for step in range(1, STEP_COUNT + 1):
        outcome = execute_step(step, state)
        assert outcome.recorded and outcome.status in ("completed", "blocked"), (
            step,
            outcome.error,
        )
        assert state.approve_and_advance(step) or step == STEP_COUNT
    return state


def _run_all(state: WorkflowState) -> WorkflowState:
    outcome = run_all(state)
    assert outcome.status == RUN_SUCCEEDED, outcome.error
    return state


def _summary(state: WorkflowState) -> dict:
    """چیزی که باید بینِ مسیرها یکی باشد."""
    step1, step2, step4 = state.result(1), state.result(2), state.result(4)
    urls = {
        item["name"].split(" — ", 1)[0]: item["request"]["url"]["raw"]
        for folder in step4["collection"]["item"]
        for item in folder["item"]
    }
    return {
        "decisions": [
            (d["scope"], d["environment"], d["evidence"]["original"])
            for d in step1["environment_decisions"]
        ],
        "rejected": step1["rejected_environment_decisions"],
        "selections": step2["host_resolution"]["selections"],
        "issue_review": step2["host_resolution"]["issue_review"],
        "urls": urls,
        "variables": [v["key"] for v in step4["collection"]["variable"]],
    }


EXPECTED_URLS = {
    "TC-001": "{{base_url_vdi}}/api/admin/voucher",
    "TC-002": "{{base_url}}/api/admin/voucher/:id",
    "TC-003": "{{base_url}}/api/report/voucher/:id",
}

PATHS = {
    "manual-step-by-step": lambda: _step_by_step(_manual_state()),
    "manual-run-all": lambda: _run_all(_manual_state()),
    "jira-step-by-step": lambda: _step_by_step(_jira_state()),
    "jira-run-all": lambda: _run_all(_jira_state()),
    "batch-shared-pipeline": lambda: _run_all(_batch_state()),
}


@pytest.mark.parametrize("path", list(PATHS))
class TestEveryExecutionPath:
    def test_hosts_and_urls_follow_the_shared_contract(self, path):
        state = PATHS[path]()
        summary = _summary(state)
        assert summary["urls"] == EXPECTED_URLS
        assert summary["variables"] == ["token"]
        assert [s["variable"] for s in summary["selections"]] == [
            "base_url_vdi",
            "base_url",
            "base_url",
        ]
        assert [s["basis"] for s in summary["selections"]] == ["service", "default", "default"]
        assert summary["rejected"] == []

    def test_no_old_host_behaviour_survives(self, path):
        collection = PATHS[path]().result(4)["collection"]
        dumped = json.dumps(collection)
        assert "baseUrl" not in dumped
        assert "devpod" not in dumped
        assert "podium-back" not in dumped

    def test_the_selection_is_persisted_and_reused_by_steps_4_and_5(self, path):
        state = PATHS[path]()
        selections = {
            s["test_case_id"]: s for s in state.result(2)["host_resolution"]["selections"]
        }
        rows = {row["test_case_id"]: row for row in state.result(4)["requests"]}
        for case_id, selection in selections.items():
            assert rows[case_id]["host_variable"] == selection["variable"]
        assert state.result(5)["review"]["issues"] == []

    def test_step3_never_sees_the_environment_fields(self, path):
        PATHS[path]()
        assert _ScenarioLLM.messages
        assert all("environment_decisions" not in message for message in _ScenarioLLM.messages)


class TestEquivalence:
    def test_run_all_and_step_by_step_are_equivalent_for_manual_input(self):
        assert _summary(PATHS["manual-run-all"]()) == _summary(PATHS["manual-step-by-step"]())

    def test_run_all_and_step_by_step_are_equivalent_for_jira_input(self):
        assert _summary(PATHS["jira-run-all"]()) == _summary(PATHS["jira-step-by-step"]())

    def test_batch_equals_single_jira_issue(self):
        batch, single = PATHS["batch-shared-pipeline"](), PATHS["jira-run-all"]()
        assert _summary(batch) == _summary(single)
        assert batch.result(1)["environment_decisions"] == single.result(1)["environment_decisions"]

    def test_manual_and_jira_origins_follow_the_same_rules(self):
        manual, jira = PATHS["manual-run-all"](), PATHS["jira-run-all"]()
        assert _summary(manual) == _summary(jira)
        # فقط منشأ فرق دارد: متنِ دستی ↔ کامنتِ Jira با شناسه‌ی واقعی‌اش
        assert manual.result(1)["environment_decisions"][0]["source"]["type"] == "description"
        jira_source = jira.result(1)["environment_decisions"][0]["source"]
        assert (jira_source["type"], jira_source["reference"], jira_source["label"]) == (
            "comment",
            "7001",
            "Comment 1",
        )

    def test_rerunning_step1_invalidates_the_saved_selection(self):
        state = PATHS["manual-step-by-step"]()
        state.set_input(INPUT_TASK_DESCRIPTION, "Implement voucher management for admins.")
        assert state.is_out_of_date(2)
        execute_step(1, state)
        # نتیجه‌ی قدیمیِ قدم ۲ (و انتخابِ میزبانش) دیگر به قدم ۴ داده نمی‌شود.
        assert "step2_result" not in state.step_inputs(4)
        assert execute_step(4, state).recorded is False


class TestLegacyGenerator:
    def test_agent_py_produces_the_same_url_as_the_wizard_for_the_default_host(self, monkeypatch):
        spec = ApiSpec(
            title="Admin",
            version="1",
            base_url=API,
            endpoints=[EndpointSpec("GET", "/admin/voucher/{id}")],
        )
        case = {
            "name": "GET voucher",
            "type": "positive",
            "method": "GET",
            "path": "/admin/voucher/{id}",
            "description": "",
            "headers": {},
            "query_params": {},
            "path_params": {"id": ""},
            "body": None,
            "expected_status": 200,
            "assertions": [],
            "save_variables": [],
            "assumptions": [],
        }
        captured: list[dict] = []
        agent = LegacyAgent(
            analyzer=SimpleNamespace(analyze=lambda url: spec),
            testcase_generator=SimpleNamespace(
                generate=lambda spec, user_id: {
                    "controllers": [{"name": "Voucher", "test_cases": [case]}]
                }
            ),
        )
        monkeypatch.setattr(
            agent, "_save_collection", lambda collection, title: captured.append(collection) or "x"
        )

        agent._run_swagger_mode(f"{API}/v3/api-docs/Admin", {"user_id": "u"})

        (collection,) = captured
        assert collection["item"][0]["item"][0]["request"]["url"]["raw"] == EXPECTED_URLS["TC-002"]
        assert "baseUrl" not in json.dumps(collection)
