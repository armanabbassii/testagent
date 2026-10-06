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
