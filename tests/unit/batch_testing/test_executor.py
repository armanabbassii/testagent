"""
تست‌های اجراگرِ یک issue (Milestone 4.4).

خطِ لوله‌ی واقعی (load_jira_task، run_all و مولدهای واقعیِ قدم ۱ تا ۵) اجرا می‌شود؛
فقط کلاینت‌های LLM، محتوای snapshot، عاملِ نگاشت، importerِ Jira و (در بیشترِ تست‌ها)
موتورِ Newman ساختگی‌اند. یک تست Newmanِ واقعی را روی سرورِ HTTPِ محلی اجرا می‌کند.
هیچ Jira، LLM یا API واقعی‌ای صدا زده نمی‌شود.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from src.agents.test_case_generator import (
    api_mapping,
    scenario_analysis,
    swagger_snapshots,
    task_analysis,
)
from src.agents.test_case_generator.api_discovery import DiscoveredApi, DiscoveredService
from src.auth import AccessToken
from src.batch_testing import executor as executor_module
from src.batch_testing.executor import (
    collection_semantics,
    execution_secrets,
    jira_candidate_secrets,
    ArtifactWriter,
    ExecutionRequest,
    IssueExecutor,
    assess_runnability,
    safe_path,
    safe_url,
    sanitize_step_result,
)
from src.llm_client import LLMResponse
from src.task_sources.jira import JiraClient, JiraImport, JiraSettings
from src.task_sources.jira import import_issue as real_import_issue
from src.task_sources.jira.importer import STAGE_ISSUE
from src.task_sources.task_input import TaskComment, TaskInput
from src.test_execution.newman.locator import locate
from src.test_execution.newman.redaction import SanitizationError
from src.test_execution.newman.models import (
    Artifacts,
    NewmanRunResult,
    RunStats,
)
from tests.unit.task_sources.fake_jira import FakeJira, adfs_settings, comment, issue_body
from tests.unit.test_case_generator.test_source_selection import write_snapshots
from tests.unit.test_case_generator.test_source_semantics import FakeLLM, route_by_task
from ui import pipeline

API = "http://podium-back.devpod.ir/api"
CREATE = ("POST", "/admin/voucher")
DETAILS = ("GET", "/admin/voucher/{id}")
VDI_SENTENCE = "The voucher create service must be tested in VDI."
NORMAL_SENTENCE = "The voucher create service must be tested in the normal environment."
JIRA_SECRET_TEXT = "INTERNAL-DESC-MARKER-77fa"  # only in the Jira description — never in artifacts
ENV_TOKEN = "EXEC-SECRET-ENV-TOKEN-5c1d"
ACCESS_TOKEN = AccessToken("SYNTHETIC-ACCESS-TOKEN-3f9a")  # ساختگی — هرگز در artifact


@dataclass
class World:
    """Everything the fake LLM, mapping agent and Jira importer answer."""

    description: str = f"Implement voucher management for admins. {JIRA_SECRET_TEXT}"
    comments: tuple[str, ...] = (VDI_SENTENCE,)
    decisions: list[dict] = field(
        default_factory=lambda: [
            {
                "scope": "service",
                "service": {"method": "POST", "path": "/admin/voucher", "name": "voucher create"},
                "environment": "vdi",
                "evidence": VDI_SENTENCE,
                "source_type": "comment",
                "source_reference": "Comment 1",
            }
        ]
    )
    mapping: dict[str, tuple[str, str] | None] = field(
        default_factory=lambda: {"TC-001": CREATE, "TC-002": DETAILS}
    )
    task_summary: str = "Voucher management."
    case_text: str = ""  # echoed into titles, steps, expected results and clarifications
    create_body: dict | None = None  # Swagger request-body example of POST /admin/voucher
    base_url: str = API  # Swagger catalog URLs of the Admin service
    source_url: str = f"{API}/v3/api-docs/Admin"
    jira_fails: bool = False
    scenario_error: BaseException | None = None
    prompts: list[str] = field(default_factory=list)
    scenario_calls: int = 0
    analysis_calls: int = 0


WORLD = World()


def _case(case_id: str, operation: tuple[str, str]) -> dict:
    return {
        "id": case_id,
        "title": f"Case {case_id} {WORLD.case_text}".strip(),
        "type": "positive",
        "priority": "high",
        "preconditions": [WORLD.case_text] if WORLD.case_text else [],
        "steps": [f"Send {operation[0]} {operation[1]} {WORLD.case_text}".strip()],
        "expected_result": f"Succeeds. {WORLD.case_text}".strip(),
        "related_service": {"method": operation[0], "path": operation[1]},
        "auth_intent": "inherit",
    }


class _TaskAnalysisLLM:
    def __init__(self, **kwargs) -> None:
        pass

    def chat(self, *, user_message, system_prompt, temperature, max_tokens):
        WORLD.analysis_calls += 1
        WORLD.prompts.append(user_message)
        payload = {
            "task_summary": WORLD.task_summary,
            "identified_requirements": [WORLD.case_text] if WORLD.case_text else [],
            "test_cases": [_case("TC-001", CREATE), _case("TC-002", DETAILS)],
            "clarifications": [WORLD.case_text] if WORLD.case_text else [],
            "variables": [],
            "environment_decisions": WORLD.decisions,
        }
        return LLMResponse(json.dumps(payload), finish_reason="stop")


class _ScenarioLLM:
    def __init__(self, **kwargs) -> None:
        pass

    def chat(self, *, user_message, system_prompt, temperature, max_tokens):
        WORLD.scenario_calls += 1
        if WORLD.scenario_error is not None:
            raise WORLD.scenario_error
        payload = {
            "scenarios": [
                {
                    "id": "SC-001",
                    "title": "Vouchers",
                    "test_case_ids": ["TC-001", "TC-002"],
                    "reason": "flow",
                }
            ],
            "execution_order": [
                {"test_case_id": "TC-001", "order": 1, "depends_on": []},
                {"test_case_id": "TC-002", "order": 2, "depends_on": []},
            ],
            "data_dependencies": [],
            "clarifications": [],
        }
        return LLMResponse(json.dumps(payload), finish_reason="stop")


def _services():
    def api(operation, operation_id):
        body = WORLD.create_body if operation == CREATE else None
        return DiscoveredApi(
            operation[0],
            operation[1],
            operation_id,
            request_body=body,
            responses={"200": {"fields": ["id"]}},
        )

    return [
        DiscoveredService(
            "Admin",
            WORLD.source_url,
            WORLD.base_url,
            apis=[api(CREATE, "c"), api(DETAILS, "d")],
        )
    ]


def _map(self, test_cases, services, user_id):
    mappings = []
    for case in test_cases:
        operation = WORLD.mapping.get(case["id"])
        mappings.append(
            {
                "test_case_id": case["id"],
                "api": {"method": operation[0], "path": operation[1], "operation_id": ""}
                if operation
                else None,
                "confidence": "high" if operation else "low",
                "reason": "matches" if operation else "no API",
                "clarification": "" if operation else "Which API?",
            }
        )
    return mappings, []


def _task_input() -> TaskInput:
    return TaskInput(
        title="Voucher management",
        description=WORLD.description,
        comments=tuple(
            TaskComment("Lead", "2026-01-01", body, id=f"700{n}")
            for n, body in enumerate(WORLD.comments, 1)
        ),
        source="jira",
        source_reference="DEMO-7",
    )


def _import(reference, *, settings, client):
    if WORLD.jira_fails:
        return JiraImport(reference=reference).fail(STAGE_ISSUE, RuntimeError("HTTP 403"))
    return JiraImport(
        reference=reference,
        issue_key="DEMO-7",
        task_input=_task_input(),
        auth_method="ADFS",
        identity="Test User",
        authenticated=True,
        issue_found=True,
        comments_retrieved=True,
    )


@pytest.fixture(autouse=True)
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    global WORLD
    WORLD = World()
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    monkeypatch.setattr(swagger_snapshots, "SNAPSHOT_DIR", write_snapshots(snapshots))
    FakeLLM(monkeypatch, router=route_by_task)  # source selection: voucher → Admin
    monkeypatch.setattr(task_analysis, "LLMClient", _TaskAnalysisLLM)
    monkeypatch.setattr(scenario_analysis, "LLMClient", _ScenarioLLM)
    monkeypatch.setattr(api_mapping, "load_snapshots", lambda keys: (_services(), []))
    monkeypatch.setattr(api_mapping.ApiMappingAgent, "map_test_cases", _map)
    monkeypatch.setattr(pipeline, "import_issue", _import)
    monkeypatch.setattr(JiraSettings, "from_config", classmethod(lambda cls: adfs_settings()))
    monkeypatch.setattr(pipeline, "_jira_clients", {})
    return WORLD


@dataclass
class FakeEngine:
    status: str = "PASS"
    error_kind: str = ""
    stats: RunStats | None = None
    calls: list[dict] = field(default_factory=list)

    def run(self, request, cancel_event=None):
        self.calls.append({"request": request, "cancel_event": cancel_event})
        run_dir = Path(request.output_dir) / "20261031T100000Z_fake0001"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text("{}", encoding="utf-8")
        return NewmanRunResult(
            run_id="20261031T100000Z_fake0001",
            status=self.status,
            error_kind=self.error_kind,
            error_message="Newman reported a runtime error (Error)."
            if self.status == "ERROR"
            else "",
            stats=self.stats
            or RunStats(
                requests_executed=2,
                assertions_total=2,
                assertions_failed=1 if self.status == "FAIL" else 0,
            ),
            artifacts=Artifacts(run_dir=str(run_dir), result_json=str(run_dir / "result.json")),
        )


def _env_files(tmp_path: Path, base_url: str = "http://podium.test") -> tuple[Path, Path]:
    environment = tmp_path / "dev.json"
    globals_path = tmp_path / "globals.json"
    environment.write_text(
        json.dumps(
            {
                "name": "dev",
                "_postman_variable_scope": "environment",
                "values": [
                    {"key": "base_url", "value": base_url, "enabled": True},
                    {"key": "base_url_vdi", "value": base_url, "enabled": True},
                    {"key": "token", "value": ENV_TOKEN, "enabled": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    globals_path.write_text(
        json.dumps({"name": "Globals", "_postman_variable_scope": "globals", "values": []}),
        encoding="utf-8",
    )
    return environment, globals_path


def _request(
    tmp_path: Path,
    *,
    attempt: int = 1,
    issue_key: str = "DEMO-7",
    cancel=None,
    base_url=None,
    access_token: AccessToken | None = ACCESS_TOKEN,
):
    environment, globals_path = _env_files(tmp_path, base_url or "http://podium.test")
    return ExecutionRequest(
        batch_id="batch01",
        issue_key=issue_key,
        execution_id=f"exec-{attempt}",
        attempt=attempt,
        artifact_root=tmp_path / "artifacts",
        environment_path=environment,
        globals_path=globals_path,
        cancel_event=cancel,
        access_token=access_token,
    )


def _execute(tmp_path, engine=None, **kwargs):
    engine = engine or FakeEngine()
    outcome = IssueExecutor(engine=engine).execute(_request(tmp_path, **kwargs))
    return outcome, engine


def _artifact_texts(outcome) -> dict[str, str]:
    root = Path(outcome.artifact_dir)
    return {
        str(p.relative_to(root)): p.read_text(encoding="utf-8")
        for p in root.rglob("*")
        if p.is_file()
    }


# ── Jira ─────────────────────────────────────────────────────────────────────


class TestJira:
    def test_a_successful_issue_runs_the_pipeline_and_newman(self, tmp_path):
        outcome, engine = _execute(tmp_path)
        assert outcome.status == "PASS"
        assert WORLD.analysis_calls == 1 and WORLD.scenario_calls == 1
        assert len(engine.calls) == 1

    def test_a_jira_failure_stops_before_step_1(self, tmp_path, world):
        world.jira_fails = True
        outcome, engine = _execute(tmp_path)
        assert (outcome.status, outcome.error_kind, outcome.error_stage) == (
            "ERROR",
            "JIRA_FETCH_FAILED",
            "jira_fetch",
        )
        assert world.analysis_calls == 0 and engine.calls == []
        assert set(_artifact_texts(outcome)) == {"execution_summary.json"}

    def test_only_read_requests_reach_jira(self, tmp_path, monkeypatch):
        server = FakeJira(
            issues={
                "DEMO-7": issue_body(description=f"Implement voucher management. {VDI_SENTENCE}")
            },
            comments={"DEMO-7": [comment(1, body="Looks good")]},
        )
        monkeypatch.setattr(pipeline, "import_issue", real_import_issue)  # the real importer
        monkeypatch.setattr(
            pipeline, "JiraClient", lambda settings: JiraClient(settings, session=server.session())
        )
        WORLD.decisions = []
        outcome, _ = _execute(tmp_path)
        assert outcome.status == "PASS", outcome.error_message
        rest = [call for call in server.calls_to("jira.test") if "/rest/" in call.url]
        assert rest and {call.method for call in rest} == {"GET"}

    def test_source_provenance_reaches_step_1(self, tmp_path):
        outcome, _ = _execute(tmp_path)
        step1 = json.loads(Path(outcome.step_artifacts["step1.json"]).read_text(encoding="utf-8"))
        decision = step1["environment_decisions"][0]
        assert decision["source"]["type"] == "comment" and decision["source"]["reference"] == "7001"
        assert outcome.task_source["issue_key"] == "DEMO-7"
        assert [s["type"] for s in outcome.task_source["sources"]] == ["description", "comment"]
        assert all(len(s["sha256"]) == 64 for s in outcome.task_source["sources"])


# ── خطِ لوله و دروازه ───────────────────────────────────────────────────────


class TestPipelineGate:
    def test_the_existing_run_all_is_used_with_a_hook(self, tmp_path):
        seen: list[Any] = []

        def spy(state, **kwargs):
            seen.append(kwargs)
            return pipeline.run_all(state, **kwargs)

        outcome = IssueExecutor(engine=FakeEngine(), run_pipeline=spy).execute(_request(tmp_path))
        assert outcome.status == "PASS"
        assert list(seen[0]) == ["before_step"] and callable(seen[0]["before_step"])

    def test_the_executor_does_not_reimplement_the_steps(self):
        source = Path(executor_module.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "execute_step(",
            "TaskAnalysisGenerator",
            "Step2ApiMappingGenerator",
            "Step4PostmanGenerator",
            "FinalReviewGenerator",
            "PostmanBuilder",
        ):
            assert forbidden not in source

    def test_a_runnable_case_lets_step_3_run(self, tmp_path):
        outcome, _ = _execute(tmp_path)
        assert WORLD.scenario_calls == 1
        assert (outcome.cases_total, outcome.cases_executable, outcome.cases_not_executed) == (
            2,
            2,
            0,
        )

    def test_no_runnable_case_stops_before_step_3_without_newman(self, tmp_path, world):
        world.mapping = {"TC-001": None, "TC-002": None}
        outcome, engine = _execute(tmp_path)
        assert (outcome.status, outcome.review_status) == ("NOT_EXECUTED", "NEEDS_REVIEW")
        assert outcome.not_executed_reason
        assert world.scenario_calls == 0 and engine.calls == []
        assert outcome.newman_executed is False
        kinds = {r["kind"] for r in outcome.review_reasons}
        # both cases are unmapped, so the VDI statement also matches no service (UNMATCHED)
        assert kinds == {"NOT_RUNNABLE", "UNMATCHED"}
        assert set(outcome.step_artifacts) == {"step1.json", "step2.json", "summary"}

    def test_an_informational_vdi_warning_does_not_block(self, tmp_path, world):
        world.comments = ("Some admins open the panel from a VDI session.",)
        world.decisions = []
        outcome, engine = _execute(tmp_path)
        step1 = json.loads(Path(outcome.step_artifacts["step1.json"]).read_text(encoding="utf-8"))
        assert step1["environment_warnings"] == 1  # stored as a count, never as text
        assert (outcome.status, outcome.review_status) == ("PASS", "NONE")
        assert len(engine.calls) == 1

    def test_a_conflicting_case_is_excluded_and_reported(self, tmp_path, world):
        world.comments = (VDI_SENTENCE, NORMAL_SENTENCE)
        world.decisions = world.decisions + [
            {
                **world.decisions[0],
                "environment": "normal",
                "evidence": NORMAL_SENTENCE,
                "source_reference": "Comment 2",
            }
        ]
        outcome, engine = _execute(tmp_path)
        assert outcome.status == "PASS" and len(engine.calls) == 1
        assert (outcome.cases_executable, outcome.cases_not_executed) == (1, 1)
        assert outcome.review_status == "NEEDS_REVIEW"
        blocked = [r for r in outcome.review_reasons if r["kind"] == "NOT_RUNNABLE"]
        assert blocked == [
            {
                "kind": "NOT_RUNNABLE",
                "test_case_id": "TC-001",
                "message": "Its host selection is CONFLICT.",
            }
        ]
        collection = json.loads(Path(outcome.collection_path).read_text(encoding="utf-8"))
        names = [item["name"] for folder in collection["item"] for item in folder["item"]]
        assert names and all(not name.startswith("TC-001") for name in names)

    def test_earlier_results_stay_available_after_stopping(self, tmp_path, world):
        world.mapping = {"TC-001": None, "TC-002": None}
        outcome, _ = _execute(tmp_path)
        assert Path(outcome.step_artifacts["step1.json"]).is_file()
        assert Path(outcome.step_artifacts["step2.json"]).is_file()

    def test_a_pipeline_step_failure_is_error_with_earlier_steps_kept(self, tmp_path, world):
        world.scenario_error = RuntimeError(f"model down token={ENV_TOKEN}")
        outcome, engine = _execute(tmp_path)
        assert (outcome.status, outcome.error_kind, outcome.error_stage) == (
            "ERROR",
            "PIPELINE_STEP_FAILED",
            "step3",
        )
        assert ENV_TOKEN not in outcome.error_message
        assert engine.calls == []
        assert {"step1.json", "step2.json"} <= set(outcome.step_artifacts)

    def test_assess_runnability_mirrors_step_4_conditions(self):
        step1 = {"test_cases": [{"id": "TC-001"}, {"id": "TC-002"}, {"id": "TC-003"}]}
        step2 = {
            "services": [{"name": "A", "base_url": API, "apis": [{"method": "GET", "path": "/a"}]}],
            "mappings": [
                {"test_case_id": "TC-001", "api": {"method": "GET", "path": "/a"}},
                {"test_case_id": "TC-002", "api": {"method": "GET", "path": "/missing"}},
                {"test_case_id": "TC-003", "api": None},
            ],
        }
        result = assess_runnability(step1, step2)
        assert result.runnable == ["TC-001"]
        assert [item["test_case_id"] for item in result.blocked] == ["TC-002", "TC-003"]


# ── قدمِ ۵ و Newman ─────────────────────────────────────────────────────────


class TestNewman:
    def test_step_5_needs_review_still_runs_newman(self, tmp_path, world):
        world.mapping = {"TC-001": CREATE, "TC-002": None}
        outcome, engine = _execute(tmp_path)
        assert outcome.final_review_status == "needs_review"
        assert len(engine.calls) == 1 and outcome.status == "PASS"
        assert outcome.review_status == "NEEDS_REVIEW"

    @pytest.mark.parametrize(
        ("status", "error_kind", "expected"),
        [
            ("PASS", "", "PASS"),
            ("FAIL", "", "FAIL"),
            ("ERROR", "NEWMAN_RUNTIME_ERROR", "ERROR"),
            ("ERROR", "PROCESS_CANCELLED", "CANCELLED"),
        ],
    )
    def test_newman_results_map_to_execution_statuses(self, tmp_path, status, error_kind, expected):
        outcome, _ = _execute(tmp_path, engine=FakeEngine(status=status, error_kind=error_kind))
        assert outcome.status == expected
        assert outcome.newman_status == status and outcome.newman_error_kind == error_kind
        if expected == "ERROR":
            assert (outcome.error_kind, outcome.error_stage) == ("NEWMAN_ERROR", "newman")

    def test_unknown_newman_counts_stay_unknown_not_zero(self, tmp_path):
        engine = FakeEngine(
            status="ERROR", error_kind="SANITIZATION_FAILED", stats=RunStats.unknown()
        )
        outcome, _ = _execute(tmp_path, engine=engine)
        assert (outcome.status, outcome.error_kind) == ("ERROR", "NEWMAN_ERROR")
        assert outcome.newman_error_kind == "SANITIZATION_FAILED"
        assert (
            outcome.requests_executed,
            outcome.assertions_total,
            outcome.assertions_failed,
        ) == (None, None, None)

    def test_counts_and_artifact_paths_are_preserved(self, tmp_path):
        outcome, engine = _execute(tmp_path, engine=FakeEngine(status="FAIL"))
        assert (outcome.requests_executed, outcome.assertions_total, outcome.assertions_failed) == (
            2,
            2,
            1,
        )
        assert Path(outcome.newman_result_path).is_file()
        assert outcome.newman_run_dir.startswith(outcome.artifact_dir)
        request = engine.calls[0]["request"]
        assert Path(request.collection_path) == Path(outcome.collection_path)
        assert request.output_dir == Path(outcome.artifact_dir) / "newman"

    def test_the_cancel_event_is_passed_to_newman(self, tmp_path):
        event = threading.Event()
        outcome, engine = _execute(tmp_path, cancel=event)
        assert engine.calls[0]["cancel_event"] is event

    def test_an_invalid_collection_is_error_without_newman(self, tmp_path):
        def broken(state, **kwargs):
            outcome = pipeline.run_all(state, **kwargs)
            state.results[4]["collection"] = {}
            return outcome

        engine = FakeEngine()
        outcome = IssueExecutor(engine=engine, run_pipeline=broken).execute(_request(tmp_path))
        assert (outcome.status, outcome.error_kind) == ("ERROR", "COLLECTION_INVALID")
        assert engine.calls == []

    def test_cancel_before_start_and_during_the_pipeline(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        early = threading.Event()
        early.set()
        outcome, engine = _execute(tmp_path / "a", cancel=early)
        assert outcome.status == "CANCELLED" and WORLD.analysis_calls == 0 and engine.calls == []

        late = threading.Event()
        engine = FakeEngine()
        executor = IssueExecutor(
            engine=engine, on_phase=lambda phase: late.set() if phase == "step2" else None
        )
        outcome = executor.execute(_request(tmp_path / "b", cancel=late))
        assert (outcome.status, outcome.phase) == ("CANCELLED", "step3")
        assert engine.calls == [] and WORLD.scenario_calls == 0


# ── حصارِ Execution Gate پیش از Newman (M7.2) ───────────────────────────────


def _guarded(request: ExecutionRequest, guard) -> ExecutionRequest:
    return dataclasses.replace(request, before_newman=guard)


class TestGateBoundary:
    def test_the_guard_runs_once_immediately_before_newman(self, tmp_path):
        order: list[str] = []

        class OrderedEngine(FakeEngine):
            def run(self, request, cancel_event=None):
                order.append("newman")
                return super().run(request, cancel_event)

        executor = IssueExecutor(
            engine=OrderedEngine(), on_phase=lambda phase: order.append(f"phase:{phase}")
        )
        outcome = executor.execute(_guarded(_request(tmp_path), lambda: order.append("gate")))
        assert outcome.status == "PASS"
        assert order[-3:] == ["phase:newman", "gate", "newman"]
        assert order.count("gate") == 1

    def test_the_newman_request_and_result_are_unchanged_by_the_guard(self, tmp_path):
        guarded_engine, plain_engine = FakeEngine(), FakeEngine()
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        guarded = IssueExecutor(engine=guarded_engine).execute(
            _guarded(_request(tmp_path / "a"), lambda: None)
        )
        plain = IssueExecutor(engine=plain_engine).execute(_request(tmp_path / "b"))
        sent, baseline = guarded_engine.calls[0]["request"], plain_engine.calls[0]["request"]
        assert (sent.options, sent.access_token) == (baseline.options, baseline.access_token)
        assert sent.collection_path.read_text(encoding="utf-8") == (
            baseline.collection_path.read_text(encoding="utf-8")
        )
        assert sent.environment_path.read_bytes() == baseline.environment_path.read_bytes()
        assert sent.globals_path.read_bytes() == baseline.globals_path.read_bytes()
        paths = {"artifact_dir", "collection_path", "newman_run_dir", "newman_result_path"}
        same = {k: v for k, v in guarded.storage_fields().items() if k not in paths}
        assert same == {k: v for k, v in plain.storage_fields().items() if k not in paths}

    def test_a_refusing_guard_never_starts_newman(self, tmp_path):
        engine = FakeEngine()

        def refuse() -> None:
            raise RuntimeError("gate lost")

        outcome = IssueExecutor(engine=engine).execute(_guarded(_request(tmp_path), refuse))
        assert engine.calls == []
        assert (outcome.status, outcome.phase) == ("CANCELLED", "newman")
        assert outcome.error_message == (
            "Newman was not started: the shared execution gate could not be confirmed."
        )

    def test_without_a_guard_the_executor_behaves_as_before(self, tmp_path):
        outcome, engine = _execute(tmp_path)
        assert outcome.status == "PASS" and len(engine.calls) == 1


# ── artifactها و امنیت ──────────────────────────────────────────────────────


class TestArtifacts:
    def test_all_steps_the_collection_and_the_summary_are_written(self, tmp_path):
        outcome, _ = _execute(tmp_path)
        files = _artifact_texts(outcome)
        for name in (
            "step1.json",
            "step2.json",
            "step3.json",
            "step4.json",
            "step5.json",
            "collection.json",
            "execution_summary.json",
        ):
            assert name in files
        assert (
            Path(outcome.artifact_dir)
            == (tmp_path / "artifacts" / "batch01" / "DEMO-7" / "execution_1").resolve()
        )
        assert json.loads(files["step4.json"])["collection"] == {"see": "collection.json"}
        summary = json.loads(files["execution_summary.json"])
        assert (summary["status"], summary["attempt"]) == ("PASS", 1)

    def test_no_raw_jira_text_prompt_or_evidence_reaches_artifacts(self, tmp_path):
        outcome, _ = _execute(tmp_path)
        texts = _artifact_texts(outcome)
        prompt_only = "How to read this task"  # in the Step 1 prompt input, never in a result
        assert any(prompt_only in prompt for prompt in WORLD.prompts)
        for name, text in texts.items():
            assert JIRA_SECRET_TEXT not in text, name
            assert VDI_SENTENCE not in text, name
            assert prompt_only not in text, name
        step1 = json.loads(texts["step1.json"])
        evidence = step1["environment_decisions"][0]["evidence"]
        assert evidence["redacted"] is True and len(evidence["sha256"]) == 64

    def test_model_free_text_is_never_persisted(self, tmp_path, world):
        world.task_summary = "Use password=hunter2-local for testing."
        outcome, _ = _execute(tmp_path)
        step1 = json.loads(Path(outcome.step_artifacts["step1.json"]).read_text(encoding="utf-8"))
        assert (
            step1["task_summary"]["redacted"] is True and len(step1["task_summary"]["sha256"]) == 64
        )
        for name, text in _artifact_texts(outcome).items():
            assert "hunter2" not in text, name
            assert "Use password" not in text, name

    def test_a_secret_in_an_execution_value_fails_closed(self, tmp_path, world):
        world.create_body = {
            "code": "SUMMER",
            "apiKey": ENV_TOKEN,
        }  # cannot be removed without changing the request
        outcome, engine = _execute(tmp_path)
        assert (outcome.status, outcome.error_kind) == ("ERROR", "ARTIFACT_SANITIZATION_FAILED")
        assert engine.calls == [] and outcome.newman_executed is False
        texts = _artifact_texts(outcome)
        assert "collection.json" not in texts
        for name, text in texts.items():
            assert ENV_TOKEN not in text, name
        for name in ("step1.json", "step2.json", "step3.json", "step4.json", "step5.json"):
            assert json.loads(texts[name])  # earlier safe artifacts are intact

    @pytest.mark.parametrize("issue_key", ["../escape", "DEMO-7/../../x", "", "a" * 200])
    def test_artifact_paths_cannot_escape_the_root(self, tmp_path, issue_key):
        outcome, engine = _execute(tmp_path, issue_key=issue_key)
        assert (outcome.status, outcome.error_kind) == ("ERROR", "INVALID_EXECUTION_REQUEST")
        assert engine.calls == [] and WORLD.analysis_calls == 0
        assert not (tmp_path / "escape").exists() and not (tmp_path / "x").exists()

    def test_a_retry_never_overwrites_an_earlier_attempt(self, tmp_path):
        first, _ = _execute(tmp_path, attempt=1)
        before = _artifact_texts(first)
        again, engine = _execute(tmp_path, attempt=1)
        assert (again.status, again.error_kind) == ("ERROR", "ARTIFACT_WRITE_FAILED")
        assert engine.calls == []
        second, _ = _execute(tmp_path, attempt=2)
        assert second.artifact_dir.endswith("execution_2") and second.status == "PASS"
        assert _artifact_texts(first) == before

    def test_storage_fields_match_finish_execution(self, tmp_path):
        from src.batch_testing.models import IssueSnapshot, NewBatch
        from src.batch_testing.storage import BatchStore

        outcome, _ = _execute(tmp_path)
        with BatchStore.open(tmp_path / "db.sqlite3") as store:
            batch = store.create_batch(
                NewBatch(
                    1,
                    "b",
                    "scrum",
                    "QC Test",
                    {},
                    {},
                    (IssueSnapshot("DEMO-7"),),
                    "e",
                    "x",
                    "g",
                    "y",
                    "a",
                )
            )
            execution = store.start_execution(
                store.create_execution(batch.id, "DEMO-7", trigger="initial").id
            )
            saved = store.finish_execution(execution.id, outcome.status, **outcome.storage_fields())
        assert (saved.status, saved.collection_path, saved.cases_executable) == (
            "PASS",
            outcome.collection_path,
            2,
        )


# ── Newmanِ واقعی، سرورِ محلی ───────────────────────────────────────────────

INSTALL = locate()


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self._reply()

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._reply()

    def _reply(self):
        body = b'{"id": 1}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.mark.skipif(not INSTALL.ok, reason="Newman is not installed")
def test_end_to_end_with_real_newman_against_a_local_server(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        outcome = IssueExecutor().execute(_request(tmp_path, base_url=base))
    finally:
        server.shutdown()
    assert outcome.status == "PASS", (outcome.error_kind, outcome.error_message)
    assert outcome.newman_executed and outcome.requests_executed == 2
    assert Path(outcome.newman_result_path).is_file()
    for name, text in _artifact_texts(outcome).items():
        assert ENV_TOKEN not in text, name
        assert JIRA_SECRET_TEXT not in text, name


# ── audit 4.4: Jira-derived text, collection metadata, Step 5 blocked ────────

DESC_SECRET = "Desc-Secret-9911"
COMMENT_SECRET = "Cmt-Secret-7731xy"


class TestJiraDerivedText:
    def test_a_description_only_secret_cannot_leak_through_model_text(self, tmp_path, world):
        world.description = f"Implement voucher management. password={DESC_SECRET}"
        world.case_text = f"login with password {DESC_SECRET}"
        world.task_summary = f"Vouchers; the admin password is {DESC_SECRET}."
        outcome, engine = _execute(tmp_path)
        assert outcome.status == "PASS", outcome.error_kind
        for name, text in _artifact_texts(outcome).items():
            assert DESC_SECRET not in text, name
        assert "Desc-Secret" not in json.dumps(outcome.storage_fields())

    def test_a_comment_only_secret_cannot_leak_through_titles_or_descriptions(
        self, tmp_path, world
    ):
        world.comments = (VDI_SENTENCE, f"Use token: {COMMENT_SECRET} for the sandbox.")
        world.case_text = f"(token {COMMENT_SECRET})"
        outcome, engine = _execute(tmp_path)
        assert outcome.status == "PASS"
        collection = json.loads(Path(outcome.collection_path).read_text(encoding="utf-8"))
        for name, text in _artifact_texts(outcome).items():
            assert COMMENT_SECRET not in text, name
        names = [item["name"] for folder in collection["item"] for item in folder["item"]]
        assert names == ["TC-001", "TC-002"]

    def test_collection_metadata_carries_no_source_derived_text(self, tmp_path, world):
        world.task_summary = "Secret project codename Bluebird."
        world.case_text = "Bluebird"
        outcome, _ = _execute(tmp_path)
        collection = json.loads(Path(outcome.collection_path).read_text(encoding="utf-8"))
        assert collection["info"] == {
            "name": "Batch DEMO-7 attempt 1",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        }
        assert [folder["name"] for folder in collection["item"]] == ["Folder 1"]
        for folder in collection["item"]:
            for item in folder["item"]:
                assert "description" not in item["request"] and "description" not in item
        assert "Bluebird" not in json.dumps(collection)

    def test_collection_execution_semantics_are_unchanged(self, tmp_path, world):
        world.case_text = "some model text"
        original: dict = {}

        def capture(state, **kwargs):
            outcome = pipeline.run_all(state, **kwargs)
            original["collection"] = json.loads(json.dumps(state.result(4)["collection"]))
            return outcome

        engine = FakeEngine()
        outcome = IssueExecutor(engine=engine, run_pipeline=capture).execute(_request(tmp_path))
        persisted = json.loads(Path(outcome.collection_path).read_text(encoding="utf-8"))
        assert collection_semantics(persisted) == collection_semantics(original["collection"])
        assert persisted != original["collection"]  # only names/descriptions differ
        executed = json.loads(
            Path(engine.calls[0]["request"].collection_path).read_text(encoding="utf-8")
        )
        assert executed == persisted  # Newman runs exactly the persisted collection

    def test_a_jira_credential_in_an_execution_value_fails_closed(self, tmp_path, world):
        jira_key = "JiraKey-55667788"
        world.description = f"Implement voucher management. api_key={jira_key}"
        world.create_body = {"code": "SUMMER", "key": jira_key}
        outcome, engine = _execute(tmp_path)
        assert (outcome.status, outcome.error_kind) == ("ERROR", "ARTIFACT_SANITIZATION_FAILED")
        assert engine.calls == []
        texts = _artifact_texts(outcome)
        assert "collection.json" not in texts
        assert all(jira_key not in text for text in texts.values())
        assert {"step1.json", "step2.json", "step3.json", "step4.json", "step5.json"} <= set(texts)

    def test_jira_candidate_secrets_are_credential_shaped(self):
        found = jira_candidate_secrets(
            [
                "token: abc and voucherTokenType: percentage",
                "password=hunter2-local; see http://u:p4ssw0rd@host/x?api_key=K3y-123456&page=2",
                "Authorization: Bearer abcDEF123456789xyz",
            ]
        )
        assert found == {"hunter2-local", "p4ssw0rd", "K3y-123456", "abcDEF123456789xyz"}


class TestFinalReviewBlocked:
    @staticmethod
    def _blocked(codes, break_collection=False):
        def run(state, **kwargs):
            pipeline.run_all(state, **kwargs)
            review = state.results[5]["review"]
            review["status"] = "blocked"
            review["issues"] = [
                {"code": code, "step": 3, "test_case_id": "", "message": f"detail {DESC_SECRET}"}
                for code in codes
            ]
            if break_collection:
                url = state.results[4]["collection"]["item"][0]["item"][0]["request"]["url"]
                url["host"] = ["{{baseUrl}}"]
            return pipeline.RunOutcome(pipeline.RUN_BLOCKED, 1, (1, 2, 3, 4), 5, "blocked")

        return run

    def test_blocked_for_a_reason_unrelated_to_the_collection(self, tmp_path):
        engine = FakeEngine()
        outcome = IssueExecutor(
            engine=engine, run_pipeline=self._blocked(["missing_requested_variable"])
        ).execute(_request(tmp_path))
        assert (outcome.status, outcome.error_kind, outcome.error_stage) == (
            "ERROR",
            "FINAL_REVIEW_BLOCKED",
            "step5",
        )
        assert outcome.final_review_status == "blocked"
        assert outcome.review_status == "NEEDS_REVIEW"
        assert {
            "kind": "FINAL_REVIEW_ISSUE",
            "code": "missing_requested_variable",
            "message": "Step 5 reported a blocking issue.",
        } in outcome.review_reasons
        assert engine.calls == []
        assert DESC_SECRET not in json.dumps(outcome.storage_fields())

    def test_blocked_with_a_genuinely_invalid_collection(self, tmp_path):
        engine = FakeEngine()
        outcome = IssueExecutor(
            engine=engine, run_pipeline=self._blocked(["invalid_collection"], break_collection=True)
        ).execute(_request(tmp_path))
        assert (outcome.status, outcome.error_kind) == ("ERROR", "COLLECTION_INVALID")
        assert engine.calls == []

    def test_a_missing_collection_is_invalid(self, tmp_path):
        def run(state, **kwargs):
            outcome = pipeline.run_all(state, **kwargs)
            state.results[4].pop("collection")
            return outcome

        outcome = IssueExecutor(engine=FakeEngine(), run_pipeline=run).execute(_request(tmp_path))
        assert outcome.error_kind == "COLLECTION_INVALID"

    @pytest.mark.parametrize(
        "mapping", [{"TC-001": CREATE, "TC-002": DETAILS}, {"TC-001": CREATE, "TC-002": None}]
    )
    def test_a_valid_collection_runs_newman_unless_step_5_is_blocked(
        self, tmp_path, world, mapping
    ):
        world.mapping = mapping
        outcome, engine = _execute(tmp_path)
        assert outcome.final_review_status in ("ready", "needs_review")
        assert len(engine.calls) == 1 and outcome.status == "PASS"


# ── فیلدهای ساختاریِ نگه‌داشته (URL، شناسه، مسیر) ───────────────────────────

URL_PASSWORD = "Us3r-Pa55-0417"
DOCS_TOKEN = "Qx7-docs-tok-0042"
TOKEN_ID = "sk_live_4eC39HqLyjWDarjtT1zdp7dc"


def _json(outcome, name: str) -> Any:
    return json.loads(_artifact_texts(outcome)[name])


class TestStructuredFields:
    def test_url_userinfo_credentials_are_never_persisted(self, tmp_path, world):
        world.base_url = f"http://svc-user:{URL_PASSWORD}@podium-back.devpod.ir/api"
        world.source_url = f"http://svc-user:{URL_PASSWORD}@podium-back.devpod.ir/api/v3/docs"
        original: dict = {}

        def capture(state, **kwargs):
            outcome = pipeline.run_all(state, **kwargs)
            original["collection"] = json.loads(json.dumps(state.result(4)["collection"]))
            return outcome

        engine = FakeEngine()
        outcome = IssueExecutor(engine=engine, run_pipeline=capture).execute(_request(tmp_path))
        assert outcome.status == "PASS", outcome.error_kind
        for name, text in _artifact_texts(outcome).items():
            assert URL_PASSWORD not in text and "svc-user" not in text, name
        assert URL_PASSWORD not in json.dumps(outcome.storage_fields())
        service = _json(outcome, "step2.json")["services"][0]
        assert service["base_url"] == API
        assert service["source_url"] == f"{API}/v3/docs"
        persisted = json.loads(Path(outcome.collection_path).read_text(encoding="utf-8"))
        assert collection_semantics(persisted) == collection_semantics(original["collection"])

    def test_sensitive_query_values_and_fragments_are_never_persisted(self, tmp_path, world):
        world.source_url = (
            f"{API}/v3/api-docs/Admin?access_token={DOCS_TOKEN}&group=admin#frag-Secret9"
        )
        outcome, _ = _execute(tmp_path)
        assert outcome.status == "PASS"
        for name, text in _artifact_texts(outcome).items():
            assert DOCS_TOKEN not in text and "frag-Secret9" not in text, name
        service = _json(outcome, "step2.json")["services"][0]
        assert service["source_url"] == f"{API}/v3/api-docs/Admin?access_token=***&group=***"

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://u:p4ss-w0rd@host.test:8443/api?x=1#f", "https://host.test:8443/api?x=***"),
            ("http://host.test/api?api_key=K3y-123456", "http://host.test/api?api_key=***"),
            ("{{base_url}}/api", "{{base_url}}/api"),
            (f"http://host.test/api/{TOKEN_ID}/x", "http://host.test/api/***/x"),
        ],
    )
    def test_safe_url_keeps_only_traceable_parts(self, url, expected):
        assert safe_url(url) == expected

    def test_unparseable_urls_are_hashed(self):
        for url in ("ftp://u:p@host/x", "http://[bad/x", "not a url with password=Xy-1234"):
            value = safe_url(url)
            assert value["redacted"] is True and len(value["sha256"]) == 64

    def test_token_shaped_identifiers_and_keys_are_not_retained(self):
        source = {"type": "comment", "reference": TOKEN_ID, "sha256": "not-a-hash"}
        step1 = sanitize_step_result(
            1,
            {
                "test_cases": [{"id": TOKEN_ID, "auth_intent": "inherit"}],
                "variables": [{"name": TOKEN_ID, "response_path": f"data.{TOKEN_ID}"}],
                "environment_decisions": [
                    {"id": "ENV-1", "test_case_ids": ["TC-001", TOKEN_ID], "source": source}
                ],
            },
        )
        step5 = sanitize_step_result(5, {"metrics": {TOKEN_ID: 1, "total_requests": 2}})
        step2 = sanitize_step_result(
            2, {"mappings": [{"test_case_id": "TC-001", "api": {"path": f"/v/{TOKEN_ID}"}}]}
        )
        text = json.dumps([step1, step5, step2])
        assert TOKEN_ID not in text and "4eC39HqLyjWDarjtT1zdp7dc" not in text
        assert step1["test_cases"][0]["id"]["redacted"] is True
        assert step1["variables"][0]["name"]["redacted"] is True
        decision = step1["environment_decisions"][0]
        assert decision["test_case_ids"] == ["TC-001"]
        assert decision["source"]["reference"]["redacted"] is True
        assert decision["source"]["sha256"] == ""
        assert step5["metrics"] == {"total_requests": 2}
        assert step2["mappings"][0]["api"]["path"] == "/v/***"

    def test_a_known_secret_in_a_retained_identifier_or_key_fails_closed(self, tmp_path):
        writer = ArtifactWriter.create(tmp_path, "batch01", "DEMO-7", 1)
        secret = "Plain-Env-Secret-7"  # identifier-shaped, so only the secret check catches it
        for name, document in (
            ("a.json", {"variables": [{"name": secret}]}),
            ("b.json", {"metrics": {secret: 1}}),
        ):
            with pytest.raises(SanitizationError):
                writer.write_json(name, document, {secret})
            assert not (writer.directory / name).exists()

    def test_ordinary_urls_paths_and_identifiers_remain_traceable(self, tmp_path):
        outcome, _ = _execute(tmp_path)
        assert outcome.status == "PASS"
        step2 = _json(outcome, "step2.json")
        assert step2["services"] == [
            {
                "name": "Admin",
                "source_url": f"{API}/v3/api-docs/Admin",
                "base_url": API,
                "operations": 2,
            }
        ]
        paths = {m["test_case_id"]: m["api"]["path"] for m in step2["mappings"]}
        assert paths == {"TC-001": "/admin/voucher", "TC-002": "/admin/voucher/{id}"}
        requests = _json(outcome, "step4.json")["requests"]
        assert [(r["test_case_id"], r["method"], r["path"]) for r in requests] == [
            ("TC-001", "POST", "/admin/voucher"),
            ("TC-002", "GET", "/admin/voucher/{id}"),
        ]
        assert safe_path("/admin/{{voucherId}}/items/:id") == "/admin/{{voucherId}}/items/:id"
        dependency = {
            "variable_name": "voucherId",
            "source": {"test_case_id": "TC-001", "location": "body", "path": "data.items[0].id"},
        }
        step3 = sanitize_step_result(3, {"data_dependencies": [dependency]})
        assert step3["data_dependencies"][0]["variable_name"] == "voucherId"
        assert step3["data_dependencies"][0]["source"]["path"] == "data.items[0].id"

    def test_a_literal_credential_in_a_required_execution_value_fails_closed(self, tmp_path, world):
        literal = "Lit3ral-Pass-998"  # in neither Jira nor the environment files
        world.create_body = {"code": "SUMMER", "password": literal}
        outcome, engine = _execute(tmp_path)
        assert (outcome.status, outcome.error_kind) == ("ERROR", "ARTIFACT_SANITIZATION_FAILED")
        assert engine.calls == [] and outcome.newman_executed is False
        texts = _artifact_texts(outcome)
        assert "collection.json" not in texts
        assert all(literal not in text for text in texts.values())
        assert {"step1.json", "step2.json", "step3.json", "step4.json", "step5.json"} <= set(texts)

    def test_swagger_placeholder_values_do_not_fail_closed(self, tmp_path, world):
        world.create_body = {"code": "SUMMER", "password": "string", "tokenType": "fixed"}
        outcome, engine = _execute(tmp_path)
        assert outcome.status == "PASS" and len(engine.calls) == 1

    def test_execution_secrets_reads_headers_queries_auth_and_urls(self):
        collection = {
            "variable": [{"key": "apiKey", "value": "Var-Key-0001"}],
            "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "Auth-Tok-0002"}]},
            "item": [
                {
                    "request": {
                        "header": [
                            {"key": "Authorization", "value": "{{token}}"},
                            {"key": "X-Api-Key", "value": "Hdr-Key-0003"},
                        ],
                        "url": {
                            "raw": "http://u:Url-Pass-0004@h/x?session=Sess-0005&page=2",
                            "host": ["h"],
                            "query": [{"key": "page", "value": "2"}],
                        },
                        "body": {"mode": "raw", "raw": '{"password": "Body-Pass-0006"}'},
                    }
                }
            ],
        }
        step2 = {"services": [{"base_url": "http://svc:Svc-Pass-0007@h/api"}]}
        assert execution_secrets(step2, collection) == {
            "Var-Key-0001",
            "Auth-Tok-0002",
            "Hdr-Key-0003",
            "Url-Pass-0004",
            "Sess-0005",
            "Body-Pass-0006",
            "Svc-Pass-0007",
        }


class TestAccessToken:
    def test_without_an_access_token_nothing_runs(self, tmp_path):
        loaded: list[str] = []
        engine = FakeEngine()
        executor = IssueExecutor(engine=engine, load_task=lambda state, key: loaded.append(key))
        outcome = executor.execute(_request(tmp_path, access_token=None))
        assert (outcome.status, outcome.error_kind, outcome.error_stage) == (
            "ERROR",
            executor_module.ERROR_AUTHENTICATION,
            executor_module.STAGE_AUTHENTICATION,
        )
        assert loaded == [] and engine.calls == []  # نه Jira، نه قدم‌ها، نه Newman
        assert WORLD.analysis_calls == 0
        assert outcome.newman_executed is False and outcome.newman_error_kind == ""
        assert set(_artifact_texts(outcome)) == {"execution_summary.json"}

    def test_the_session_token_reaches_newman_raw_and_nowhere_else(self, tmp_path):
        outcome, engine = _execute(tmp_path)
        assert outcome.status == "PASS"
        request = engine.calls[0]["request"]
        assert request.access_token == ACCESS_TOKEN.value  # خام؛ Bearer را موتور می‌گذارد
        assert ACCESS_TOKEN.value not in repr(request)
        assert ACCESS_TOKEN.value not in repr(
            ExecutionRequest("b", "DEMO-7", "e", 1, access_token=ACCESS_TOKEN)
        )
        for text in _artifact_texts(outcome).values():
            assert ACCESS_TOKEN.value not in text

    def test_the_token_is_a_known_secret_for_every_artifact(self, tmp_path):
        secrets = IssueExecutor._known_secrets(_request(tmp_path))
        assert {ACCESS_TOKEN.value, f"Bearer {ACCESS_TOKEN.value}"} <= secrets

    def test_an_artifact_that_would_contain_the_token_is_not_written(self, tmp_path, world):
        world.description = f"Implement voucher management. token {ACCESS_TOKEN.value}"
        outcome, engine = _execute(tmp_path)
        for text in _artifact_texts(outcome).values():
            assert ACCESS_TOKEN.value not in text
