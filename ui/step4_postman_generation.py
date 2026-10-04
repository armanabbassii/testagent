"""
ui/step4_postman_generation.py — رابطِ ساده‌ی Streamlit برای قدمِ چهارمِ جریان

    نتیجه‌ی قدم ۱ (تست‌کیس‌های کسب‌وکاری)
  + نتیجه‌ی قدم ۲ (کشفِ API و نگاشتِ تست‌کیس به عملیات)
  + نتیجه‌ی قدم ۳ (سناریو، ترتیبِ اجرا و وابستگی‌های داده)
        → Postman Collection v2.1
        → بازبینی، دانلود و تأییدِ انسانی

این لایه کاملاً نازک است: ورودی می‌گیرد، Step4PostmanGenerator را صدا می‌زند،
خطا را نشان می‌دهد و کالکشن را نمایش می‌دهد. هیچ منطقِ کسب‌وکاری اینجا نیست —
ساختِ درخواست‌ها، تصمیم‌های احراز هویت، انتسابِ وابستگی‌های داده و اعتبارسنجیِ
کالکشن همه در src/agents/test_case_generator/postman_generation.py می‌مانند.

این صفحه هیچ API ای را اجرا نمی‌کند، کالکشن را در Postman اجرا نمی‌کند، هیچ
سندِ Swagger ای را نمی‌خواند، هیچ LLM ای صدا نمی‌زند و قدم پنجم را شروع نمی‌کند.

اجرا (از ریشه‌ی پروژه):

    uv sync --group ui
    uv run --group ui streamlit run ui/step4_postman_generation.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Streamlit فقط دایرکتوریِ خودِ اسکریپت را در sys.path می‌گذارد؛ ریشه‌ی پروژه
# باید صریحاً اضافه شود تا importهای «src.» و «ui.» کار کنند.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st  # noqa: E402

from src.agents.test_case_generator.postman_generation import (  # noqa: E402
    PostmanGenerationError,
    Step4PostmanGenerator,
)
from src.debug import DebugConfig  # noqa: E402
from ui.formatting import (  # noqa: E402
    collection_filename,
    collection_json,
    step4_counts,
    step4_request_rows,
    step4_unresolved_rows,
    step4_warnings,
)

_RESULT_KEY = "postman_generation_result"
_ERROR_KEY = "postman_generation_error"
_APPROVED_KEY = "postman_generation_approved"


def _generate(
    step1_raw: str, step2_raw: str, step3_raw: str, collection_name: str
) -> None:
    """کالکشن را می‌سازد و نتیجه یا خطا را در session_state می‌گذارد.

    هر نتیجه‌ی تازه تأییدِ قبلی را باطل می‌کند تا تأییدِ کهنه روی کالکشنِ جدید
    نماند.
    """
    st.session_state[_APPROVED_KEY] = False

    try:
        step1_result = json.loads(step1_raw)
        step2_result = json.loads(step2_raw)
        step3_result = json.loads(step3_raw)
    except json.JSONDecodeError as exc:
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = f"Input is not valid JSON: {exc}"
        return

    try:
        # قطعی است و هیچ LLM/شبکه‌ای درگیر نیست — پس spinner لازم نیست.
        result = Step4PostmanGenerator(
            debug_config=DebugConfig.from_env(),
            collection_name=collection_name,
        ).generate(
            step1_result=step1_result,
            step2_result=step2_result,
            step3_result=step3_result,
        )
    except PostmanGenerationError as exc:
        # ورودیِ ناسازگار یا کالکشنِ نامعتبر
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = f"Collection generation failed:\n\n{exc}"
    except Exception as exc:  # noqa: BLE001 — خطای غیرمنتظره نباید UI را بترکاند
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = (
            f"Collection generation failed: {type(exc).__name__}: {exc}"
        )
    else:
        st.session_state[_RESULT_KEY] = result
        st.session_state[_ERROR_KEY] = None


def _render_unresolved(result: dict) -> None:
    """تست‌کیس‌هایی که به درخواست تبدیل نشدند — با دلیلِ روشن."""
    st.subheader("Test cases without a request")

    rows = step4_unresolved_rows(result)
    if not rows:
        st.caption("Every Step 1 test case has a request in this collection.")
        return

    st.warning(
        "These test cases were not turned into requests. Step 4 did not invent a "
        "mapping for them — resolve them in Step 2 and run this step again."
    )
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_clarifications(result: dict) -> None:
    """ابهام‌هایی که قدم سوم باز گذاشته — هیچ‌کدام به assertion تبدیل نشده."""
    st.subheader("Unresolved clarifications")

    clarifications = result.get("clarifications") or []
    if not clarifications:
        st.caption("None — no clarification was left open by Step 3.")
        return

    st.warning(
        "No assertion or value was guessed for these. They stay visible in the "
        "collection description and on the affected requests so a reviewer can "
        "decide what to do."
    )
    for clarification in clarifications:
        kind = str(clarification.get("type") or "general").strip()
        case_id = str(clarification.get("test_case_id") or "").strip()
        message = str(clarification.get("message") or "").strip()
        prefix = f"{kind} ({case_id})" if case_id else kind
        st.markdown(f"- **{prefix}:** {message}")


def _render_warnings(result: dict) -> None:
    """هشدارهای ساخت — مثلاً ناسازگاریِ ترتیبِ اجرا با یک وابستگیِ داده."""
    warnings = step4_warnings(result)
    if not warnings:
        return

    st.subheader("Warnings")
    for warning in warnings:
        st.warning(warning)


def _render_requests(result: dict) -> None:
    """فهرستِ درخواست‌های ساخته‌شده با متد، مسیر و سناریو."""
    st.subheader("Requests")

    rows = step4_request_rows(result)
    if not rows:
        st.caption("No request was generated.")
        return
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_collection(result: dict) -> None:
    """کالکشنِ کامل را نشان می‌دهد و امکانِ دانلود می‌دهد.

    نمایش عمداً پشت expander پنهان نشده است: خروجیِ این قدم خودِ کالکشن است و
    بازبین باید بتواند بدونِ باز کردن چیزی آن را ببیند.
    """
    st.subheader("Postman collection")

    collection = result.get("collection") or {}
    name = result.get("collection_name") or ""
    filename = collection_filename(name)

    st.markdown(f"**Collection name:** {name or '—'}")
    st.caption(
        "This is the artifact itself — shown in full rather than hidden behind a "
        "section. It has not been executed and no token, credential or API call is "
        "involved."
    )

    st.download_button(
        "Download collection JSON",
        data=collection_json(collection),
        file_name=filename,
        mime="application/json",
    )

    st.json(collection)


def _render_result(result: dict) -> None:
    """کالکشنِ ساخته‌شده را برای بازبینیِ انسانی نمایش می‌دهد."""
    counts = step4_counts(result)
    columns = st.columns(4)
    columns[0].metric("Scenarios", counts["scenarios"])
    columns[1].metric("Requests", counts["requests"])
    columns[2].metric("Without a request", counts["unresolved"])
    columns[3].metric("Clarifications", counts["clarifications"])

    _render_collection(result)
    _render_unresolved(result)
    _render_clarifications(result)
    _render_warnings(result)
    _render_requests(result)

    with st.expander("Raw JSON (input for the next step of the workflow)"):
        st.json(result)


def _render_approval(result: dict) -> None:
    """تأییدِ انسانیِ قدم چهارم — کالکشن در همین نشست نگه داشته می‌شود."""
    st.subheader("Review")

    if st.session_state.get(_APPROVED_KEY):
        st.success(
            "Step 4 approved — this collection is held for the next workflow step."
        )
        return

    unresolved = step4_counts(result)["unresolved"]
    if unresolved:
        st.caption(
            f"{unresolved} test case(s) have no request. You can still approve, but "
            f"they will be missing from the collection."
        )

    if st.button("Approve Step 4"):
        st.session_state[_APPROVED_KEY] = True
        st.success(
            "Step 4 approved — this collection is held for the next workflow step."
        )


def _render_inputs() -> None:
    """فرمِ ورودی را می‌سازد و در صورت ارسال، کالکشن را می‌سازد."""
    with st.form("postman_generation_form"):
        step1_raw = st.text_area(
            "Step 1 result (JSON) *",
            height=160,
            placeholder=(
                "Paste the JSON produced by Step 1 — the 'Raw JSON' section at the "
                "bottom of the Step 1 page is exactly this."
            ),
        )
        step2_raw = st.text_area(
            "Step 2 result (JSON) *",
            height=160,
            placeholder=(
                "Paste the JSON produced by Step 2 — the 'Raw JSON' section at the "
                "bottom of the Step 2 page is exactly this."
            ),
        )
        step3_raw = st.text_area(
            "Step 3 result (JSON) *",
            height=160,
            placeholder=(
                "Paste the JSON produced by Step 3 — the 'Raw JSON' section at the "
                "bottom of the Step 3 page is exactly this."
            ),
        )
        collection_name = st.text_input(
            "Collection name (optional)",
            placeholder=(
                "Leave empty to derive it from the Step 1 task summary."
            ),
        )
        submitted = st.form_submit_button("Generate Postman Collection")

    if not submitted:
        return

    for label, value in (
        ("Step 1", step1_raw),
        ("Step 2", step2_raw),
        ("Step 3", step3_raw),
    ):
        if not value.strip():
            st.session_state[_RESULT_KEY] = None
            st.session_state[_ERROR_KEY] = f"The {label} result is required."
            return

    _generate(step1_raw, step2_raw, step3_raw, collection_name)


def main() -> None:
    st.set_page_config(
        page_title="Step 4 — Postman Generation",
        page_icon="📦",
        layout="wide",
    )

    st.title("Step 4 — Postman Collection Generation")
    st.caption(
        "Step 1 test cases + Step 2 mappings + Step 3 scenarios and dependencies → "
        "a Postman Collection v2.1 for human review. This step is deterministic: it "
        "does not call an LLM, does not fetch or parse Swagger, does not execute any "
        "API and does not run the collection."
    )

    _render_inputs()

    error = st.session_state.get(_ERROR_KEY)
    if error:
        st.error(error)

    result = st.session_state.get(_RESULT_KEY)
    if result:
        _render_result(result)
        _render_approval(result)


main()
