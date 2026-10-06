"""
ui/components.py — تکه‌های نمایشیِ مشترکِ قدم‌ها داخلِ ویزارد

هر قدم در ویزارد ورودی‌هایش را از وضعیتِ جریان می‌گیرد، نه از کپی/پیستِ
JSON. این ماژول همان نمایشِ مشترکِ آن ورودی‌هاست: به کاربر نشان می‌دهد چه
چیزی خودکار از قدم‌های قبلی رسیده و امکانِ دیدنِ خودِ JSON را هم می‌دهد.

هیچ منطقِ کسب‌وکاری اینجا نیست — فقط نمایش.
"""

from __future__ import annotations

import streamlit as st

from ui.workflow import WorkflowState, propagated_steps


def render_propagated_inputs(workflow: WorkflowState, step: int) -> None:
    """نتیجه‌های قدم‌های قبلی را که خودکار به این قدم داده شده‌اند نشان می‌دهد.

    کاربر چیزی وارد نمی‌کند و چیزی کپی نمی‌کند؛ فقط می‌بیند این قدم روی چه
    ورودی‌ای اجرا می‌شود.
    """
    sources = propagated_steps(step)
    if not sources:
        return

    labels = ", ".join(f"Step {source} result" for source in sources)
    st.caption(
        f"Input for this step: {labels} — taken from the workflow automatically. "
        f"Nothing to paste."
    )

    for source in sources:
        with st.expander(f"Step {source} result (input to Step {step})"):
            st.json(workflow.result(source))
