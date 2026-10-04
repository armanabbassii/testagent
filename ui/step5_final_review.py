"""
ui/step5_final_review.py — رابطِ ساده‌ی Streamlit برای قدمِ پنجمِ جریان

    نتیجه‌ی قدم ۱ (تست‌کیس‌های کسب‌وکاری)
  + نتیجه‌ی قدم ۲ (کشفِ API و نگاشت)
  + نتیجه‌ی قدم ۳ (سناریو، ترتیبِ اجرا و وابستگی‌های داده)
  + نتیجه‌ی قدم ۴ (Postman Collection v2.1)
        → بازبینیِ نهایی: وضعیت، متریک‌ها، issue ها، هشدارها و مواردِ حل‌نشده
        → صادراتِ همان کالکشنِ قدم چهارم — دست‌نخورده

این لایه کاملاً نازک است: ورودی می‌گیرد، FinalReviewGenerator را صدا می‌زند،
خطا را نشان می‌دهد و گزارش را نمایش می‌دهد. هیچ منطقِ کسب‌وکاری اینجا نیست —
وضعیت، پوششِ تست‌کیس‌ها، سازگاریِ بین‌قدمی، متریک‌ها و خلاصه همه در
src/agents/test_case_generator/final_review.py می‌مانند.

این صفحه هیچ LLM ای صدا نمی‌زند، هیچ Swagger ای نمی‌خواند، هیچ API ای را اجرا
نمی‌کند، کالکشن را در Postman اجرا نمی‌کند و هیچ نتیجه‌ی قبلی را تغییر نمی‌دهد.
تأییدِ این قدم یعنی «بازبین artifact را دید و پذیرفت» — نه اجرای چیزی.

اجرا (از ریشه‌ی پروژه):

    uv sync --group ui
    uv run --group ui streamlit run ui/step5_final_review.py
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

from src.agents.test_case_generator.final_review import (  # noqa: E402
    FinalReviewGenerator,
    STATUS_BLOCKED,
    STATUS_NEEDS_REVIEW,
    STATUS_READY,
)
from src.debug import DebugConfig  # noqa: E402
from ui.formatting import (  # noqa: E402
    collection_filename,
    collection_json,
    collection_name,
    postman_collection,
    step5_issue_rows,
    step5_metrics,
    step5_review_item_rows,
    step5_status,
    step5_status_label,
    step5_summary,
    step5_warnings,
)

_RESULT_KEY = "final_review_result"
_ERROR_KEY = "final_review_error"
_APPROVED_KEY = "final_review_approved"

_INPUT_KEYS = (
    ("Step 1", "step1_result", "the 'Raw JSON' section at the bottom of the Step 1 page"),
    ("Step 2", "step2_result", "the 'Raw JSON' section at the bottom of the Step 2 page"),
    ("Step 3", "step3_result", "the 'Raw JSON' section at the bottom of the Step 3 page"),
    (
        "Step 4",
        "step4_result",
        "the 'Raw JSON' section at the bottom of the Step 4 page — or the Postman "
        "collection itself",
    ),
)


def _review(inputs: dict[str, str]) -> None:
    """بازبینیِ نهایی را اجرا می‌کند و نتیجه یا خطا را در session_state می‌گذارد.

    هر نتیجه‌ی تازه تأییدِ قبلی را باطل می‌کند تا تأییدِ کهنه روی گزارشِ جدید
    نماند.
    """
    st.session_state[_APPROVED_KEY] = False

    parsed: dict[str, object] = {}
    for label, key, _ in _INPUT_KEYS:
        try:
            parsed[key] = json.loads(inputs[key])
        except json.JSONDecodeError as exc:
            st.session_state[_RESULT_KEY] = None
            st.session_state[_ERROR_KEY] = f"{label} input is not valid JSON: {exc}"
            return

    try:
        # قطعی است و هیچ LLM/شبکه‌ای درگیر نیست — پس spinner لازم نیست.
        result = FinalReviewGenerator(
            debug_config=DebugConfig.from_env()
        ).generate(
            step1_result=parsed["step1_result"],
            step2_result=parsed["step2_result"],
            step3_result=parsed["step3_result"],
            step4_result=parsed["step4_result"],
        )
    except Exception as exc:  # noqa: BLE001 — خطای غیرمنتظره نباید UI را بترکاند
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = f"Final review failed: {type(exc).__name__}: {exc}"
    else:
        st.session_state[_RESULT_KEY] = result
        st.session_state[_ERROR_KEY] = None


def _render_status(result: dict) -> None:
    """وضعیتِ نهایی و خلاصه — پررنگ و بدونِ پنهان‌کردنِ چیزی."""
    st.subheader("Final status")

    status = step5_status(result)
    label = step5_status_label(result)
    if status == STATUS_READY:
        st.success(f"Status: {label}")
    elif status == STATUS_NEEDS_REVIEW:
        st.warning(f"Status: {label}")
    elif status == STATUS_BLOCKED:
        st.error(f"Status: {label}")
    else:
        st.info(f"Status: {label}")

    summary = step5_summary(result)
    if summary:
        # شکستنِ سختِ خطوطِ markdown تا خلاصه دقیقاً با همان خط‌بندیِ تولیدشده
        # نمایش داده شود.
        st.markdown(summary.replace("\n", "  \n"))


def _render_metrics(result: dict) -> None:
    """متریک‌های قطعی — همان اعدادی که در گزارشِ JSON هم هستند."""
    metrics = step5_metrics(result)
    st.subheader("Metrics")

    first = st.columns(3)
    first[0].metric("Test Cases", metrics["test_cases"])
    first[1].metric("Mapped APIs", metrics["mapped_test_cases"])
    first[2].metric("Requests", metrics["generated_requests"])

    second = st.columns(3)
    second[0].metric("Scenarios", metrics["scenarios"])
    second[1].metric("Dependencies", metrics["data_dependencies"])
    second[2].metric("Clarifications", metrics["clarifications"])


def _render_issues(result: dict) -> None:
    """issue های بازدارنده — چیزی که تا حل نشود artifact قابلِ اتکا نیست."""
    st.subheader("Issues")

    rows = step5_issue_rows(result)
    if not rows:
        st.caption(
            "None — the collection is structurally valid and every Step 1 test case "
            "is accounted for."
        )
        return

    st.error(
        f"{len(rows)} blocking issue(s) found. The collection is shown below for "
        f"review, but it should not be used as-is until they are resolved."
    )
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_warnings(result: dict) -> None:
    """هشدارهای بازبینی — بازدارنده نیستند ولی بازبین باید بداند."""
    warnings = step5_warnings(result)
    if not warnings:
        return

    st.subheader("Warnings")
    for warning in warnings:
        st.warning(warning)


def _render_review_items(result: dict) -> None:
    """مواردِ حل‌نشده — هیچ‌کدام حدس زده نشده و همه با منبعشان می‌مانند."""
    st.subheader("Unresolved clarifications")

    rows = step5_review_item_rows(result)
    if not rows:
        st.caption("None — no clarification or test case was left open.")
        return

    st.warning(
        "These stayed open through the previous steps and are reported here as "
        "they are: no HTTP status, response field or value was guessed for them. "
        "They require a human answer — but they do not make the collection itself "
        "invalid."
    )
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_collection(result: dict) -> None:
    """کالکشنِ صادرشده را کامل نشان می‌دهد — پشتِ expander پنهان نمی‌شود."""
    st.subheader("Postman collection")

    collection = postman_collection(result)
    name = collection_name(collection)

    st.markdown(f"**Collection name:** {name or '—'}")
    st.caption(
        "This is the collection exactly as Step 4 generated it — shown in full "
        "rather than hidden behind a section."
    )
    st.json(collection)


def _render_export(result: dict) -> None:
    """صادرات — همان کالکشنِ قدم چهارم، بدونِ هیچ تغییری."""
    st.subheader("Export")

    collection = postman_collection(result)
    filename = collection_filename(collection_name(collection))

    st.caption(
        "The exported file is the Step 4 collection itself. Step 5 does not "
        "regenerate it, does not modify it and does not add its own metadata to it — "
        "the review result lives only in this page's JSON, never inside the "
        "collection."
    )
    st.download_button(
        "Download collection JSON",
        data=collection_json(collection),
        file_name=filename,
        mime="application/json",
    )


def _render_approval(result: dict) -> None:
    """تأییدِ انسانیِ قدم پنجم — یعنی artifact بازبینی و پذیرفته شد."""
    st.subheader("Review")

    if st.session_state.get(_APPROVED_KEY):
        st.success(
            "Step 5 approved — the reviewed collection is accepted as the final "
            "artifact of this workflow."
        )
        return

    metrics = step5_metrics(result)
    if metrics["issues"]:
        st.caption(
            f"{metrics['issues']} blocking issue(s) are reported above. Approving "
            f"records that you reviewed the artifact — it does not resolve them, "
            f"and nothing is executed by this step."
        )
    elif metrics["clarifications"]:
        st.caption(
            f"{metrics['clarifications']} unresolved item(s) are reported above. "
            f"Approving records that you reviewed and accepted the artifact as-is."
        )
    else:
        st.caption(
            "Approving records that you reviewed the artifact. It does not execute "
            "the collection, does not call any API and does not change any earlier "
            "step."
        )

    if st.button("Approve Step 5"):
        st.session_state[_APPROVED_KEY] = True
        st.success(
            "Step 5 approved — the reviewed collection is accepted as the final "
            "artifact of this workflow."
        )


def _render_result(result: dict) -> None:
    """گزارشِ بازبینی را برای تصمیمِ انسانی نمایش می‌دهد."""
    _render_status(result)
    _render_metrics(result)
    _render_issues(result)
    _render_warnings(result)
    _render_review_items(result)
    _render_collection(result)
    _render_export(result)


def _render_inputs() -> None:
    """فرمِ ورودی را می‌سازد و در صورت ارسال، بازبینی را اجرا می‌کند."""
    with st.form("final_review_form"):
        raw: dict[str, str] = {}
        for label, key, hint in _INPUT_KEYS:
            raw[key] = st.text_area(
                f"{label} result (JSON) *",
                height=160,
                placeholder=f"Paste {hint}.",
                key=f"final_review_{key}",
            )
        submitted = st.form_submit_button("Run final review")

    if not submitted:
        return

    for label, key, _ in _INPUT_KEYS:
        if not raw[key].strip():
            st.session_state[_RESULT_KEY] = None
            st.session_state[_ERROR_KEY] = f"The {label} result is required."
            return

    _review(raw)


def main() -> None:
    st.set_page_config(
        page_title="Step 5 — Final Review & Export",
        page_icon="✅",
        layout="wide",
    )

    st.title("Step 5 — Final Review & Export")
    st.caption(
        "Steps 1–4 results → a deterministic final review: coverage, cross-step "
        "consistency, metrics and the unresolved items that stayed open, plus the "
        "Step 4 collection for export. This step does not call an LLM, does not "
        "fetch Swagger, does not rediscover APIs, does not execute any API and does "
        "not run the collection."
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
