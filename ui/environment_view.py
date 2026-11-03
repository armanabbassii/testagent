"""
ui/environment_view.py — نمایشِ تصمیم‌های محیط و میزبانِ هر تست‌کیس

فقط نمایش است: داده‌ها را ui/formatting.py آماده می‌کند و تصمیم‌ها پیش‌تر در
قدم ۱ (سنجشِ شاهد) و قدم ۲ (انتخابِ قطعیِ میزبان) گرفته شده‌اند. بازبینیِ
NEEDS_REVIEW هشدار است، نه خطا.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from ui.formatting import (
    environment_decision_rows,
    environment_issue_review,
    environment_warnings,
    host_explanations,
    host_selection_rows,
    unmatched_decision_rows,
)


def render_environment_decisions(step1_result: Any) -> None:
    """قدم ۱: جمله‌های صریحِ VDI/عادی، شاهدِ دقیق و منبعشان — پذیرفته یا ردشده."""
    rows = environment_decision_rows(step1_result)
    warnings = environment_warnings(step1_result)
    st.subheader("Environment Decisions")
    if not rows:
        st.caption(
            "The task states no VDI or normal environment, so every test case uses {{base_url}}."
        )
    else:
        st.caption(
            "Explicit statements about where a service must run. Step 2 applies them to "
            "the mapped test cases; rejected statements put the affected test cases on "
            "hold instead of falling back to {{base_url}}."
        )
        st.dataframe(rows, hide_index=True)
    for warning in warnings:
        st.warning(warning)


def render_host_resolution(step1_result: Any, step2_result: Any) -> None:
    """قدم ۲: میزبانِ هر تست‌کیس، دلیلش، و هر چیزی که بازبینیِ انسانی لازم دارد."""
    rows = host_selection_rows(step2_result)
    if not rows:
        return
    review = environment_issue_review(step2_result)
    st.subheader("Hosts")
    if review["status"] == "NEEDS_REVIEW":
        st.warning(
            "Environment review needed — test cases on hold are not executed until a "
            "human resolves them; the other test cases continue.\n\n"
            + "\n".join(f"- {reason}" for reason in review["reasons"])
        )
    else:
        st.caption("Every test case has a host; values come from the Postman environment.")
    st.dataframe(rows, hide_index=True)

    unmatched = unmatched_decision_rows(step2_result)
    if unmatched:
        st.markdown("**Unmatched environment statements** — assigned to no test case")
        st.dataframe(unmatched, hide_index=True)
    for clarification in review["clarifications"]:
        st.warning(clarification)

    with st.expander("Why each test case uses its host"):
        for explanation in host_explanations(step1_result, step2_result):
            st.markdown(f"**{explanation['heading']}**")
            st.markdown("\n".join(f"- {line}" for line in explanation["lines"]) or "—")


__all__ = ["render_environment_decisions", "render_host_resolution"]
