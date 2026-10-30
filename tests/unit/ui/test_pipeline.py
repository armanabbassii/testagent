"""
تست‌های اجرای مشترکِ قدم‌ها و Run All — ui/pipeline.py

هر پنج Generator با نسخه‌ی ساختگی جایگزین می‌شوند (هیچ LLM یا شبکه‌ای صدا
زده نمی‌شود). خروجی‌های ساختگی همان زنجیره‌ی واقعیِ tests/…/test_final_review.py
هستند، پس قدم‌ها دقیقاً داده‌ای را به هم می‌دهند که در ویزارد می‌دهند.

سنجیده می‌شود:
  * execute_step: جمع‌کردنِ ورودی، فراخوانیِ Generator، ثبتِ نتیجه/خطا؛
  * انتقالِ URLهای Developed Services به ورودیِ قدم ۲؛
  * انتخابِ خودکارِ منابعِ Swagger در Run All (ثابت در طولِ اجرا، بدونِ نشتِ
    انتخابِ دستیِ قبلی) و ورودیِ واقعیِ Step2ApiMappingGenerator؛
  * run_all: ترتیب، انتقالِ خروجی‌ها، توقف روی خطا، blocked، هم‌پوشانی، کهنه‌شدن؛
  * مسیرِ دستیِ قدم‌به‌قدم بدونِ تغییر؛
  * همان کالکشن و نامِ فایلی که دکمه‌های دانلود استفاده می‌کنند.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agents.test_case_generator import swagger_snapshots
from src.agents.test_case_generator.api_mapping import (
    ApiMappingAgent,
    Step2ApiMappingGenerator,
    compact_step2_result,
)
from src.agents.test_case_generator.source_selection import MODE_AUTO, MODE_MANUAL
from src.agents.test_case_generator.final_review import STATUS_BLOCKED as REVIEW_BLOCKED
from src.agents.test_case_generator.swagger_snapshots import snapshot_path
from src.token_usage import report_usage
from tests.unit.test_case_generator.test_source_selection import write_snapshots
from tests.unit.test_case_generator.test_source_semantics import (
    FakeLLM,
    answer,
    op,
    route_by_task,
)
from tests.unit.test_case_generator.test_final_review import (
    _build,
    _review,
    _step1_result,
    _step2_result,
    _step3_result,
)
from ui import pipeline
from ui.formatting import (
    collection_filename,
    collection_json,
    collection_name,
    postman_collection,
)
from ui.pipeline import (
    ALREADY_RUNNING,
    RUN_BLOCKED,
    RUN_FAILED,
    RUN_INTERRUPTED,
    RUN_REJECTED,
    RUN_SUCCEEDED,
    STEP_BLOCKED,
    STEP_COMPLETED,
    STEP_FAILED,
    STEP_RUNNING,
    execute_step,
    plan_swagger_sources,
    run_all,
)
from ui.workflow import (
    INPUT_COLLECTION_NAME,
    INPUT_DEVELOPED_SERVICES,
    INPUT_SWAGGER_SELECTION,
    INPUT_SWAGGER_SOURCES,
    INPUT_TASK_DESCRIPTION,
    STATUS_AUTO_COMPLETED,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_LABELS,
    STATUS_MARKERS,
    STATUS_RUNNING,
    STATUS_STALE,
    WorkflowState,
    progress_text,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

_TASK = "Implement voucher management endpoints."
_ADMIN_UI = (
    "https://podium-admin.sandpod.ir/api/swagger-ui/index.html"
    "?urls.primaryName=Admin#/voucher-admin-controller/getVoucherDetails"
)
_CUSTOMER_RAW = "http://podium-back.devpod.ir/api/v3/api-docs/Customer"
_UNKNOWN_SWAGGER = "https://host.test/api/swagger-ui/index.html?urls.primaryName=Billing"

_GENERATOR_CLASSES = {
    1: "TaskAnalysisGenerator",
    2: "Step2ApiMappingGenerator",
    3: "Step3ScenarioAnalysisGenerator",
    4: "Step4PostmanGenerator",
    5: "FinalReviewGenerator",
}


def _chain() -> dict[int, dict]:
    """خروجیِ واقعیِ هر قدم — قدم ۴ و ۵ با Generatorهای واقعی ساخته می‌شوند."""
    step1, step2, step3 = _step1_result(), _step2_result(), _step3_result()
    step4 = _build(step1, step2, step3)
    step5 = _review(step1, step2, step3, step4)
    return {1: step1, 2: step2, 3: step3, 4: step4, 5: step5}


class FakeGenerators:
    """Generatorهای ساختگی: هر فراخوانی (سازنده و generate) ثبت می‌شود."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.outputs: dict[int, object] = _chain()
        self.errors: dict[int, BaseException] = {}
        self.usage: dict[int, object] = {}
        self.init: dict[int, list[dict]] = {}
        self.calls: dict[int, list[dict]] = {}
        self.order: list[int] = []
        for step, name in _GENERATOR_CLASSES.items():
            monkeypatch.setattr(pipeline, name, self._fake(step))

    def _fake(self, step: int) -> type:
        fakes = self

        class Fake:
            def __init__(self, *args, **kwargs) -> None:
                fakes.init.setdefault(step, []).append(kwargs)

            def generate(self, *args, **kwargs):
                fakes.order.append(step)
                fakes.calls.setdefault(step, []).append(kwargs)
                if step in fakes.usage:
                    report_usage(fakes.usage[step])  # همان گزارشی که LLMClient.chat می‌دهد
                if step in fakes.errors:
                    raise fakes.errors[step]
                return copy.deepcopy(fakes.outputs[step])

        return Fake

    def last(self, step: int) -> dict:
        return self.calls[step][-1]


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> FakeGenerators:
    return FakeGenerators(monkeypatch)


@pytest.fixture(autouse=True)
def fixture_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """انتخابِ منابع و قدم ۲ snapshotهای ساختگیِ هر پنج منبع را می‌خوانند، نه data/swagger/."""
    directory = tmp_path / "swagger"
    directory.mkdir()
    write_snapshots(directory)
    monkeypatch.setattr(swagger_snapshots, "SNAPSHOT_DIR", directory)
    return directory


@pytest.fixture(autouse=True)
def semantic_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """تحلیلِ معناییِ منابع هرگز LLMِ واقعی را صدا نمی‌زند (ووچر → Admin، …)."""
    return FakeLLM(monkeypatch, router=route_by_task)


def _state(services: str = "") -> WorkflowState:
    state = WorkflowState()
    state.set_input(INPUT_TASK_DESCRIPTION, _TASK)
    state.set_input(INPUT_DEVELOPED_SERVICES, services)
    return state


def _blocked_review(fakes: FakeGenerators) -> None:
    review = copy.deepcopy(fakes.outputs[5])
    review["review"]["status"] = REVIEW_BLOCKED
    review["review"]["issues"] = [{"code": "missing_request", "message": "TC-001"}]
    fakes.outputs[5] = review


# ── Streamlit-free ──────────────────────────────────────────────────────────

class TestNoStreamlit:
    def test_the_pipeline_does_not_import_streamlit(self):
        code = (
            "import sys, ui.pipeline; "
            "sys.exit(1 if 'streamlit' in sys.modules else 0)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code], cwd=_REPO_ROOT, capture_output=True, text=True
        )
        assert completed.returncode == 0, completed.stderr


# ── execute_step ────────────────────────────────────────────────────────────

class TestExecuteStep:
    def test_step_one_needs_a_task_description_and_records_nothing(self, fakes):
        state = WorkflowState()
        outcome = execute_step(1, state)
        assert outcome.status == STEP_FAILED
        assert outcome.recorded is False
        assert outcome.error == "Task Description is required."
        assert fakes.calls == {}
        assert state.errors == {}

    def test_step_one_receives_both_manual_inputs(self, fakes):
        state = _state(f"Admin: {_ADMIN_UI}")
        outcome = execute_step(1, state)
        assert outcome.status == STEP_COMPLETED
        assert fakes.last(1) == {
            "task_description": _TASK,
            "user_id": pipeline.USER_IDS[1],
            "developed_services": f"Admin: {_ADMIN_UI}",
        }
        assert state.result(1) == fakes.outputs[1]

    def test_llm_steps_track_their_usage(self, fakes):
        fakes.usage[1] = SimpleNamespace(prompt_tokens=120, completion_tokens=30)
        state = _state()
        execute_step(1, state)
        assert state.usage_for(1).calls == 1
        assert state.usage_for(1).total_tokens == 150

    def test_a_missing_upstream_result_is_reported_and_not_recorded(self, fakes):
        state = _state()
        outcome = execute_step(3, state)
        assert outcome.status == STEP_FAILED
        assert outcome.recorded is False
        assert "Step 1 and Step 2 results are not available" in outcome.error
        assert 3 not in fakes.calls
        assert state.error(3) == ""

    def test_a_generator_exception_is_recorded_with_the_existing_message(self, fakes):
        fakes.errors[1] = RuntimeError("service unavailable")
        state = _state()
        outcome = execute_step(1, state)
        assert outcome.status == STEP_FAILED
        assert outcome.error == "Task analysis failed: RuntimeError: service unavailable"
        assert state.error(1) == outcome.error
        assert state.has_result(1) is False

    def test_an_unusable_result_is_a_failure_not_a_success(self, fakes):
        fakes.outputs[1] = {"task_summary": "no test cases key"}
        state = _state()
        outcome = execute_step(1, state)
        assert outcome.status == STEP_FAILED
        assert "test_cases" in outcome.error
        assert state.has_result(1) is False

    def test_step_three_receives_the_compact_contract_not_the_catalogue(self, fakes):
        state = _state()
        for step in (1, 2):
            execute_step(step, state)
        execute_step(3, state)
        sent = fakes.last(3)["step2_result"]
        assert sent == compact_step2_result(fakes.outputs[2])
        assert "services" not in sent

    def test_step_four_uses_the_stored_collection_name(self, fakes):
        state = _state()
        state.set_input(INPUT_COLLECTION_NAME, "Voucher Suite")
        for step in (1, 2, 3, 4):
            execute_step(step, state)
        assert fakes.init[4][-1]["collection_name"] == "Voucher Suite"

    def test_a_blocked_review_is_recorded_but_marked_blocked(self, fakes):
        _blocked_review(fakes)
        state = _state()
        for step in (1, 2, 3, 4):
            execute_step(step, state)
        outcome = execute_step(5, state)
        assert outcome.status == STEP_BLOCKED
        assert "blocked by 1 issue" in outcome.error
        assert state.has_result(5) is True
        assert state.is_blocked(5) is True
        assert state.status(5) == STATUS_BLOCKED
        assert state.auto_advance(5) is False


# ── URLهای سرویس → قدم ۲ (رگرسیونِ نقصِ فاز ۱) ─────────────────────────────

class TestServiceUrlsReachStepTwo:
    def _run_step_two(self, state: WorkflowState) -> pipeline.StepOutcome:
        execute_step(1, state)
        return execute_step(2, state)

    def test_an_admin_url_makes_step_two_read_only_the_admin_snapshot(self, fakes):
        outcome = self._run_step_two(_state(f"Voucher details: {_ADMIN_UI}"))
        assert outcome.ok
        assert fakes.last(2)["snapshot_keys"] == ["admin"]

    def test_a_customer_url_makes_step_two_read_only_the_customer_snapshot(self, fakes):
        state = _state(_CUSTOMER_RAW)
        self._run_step_two(state)
        assert fakes.last(2)["snapshot_keys"] == ["customer"]
        assert state.input_value(INPUT_SWAGGER_SOURCES) == str(snapshot_path("customer"))

    def test_both_urls_read_both_snapshots(self, fakes):
        self._run_step_two(_state(f"{_ADMIN_UI}\n{_CUSTOMER_RAW}"))
        assert fakes.last(2)["snapshot_keys"] == ["admin", "customer"]

    def test_without_a_swagger_url_the_endpoint_path_decides(self, fakes):
        self._run_step_two(_state("POST /admin/voucher"))
        assert fakes.last(2)["snapshot_keys"] == ["admin"]

    def test_without_any_evidence_step_two_fails_instead_of_reading_both(self, fakes):
        state = WorkflowState()
        state.set_input(INPUT_TASK_DESCRIPTION, "Fix the bug reported yesterday.")
        outcome = self._run_step_two(state)
        assert outcome.status == STEP_FAILED
        assert "could not be determined" in outcome.error
        assert 2 not in fakes.calls

    def test_step_two_receives_the_step_one_result(self, fakes):
        self._run_step_two(_state(_ADMIN_UI))
        assert fakes.last(2)["step1_result"] == fakes.outputs[1]

    def test_an_unknown_swagger_url_stops_step_two_with_a_clear_error(self, fakes):
        state = _state(_UNKNOWN_SWAGGER)
        outcome = self._run_step_two(state)
        assert outcome.status == STEP_FAILED
        assert _UNKNOWN_SWAGGER in outcome.error
        assert 2 not in fakes.calls  # هیچ snapshotِ بی‌ربطی خوانده نشد
        assert state.error(2) == outcome.error

    def test_an_explicit_selection_is_not_replaced_by_the_url(self, fakes):
        state = _state(_ADMIN_UI)
        state.set_input(INPUT_SWAGGER_SELECTION, "both")
        self._run_step_two(state)
        assert fakes.last(2)["snapshot_keys"] == ["admin", "customer"]

    def test_a_conflicting_explicit_selection_is_an_error(self, fakes):
        state = _state(_ADMIN_UI)
        state.set_input(INPUT_SWAGGER_SELECTION, "customer")
        outcome = self._run_step_two(state)
        assert outcome.status == STEP_FAILED
        assert _ADMIN_UI in outcome.error
        assert 2 not in fakes.calls

    def test_the_plan_reports_how_each_source_was_chosen(self):
        plan = plan_swagger_sources(_state(_ADMIN_UI))
        assert plan.mode == MODE_AUTO
        assert plan.evidence_for("admin")[0].kind == "swagger_url"
        assert plan_swagger_sources(_state("")).evidence_for("admin")[0].kind == "metadata"
        semantic = plan_swagger_sources(_state(""), semantic=True)
        assert semantic.evidence_for("admin")[0].kind == "semantic"
        state = _state("")
        state.set_input(INPUT_SWAGGER_SELECTION, "admin")
        assert plan_swagger_sources(state).mode == MODE_MANUAL

    def test_changing_the_service_urls_after_step_two_is_reported(self, fakes):
        state = _state(_ADMIN_UI)
        self._run_step_two(state)
        state.set_input(INPUT_DEVELOPED_SERVICES, _CUSTOMER_RAW)
        assert "Developed Services" in state.pending_input_changes(2)
        assert state.status(2) == STATUS_STALE


# ── Run All ─────────────────────────────────────────────────────────────────

class TestAutomaticSourceSelection:
    def test_run_all_selects_sources_automatically_and_passes_them_to_step_two(self, fakes):
        state = _state("GET /crm/digitalService/requests\nhttps://h.test/api/admin/voucher/3")
        outcome = run_all(state)
        assert outcome.status == RUN_SUCCEEDED
        assert fakes.last(2)["snapshot_keys"] == ["admin", "crm"]
        assert state.swagger_selection.summary() == (
            "Admin (endpoint path), CRM (endpoint path) — automatic"
        )

    def test_a_previous_manual_selection_does_not_leak_into_run_all(self, fakes):
        state = _state("/admin/voucher/1")
        state.set_input(INPUT_SWAGGER_SELECTION, "customer")  # از اجرای دستیِ قبلی
        run_all(state)
        assert fakes.last(2)["snapshot_keys"] == ["admin"]
        assert state.input_value(INPUT_SWAGGER_SELECTION) == "auto"
        assert state.swagger_selection.mode == MODE_AUTO

    def test_the_selection_is_made_once_and_used_throughout_the_run(self, fakes, monkeypatch):
        calls = []
        real = pipeline.select_sources

        def counting(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(pipeline, "select_sources", counting)
        state = _state(_ADMIN_UI)
        run_all(state)
        assert len(calls) == 1
        keys = list(state.swagger_selection.keys)
        assert fakes.last(2)["snapshot_keys"] == keys == ["admin"]
        assert state.input_value(INPUT_SWAGGER_SOURCES) == str(snapshot_path("admin"))
        assert state.step_inputs(2)[INPUT_SWAGGER_SOURCES] == str(snapshot_path("admin"))

    def test_an_ambiguous_selection_stops_run_all_at_step_two(self, fakes):
        state = WorkflowState()
        state.set_input(INPUT_TASK_DESCRIPTION, "Fix the bug reported yesterday.")
        outcome = run_all(state)
        assert outcome.status == RUN_FAILED
        assert outcome.failed_step == 2
        assert outcome.completed_steps == (1,)
        assert "could not be determined" in outcome.error
        assert fakes.order == [1]

    def test_a_missing_snapshot_stops_run_all_with_the_path(self, fakes, fixture_snapshots):
        (fixture_snapshots / "provider.json").unlink()
        outcome = run_all(_state("/provider/serviceSet/4"))
        assert outcome.failed_step == 2
        assert str(fixture_snapshots / "provider.json") in outcome.error

    def test_manual_step_two_keeps_the_manual_override(self, fakes):
        state = _state("/admin/voucher/1")
        execute_step(1, state)
        state.set_input(INPUT_SWAGGER_SELECTION, "admin,site")
        assert execute_step(2, state).ok
        assert fakes.last(2)["snapshot_keys"] == ["admin", "site"]
        assert state.swagger_selection.mode == MODE_MANUAL

    def test_a_persian_task_is_analysed_once_and_reaches_step_two(self, fakes, semantic_llm):
        state = WorkflowState()
        state.set_input(INPUT_TASK_DESCRIPTION, "ادمین بتواند جزئیات ووچر را مشاهده کند.")
        outcome = run_all(state)
        assert outcome.status == RUN_SUCCEEDED
        assert len(semantic_llm.calls) == 1
        assert fakes.last(2)["snapshot_keys"] == ["admin"]
        assert state.swagger_selection.summary() == (
            "Admin (task analysis, verified operation) — automatic"
        )

    def test_explicit_evidence_runs_without_any_llm_call(self, fakes, semantic_llm):
        run_all(_state("GET /crm/digitalService/requests"))
        assert semantic_llm.calls == []
        assert fakes.last(2)["snapshot_keys"] == ["crm"]

    def test_the_analysis_tokens_count_as_step_two_usage(self, fakes, monkeypatch):
        FakeLLM(
            monkeypatch,
            answer(op("Admin", "Get voucher details", "voucher")),
            usage=SimpleNamespace(prompt_tokens=200, completion_tokens=40),
        )
        state = _state()
        execute_step(1, state)
        assert execute_step(2, state).ok
        assert state.usage_for(2).calls == 1
        assert state.usage_for(2).total_tokens == 240

    def test_a_failed_analysis_stops_run_all_at_step_two_without_a_guess(self, fakes, monkeypatch):
        FakeLLM(monkeypatch, ConnectionError("model unavailable"))
        state = WorkflowState()
        state.set_input(INPUT_TASK_DESCRIPTION, "ادمین بتواند جزئیات ووچر را مشاهده کند.")
        outcome = run_all(state)
        assert outcome.failed_step == 2
        assert "Task analysis failed: ConnectionError: model unavailable" in outcome.error
        assert fakes.order == [1]

    def test_end_to_end_the_selected_snapshot_is_what_the_mapping_sees(
        self, fakes, monkeypatch
    ):
        # قدم ۲ واقعی است (فقط فراخوانیِ LLM جایگزین شده): کاتالوگی که به نگاشت
        # می‌رسد باید دقیقاً همان snapshotِ انتخاب‌شده باشد.
        seen = {}

        def fake_map(self, test_cases, services, user_id):
            seen["services"] = services
            return [], []

        monkeypatch.setattr(pipeline, "Step2ApiMappingGenerator", Step2ApiMappingGenerator)
        monkeypatch.setattr(ApiMappingAgent, "map_test_cases", fake_map)
        state = _state("GET /crm/digitalService/requests")
        execute_step(1, state)
        outcome = execute_step(2, state)

        assert outcome.ok, outcome.error
        assert [service.name for service in seen["services"]] == ["CRM"]
        assert [api.label for api in seen["services"][0].apis] == [
            "GET /crm/digitalService/requests"
        ]
        assert [service["name"] for service in state.result(2)["services"]] == ["CRM"]


class TestRunAllSuccess:
    def test_all_five_steps_run_once_in_order(self, fakes):
        state = _state(_ADMIN_UI)
        outcome = run_all(state)
        assert outcome.status == RUN_SUCCEEDED
        assert outcome.succeeded
        assert fakes.order == [1, 2, 3, 4, 5]
        assert outcome.completed_steps == (1, 2, 3, 4, 5)
        assert outcome.last_completed_step == 5
        assert outcome.failed_step == 0
        assert state.last_run == outcome

    def test_outputs_flow_into_the_next_steps(self, fakes):
        state = _state(_ADMIN_UI)
        run_all(state)
        out = fakes.outputs
        assert fakes.last(2)["step1_result"] == out[1]
        assert fakes.last(2)["snapshot_keys"] == ["admin"]
        assert fakes.last(3)["step1_result"] == out[1]
        assert fakes.last(3)["step2_result"] == compact_step2_result(out[2])
        assert fakes.last(4) == {"step1_result": out[1], "step2_result": out[2], "step3_result": out[3]}
        assert fakes.last(5) == {
            "step1_result": out[1],
            "step2_result": out[2],
            "step3_result": out[3],
            "step4_result": out[4],
        }

    def test_no_manual_approval_is_needed_and_none_is_faked(self, fakes):
        state = _state()
        run_all(state)
        assert state.approved == set()
        assert state.auto_advanced == {1, 2, 3, 4, 5}
        assert all(state.is_unlocked(step) for step in range(1, 6))
        assert [status for _, status in state.statuses()][:4] == [STATUS_AUTO_COMPLETED] * 4
        assert state.current_step == 5
        assert "5 completed by Run All (not reviewed)" in progress_text(state)
        assert "0 of 5 approved" in progress_text(state)

    def test_a_human_can_still_approve_an_automatic_result(self, fakes):
        state = _state()
        run_all(state)
        assert state.approve(2) is True
        assert state.is_approved(2) is True
        assert state.is_auto_advanced(2) is False

    def test_progress_is_reported_before_and_after_every_step(self, fakes):
        events: list[tuple[int, str, str]] = []
        state = _state()
        run_all(
            state,
            on_progress=lambda step, status: events.append((step, status, state.status(step))),
        )
        assert [(step, status) for step, status, _ in events] == [
            (step, phase) for step in range(1, 6) for phase in (STEP_RUNNING, STEP_COMPLETED)
        ]
        assert all(display == STATUS_RUNNING for _, status, display in events if status == STEP_RUNNING)

    def test_the_lock_is_released_after_the_run(self, fakes):
        state = _state()
        run_all(state)
        assert state.run_in_progress is False
        assert state.running_step == 0
        assert state.notice == ""


class TestRunAllFailure:
    @pytest.mark.parametrize("failing", [1, 2, 3, 4, 5])
    def test_a_failure_stops_the_pipeline_at_that_step(self, fakes, failing):
        fakes.errors[failing] = RuntimeError(f"boom in {failing}")
        state = _state()
        outcome = run_all(state)

        assert outcome.status == RUN_FAILED
        assert not outcome.succeeded
        assert outcome.failed_step == failing
        assert f"boom in {failing}" in outcome.error
        assert outcome.completed_steps == tuple(range(1, failing))
        assert fakes.order == list(range(1, failing + 1))  # هیچ قدمِ وابسته‌ای اجرا نشد
        assert state.error(failing) == outcome.error
        assert state.status(failing) == STATUS_FAILED
        assert state.current_step == failing
        assert all(not state.has_result(step) for step in range(failing, 6))

    def test_an_unusable_output_stops_the_pipeline(self, fakes):
        fakes.outputs[3] = {"execution_order": []}
        outcome = run_all(_state())
        assert outcome.status == RUN_FAILED
        assert outcome.failed_step == 3
        assert "scenarios" in outcome.error
        assert fakes.order == [1, 2, 3]

    def test_an_unresolvable_service_url_stops_at_step_two(self, fakes):
        state = _state(_UNKNOWN_SWAGGER)
        outcome = run_all(state)
        assert outcome.failed_step == 2
        assert _UNKNOWN_SWAGGER in outcome.error
        assert fakes.order == [1]

    def test_an_empty_task_description_fails_before_anything_runs(self, fakes):
        state = WorkflowState()
        outcome = run_all(state)
        assert outcome.status == RUN_FAILED
        assert outcome.failed_step == 1
        assert outcome.error == "Task Description is required."
        assert fakes.order == []
        assert state.last_run == outcome

    def test_a_blocked_review_is_not_a_success(self, fakes):
        _blocked_review(fakes)
        state = _state()
        outcome = run_all(state)
        assert outcome.status == RUN_BLOCKED
        assert not outcome.succeeded
        assert outcome.failed_step == 5
        assert outcome.completed_steps == (1, 2, 3, 4)
        assert "blocked" in outcome.error
        assert state.status(5) == STATUS_BLOCKED
        assert state.is_auto_advanced(5) is False


class TestOverlapAndInterruption:
    def test_a_second_run_is_rejected_while_one_is_in_progress(self, fakes):
        state = _state()
        state.run_in_progress = True
        outcome = run_all(state)
        assert outcome.status == RUN_REJECTED
        assert outcome.error == ALREADY_RUNNING
        assert fakes.order == []

    def test_a_nested_start_from_a_progress_callback_is_rejected(self, fakes):
        state = _state()
        nested: list[pipeline.RunOutcome] = []

        def on_progress(step: int, status: str) -> None:
            if step == 2 and status == STEP_RUNNING:
                nested.append(run_all(state))

        outcome = run_all(state, on_progress=on_progress)
        assert outcome.status == RUN_SUCCEEDED
        assert [result.status for result in nested] == [RUN_REJECTED]
        assert fakes.order == [1, 2, 3, 4, 5]

    def test_an_interruption_releases_the_lock_and_is_recorded(self, fakes):
        class Interrupt(BaseException):
            """مثلِ RerunExceptionِ Streamlit — از Exception ارث نمی‌برد."""

        def on_progress(step: int, status: str) -> None:
            if step == 3 and status == STEP_RUNNING:
                raise Interrupt

        state = _state()
        with pytest.raises(Interrupt):
            run_all(state, on_progress=on_progress)
        assert state.run_in_progress is False
        assert state.running_step == 0
        assert state.last_run.status == RUN_INTERRUPTED
        assert state.last_run.failed_step == 3
        assert state.last_run.completed_steps == (1, 2)
        assert fakes.order == [1, 2]


class TestStaleResults:
    def test_a_new_run_marks_every_old_result_out_of_date_before_it_starts(self, fakes):
        state = _state()
        run_all(state)
        seen: dict[int, bool] = {}

        def on_progress(step: int, status: str) -> None:
            if step == 1 and status == STEP_RUNNING:
                seen.update({later: state.is_stale(later) for later in range(1, 6)})

        run_all(state, on_progress=on_progress)
        assert seen == {1: True, 2: True, 3: True, 4: True, 5: True}

    def test_old_downstream_results_stay_out_of_date_after_a_failed_rerun(self, fakes):
        state = _state()
        run_all(state)
        old_collection = state.result(4)["collection"]

        fakes.errors[3] = RuntimeError("model down")
        outcome = run_all(state)

        assert outcome.failed_step == 3
        assert state.result(4)["collection"] == old_collection  # نتیجه پاک نشد…
        assert state.is_out_of_date(4) and state.is_out_of_date(5)  # …ولی کهنه است
        assert state.status(4) == STATUS_STALE
        assert state.status(5) == STATUS_STALE
        assert state.step_inputs(5) == {
            "step1_result": state.result(1),
            "step2_result": state.result(2),
        }
        assert not state.is_accepted(4) and not state.is_accepted(5)

    def test_identical_regenerated_results_are_current_again(self, fakes):
        state = _state()
        run_all(state)
        run_all(state)
        assert state.stale == set()
        assert state.last_run.succeeded


class TestRunAllFromAStep:
    def test_a_retry_from_the_failed_step_reuses_earlier_results(self, fakes):
        fakes.errors[3] = RuntimeError("model down")
        state = _state()
        run_all(state)
        del fakes.errors[3]
        fakes.order.clear()

        outcome = run_all(state, start=3)
        assert outcome.status == RUN_SUCCEEDED
        assert outcome.completed_steps == (3, 4, 5)
        assert fakes.order == [3, 4, 5]

    def test_a_retry_needs_current_earlier_results(self, fakes):
        state = _state()
        outcome = run_all(state, start=3)
        assert outcome.status == RUN_FAILED
        assert outcome.failed_step == 1
        assert "Start from Step 1" in outcome.error
        assert fakes.order == []

    def test_an_invalid_start_is_rejected(self):
        with pytest.raises(ValueError):
            run_all(_state(), start=6)


# ── مسیرِ دستیِ قدم‌به‌قدم ──────────────────────────────────────────────────

class TestManualPathIsPreserved:
    def test_step_by_step_with_human_approval(self, fakes):
        state = _state(_ADMIN_UI)
        for step in range(1, 6):
            assert state.is_unlocked(step)
            assert execute_step(step, state).ok
            assert state.approve_and_advance(step)
        assert state.approved == {1, 2, 3, 4, 5}
        assert state.auto_advanced == set()
        assert fakes.order == [1, 2, 3, 4, 5]
        assert progress_text(state) == "Step 5 of 5 · 5 of 5 approved"

    def test_a_manual_step_does_not_unlock_the_next_without_approval(self, fakes):
        state = _state()
        execute_step(1, state)
        assert state.is_unlocked(2) is False

    def test_a_manual_rerun_of_step_one_invalidates_automatic_downstream(self, fakes):
        state = _state()
        run_all(state)
        fakes.outputs[1] = {**fakes.outputs[1], "task_summary": "changed"}
        execute_step(1, state)
        assert state.stale == {2, 3, 4, 5}
        assert state.auto_advanced == set()
        assert state.is_unlocked(2) is False


class TestStatusLabels:
    def test_every_new_status_has_a_marker_and_a_label(self):
        for status in (STATUS_RUNNING, STATUS_FAILED, STATUS_BLOCKED, STATUS_AUTO_COMPLETED):
            assert status in STATUS_MARKERS
            assert status in STATUS_LABELS


# ── همان کالکشن و نامِ فایلِ دکمه‌های دانلود ───────────────────────────────

class TestDownloadsArePreserved:
    def test_the_step5_export_is_the_step4_collection_unchanged(self, fakes):
        state = _state()
        run_all(state)
        step4, step5 = state.result(4), state.result(5)
        assert postman_collection(step5) == step4["collection"]
        assert collection_json(postman_collection(step5)) == collection_json(step4["collection"])

    def test_both_downloads_use_the_same_filename(self, fakes):
        state = _state()
        run_all(state)
        step4, step5 = state.result(4), state.result(5)
        assert collection_filename(collection_name(postman_collection(step5))) == (
            collection_filename(step4["collection_name"])
        )
        assert collection_filename(step4["collection_name"]) == (
            "voucher_api_tests.postman_collection.json"
        )


class TestExportGuards:
    """دکمه‌ی دانلودِ قدم ۵ هرگز کالکشنِ مسدود، غایب یا کهنه را عرضه نمی‌کند."""

    @pytest.fixture(autouse=True)
    def _page(self):
        pytest.importorskip("streamlit")
        from ui import step5_final_review

        self.reason = step5_final_review.export_unavailable_reason

    def test_a_successful_run_can_be_exported(self, fakes):
        state = _state()
        run_all(state)
        assert self.reason(state.result(5), state.is_out_of_date(5)) == ""

    def test_a_blocked_review_cannot_be_exported(self, fakes):
        _blocked_review(fakes)
        state = _state()
        run_all(state)
        assert "blocked" in self.reason(state.result(5), state.is_out_of_date(5))

    def test_a_stale_review_cannot_be_exported(self, fakes):
        state = _state()
        run_all(state)
        fakes.errors[4] = RuntimeError("boom")
        run_all(state)
        assert "out of date" in self.reason(state.result(5), state.is_out_of_date(5))

    def test_a_review_without_a_collection_cannot_be_exported(self):
        assert "no Step 4 collection" in self.reason({"review": {"status": "ready"}})
