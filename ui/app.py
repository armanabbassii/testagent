"""
ui/app.py — ویزاردِ یک‌صفحه‌ایِ جریانِ test_case_generator

    uv run --group ui streamlit run ui/app.py

این فایل «لایه‌ی هماهنگ‌کننده» است، نه بازنویسیِ قدم‌ها. هر پنج قدم همان
پیاده‌سازیِ قبلیِ خودشان را دارند (ui/step1_… تا ui/step5_… و ماژول‌های
src/agents/test_case_generator/)، و این صفحه فقط:

    * وضعیتِ جریان را در st.session_state نگه می‌دارد (ui/workflow.py)،
    * نتیجه‌ی هر قدم را خودکار به قدمِ بعد پاس می‌دهد (بدونِ کپی/پیست)،
    * ترتیبِ ۱ → ۲ → ۳ → ۴ → ۵ را با قفل اعمال می‌کند،
    * بین قدم‌ها منتظرِ تأییدِ انسانی می‌ماند — مگر کاربر Run All را بزند که
      همان قدم‌ها را با ui.pipeline.run_all پشتِ سرِ هم اجرا و خودکار (نه به‌نامِ
      تأییدِ انسانی) می‌پذیرد،
    * و اگر نتیجه‌ی قدمی عوض شود، قدم‌های پایین‌دستی را کهنه اعلام می‌کند.

هیچ منطقِ کسب‌وکاری اینجا نیست: نه فراخوانیِ LLM، نه کشفِ API، نه ساختِ
Postman، نه اجرای درخواست‌ها. صفحه‌های مستقلِ قدم‌ها هم دست‌نخورده باقی
مانده‌اند و هرکدام جداگانه هم قابلِ اجرا هستند.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Streamlit فقط دایرکتوریِ خودِ اسکریپت را در sys.path می‌گذارد؛ ریشه‌ی پروژه
# باید صریحاً اضافه شود تا importهای «src.» و «ui.» کار کنند.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st  # noqa: E402

from src.agents.test_case_generator.source_selection import SourceSelection  # noqa: E402
from ui import (  # noqa: E402
    step1_task_analysis,
    step2_api_mapping,
    step3_scenario_analysis,
    step4_postman_generation,
    step5_final_review,
)
from ui.components import (  # noqa: E402
    STATUS_ICONS,
    STEP_SYMBOLS,
    apply_execution_button_styles,
    status_badge,
    status_line,
    take_run_all_request,
)
from ui.pipeline import (  # noqa: E402
    RUN_BLOCKED,
    RUN_FAILED,
    RUN_INTERRUPTED,
    RUN_SUCCEEDED,
    STEP_RUNNING,
    RunOutcome,
    run_all,
)
from ui.workflow import (  # noqa: E402
    STATUS_LABELS,
    STEP_COUNT,
    STEP_TITLES,
    WorkflowState,
    approve_label,
    error_retry_message,
    progress_text,
    stale_message,
    usage_rows,
    usage_totals,
)

_STATE_KEY = "workflow_state"
_RESET_ARMED_KEY = "_workflow_reset_armed"

# نگاشتِ شماره‌ی قدم به صفحه‌ی همان قدم. تنها چیزی که این لایه از قدم‌ها
# می‌داند همین است: هر صفحه یک render_step(state) دارد.
STEP_MODULES = {
    1: step1_task_analysis,
    2: step2_api_mapping,
    3: step3_scenario_analysis,
    4: step4_postman_generation,
    5: step5_final_review,
}


def get_state() -> WorkflowState:
    """وضعیتِ جریان را از session_state می‌گیرد و در اولین اجرا می‌سازد.

    تنها منبعِ حقیقتِ ویزارد همین شیء است؛ همه‌ی قدم‌ها از آن می‌خوانند و در
    آن می‌نویسند.
    """
    if _STATE_KEY not in st.session_state:
        st.session_state[_STATE_KEY] = WorkflowState()
    return st.session_state[_STATE_KEY]


def _render_notice(state: WorkflowState) -> None:
    """پیامِ کهنه‌شدن — تا کاربر ببنددش، بالای صفحه می‌ماند."""
    if not state.notice:
        return

    with st.container(horizontal=True, vertical_alignment="center"):
        st.warning(state.notice, icon=":material/history:", width="stretch")
        if st.button(
            "Dismiss", key="workflow_notice_dismiss", type="tertiary", icon=":material/close:"
        ):
            state.dismiss_notice()
            st.rerun()


def _done_count(state: WorkflowState) -> int:
    """قدم‌هایی که نتیجه‌ی معتبر (نه کهنه، نه خطا) دارند — برای نوارِ پیشرفت."""
    return sum(
        1
        for step in range(1, STEP_COUNT + 1)
        if state.has_result(step) and not state.is_stale(step) and not state.error(step)
    )


def _progress_lines(state: WorkflowState) -> str:
    """وضعیتِ هر پنج قدم به‌صورتِ متن — برای پنلِ زنده‌ی Run All (بدونِ دکمه)."""
    return "\n".join(f"- {status_line(step, status)}" for step, status in state.statuses())


def _execute_run_all(state: WorkflowState) -> None:
    """Run All را با پنلِ پیشرفتِ زنده اجرا می‌کند و بعد صفحه را دوباره می‌کشد.

    اجرا فقط ui.pipeline.run_all است؛ این تابع فقط وضعیتِ هر قدم را نشان
    می‌دهد. پنل دکمه‌ای ندارد (پیشرفت روی یک placeholder دوباره نوشته می‌شود)،
    و بقیه‌ی صفحه تا پایانِ اجرا کشیده نمی‌شود — پس دکمه‌ی دیگری برای شروعِ
    اجرای هم‌پوشان وجود ندارد.
    """
    panel = st.status("Run All — starting…", expanded=True)
    with panel:
        bar = st.progress(0.0, text=f"0 of {STEP_COUNT} steps done")
        lines = st.empty()

    def on_progress(step: int, status: str) -> None:
        if status == STEP_RUNNING:
            panel.update(label=f"Run All — Step {step} of {STEP_COUNT}: {STEP_TITLES[step]}…")
        done = _done_count(state)
        bar.progress(done / STEP_COUNT, text=f"{done} of {STEP_COUNT} steps done")
        lines.markdown(_progress_lines(state))

    outcome = run_all(state, on_progress=on_progress)
    panel.update(
        label=_outcome_title(outcome),
        state="complete" if outcome.succeeded else "error",
        expanded=not outcome.succeeded,
    )
    st.rerun()


def _outcome_title(outcome: RunOutcome) -> str:
    """یک خطِ خلاصه برای نتیجه‌ی Run All."""
    step = outcome.failed_step
    if outcome.status == RUN_SUCCEEDED:
        return f"Run All finished — Steps {outcome.start}–{STEP_COUNT} completed."
    if outcome.status == RUN_BLOCKED:
        return f"Run All finished, but the Step {step} review is blocked."
    if outcome.status == RUN_FAILED:
        return f"Run All stopped at Step {step} — {STEP_TITLES.get(step, '')}."
    if outcome.status == RUN_INTERRUPTED:
        return "Run All was interrupted."
    return "Run All did not start."


def _render_run_outcome(state: WorkflowState) -> None:
    """نتیجه‌ی آخرین Run All — موفق، مسدود یا ناموفق، با قدم و پیامِ دقیق."""
    outcome = state.last_run
    if not isinstance(outcome, RunOutcome):
        return

    title = _outcome_title(outcome)
    with st.container(border=True):
        if outcome.status == RUN_SUCCEEDED:
            st.success(
                f"{title} The steps were accepted automatically, not reviewed by a "
                f"person — review them and approve if you wish. The collection can be "
                f"downloaded in Step {STEP_COUNT}.",
                icon=":material/check_circle:",
            )
        elif outcome.status == RUN_INTERRUPTED:
            st.warning(f"{title}\n\n{outcome.error}", icon=":material/pause_circle:")
        else:
            st.error(f"{title}\n\n{outcome.error}", icon=":material/error:")
            if outcome.completed_steps:
                st.caption(
                    f"Completed in this run: Step "
                    f"{', '.join(str(step) for step in outcome.completed_steps)}. "
                    f"Later steps did not run — their earlier results are out of date "
                    f"and cannot be downloaded."
                )
        with st.container(horizontal=True, vertical_alignment="center"):
            selection = state.swagger_selection
            if isinstance(selection, SourceSelection) and selection.keys and not selection.error:
                st.caption(f"Swagger sources used in Step 2: {selection.summary()}")
            if st.button(
                "Dismiss",
                key="workflow_run_outcome_dismiss",
                type="tertiary",
                icon=":material/close:",
            ):
                state.last_run = None
                st.rerun()


def _render_indicator(state: WorkflowState) -> None:
    """نوارِ پنج‌قدمی: یک کارت برای هر قدم با وضعیتش — همیشه در دیدِ کاربر.

    پرش به قدمی که پیش‌نیازش وجود ندارد ممکن نیست: دکمه‌اش غیرفعال است. روی
    صفحه‌ی باریک کارت‌ها به ردیفِ بعد می‌روند.
    """
    with st.container(horizontal=True, gap="small"):
        for step, status in state.statuses():
            with st.container(border=True, gap="small", key=f"workflow_step_card_{step}"):
                if st.button(
                    f"Step {step}",
                    key=f"workflow_nav_{step}",
                    icon=STATUS_ICONS[status],
                    type="primary" if step == state.current_step else "secondary",
                    disabled=state.run_in_progress or not state.is_unlocked(step),
                    help=f"{STEP_TITLES[step]} — {STATUS_LABELS[status]}",
                    width="stretch",
                ):
                    state.go_to(step)
                    st.rerun()
                st.caption(f"{STEP_SYMBOLS[step]} {STEP_TITLES[step]}")
                status_badge(status)


def _render_step(state: WorkflowState) -> None:
    """بدنه‌ی قدمِ جاری را از صفحه‌ی همان قدم می‌کشد.

    خطاها و نتیجه‌های کهنه بالای بدنه اعلام می‌شوند تا پنهان نمانند.
    """
    step = state.current_step

    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        st.subheader(
            f"Step {step} — {STEP_TITLES[step]}", icon=STEP_SYMBOLS[step], width="content"
        )
        status_badge(state.status(step))

    if state.is_stale(step):
        st.warning(stale_message(step), icon=":material/history:")
    if state.error(step):
        st.error(state.error(step), icon=":material/error:")
        st.caption(error_retry_message(step))

    STEP_MODULES[step].render_step(state)


def _render_footer(state: WorkflowState) -> None:
    """پایینِ هر قدم: وضعیتِ تأیید و دکمه‌ی «تأیید و ادامه».

    بیرون از Run All هیچ قدمی خودکار اجرا نمی‌شود؛ نتیجه فقط با تأییدِ انسانی
    قدمِ بعد را باز می‌کند و اجرای قدمِ بعد هم به دکمه‌ی خودش سپرده شده است.
    قدمی که Run All پذیرفته جدا نشان داده می‌شود و هنوز قابلِ تأییدِ انسانی است.
    """
    step = state.current_step
    with st.container(border=True):
        st.markdown(":material/rate_review: **Review & approval**")
        _render_footer_body(state, step)


def _render_footer_body(state: WorkflowState, step: int) -> None:
    if state.is_stale(step):
        st.caption(
            f"A result that is out of date cannot be approved. "
            f"Run Step {step} again to unlock approval."
        )
        return

    if state.is_approved(step):
        st.success(
            f"Step {step} approved — this result is held for the next step.",
            icon=":material/verified:",
        )
        if step < STEP_COUNT:
            st.caption(f"Step {step + 1} is now unlocked.")
        return

    if state.is_auto_advanced(step):
        st.info(
            f"Step {step} was completed automatically by Run All — nobody has "
            f"reviewed it yet. Approving records your review.",
            icon=":material/task_alt:",
        )

    if not state.has_result(step):
        st.caption(
            f"Waiting for a Step {step} result. Step {step + 1} stays locked until "
            f"you review and approve this step."
            if step < STEP_COUNT
            else f"Waiting for a Step {step} result."
        )
        return

    changes = state.pending_input_changes(step)
    if changes:
        st.info(
            f"{', '.join(changes)} changed after Step {step} ran. The result below "
            f"belongs to the earlier input — run Step {step} again to apply the "
            f"change.",
            icon=":material/edit_note:",
        )

    st.caption(
        "Approving does not run Step "
        f"{step + 1 if step < STEP_COUNT else step} — it only records that you "
        "reviewed this result and unlocks the next step."
    )
    if st.button(
        approve_label(step),
        type="primary",
        key=f"workflow_approve_{step}",
        icon=":material/check:",
    ):
        if state.approve_and_advance(step):
            st.rerun()
        else:
            st.error(
                f"Step {step} could not be approved. Run it again and review the "
                f"fresh result."
            )


def _render_token_usage(state: WorkflowState) -> None:
    """مصرفِ توکنِ هر قدم — از فراداده‌ی واقعیِ ارائه‌دهنده، نه تخمینِ متن.

    «—» یعنی قدم هنوز اجرا نشده، «no calls» یعنی اجرا شده ولی LLM صدا زده
    نشده و «not reported» یعنی فراخوانی بوده ولی ارائه‌دهنده usage نداده است.
    ستونِ Cached فقط وقتی نمایش داده می‌شود که جایی گزارش شده باشد.
    جمعِ کل در عنوانِ بخش هم دیده می‌شود تا جدول بسته بماند.
    """
    totals = usage_totals(state)
    label = "LLM token usage"
    if totals is not None and totals.total_tokens is not None:
        label += f" · {totals.total_tokens:,} tokens"
    with st.expander(label, icon=":material/data_usage:"):
        st.dataframe(usage_rows(state), hide_index=True, width="stretch")
        st.caption(
            "Provider-reported usage. Each step's count includes every model call it "
            "made (batches and retries), and is replaced — not added — when the step "
            "is run again. A failed run keeps what it already consumed."
        )


def _render_sidebar_steps(state: WorkflowState) -> None:
    """فهرستِ فشرده‌ی قدم‌ها — همان قواعدِ قفلِ نوارِ بالای صفحه."""
    st.progress(_done_count(state) / STEP_COUNT, text=progress_text(state))
    for step, status in state.statuses():
        if st.button(
            f"{step} · {STEP_TITLES[step]}",
            key=f"workflow_sidebar_nav_{step}",
            icon=STATUS_ICONS[status],
            type="primary" if step == state.current_step else "tertiary",
            disabled=state.run_in_progress or not state.is_unlocked(step),
            help=STATUS_LABELS[status],
        ):
            state.go_to(step)
            st.rerun()


def _render_reset(state: WorkflowState) -> None:
    """بازنشانیِ کلِ جریان — دو مرحله‌ای (درخواست، بعد تأیید)."""
    with st.expander("Reset workflow", icon=":material/restart_alt:"):
        st.caption(
            "Clears every result and every input of all five steps and starts "
            "again at Step 1. This cannot be undone."
        )

        if st.button("Reset workflow", key="workflow_reset", icon=":material/delete_sweep:"):
            st.session_state[_RESET_ARMED_KEY] = True

        if st.session_state.get(_RESET_ARMED_KEY):
            st.warning("All progress in this workflow will be lost. Are you sure?")
            with st.container(horizontal=True):
                if st.button("Yes, reset", type="primary", key="workflow_reset_yes"):
                    state.reset()
                    st.session_state[_RESET_ARMED_KEY] = False
                    st.rerun()
                if st.button("Cancel", key="workflow_reset_no"):
                    st.session_state[_RESET_ARMED_KEY] = False
                    st.rerun()


def _render_sidebar(state: WorkflowState) -> None:
    """پیشرفت و ناوبریِ فشرده، مصرفِ توکن و بازنشانیِ جریان (با تأیید)."""
    with st.sidebar:
        st.subheader("Workflow", icon=":material/route:")
        _render_sidebar_steps(state)
        st.space("small")
        _render_token_usage(state)
        _render_reset(state)


def _render_header(state: WorkflowState) -> None:
    with st.container(
        horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"
    ):
        st.title("Test case generator", icon=":material/route:", width="content")
        st.badge(progress_text(state), icon=":material/timeline:", color="gray")
    st.caption(
        "Steps 1–5 in one application. Each step receives the previous step's "
        "result automatically. Run them one at a time — every result waits for your "
        "review before the next step unlocks — or use Run All in Step 1 to run all "
        "five without stopping. Step 5 exports the Postman collection. No API is "
        "called and no collection is executed from this page."
    )


def main() -> None:
    st.set_page_config(
        page_title="Test case generator",
        page_icon=":material/route:",
        layout="wide",
    )

    apply_execution_button_styles(STEP_COUNT)
    state = get_state()

    _render_header(state)

    if take_run_all_request():
        _execute_run_all(state)

    _render_run_outcome(state)
    _render_notice(state)
    _render_indicator(state)
    _render_step(state)
    _render_footer(state)
    _render_sidebar(state)


if __name__ == "__main__":
    main()
