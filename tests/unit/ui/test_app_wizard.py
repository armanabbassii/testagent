"""
تست‌های لایه‌ی هماهنگ‌کننده — ui/app.py و پوسته‌ی صفحه‌های قدم‌ها

دو چیز اینجا سنجیده می‌شود:

  1. قراردادِ صفحه‌ها: هر قدم باید یک render_step(state) قابلِ فراخوانی داشته
     باشد و import کردنش نباید صفحه را اجرا کند (وگرنه ویزارد پنج صفحه را همزمان
     اجرا می‌کرد و پنج صفحه‌ی مستقل هم دیگر کار نمی‌کردند).
  2. خودِ ویزارد: روی runtime واقعیِ Streamlit (بدونِ مرورگر) اجرا می‌شود تا
     مطمئن شویم بدونِ خطا بالا می‌آید، از قدمِ ۱ شروع می‌کند، قدم‌های بعدی را
     قفل نگه می‌دارد و وضعیتش بین rerun ها می‌ماند.

هیچ‌کدام از این تست‌ها LLM صدا نمی‌زنند و به شبکه دست نمی‌زنند: ویزارد فقط
وقتی اجرا می‌شود که کاربر دکمه‌ی اجرای یک قدم را بزند.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

streamlit = pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from ui import app as wizard  # noqa: E402
from ui.workflow import INPUT_TASK_DESCRIPTION, STEP_COUNT, WorkflowState  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_PATH = _REPO_ROOT / "ui" / "app.py"

_PAGE_MODULES = (
    "ui.step1_task_analysis",
    "ui.step2_api_mapping",
    "ui.step3_scenario_analysis",
    "ui.step4_postman_generation",
    "ui.step5_final_review",
)


class TestPageContract:
    def test_every_step_has_a_page(self):
        assert sorted(wizard.STEP_MODULES) == list(range(1, STEP_COUNT + 1))

    @pytest.mark.parametrize("name", _PAGE_MODULES)
    def test_every_step_page_exposes_the_shared_entry_point(self, name):
        module = importlib.import_module(name)
        assert callable(getattr(module, "render_step", None))

    @pytest.mark.parametrize("name", _PAGE_MODULES)
    def test_every_step_page_keeps_its_standalone_entry_point(self, name):
        module = importlib.import_module(name)
        assert callable(getattr(module, "main", None))

    @pytest.mark.parametrize("name", _PAGE_MODULES)
    def test_importing_a_page_does_not_run_it(self, name, monkeypatch):
        """import کردنِ یک صفحه نباید چیزی رندر کند."""
        module = importlib.import_module(name)
        calls: list = []
        monkeypatch.setattr(
            streamlit, "set_page_config", lambda *args, **kwargs: calls.append(args)
        )

        importlib.reload(module)

        assert calls == []

    def test_importing_the_wizard_does_not_run_it(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            streamlit, "set_page_config", lambda *args, **kwargs: calls.append(args)
        )

        importlib.reload(wizard)

        assert calls == []


class TestWizardRuntime:
    """ویزارد روی runtime واقعیِ Streamlit — بدونِ مرورگر و بدونِ فراخوانیِ LLM."""

    @staticmethod
    def _run():
        return AppTest.from_file(str(_APP_PATH), default_timeout=120).run()

    def test_the_wizard_renders_without_errors(self):
        assert self._run().exception == []

    def test_the_wizard_starts_on_step_one(self):
        state = self._run().session_state["workflow_state"]
        assert isinstance(state, WorkflowState)
        assert state.current_step == 1
        assert state.results == {}
        assert state.completed_count() == 0

    def test_the_wizard_locks_every_later_step(self):
        state = self._run().session_state["workflow_state"]
        assert state.is_unlocked(1) is True
        assert [state.is_unlocked(step) for step in range(2, STEP_COUNT + 1)] == [
            False
        ] * (STEP_COUNT - 1)

    def test_the_wizard_state_survives_a_rerun(self):
        app = self._run()
        app.session_state["workflow_state"].set_input(
            INPUT_TASK_DESCRIPTION, "Implement voucher management endpoints."
        )

        app.run()

        assert app.exception == []
        state = app.session_state["workflow_state"]
        assert state.input_value(INPUT_TASK_DESCRIPTION) == (
            "Implement voucher management endpoints."
        )
        assert state.current_step == 1


class TestRunAllButton:
    """دکمه‌ی Run All روی runtime واقعیِ Streamlit — Generatorها ساختگی‌اند.

    کلیک فقط درخواست را ثبت می‌کند و اجرای بعدیِ اسکریپت همان
    ui.pipeline.run_all را صدا می‌زند؛ هیچ کلیکِ Approve یا Run دیگری لازم نیست.
    """

    _ADMIN_UI = (
        "https://podium-admin.sandpod.ir/api/swagger-ui/index.html"
        "?urls.primaryName=Admin#/voucher-admin-controller/getVoucherDetails"
    )

    @pytest.fixture
    def fakes(self, monkeypatch, tmp_path):
        from src.agents.test_case_generator import swagger_snapshots
        from tests.unit.test_case_generator.test_source_selection import write_snapshots
        from tests.unit.test_case_generator.test_source_semantics import FakeLLM, route_by_task
        from tests.unit.ui.test_pipeline import FakeGenerators

        # انتخابِ منابع snapshotهای ساختگی را می‌خواند، نه data/swagger/، و تحلیلِ
        # معناییِ تسک LLMِ ساختگی را صدا می‌زند.
        monkeypatch.setattr(swagger_snapshots, "SNAPSHOT_DIR", write_snapshots(tmp_path))
        FakeLLM(monkeypatch, router=route_by_task)
        return FakeGenerators(monkeypatch)

    def _click_run_all(self, services: str = "") -> AppTest:
        app = AppTest.from_file(str(_APP_PATH), default_timeout=120).run()
        app.text_area[0].input("Implement voucher management endpoints.")
        app.text_area[1].input(services)
        run_all = next(button for button in app.button if button.label.startswith("Run All"))
        run_all.click()
        return app.run()

    def test_one_click_runs_all_five_steps(self, fakes):
        app = self._click_run_all(self._ADMIN_UI)

        assert app.exception == []
        state = app.session_state["workflow_state"]
        assert fakes.order == [1, 2, 3, 4, 5]
        assert state.last_run.succeeded
        assert state.current_step == STEP_COUNT
        assert state.approved == set()
        assert state.run_in_progress is False
        assert fakes.last(2)["snapshot_keys"] == ["admin"]
        assert any("Run All finished" in message.value for message in app.success)

    def test_the_final_step_offers_the_existing_download(self, fakes):
        app = self._click_run_all()

        assert app.exception == []
        downloads = [
            element
            for element in app.get("download_button")
            if "Download collection JSON" in str(element.proto)
        ]
        assert len(downloads) == 1
        assert downloads[0].proto.disabled is False

    def test_a_failure_shows_the_failed_step_and_its_error(self, fakes):
        fakes.errors[3] = RuntimeError("model unavailable")
        app = self._click_run_all()

        assert app.exception == []
        state = app.session_state["workflow_state"]
        assert state.last_run.failed_step == 3
        assert state.current_step == 3
        assert fakes.order == [1, 2, 3]
        errors = " ".join(message.value for message in app.error)
        assert "Run All stopped at Step 3" in errors
        assert "model unavailable" in errors

    def test_the_outcome_names_the_automatically_selected_sources(self, fakes):
        app = self._click_run_all("GET /crm/digitalService/requests")

        assert app.exception == []
        assert fakes.last(2)["snapshot_keys"] == ["crm"]
        captions = " ".join(element.value for element in app.caption)
        assert "Swagger sources used in Step 2: CRM (endpoint path) — automatic" in captions

    def test_step_two_shows_the_automatic_selection_after_run_all(self, fakes):
        app = self._click_run_all("GET /crm/digitalService/requests")
        next(button for button in app.button if button.label.endswith("Step 2")).click()
        app.run()

        assert app.exception == []
        assert app.session_state["workflow_state"].current_step == 2
        assert app.radio(key="workflow_swagger_selection").value == "Automatic (recommended)"
        table = app.dataframe[0].value
        selected = table[table["Selected"] == "✅"]["Source"].tolist()
        assert selected == ["CRM"]

    def test_an_ambiguous_task_stops_run_all_at_step_two(self, fakes):
        app = AppTest.from_file(str(_APP_PATH), default_timeout=120).run()
        app.text_area[0].input("Fix the bug reported yesterday.")
        next(button for button in app.button if button.label.startswith("Run All")).click()
        app.run()

        assert app.exception == []
        assert fakes.order == [1]
        errors = " ".join(message.value for message in app.error)
        assert "Run All stopped at Step 2" in errors
        assert "could not be determined" in errors

    def test_an_empty_task_description_does_not_start_a_run(self, fakes):
        app = AppTest.from_file(str(_APP_PATH), default_timeout=120).run()
        next(button for button in app.button if button.label.startswith("Run All")).click()
        app.run()

        assert fakes.order == []
        assert app.session_state["workflow_state"].last_run is None
        assert any("Task Description is required." in message.value for message in app.error)


class TestJiraInput:
    """ورودیِ Jira روی runtime واقعیِ Streamlit — Jira و Generatorها ساختگی‌اند."""

    @pytest.fixture
    def fakes(self, monkeypatch, tmp_path):
        from src.agents.test_case_generator import swagger_snapshots
        from src.task_sources.jira import JiraSettings
        from tests.unit.task_sources.fake_jira import adfs_settings
        from tests.unit.test_case_generator.test_source_selection import write_snapshots
        from tests.unit.test_case_generator.test_source_semantics import FakeLLM, route_by_task
        from tests.unit.ui.test_jira_input import FakeImporter
        from tests.unit.ui.test_pipeline import FakeGenerators
        from ui import pipeline

        monkeypatch.setattr(swagger_snapshots, "SNAPSHOT_DIR", write_snapshots(tmp_path))
        FakeLLM(monkeypatch, router=route_by_task)
        self.importer = FakeImporter()
        monkeypatch.setattr(pipeline, "import_issue", self.importer)
        monkeypatch.setattr(JiraSettings, "from_config", classmethod(lambda cls: adfs_settings()))
        monkeypatch.setattr(pipeline, "_jira_clients", {})
        return FakeGenerators(monkeypatch)

    @staticmethod
    def _fetch(reference: str = "DEMO-7") -> AppTest:
        app = AppTest.from_file(str(_APP_PATH), default_timeout=120).run()
        app.segmented_control(key="workflow_task_source").set_value("Jira issue").run()
        app.text_input(key="workflow_jira_reference").input(reference).run()
        next(button for button in app.button if button.label == "Fetch issue").click().run()
        return app

    @staticmethod
    def _click(app: AppTest, prefix: str) -> AppTest:
        next(button for button in app.button if button.label.startswith(prefix)).click()
        return app.run()

    def test_manual_mode_is_the_default_and_has_no_jira_field(self, fakes):
        app = AppTest.from_file(str(_APP_PATH), default_timeout=120).run()
        assert app.segmented_control(key="workflow_task_source").value == "Manual description"
        assert not any(field.key == "workflow_jira_reference" for field in app.text_input)

    def test_fetching_fills_the_task_description_and_runs_nothing(self, fakes):
        app = self._fetch("https://jira.test/browse/DEMO-7")

        assert app.exception == []
        assert app.text_area[0].value.startswith("# Task DEMO-7: Voucher details for admins")
        successes = " ".join(message.value for message in app.success)
        assert "Signed in to Jira as Test User" in successes
        assert "Issue DEMO-7 retrieved" in successes
        assert fakes.order == []

    def test_run_all_uses_the_fetched_issue(self, fakes):
        app = self._click(self._fetch(), "Run All")

        assert app.exception == []
        assert fakes.order == [1, 2, 3, 4, 5]
        assert fakes.last(1)["task_description"].startswith("# Task DEMO-7:")
        assert app.session_state["workflow_state"].input_value("jira_issue") == "DEMO-7"

    def test_a_changed_key_blocks_running_until_it_is_fetched(self, fakes):
        app = self._fetch()
        app.text_input(key="workflow_jira_reference").input("DEMO-8").run()
        assert any("Fetch DEMO-8 first" in message.value for message in app.warning)

        app = self._click(app, "Run Step 1")

        assert fakes.order == []
        assert any("Fetch DEMO-8 first" in message.value for message in app.error)

    def test_a_failed_fetch_shows_the_stage_and_keeps_the_description(self, fakes):
        from src.task_sources.jira import JiraImport

        self.importer.results["DEMO-7"] = JiraImport(reference="DEMO-7").fail(
            "authentication", RuntimeError("ADFS rejected the sign-in")
        )
        app = self._fetch()

        assert app.exception == []
        assert any("Jira sign-in failed" in message.value for message in app.error)
        assert app.text_area[0].value == ""


def _walk(node):
    for child in getattr(node, "children", {}).values():
        yield child
        yield from _walk(child)


def _button_rows(node) -> list[list]:
    """دکمه‌ها، گروه‌بندی‌شده بر اساسِ ظرفی که مستقیم نگهشان می‌دارد."""
    rows = []
    for element in [node, *_walk(node)]:
        buttons = [
            child
            for child in getattr(element, "children", {}).values()
            if getattr(child, "type", "") == "button"
        ]
        if buttons:
            rows.append(buttons)
    return rows


def _step_result(summary: str) -> dict:
    return {
        "task_summary": summary,
        "identified_requirements": [],
        "clarifications": [],
        "test_cases": [],
    }


class TestDashboardLayout:
    """چیدمانِ تازه‌ی ویزارد — همان قواعدِ جریان، نمایشِ تازه."""

    @staticmethod
    def _run(state: WorkflowState | None = None) -> AppTest:
        app = AppTest.from_file(str(_APP_PATH), default_timeout=120)
        if state is not None:
            app.session_state["workflow_state"] = state
        return app.run()

    def test_run_step_one_and_run_all_share_one_row_with_run_all_primary(self):
        app = self._run()

        rows = [[button.label for button in row] for row in _button_rows(app.main)]
        assert ["Run Step 1 — Task Analysis", "Run All — Steps 1 to 5"] in rows
        buttons = {button.label: button for button in app.button}
        assert buttons["Run All — Steps 1 to 5"].proto.type == "primary"
        assert buttons["Run Step 1 — Task Analysis"].proto.type == "secondary"

    def test_run_step_one_still_runs_only_step_one(self, monkeypatch, tmp_path):
        from src.agents.test_case_generator import swagger_snapshots
        from tests.unit.test_case_generator.test_source_selection import write_snapshots
        from tests.unit.ui.test_pipeline import FakeGenerators

        monkeypatch.setattr(swagger_snapshots, "SNAPSHOT_DIR", write_snapshots(tmp_path))
        fakes = FakeGenerators(monkeypatch)
        app = self._run()
        app.text_area[0].input("Implement voucher management endpoints.")
        next(button for button in app.button if button.label.startswith("Run Step 1")).click()
        app.run()

        assert app.exception == []
        assert fakes.order == [1]
        state = app.session_state["workflow_state"]
        assert state.has_result(1)
        assert state.last_run is None

    def test_the_stepper_shows_every_step_and_keeps_locked_steps_closed(self):
        app = self._run()

        steps = [button for button in app.main.button if button.label.startswith("Step ")]
        assert [button.label for button in steps] == [f"Step {n}" for n in range(1, 6)]
        assert [button.disabled for button in steps] == [False, True, True, True, True]
        assert steps[0].proto.type == "primary"
        badges = [element.value for element in app.main.markdown if "-badge[" in element.value]
        assert sum("locked" in badge for badge in badges) == 4

    def test_an_outdated_step_is_marked_out_of_date(self):
        state = WorkflowState()
        state.record_result(1, _step_result("first"))
        state.approve(1)
        state.record_result(2, {"mappings": []})
        state.record_result(1, _step_result("second"))

        app = self._run(state)

        assert app.exception == []
        badges = [element.value for element in app.main.markdown if "-badge[" in element.value]
        assert any("orange-badge" in badge and "out of date" in badge for badge in badges)

    def test_the_sidebar_navigation_follows_the_same_locks(self):
        app = self._run()

        nav = [button for button in app.sidebar.button if button.key.startswith("workflow_sidebar")]
        assert [button.disabled for button in nav] == [False, True, True, True, True]
        assert nav[0].proto.type == "primary"

    def test_navigation_and_run_all_are_disabled_while_a_run_is_in_progress(self):
        state = WorkflowState()
        state.run_in_progress = True

        app = self._run(state)

        steps = [button for button in app.main.button if button.label.startswith("Step ")]
        assert all(button.disabled for button in steps)
        run_all = next(button for button in app.button if button.label.startswith("Run All"))
        assert run_all.disabled

    def test_every_workflow_status_has_an_icon_and_a_colour(self):
        from ui.components import STATUS_COLORS, STATUS_ICONS
        from ui.workflow import STATUS_LABELS

        assert set(STATUS_ICONS) == set(STATUS_LABELS)
        assert set(STATUS_COLORS) == set(STATUS_LABELS)


class TestExecutionButtonColours:
    """دکمه‌های اجرا رنگِ خودشان را دارند؛ تأیید و نشانه‌های وضعیت همان رنگِ تم."""

    def test_the_run_buttons_carry_the_styled_keys(self):
        app = AppTest.from_file(str(_APP_PATH), default_timeout=120).run()

        keys = {button.label: button.key for button in app.button}
        assert keys["Run Step 1 — Task Analysis"] == "workflow_run_1"
        assert keys["Run All — Steps 1 to 5"] == "workflow_run_all"

    def test_the_page_loads_the_stylesheet_once(self):
        from ui.components import execution_button_css

        app = AppTest.from_file(str(_APP_PATH), default_timeout=120).run()

        styles = [element for element in app.get("html") if "workflow_run_all" in str(element.proto)]
        assert len(styles) == 1
        assert execution_button_css(5).strip() in styles[0].proto.body.strip()

    def test_run_step_is_blue_and_run_all_is_violet(self):
        from ui.components import execution_button_css

        css = execution_button_css(5)
        run_step, run_all = css.split(".st-key-workflow_run_all", 1)
        for step in range(1, 6):
            assert f".st-key-workflow_run_{step} button:not(:disabled)" in css
        assert "#3B82F6" in run_step and "#7C3AED" not in run_step
        assert "#7C3AED" in run_all

    def test_only_execution_buttons_are_targeted(self):
        from ui.components import execution_button_css

        css = execution_button_css(5)
        targeted = set(re.findall(r"\.st-key-([\w-]+)", css))
        assert targeted == {f"workflow_run_{step}" for step in range(1, 6)} | {"workflow_run_all"}
        # تأیید (سبز/فیروزه‌ای) و حالتِ غیرفعال دست نمی‌خورند.
        assert "workflow_approve" not in css
        assert css.count("button:not(:disabled)") == css.count("button")

    def test_approve_stays_on_the_theme_colour(self):
        state = WorkflowState()
        state.record_result(1, _step_result("done"))
        app = AppTest.from_file(str(_APP_PATH), default_timeout=120)
        app.session_state["workflow_state"] = state
        app.run()

        approve = next(button for button in app.button if button.key == "workflow_approve_1")
        assert approve.proto.type == "primary"
