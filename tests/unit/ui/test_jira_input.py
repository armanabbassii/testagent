"""
تست‌های ورودیِ Jira در ویزارد — ui/pipeline.py و ui/formatting.py، بدونِ Streamlit.

دریافتِ issue ساختگی است (هیچ Jira یا اعتبارنامه‌ی واقعی‌ای لازم نیست)؛ قدم‌ها
همان Generatorهای ساختگیِ test_pipeline اند.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.test_case_generator import swagger_snapshots
from src.task_sources import (
    ATTACHMENT_FAILED,
    ATTACHMENT_PROCESSED,
    ATTACHMENT_UNSUPPORTED,
    TaskAttachment,
    TaskComment,
    TaskInput,
)
from src.task_sources.jira import JiraImport, JiraSettings
from src.task_sources.jira.importer import STAGE_ISSUE
from tests.unit.task_sources.fake_jira import adfs_settings
from tests.unit.test_case_generator.test_source_selection import write_snapshots
from tests.unit.test_case_generator.test_source_semantics import FakeLLM, route_by_task
from tests.unit.ui.test_pipeline import FakeGenerators
from ui import pipeline
from ui.formatting import (
    MESSAGE_ERROR,
    MESSAGE_INFO,
    MESSAGE_SUCCESS,
    MESSAGE_WARNING,
    jira_attachment_rows,
    jira_status_messages,
)
from ui.pipeline import RUN_SUCCEEDED, execute_step, jira_input_problem, load_jira_task, run_all
from ui.workflow import (
    INPUT_DEVELOPED_SERVICES,
    INPUT_JIRA_ISSUE,
    INPUT_TASK_DESCRIPTION,
    WorkflowState,
)


def _task(key: str = "DEMO-7", title: str = "Voucher details for admins") -> TaskInput:
    return TaskInput(
        title=title,
        description="Admins can see voucher details.",
        comments=(TaskComment("Dev", "2026-01-01 10:00 +03:30", "GET /admin/voucher/{id}"),),
        attachments=(
            TaskAttachment("curl.txt", "text/plain", 30, ATTACHMENT_PROCESSED, "curl -X GET x"),
            TaskAttachment("shot.png", "image/png", 9, ATTACHMENT_UNSUPPORTED, detail="no"),
            TaskAttachment("bad.txt", "text/plain", 5, ATTACHMENT_FAILED, detail="not text"),
        ),
        source="jira",
        source_reference=key,
        source_url=f"https://jira.test/browse/{key}",
    )


def _success(reference: str, key: str = "DEMO-7", **task) -> JiraImport:
    return JiraImport(
        reference=reference,
        issue_key=key,
        task_input=_task(key, **task),
        auth_method="ADFS (SAML 2.0, forms authentication)",
        identity="Test User",
        authenticated=True,
        issue_found=True,
        comments_retrieved=True,
        warnings=["Attachment shot.png: unsupported file type — no"],
    )


class FakeImporter:
    """جای import_issue — هر فراخوانی (کلید، کلاینت) ثبت می‌شود."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.results: dict[str, JiraImport] = {}

    def __call__(self, reference, *, settings, client):
        self.calls.append((reference, client))
        key = reference.rsplit("/", 1)[-1].upper()
        return self.results.get(key) or _success(reference, key)


@pytest.fixture
def importer(monkeypatch: pytest.MonkeyPatch) -> FakeImporter:
    fake = FakeImporter()
    monkeypatch.setattr(pipeline, "import_issue", fake)
    monkeypatch.setattr(JiraSettings, "from_config", classmethod(lambda cls: adfs_settings()))
    monkeypatch.setattr(pipeline, "_jira_clients", {})
    return fake


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeGenerators:
    monkeypatch.setattr(swagger_snapshots, "SNAPSHOT_DIR", write_snapshots(tmp_path))
    FakeLLM(monkeypatch, router=route_by_task)
    return FakeGenerators(monkeypatch)


class TestLoadJiraTask:
    def test_a_fetched_issue_becomes_the_step_one_task_description(self, importer):
        state = WorkflowState()

        result = load_jira_task(state, "https://jira.test/browse/DEMO-7")

        assert result.ok
        assert state.input_value(INPUT_TASK_DESCRIPTION) == _task().to_task_description()
        assert state.input_value(INPUT_JIRA_ISSUE) == "DEMO-7"
        assert state.task_import is result

    def test_fetching_never_runs_a_step(self, importer, fakes):
        state = WorkflowState()
        load_jira_task(state, "DEMO-7")
        assert fakes.order == []
        assert state.results == {}

    def test_a_failed_fetch_leaves_the_state_untouched(self, importer):
        state = WorkflowState()
        state.set_input(INPUT_TASK_DESCRIPTION, "manual text")
        importer.results["DEMO-9"] = JiraImport(
            reference="DEMO-9", issue_key="DEMO-9", authenticated=True
        ).fail(STAGE_ISSUE, RuntimeError("HTTP 403"))

        result = load_jira_task(state, "DEMO-9")

        assert not result.ok
        assert state.input_value(INPUT_TASK_DESCRIPTION) == "manual text"
        assert state.input_value(INPUT_JIRA_ISSUE) == ""
        assert state.task_import is None

    def test_missing_configuration_is_reported_without_a_request(self, importer, monkeypatch):
        monkeypatch.setattr(
            JiraSettings, "from_config", classmethod(lambda cls: JiraSettings(base_url=""))
        )

        result = load_jira_task(WorkflowState(), "DEMO-7")

        assert result.error_stage == "configuration"
        assert "JIRA_BASE_URL" in result.error
        assert importer.calls == []

    def test_the_signed_in_client_is_reused(self, importer):
        load_jira_task(WorkflowState(), "DEMO-7")
        load_jira_task(WorkflowState(), "DEMO-8")
        assert importer.calls[0][1] is importer.calls[1][1]

    def test_new_settings_get_a_new_client(self, importer, monkeypatch):
        load_jira_task(WorkflowState(), "DEMO-7")
        monkeypatch.setattr(
            JiraSettings, "from_config", classmethod(lambda cls: adfs_settings(username="other"))
        )
        load_jira_task(WorkflowState(), "DEMO-7")
        assert importer.calls[0][1] is not importer.calls[1][1]


class TestStaleJiraInput:
    def test_nothing_fetched_yet(self):
        assert jira_input_problem(WorkflowState(), "DEMO-7") == (
            "Fetch DEMO-7 from Jira before running Step 1."
        )

    def test_an_invalid_reference(self):
        assert "not a Jira issue key" in jira_input_problem(WorkflowState(), "nope")

    def test_the_fetched_issue_matches_key_or_url(self, importer):
        state = WorkflowState()
        load_jira_task(state, "DEMO-7")
        assert jira_input_problem(state, "demo-7") == ""
        assert jira_input_problem(state, "https://jira.test/browse/DEMO-7") == ""

    def test_a_changed_key_blocks_the_old_content(self, importer):
        state = WorkflowState()
        load_jira_task(state, "DEMO-7")
        problem = jira_input_problem(state, "DEMO-8")
        assert "holds DEMO-7" in problem
        assert "Fetch DEMO-8 first" in problem

    def test_a_new_issue_makes_the_step_one_result_out_of_date(self, importer, fakes):
        state = WorkflowState()
        load_jira_task(state, "DEMO-7")
        assert execute_step(1, state).ok

        load_jira_task(state, "DEMO-8")

        assert state.is_out_of_date(1)
        assert state.pending_input_changes(1) == ["Task Description", "Jira issue"]

    def test_a_different_issue_with_identical_text_is_still_a_change(self, importer, fakes):
        state = WorkflowState()
        load_jira_task(state, "DEMO-7")
        execute_step(1, state)
        state.set_input(INPUT_JIRA_ISSUE, "DEMO-8")
        assert state.pending_input_changes(1) == ["Jira issue"]

    def test_refetching_the_same_issue_keeps_the_result_current(self, importer, fakes):
        state = WorkflowState()
        load_jira_task(state, "DEMO-7")
        execute_step(1, state)
        load_jira_task(state, "DEMO-7")
        assert not state.is_out_of_date(1)

    def test_reset_forgets_the_import(self, importer):
        state = WorkflowState()
        load_jira_task(state, "DEMO-7")
        state.reset()
        assert state.task_import is None
        assert state.input_value(INPUT_JIRA_ISSUE) == ""


class TestPipelineCompatibility:
    def test_step_one_receives_the_structured_jira_text(self, importer, fakes):
        state = WorkflowState()
        load_jira_task(state, "DEMO-7")

        execute_step(1, state)

        received = fakes.last(1)["task_description"]
        assert received.startswith("# Task DEMO-7: Voucher details for admins")
        assert "## Description (original requirement)" in received
        assert "### Attachment: curl.txt" in received

    def test_run_all_runs_every_step_from_jira_input(self, importer, fakes):
        state = WorkflowState()
        load_jira_task(state, "DEMO-7")
        state.set_input(INPUT_DEVELOPED_SERVICES, "")

        outcome = run_all(state)

        assert outcome.status == RUN_SUCCEEDED
        assert fakes.order == [1, 2, 3, 4, 5]
        assert fakes.last(2)["snapshot_keys"] == ["admin"]

    def test_the_manual_workflow_does_not_touch_jira(self, importer, fakes):
        state = WorkflowState()
        state.set_input(INPUT_TASK_DESCRIPTION, "Admins can see voucher details.")

        assert run_all(state).status == RUN_SUCCEEDED
        assert importer.calls == []
        assert state.input_value(INPUT_JIRA_ISSUE) == ""


class TestStatusFormatting:
    def test_a_successful_import(self):
        messages = jira_status_messages(_success("DEMO-7"))

        assert messages[0] == (
            MESSAGE_SUCCESS,
            "Signed in to Jira as Test User (ADFS (SAML 2.0, forms authentication)).",
        )
        assert messages[1] == (MESSAGE_SUCCESS, "Issue DEMO-7 retrieved: Voucher details for admins")
        assert messages[2] == (
            MESSAGE_INFO,
            "1 comment(s) retrieved · 3 attachment(s) found · 1 processed · 1 unsupported · "
            "1 failed or too large",
        )
        assert messages[3][0] == MESSAGE_WARNING

    def test_a_permission_failure_still_shows_the_successful_sign_in(self):
        result = JiraImport(
            reference="DEMO-9", issue_key="DEMO-9", identity="Test User", authenticated=True,
            auth_method="ADFS",
        ).fail(STAGE_ISSUE, RuntimeError("HTTP 403"))

        assert jira_status_messages(result) == [
            (MESSAGE_SUCCESS, "Signed in to Jira as Test User (ADFS)."),
            (MESSAGE_ERROR, "Issue not accessible: HTTP 403"),
        ]

    def test_an_authentication_failure(self):
        result = JiraImport(reference="DEMO-9").fail("authentication", RuntimeError("rejected"))
        assert jira_status_messages(result) == [(MESSAGE_ERROR, "Jira sign-in failed: rejected")]

    def test_attachment_rows(self):
        rows = jira_attachment_rows(_task())
        assert [row["Status"] for row in rows] == ["processed", "unsupported file type", "failed"]
        assert rows[0]["Extracted"] == "13 chars"
        assert rows[1]["Extracted"] == "—"
        assert rows[2]["Detail"] == "not text"
