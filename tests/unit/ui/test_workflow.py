"""
تست‌های واحدِ دفترِ وضعیتِ ویزارد — ui/workflow.py

این ماژول هیچ وابستگی‌ای به Streamlit ندارد، پس همه‌ی قواعدِ ویزارد (قفلِ قدم‌ها،
انتقالِ خودکارِ نتیجه‌ها، ناوبریِ برگشتی، کهنه‌شدنِ نتایجِ پایین‌دستی، خطا و
بازنشانی) بدونِ اجرای UI تست می‌شوند.

تست‌ها هیچ منطقِ کسب‌وکاری‌ای را دوباره پیاده نمی‌کنند: فقط همان قراردادی را
می‌سنجند که ویزارد به کاربر نشان می‌دهد.
"""

from __future__ import annotations

from ui.workflow import (
    INPUT_COLLECTION_NAME,
    INPUT_SWAGGER_SOURCES,
    INPUT_TASK_DESCRIPTION,
    RESULT_ARGUMENT,
    STATUS_AVAILABLE,
    STATUS_COMPLETED,
    STATUS_CURRENT,
    STATUS_LABELS,
    STATUS_LOCKED,
    STATUS_MARKERS,
    STATUS_STALE,
    STEP_COUNT,
    STEP_ICONS,
    STEP_TITLES,
    WorkflowState,
    approve_label,
    error_retry_message,
    invalidated_message,
    progress_text,
    propagated_steps,
    run_label,
    stale_message,
)

_TASK = "Implement voucher management endpoints."
_SWAGGER = "https://example.test/swagger.json"


def _result(step: int, marker: str = "a") -> dict:
    """نتیجه‌ی ساختگیِ یک قدم — شکلِ واقعی اینجا مهم نیست، هویتِ آن مهم است."""
    return {"step": step, "marker": marker}


def _completed_through(last: int) -> WorkflowState:
    """جریانی که قدم‌های ۱ تا last را اجرا و تأیید کرده است."""
    state = WorkflowState()
    state.set_input(INPUT_TASK_DESCRIPTION, _TASK)
    state.set_input(INPUT_SWAGGER_SOURCES, _SWAGGER)
    for step in range(1, last + 1):
        state.record_result(step, _result(step))
        state.approve_and_advance(step)
    return state


# ── وضعیتِ آغازین ────────────────────────────────────────────────────────────

class TestInitialState:
    def test_a_new_workflow_starts_on_step_one(self):
        assert WorkflowState().current_step == 1

    def test_a_new_workflow_has_no_results(self):
        state = WorkflowState()
        assert [state.has_result(step) for step in range(1, STEP_COUNT + 1)] == [
            False
        ] * STEP_COUNT

    def test_only_step_one_is_unlocked(self):
        state = WorkflowState()
        assert state.is_unlocked(1) is True
        assert [state.is_unlocked(step) for step in range(2, STEP_COUNT + 1)] == [
            False
        ] * (STEP_COUNT - 1)

    def test_the_first_step_is_the_current_one(self):
        assert WorkflowState().status(1) == STATUS_CURRENT

    def test_later_steps_report_locked(self):
        state = WorkflowState()
        assert [state.status(step) for step in range(2, STEP_COUNT + 1)] == [
            STATUS_LOCKED
        ] * (STEP_COUNT - 1)

    def test_nothing_is_approved_yet(self):
        assert WorkflowState().completed_count() == 0

    def test_no_step_can_run_before_its_input_exists(self):
        state = WorkflowState()
        assert state.can_run(1) is False  # توصیفِ تسک خالی است
        assert state.can_run(2) is False  # نه Swagger، نه نتیجه‌ی قدم ۱

    def test_jumping_ahead_is_refused(self):
        state = WorkflowState()
        assert state.go_to(3) is False
        assert state.current_step == 1

    def test_an_out_of_range_step_is_always_locked(self):
        state = WorkflowState()
        assert state.status(0) == STATUS_LOCKED
        assert state.status(STEP_COUNT + 1) == STATUS_LOCKED
        assert state.is_unlocked(0) is False
        assert state.missing_inputs(0) == []
        assert state.go_to(99) is False


# ── پیشروی و قفل ────────────────────────────────────────────────────────────

class TestProgression:
    def test_approving_step_one_unlocks_step_two(self):
        state = WorkflowState()
        state.record_result(1, _result(1))
        assert state.is_unlocked(2) is False
        assert state.approve(1) is True
        assert state.is_unlocked(2) is True
        assert state.status(2) == STATUS_AVAILABLE

    def test_approving_without_a_result_is_refused(self):
        state = WorkflowState()
        assert state.approve(1) is False
        assert state.is_approved(1) is False
        assert state.is_unlocked(2) is False

    def test_approving_a_locked_step_is_refused(self):
        state = WorkflowState()
        assert state.approve(2) is False
        assert state.is_approved(2) is False

    def test_the_whole_chain_unlocks_one_step_at_a_time(self):
        state = WorkflowState()
        for step in range(1, STEP_COUNT + 1):
            assert state.is_unlocked(step) is True
            assert state.is_unlocked(step + 1) is False
            state.record_result(step, _result(step))
            assert state.approve_and_advance(step) is True
        assert state.current_step == STEP_COUNT
        assert state.completed_count() == STEP_COUNT

    def test_approving_reports_the_step_as_completed(self):
        state = _completed_through(2)
        assert state.status(1) == STATUS_COMPLETED
        assert state.status(2) == STATUS_COMPLETED
        # تأییدِ قدم ۲ کاربر را روی قدم ۳ می‌برد — همان‌جا که «اینجا هستی».
        assert state.status(3) == STATUS_CURRENT

    def test_completed_count_counts_only_approved_steps(self):
        state = _completed_through(3)
        assert state.completed_count() == 3
        state.record_result(4, _result(4))
        assert state.completed_count() == 3

    def test_the_last_step_has_no_next_step(self):
        state = _completed_through(STEP_COUNT)
        assert state.next_step() is None
        assert state.approve_and_advance(STEP_COUNT) is True
        assert state.current_step == STEP_COUNT

    def test_approving_does_not_run_the_next_step(self):
        state = WorkflowState()
        state.record_result(1, _result(1))
        state.approve_and_advance(1)
        assert state.current_step == 2
        assert state.has_result(2) is False


# ── انتقالِ خودکارِ نتیجه‌ها بین قدم‌ها ──────────────────────────────────────

class TestPropagation:
    def test_every_step_declares_which_results_it_receives(self):
        assert [propagated_steps(step) for step in range(1, STEP_COUNT + 1)] == [
            (),
            (1,),
            (1, 2),
            (1, 2, 3),
            (1, 2, 3, 4),
        ]

    def test_step_two_receives_the_step_one_result(self):
        state = _completed_through(2)
        assert state.step_inputs(2)[RESULT_ARGUMENT[1]] == _result(1)

    def test_step_three_receives_the_step_one_and_two_results(self):
        state = _completed_through(3)
        payload = state.step_inputs(3)
        assert payload[RESULT_ARGUMENT[1]] == _result(1)
        assert payload[RESULT_ARGUMENT[2]] == _result(2)

    def test_step_four_receives_the_first_three_results(self):
        state = _completed_through(4)
        payload = state.step_inputs(4)
        assert [payload[RESULT_ARGUMENT[step]] for step in (1, 2, 3)] == [
            _result(1),
            _result(2),
            _result(3),
        ]

    def test_step_five_receives_all_four_results(self):
        state = _completed_through(5)
        payload = state.step_inputs(5)
        assert [payload[RESULT_ARGUMENT[step]] for step in range(1, 5)] == [
            _result(step) for step in range(1, 5)
        ]

    def test_the_first_step_receives_no_upstream_result(self):
        state = WorkflowState()
        assert [key for key in state.step_inputs(1) if key.startswith("step")] == []

    def test_a_stale_result_is_never_propagated(self):
        state = _completed_through(4)
        state.record_result(1, _result(1, "changed"))  # قدم‌های ۲ تا ۴ کهنه می‌شوند
        payload = state.step_inputs(4)
        assert RESULT_ARGUMENT[1] in payload  # نتیجه‌ی تازه پاس داده می‌شود
        assert RESULT_ARGUMENT[2] not in payload  # نتیجه‌ی کهنه هرگز
        assert RESULT_ARGUMENT[3] not in payload

    def test_a_missing_upstream_result_is_reported(self):
        state = WorkflowState()
        assert state.missing_inputs(3) == ["Step 1 result", "Step 2 result"]

    def test_a_stale_upstream_result_is_reported_as_missing(self):
        state = _completed_through(4)
        state.record_result(1, _result(1, "changed"))  # ۲ و ۳ کهنه می‌شوند، ۱ تازه است
        assert state.missing_inputs(4) == ["Step 2 result", "Step 3 result"]

    def test_a_required_input_is_reported_when_empty(self):
        state = WorkflowState()
        assert state.missing_inputs(1) == ["Task Description"]
        assert state.missing_inputs(2) == [
            "Swagger / OpenAPI sources",
            "Step 1 result",
        ]

    def test_an_optional_input_is_never_reported_as_missing(self):
        state = _completed_through(4)
        assert INPUT_COLLECTION_NAME not in state.missing_inputs(4)
        assert state.can_run(4) is True

    def test_the_users_own_input_travels_with_the_payload(self):
        state = _completed_through(2)
        assert state.step_inputs(2)[INPUT_SWAGGER_SOURCES] == _SWAGGER
        assert state.step_inputs(1)[INPUT_TASK_DESCRIPTION] == _TASK

    def test_a_blank_input_is_not_carried(self):
        state = _completed_through(4)
        state.set_input(INPUT_COLLECTION_NAME, "   ")
        assert INPUT_COLLECTION_NAME not in state.step_inputs(4)


# ── ناوبریِ برگشتی ──────────────────────────────────────────────────────────

class TestBackNavigation:
    def test_going_back_to_a_completed_step_keeps_every_result(self):
        state = _completed_through(4)
        assert state.go_to(1) is True
        assert state.result(1) == _result(1)
        assert state.result(2) == _result(2)
        assert state.result(3) == _result(3)
        assert state.result(4) == _result(4)
        assert state.current_step == 1

    def test_inspecting_a_previous_step_changes_no_approval(self):
        state = _completed_through(4)
        approved_before = set(state.approved)
        state.go_to(2)
        assert state.approved == approved_before
        assert state.completed_count() == 4

    def test_going_back_does_not_unlock_a_locked_step(self):
        state = _completed_through(2)
        state.go_to(1)
        assert state.go_to(4) is False
        assert state.current_step == 1

    def test_the_indicator_marks_where_the_user_is(self):
        state = _completed_through(3)
        state.go_to(1)
        statuses = dict(state.statuses())
        assert statuses[1] == STATUS_CURRENT
        assert statuses[2] == STATUS_COMPLETED
        assert statuses[3] == STATUS_COMPLETED
        assert statuses[4] == STATUS_AVAILABLE
        assert statuses[5] == STATUS_LOCKED


# ── کهنه‌شدنِ نتایجِ پایین‌دستی ──────────────────────────────────────────────

class TestInvalidation:
    def test_changing_step_one_invalidates_steps_two_to_five(self):
        state = _completed_through(5)
        invalidated = state.record_result(1, _result(1, "changed"))
        assert invalidated == [2, 3, 4, 5]
        assert state.notice == "Step 1 was changed. Steps 2–5 need to be regenerated."

    def test_changing_step_three_invalidates_only_four_and_five(self):
        state = _completed_through(5)
        state.record_result(3, _result(3, "changed"))
        assert [state.is_stale(step) for step in (1, 2)] == [False, False]
        assert [state.is_stale(step) for step in (4, 5)] == [True, True]
        assert state.is_approved(1) is True
        assert state.is_approved(2) is True

    def test_changing_the_last_step_invalidates_nothing(self):
        state = _completed_through(5)
        assert state.record_result(5, _result(5, "changed")) == []
        assert state.notice == ""

    def test_the_first_result_of_a_step_invalidates_nothing(self):
        state = _completed_through(1)
        assert state.record_result(2, _result(2)) == []

    def test_an_invalidated_step_keeps_its_result(self):
        state = _completed_through(3)
        state.record_result(1, _result(1, "changed"))
        assert state.result(2) == _result(2)
        assert state.has_result(2) is True
        assert state.status(2) == STATUS_STALE

    def test_an_invalidated_step_loses_its_approval(self):
        state = _completed_through(3)
        state.record_result(1, _result(1, "changed"))
        assert [state.is_approved(step) for step in (2, 3)] == [False, False]
        assert state.completed_count() == 0

    def test_an_invalidated_step_cannot_be_approved(self):
        state = _completed_through(3)
        state.record_result(1, _result(1, "changed"))
        assert state.approve(3) is False
        assert state.is_approved(3) is False

    def test_an_invalidated_step_is_locked_behind_its_predecessor(self):
        state = _completed_through(3)
        state.record_result(2, _result(2, "changed"))
        assert state.is_unlocked(3) is False
        assert state.status(3) == STATUS_STALE

    def test_running_a_step_again_clears_its_own_stale_flag(self):
        state = _completed_through(3)
        state.record_result(1, _result(1, "changed"))
        assert state.is_stale(2) is True
        state.record_result(2, _result(2, "again"))
        assert state.is_stale(2) is False
        assert state.status(2) != STATUS_STALE

    def test_an_identical_result_does_not_invalidate_anything(self):
        state = _completed_through(5)
        assert state.record_result(1, _result(1)) == []
        assert state.notice == ""
        # تأییدِ خودِ قدمِ اجراشده لغو می‌شود، ولی قدم‌های بعدی دست‌نخورده می‌مانند.
        assert state.completed_count() == 4
        assert state.is_approved(5) is True
        assert [state.is_stale(step) for step in range(2, 6)] == [False] * 4

    def test_the_notice_can_be_dismissed(self):
        state = _completed_through(2)
        state.record_result(1, _result(1, "changed"))
        assert state.notice != ""
        state.dismiss_notice()
        assert state.notice == ""
        assert state.is_stale(2) is True  # بستنِ پیام وضعیت را عوض نمی‌کند

    def test_a_new_invalidation_replaces_the_previous_notice(self):
        state = _completed_through(5)
        state.record_result(1, _result(1, "first"))
        assert state.notice == "Step 1 was changed. Steps 2–5 need to be regenerated."
        state.record_result(2, _result(2, "second"))
        assert state.notice == "Step 2 was changed. Steps 3–5 need to be regenerated."


class TestRerun:
    def test_rerunning_a_step_removes_its_previous_approval(self):
        state = _completed_through(2)
        state.record_result(2, _result(2, "rerun"))
        assert state.is_approved(2) is False
        assert state.is_unlocked(3) is False

    def test_rerunning_an_earlier_step_does_not_delete_downstream_results(self):
        state = _completed_through(5)
        state.record_result(1, _result(1, "changed"))
        assert [state.result(step) for step in range(2, 6)] == [
            _result(step) for step in range(2, 6)
        ]

    def test_rerunning_the_same_step_with_the_same_result_keeps_downstream_valid(self):
        state = _completed_through(5)
        state.record_result(1, _result(1))
        assert state.is_approved(1) is False  # تأییدِ خودِ قدم لغو می‌شود
        assert [state.is_stale(step) for step in range(2, 6)] == [False] * 4
        assert state.is_approved(5) is True


# ── خطا ─────────────────────────────────────────────────────────────────────

class TestErrors:
    def test_an_error_keeps_the_user_on_the_failing_step(self):
        state = _completed_through(2)
        state.record_error(2, "boom")
        assert state.current_step == 2
        assert state.error(2) == "boom"

    def test_a_failed_step_has_no_result(self):
        state = _completed_through(2)
        state.record_error(2, "boom")
        assert state.has_result(2) is False

    def test_a_failed_step_does_not_unlock_the_next_step(self):
        state = WorkflowState()
        state.set_input(INPUT_TASK_DESCRIPTION, _TASK)
        state.record_error(1, "boom")
        assert state.approve(1) is False
        assert state.is_unlocked(2) is False
        assert state.status(2) == STATUS_LOCKED

    def test_a_failed_step_can_be_retried(self):
        state = WorkflowState()
        state.record_error(1, "boom")
        state.record_result(1, _result(1))
        assert state.error(1) == ""
        assert state.approve(1) is True

    def test_every_failed_step_is_reported(self):
        state = WorkflowState()
        state.record_error(1, "boom")
        assert state.failed_steps() == [1]

    def test_an_error_after_a_successful_run_invalidates_downstream(self):
        state = _completed_through(3)
        invalidated = state.record_error(2, "boom")
        assert invalidated == [3]
        assert state.is_stale(3) is True
        assert state.result(3) == _result(3)

    def test_an_error_on_a_step_that_never_ran_invalidates_nothing(self):
        state = WorkflowState()
        assert state.record_error(2, "boom") == []
        assert state.notice == ""


# ── بازنشانی ────────────────────────────────────────────────────────────────

class TestReset:
    def test_reset_returns_to_step_one(self):
        state = _completed_through(4)
        state.reset()
        assert state.current_step == 1

    def test_reset_clears_every_result(self):
        state = _completed_through(4)
        state.reset()
        assert state.results == {}
        assert [state.has_result(step) for step in range(1, 6)] == [False] * 5

    def test_reset_clears_approvals_and_errors(self):
        state = _completed_through(3)
        state.record_error(4, "boom")
        state.reset()
        assert state.approved == set()
        assert state.errors == {}
        assert state.failed_steps() == []

    def test_reset_clears_the_users_inputs(self):
        state = _completed_through(2)
        state.reset()
        assert state.inputs == {}
        assert state.input_value(INPUT_SWAGGER_SOURCES) == ""
        assert state.used_inputs == {}

    def test_reset_clears_the_notice_and_stale_flags(self):
        state = _completed_through(3)
        state.record_result(1, _result(1, "changed"))
        state.reset()
        assert state.notice == ""
        assert state.stale == set()

    def test_only_step_one_is_unlocked_after_a_reset(self):
        state = _completed_through(5)
        state.reset()
        assert [state.is_unlocked(step) for step in range(1, 6)] == [
            True,
            False,
            False,
            False,
            False,
        ]

    def test_reset_is_safe_on_a_fresh_state(self):
        state = WorkflowState()
        state.reset()
        assert state.results == {} and state.current_step == 1


# ── ورودی‌ها ────────────────────────────────────────────────────────────────

class TestInputs:
    def test_inputs_are_kept_for_the_next_run(self):
        state = WorkflowState()
        state.set_input(INPUT_SWAGGER_SOURCES, _SWAGGER)
        assert state.input_value(INPUT_SWAGGER_SOURCES) == _SWAGGER

    def test_a_changed_input_is_reported_after_a_run(self):
        state = _completed_through(2)
        state.set_input(INPUT_SWAGGER_SOURCES, "https://other.test/swagger.json")
        assert state.pending_input_changes(2) == ["Swagger / OpenAPI sources"]

    def test_an_unchanged_input_is_not_reported(self):
        state = _completed_through(2)
        assert state.pending_input_changes(2) == []
        assert state.pending_input_changes(1) == []

    def test_a_step_that_never_ran_has_no_pending_changes(self):
        state = WorkflowState()
        state.set_input(INPUT_TASK_DESCRIPTION, _TASK)
        assert state.pending_input_changes(1) == []

    def test_rerunning_a_step_clears_its_pending_change(self):
        state = _completed_through(2)
        state.set_input(INPUT_SWAGGER_SOURCES, "https://other.test/swagger.json")
        state.record_result(2, _result(2, "rerun"))
        assert state.pending_input_changes(2) == []


# ── برچسب‌ها و پیام‌ها ──────────────────────────────────────────────────────

class TestLabels:
    def test_every_step_has_a_title_and_an_icon(self):
        assert sorted(STEP_TITLES) == list(range(1, STEP_COUNT + 1))
        assert sorted(STEP_ICONS) == list(range(1, STEP_COUNT + 1))

    def test_every_status_has_a_marker_and_a_label(self):
        for status in (
            STATUS_COMPLETED,
            STATUS_CURRENT,
            STATUS_AVAILABLE,
            STATUS_STALE,
            STATUS_LOCKED,
        ):
            assert status in STATUS_MARKERS
            assert status in STATUS_LABELS

    def test_the_run_button_becomes_a_retry_button_after_an_error(self):
        state = WorkflowState()
        assert run_label(state, 3) == f"Run Step 3 — {STEP_TITLES[3]}"
        state.record_error(3, "boom")
        assert run_label(state, 3) == f"Retry Step 3 — {STEP_TITLES[3]}"

    def test_the_approve_button_points_at_the_next_step(self):
        assert approve_label(1) == "Approve & Continue to Step 2"
        assert approve_label(4) == "Approve & Continue to Step 5"

    def test_the_last_approval_is_final(self):
        assert approve_label(STEP_COUNT) == "Approve Step 5 — finish"

    def test_the_progress_text_reports_position_and_completion(self):
        state = _completed_through(2)
        assert progress_text(state) == "Step 3 of 5 · 2 of 5 approved"

    def test_the_stale_message_tells_the_user_what_to_do(self):
        assert "Run Step 4 again" in stale_message(4)

    def test_the_retry_message_mentions_the_locked_next_step(self):
        assert "Step 3 stays locked" in error_retry_message(2)

    def test_the_retry_message_of_the_last_step_has_no_step_six(self):
        message = error_retry_message(STEP_COUNT)
        assert f"Step {STEP_COUNT + 1}" not in message

    def test_the_invalidation_message_for_a_single_step(self):
        assert invalidated_message(4, [5]) == (
            "Step 4 was changed. Step 5 needs to be regenerated."
        )

    def test_the_invalidation_message_collapses_a_range(self):
        assert invalidated_message(1, [2, 3, 4, 5]) == (
            "Step 1 was changed. Steps 2–5 need to be regenerated."
        )

    def test_the_invalidation_message_lists_a_gap(self):
        assert invalidated_message(1, [2, 4]) == (
            "Step 1 was changed. Steps 2, 4 need to be regenerated."
        )
