"""
تست‌های مصرفِ توکن در ویزارد — ui/workflow.py و بخشِ سایدبار

سه چیز سنجیده می‌شود:

  1. هر مصرف به قدمِ درست می‌چسبد و چند فراخوانیِ یک اجرا جمع می‌شوند.
  2. اجرای دوباره مصرفِ قبلی را جایگزین می‌کند (دوبار شمرده نمی‌شود) و
     اجرای ناموفق مصرفِ انجام‌شده را نگه می‌دارد.
  3. جدولِ سایدبار حالت‌های «اجرا نشده»، «بدونِ فراخوانی»، «گزارش‌نشده»،
     «موفق» و «ناموفق» را صادقانه نشان می‌دهد — نه صفرِ گمراه‌کننده.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.llm_client import LLMResponse
from src.token_usage import TokenUsage, report_usage
from ui.workflow import (
    USAGE_NO_CALLS,
    USAGE_NOT_AVAILABLE,
    USAGE_NOT_REPORTED,
    WorkflowState,
    has_cached_usage,
    usage_rows,
    usage_totals,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_PATH = _REPO_ROOT / "ui" / "app.py"


def _response(
    prompt: int | None = None,
    completion: int | None = None,
    total: int | None = None,
    cached: int | None = None,
) -> LLMResponse:
    return LLMResponse(
        "{}",
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_tokens=cached,
    )


class TestUsageIsAttachedToSteps:
    def test_a_call_is_recorded_for_the_tracked_step_only(self):
        state = WorkflowState()

        with state.track_usage(1):
            report_usage(_response(100, 10, 110, 4))

        assert state.usage_for(1) == TokenUsage(1, 100, 10, 110, 4)
        assert state.usage_for(2) is None

    def test_multiple_calls_in_one_run_are_aggregated(self):
        state = WorkflowState()

        with state.track_usage(2):
            report_usage(_response(100, 10, 110))
            report_usage(_response(200, 20, 220))
            report_usage(_response(300, 30, 330))

        assert state.usage_for(2) == TokenUsage(3, 600, 60, 660)

    def test_steps_keep_their_usage_separate(self):
        state = WorkflowState()

        with state.track_usage(1):
            report_usage(_response(100, 10, 110))
        with state.track_usage(2):
            report_usage(_response(200, 20, 220))

        assert state.usage_for(1) == TokenUsage(1, 100, 10, 110)
        assert state.usage_for(2) == TokenUsage(1, 200, 20, 220)
        assert usage_totals(state) == TokenUsage(2, 300, 30, 330)

    def test_a_failed_run_keeps_what_it_already_consumed(self):
        state = WorkflowState()

        with pytest.raises(RuntimeError):
            with state.track_usage(2):
                report_usage(_response(100, 10, 110))
                raise RuntimeError("provider died after the call")
        state.record_error(2, "API mapping failed")

        assert state.usage_for(2) == TokenUsage(1, 100, 10, 110)

    def test_rerunning_a_step_replaces_its_previous_usage(self):
        state = WorkflowState()

        with state.track_usage(1):
            report_usage(_response(100, 10, 110))
        with state.track_usage(1):
            report_usage(_response(50, 5, 55))

        assert state.usage_for(1) == TokenUsage(1, 50, 5, 55)

    def test_a_rerun_that_makes_no_call_does_not_keep_the_old_numbers(self):
        state = WorkflowState()

        with state.track_usage(1):
            report_usage(_response(100, 10, 110))
        with state.track_usage(1):
            pass  # اجرای تازه پیش از رسیدن به LLM شکست خورد

        assert state.usage_for(1) is None

    def test_resetting_the_workflow_clears_usage(self):
        state = WorkflowState()
        with state.track_usage(1):
            report_usage(_response(100, 10, 110))
        state.record_result(1, {"ok": True})

        state.reset()

        assert state.usage == {}
        assert usage_totals(state) is None


class TestUsageRows:
    def test_an_unexecuted_step_is_pending_not_zero(self):
        rows = usage_rows(WorkflowState())

        assert [row["Step"] for row in rows] == [
            "Step 1",
            "Step 2",
            "Step 3",
            "Step 4",
            "Step 5",
            "All steps",
        ]
        assert rows[0]["Prompt"] == USAGE_NOT_AVAILABLE
        assert rows[0]["Completion"] == USAGE_NOT_AVAILABLE
        assert rows[0]["Total"] == USAGE_NOT_AVAILABLE
        assert rows[5]["Total"] == USAGE_NOT_AVAILABLE
        assert "Cached" not in rows[0]

    def test_a_completed_step_shows_formatted_numbers(self):
        state = WorkflowState()
        with state.track_usage(1):
            report_usage(_response(33142, 7382, 40524, 3592))
        state.record_result(1, {"ok": True})

        rows = usage_rows(state)

        assert rows[0] == {
            "Step": "Step 1",
            "Prompt": "33,142",
            "Completion": "7,382",
            "Total": "40,524",
            "Cached": "3,592",
        }
        assert rows[5]["Step"] == "All steps"
        assert rows[5]["Prompt"] == "33,142"
        assert rows[5]["Completion"] == "7,382"
        assert rows[5]["Total"] == "40,524"
        assert rows[5]["Cached"] == "3,592"
        assert has_cached_usage(state) is True

    def test_a_failed_step_is_marked_and_keeps_its_usage(self):
        state = WorkflowState()
        with state.track_usage(2):
            report_usage(_response(1200, 240, 1440))
        state.record_error(2, "API mapping failed")

        rows = usage_rows(state)

        assert rows[1]["Step"] == "Step 2 ⚠"
        assert rows[1]["Prompt"] == "1,200"
        assert rows[1]["Completion"] == "240"
        assert rows[1]["Total"] == "1,440"

    def test_an_executed_step_without_model_calls_says_so(self):
        state = WorkflowState()
        state.record_result(4, {"collection": {}})

        rows = usage_rows(state)

        assert rows[3]["Step"] == "Step 4"
        assert rows[3]["Prompt"] == USAGE_NOT_AVAILABLE
        assert rows[3]["Total"] == USAGE_NO_CALLS

    def test_a_call_without_usage_metadata_says_not_reported(self):
        state = WorkflowState()
        with state.track_usage(3):
            report_usage(_response())

        rows = usage_rows(state)

        assert rows[2]["Prompt"] == USAGE_NOT_AVAILABLE
        assert rows[2]["Completion"] == USAGE_NOT_AVAILABLE
        assert rows[2]["Total"] == USAGE_NOT_REPORTED

    def test_the_cumulative_row_sums_every_step(self):
        state = WorkflowState()
        with state.track_usage(1):
            report_usage(_response(100, 10, 110))
        with state.track_usage(3):
            report_usage(_response(200, 20, 220))

        rows = usage_rows(state)

        assert rows[5] == {
            "Step": "All steps",
            "Prompt": "300",
            "Completion": "30",
            "Total": "330",
        }

    def test_cached_is_absent_until_a_provider_reports_it(self):
        state = WorkflowState()
        with state.track_usage(1):
            report_usage(_response(100, 10, 110))

        assert "Cached" not in usage_rows(state)[0]


class TestSidebarRendering:
    """سایدبارِ واقعیِ ویزارد — بدونِ فراخوانیِ LLM و بدونِ مرورگر."""

    @staticmethod
    def _run():
        pytest.importorskip("streamlit")
        from streamlit.testing.v1 import AppTest

        return AppTest.from_file(str(_APP_PATH), default_timeout=120).run()

    def test_the_sidebar_shows_the_usage_section_and_table(self):
        app = self._run()

        assert app.exception == []
        assert [header.value for header in app.sidebar.subheader] == ["Workflow"]
        # مصرفِ توکن و بازنشانی بخش‌های جمع‌شونده‌ی سایدبارند.
        # AppTest expanders را با نوعِ «status» (Expandable) می‌شناسد.
        assert [section.label for section in app.sidebar.status] == [
            "LLM token usage",
            "Reset workflow",
        ]
        assert len(app.sidebar.dataframe) == 1

        table = app.sidebar.dataframe[0].value
        assert list(table.columns) == ["Step", "Prompt", "Completion", "Total"]
        assert table.iloc[0]["Step"] == "Step 1"
        assert table.iloc[0]["Total"] == USAGE_NOT_AVAILABLE
        assert table.iloc[-1]["Step"] == "All steps"

    def test_the_sidebar_table_reads_the_workflow_state(self):
        pytest.importorskip("streamlit")
        from streamlit.testing.v1 import AppTest

        state = WorkflowState()
        with state.track_usage(1):
            report_usage(_response(33142, 7382, 40524, 3592))
        state.record_result(
            1,
            {
                "task_summary": "Voucher management.",
                "identified_requirements": [],
                "clarifications": [],
                "test_cases": [],
            },
        )

        app = AppTest.from_file(str(_APP_PATH), default_timeout=120)
        app.session_state["workflow_state"] = state
        app.run()

        assert app.exception == []
        table = app.sidebar.dataframe[0].value
        assert list(table.columns) == [
            "Step",
            "Prompt",
            "Completion",
            "Total",
            "Cached",
        ]
        assert table.iloc[0]["Prompt"] == "33,142"
        assert table.iloc[0]["Cached"] == "3,592"
        assert table.iloc[-1]["Total"] == "40,524"
        # جمعِ کل در عنوانِ بخشِ جمع‌شده هم دیده می‌شود.
        assert app.sidebar.status[0].label == "LLM token usage · 40,524 tokens"
