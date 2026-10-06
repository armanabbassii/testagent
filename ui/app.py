"""
ui/app.py — ویزاردِ یک‌صفحه‌ایِ جریانِ test_case_generator

    uv run --group ui streamlit run ui/app.py

این فایل «لایه‌ی هماهنگ‌کننده» است، نه بازنویسیِ قدم‌ها. هر پنج قدم همان
پیاده‌سازیِ قبلیِ خودشان را دارند (ui/step1_… تا ui/step5_… و ماژول‌های
src/agents/test_case_generator/)، و این صفحه فقط:

    * وضعیتِ جریان را در st.session_state نگه می‌دارد (ui/workflow.py)،
    * نتیجه‌ی هر قدم را خودکار به قدمِ بعد پاس می‌دهد (بدونِ کپی/پیست)،
    * ترتیبِ ۱ → ۲ → ۳ → ۴ → ۵ را با قفل اعمال می‌کند،
    * بین قدم‌ها منتظرِ تأییدِ انسانی می‌ماند،
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

from ui import (  # noqa: E402
    step1_task_analysis,
    step2_api_mapping,
    step3_scenario_analysis,
    step4_postman_generation,
    step5_final_review,
)
from ui.workflow import (  # noqa: E402
    STATUS_LABELS,
    STATUS_MARKERS,
    STEP_COUNT,
    STEP_ICONS,
    STEP_TITLES,
    WorkflowState,
    approve_label,
    error_retry_message,
    progress_text,
    stale_message,
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

    message, dismiss = st.columns([6, 1])
    message.warning(state.notice)
    if dismiss.button("Dismiss", key="workflow_notice_dismiss"):
        state.dismiss_notice()
        st.rerun()


def _render_indicator(state: WorkflowState) -> None:
    """نوارِ پنج‌قدمی: تمام‌شده، جاری، آماده و قفل — همیشه در دیدِ کاربر.

    پرش به قدمی که پیش‌نیازش وجود ندارد ممکن نیست: دکمه‌اش غیرفعال است.
    """
    columns = st.columns(STEP_COUNT)
    for column, (step, status) in zip(columns, state.statuses()):
        with column:
            if st.button(
                f"{STATUS_MARKERS[status]} Step {step}",
                key=f"workflow_nav_{step}",
                disabled=not state.is_unlocked(step),
                use_container_width=True,
            ):
                state.go_to(step)
                st.rerun()
            st.caption(f"{STEP_ICONS[step]} {STEP_TITLES[step]}")
            st.caption(STATUS_LABELS[status])


def _render_step(state: WorkflowState) -> None:
    """بدنه‌ی قدمِ جاری را از صفحه‌ی همان قدم می‌کشد.

    خطاها و نتیجه‌های کهنه بالای بدنه اعلام می‌شوند تا پنهان نمانند.
    """
    step = state.current_step

    st.divider()
    st.subheader(f"{STEP_ICONS[step]} Step {step} — {STEP_TITLES[step]}")

    if state.is_stale(step):
        st.warning(stale_message(step))
    if state.error(step):
        st.error(state.error(step))
        st.caption(error_retry_message(step))

    STEP_MODULES[step].render_step(state)


def _render_footer(state: WorkflowState) -> None:
    """پایینِ هر قدم: وضعیتِ تأیید و دکمه‌ی «تأیید و ادامه».

    هیچ قدمی خودکار اجرا نمی‌شود؛ نتیجه فقط با تأییدِ انسانی قدمِ بعد را باز
    می‌کند و اجرای قدمِ بعد هم به دکمه‌ی خودش سپرده شده است.
    """
    step = state.current_step
    st.divider()

    if state.is_stale(step):
        st.caption(
            f"A result that is out of date cannot be approved. "
            f"Run Step {step} again to unlock approval."
        )
        return

    if state.is_approved(step):
        st.success(f"Step {step} approved — this result is held for the next step.")
        if step < STEP_COUNT:
            st.caption(f"Step {step + 1} is now unlocked.")
        return

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
            f"change."
        )

    st.caption(
        "Approving does not run Step "
        f"{step + 1 if step < STEP_COUNT else step} — it only records that you "
        "reviewed this result and unlocks the next step."
    )
    if st.button(approve_label(step), type="primary", key=f"workflow_approve_{step}"):
        if state.approve_and_advance(step):
            st.rerun()
        else:
            st.error(
                f"Step {step} could not be approved. Run it again and review the "
                f"fresh result."
            )


def _render_sidebar(state: WorkflowState) -> None:
    """وضعیتِ کلی و بازنشانیِ جریان (با تأیید، چون همه‌ی پیشرفت را پاک می‌کند)."""
    with st.sidebar:
        st.header("Workflow")
        st.caption(progress_text(state))
        for step, status in state.statuses():
            st.markdown(
                f"{STATUS_MARKERS[status]} **Step {step}** — {STATUS_LABELS[status]}"
            )

        st.divider()
        st.subheader("Reset")
        st.caption(
            "Clears every result and every input of all five steps and starts "
            "again at Step 1. This cannot be undone."
        )

        if st.button("Reset workflow", key="workflow_reset"):
            st.session_state[_RESET_ARMED_KEY] = True

        if st.session_state.get(_RESET_ARMED_KEY):
            st.warning("All progress in this workflow will be lost. Are you sure?")
            confirm, cancel = st.columns(2)
            if confirm.button("Yes, reset", type="primary", key="workflow_reset_yes"):
                state.reset()
                st.session_state[_RESET_ARMED_KEY] = False
                st.rerun()
            if cancel.button("Cancel", key="workflow_reset_no"):
                st.session_state[_RESET_ARMED_KEY] = False
                st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="Test Case Generator — Workflow",
        page_icon="🧭",
        layout="wide",
    )

    state = get_state()

    st.title("🧭 Test Case Generator — Workflow")
    st.caption(
        "Steps 1–5 in one application. Each step receives the previous step's "
        "result automatically, every result waits for your review before the next "
        "step unlocks, and Step 5 exports the Postman collection. No API is called "
        "and no collection is executed from this page."
    )

    _render_notice(state)
    _render_indicator(state)
    _render_step(state)
    _render_footer(state)
    _render_sidebar(state)


if __name__ == "__main__":
    main()
