"""
ui/step3_scenario_analysis.py — رابطِ ساده‌ی Streamlit برای قدمِ سومِ جریان

    نتیجه‌ی قدم ۱ (تست‌کیس‌های کسب‌وکاری)
  + نتیجه‌ی قدم ۲ (کشفِ API و نگاشتِ تست‌کیس به عملیات)
        → سناریوها، ترتیبِ اجرا و وابستگی‌های داده
        → بازبینی و تأییدِ انسانی

این لایه کاملاً نازک است: ورودی می‌گیرد، Step3ScenarioAnalysisGenerator را صدا
می‌زند، خطا را نشان می‌دهد و نتیجه را نمایش می‌دهد. هیچ منطقِ کسب‌وکاری اینجا
نیست — ساختِ prompt، فراخوانیِ LLM و اعتبارسنجیِ قطعیِ سناریو/وابستگی همه در
src/agents/test_case_generator/scenario_analysis.py می‌مانند.

این صفحه هیچ API ای را اجرا نمی‌کند، هیچ سندِ Swagger ای را نمی‌خواند، هیچ
مجموعه‌ی Postman ای نمی‌سازد و قدم چهارم را اجرا نمی‌کند.

همین صفحه در ویزاردِ ui/app.py هم استفاده می‌شود: render_step کارِ نمایش و اجرا
را انجام می‌دهد و main فقط پوسته‌ی صفحه‌ی مستقل است. داخلِ ویزارد، نتیجه‌های قدم
اول و دوم خودکار از وضعیتِ جریان می‌آیند و این قدم هیچ ورودیِ دستیِ دیگری ندارد.

اجرا (از ریشه‌ی پروژه):

    uv sync --group ui
    uv run --group ui streamlit run ui/step3_scenario_analysis.py

یا از داخلِ ویزارد: uv run --group ui streamlit run ui/app.py
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

from src.agents.test_case_generator.scenario_analysis import (  # noqa: E402
    ScenarioAnalysisError,
    Step3ScenarioAnalysisGenerator,
    extract_test_cases,
)
from src.debug import DebugConfig  # noqa: E402
from ui.components import render_propagated_inputs  # noqa: E402
from ui.formatting import (  # noqa: E402
    clarification_details,
    dependency_details,
    execution_arrow,
    execution_steps,
    scenario_details,
    step3_counts,
)
from ui.workflow import (  # noqa: E402
    RESULT_ARGUMENT,
    WorkflowState,
    run_label,
)

# شناسه‌ی کاربری که به‌عنوان هدر x-user-id به سرویسِ LLM فرستاده می‌شود
_UI_USER_ID = "step3-scenario-analysis-ui"

_RESULT_KEY = "scenario_analysis_result"
_ERROR_KEY = "scenario_analysis_error"
_TEST_CASES_KEY = "scenario_analysis_test_cases"
_APPROVED_KEY = "scenario_analysis_approved"

_STEP1_PLACEHOLDER = (
    "Paste the JSON produced by Step 1 — the 'Raw JSON' section at the bottom of "
    "the Step 1 page is exactly this."
)
_STEP2_PLACEHOLDER = (
    "Paste the JSON produced by Step 2 — the 'Raw JSON' section at the bottom of "
    "the Step 2 page is exactly this."
)


def _run(
    step1_result: dict, step2_result: dict
) -> tuple[dict | None, str | None]:
    """تحلیلِ سناریو را اجرا می‌کند و (نتیجه، خطا) را برمی‌گرداند.

    هیچ چیزی در session_state نوشته نمی‌شود: نگه‌داشتنِ نتیجه کارِ فراخوان است —
    صفحه‌ی مستقل آن را در session_state می‌گذارد و ویزارد در وضعیتِ جریان.
    """
    try:
        with st.spinner("Grouping scenarios and analysing dependencies..."):
            result = Step3ScenarioAnalysisGenerator(
                debug_config=DebugConfig.from_env()
            ).generate(
                step1_result=step1_result,
                step2_result=step2_result,
                user_id=_UI_USER_ID,
            )
    except ScenarioAnalysisError as exc:
        # ورودیِ نامعتبر یا خروجیِ ناسازگار با قرارداد
        return None, f"Scenario analysis failed:\n\n{exc}"
    except Exception as exc:  # noqa: BLE001 — خطای شبکه/سرویس نباید UI را بترکاند
        return None, f"Scenario analysis failed: {type(exc).__name__}: {exc}"

    return result, None


def _store(result: dict | None, error: str | None) -> None:
    """نتیجه یا خطا را در session_state می‌گذارد (حالتِ صفحه‌ی مستقل).

    هر نتیجه‌ی تازه تأییدِ قبلی را باطل می‌کند تا تأییدِ کهنه روی نتیجه‌ی
    جدید نماند.
    """
    st.session_state[_APPROVED_KEY] = False
    st.session_state[_RESULT_KEY] = result
    st.session_state[_ERROR_KEY] = error


def _render_scenarios(result: dict, test_cases: list[dict]) -> None:
    """هر سناریو را با فهرستِ شماره‌دارِ تست‌کیس‌هایش نشان می‌دهد."""
    st.subheader("Scenarios")

    details = scenario_details(result, test_cases)
    if not details:
        st.caption("No scenario was identified.")
        return

    for scenario in details:
        st.markdown(f"#### {scenario['heading']}")
        st.markdown(f"**Reason:** {scenario['reason'] or '—'}")
        for case in scenario["cases"]:
            st.markdown(f"{case['position']}. {case['heading']}")
        st.divider()


def _render_execution_order(result: dict, test_cases: list[dict]) -> None:
    """ترتیبِ اجرا را یک‌خطی و سپس هر گام را با وابستگی‌هایش نشان می‌دهد."""
    st.subheader("Execution Order")

    arrow = execution_arrow(result, test_cases)
    if not arrow:
        st.caption("No execution order was established.")
        return

    st.markdown(f"### {arrow}")

    for step in execution_steps(result, test_cases):
        st.markdown(f"**{step['order']}. {step['heading']}**")
        st.markdown(f"Depends on: {step['depends_on_label'] or 'None'}")
        st.divider()


def _render_data_dependencies(result: dict, test_cases: list[dict]) -> None:
    """هر وابستگیِ داده را با مبدأ، مقصدها، اطمینان و دلیلش نشان می‌دهد."""
    st.subheader("Data Dependencies")

    details = dependency_details(result, test_cases)
    if not details:
        st.caption("No data dependency was identified.")
        return

    for dependency in details:
        st.markdown(f"#### {dependency['variable_name'] or '—'}")
        st.markdown(f"**Source:** {dependency['source']['label'] or '—'}")
        st.markdown("**Targets:**")
        for target in dependency["targets"]:
            st.markdown(f"- {target['label']}")
        st.markdown(f"**Confidence:** {dependency['confidence'] or '—'}")
        st.markdown(f"**Reason:** {dependency['reason'] or '—'}")
        st.divider()


def _render_clarifications(result: dict, test_cases: list[dict]) -> None:
    """ابهام‌هایی که تحلیل نتوانسته قطعی کند."""
    st.subheader("Clarifications")

    details = clarification_details(result, test_cases)
    if not details:
        st.caption("None — the analysis did not leave any open question.")
        return

    st.caption(
        "The analysis could not settle these points and refused to guess — they "
        "need a human answer."
    )
    for clarification in details:
        prefix = clarification["heading"] or clarification["type"] or "General"
        st.warning(f"{prefix}: {clarification['message']}")


def _render_result(result: dict, test_cases: list[dict]) -> None:
    """نتیجه‌ی قدم سوم را برای بازبینیِ انسانی نمایش می‌دهد."""
    counts = step3_counts(result)
    columns = st.columns(4)
    columns[0].metric("Scenarios", counts["scenarios"])
    columns[1].metric("Ordered", counts["ordered"])
    columns[2].metric("Data Dependencies", counts["dependencies"])
    columns[3].metric("Clarifications", counts["clarifications"])

    _render_scenarios(result, test_cases)
    _render_execution_order(result, test_cases)
    _render_data_dependencies(result, test_cases)
    _render_clarifications(result, test_cases)

    with st.expander("Raw JSON (input for the next step of the workflow)"):
        st.json(result)


def _render_approval() -> None:
    """تأییدِ انسانیِ قدم سوم — نتیجه در همین نشست نگه داشته می‌شود."""
    st.subheader("Review")

    if st.session_state.get(_APPROVED_KEY):
        st.success(
            "Step 3 approved — this analysis is held for the next workflow step."
        )
        return

    if st.button("Approve Step 3"):
        st.session_state[_APPROVED_KEY] = True
        st.success(
            "Step 3 approved — this analysis is held for the next workflow step."
        )


def _render_inputs() -> None:
    """فرمِ ورودیِ صفحه‌ی مستقل را می‌سازد و در صورت ارسال، تحلیل را اجرا می‌کند."""
    with st.form("scenario_analysis_form"):
        step1_raw = st.text_area(
            "Step 1 result (JSON) *",
            height=180,
            placeholder=_STEP1_PLACEHOLDER,
        )
        step2_raw = st.text_area(
            "Step 2 result (JSON) *",
            height=180,
            placeholder=_STEP2_PLACEHOLDER,
        )
        submitted = st.form_submit_button("Analyze Scenarios & Dependencies")

    if not submitted:
        return

    if not step1_raw.strip():
        _store(None, "The Step 1 result is required.")
        return

    if not step2_raw.strip():
        _store(None, "The Step 2 result is required.")
        return

    try:
        step1_result = json.loads(step1_raw)
        step2_result = json.loads(step2_raw)
    except json.JSONDecodeError as exc:
        _store(None, f"Input is not valid JSON: {exc}")
        return

    result, error = _run(step1_result, step2_result)
    _store(result, error)
    if error is None:
        st.session_state[_TEST_CASES_KEY] = extract_test_cases(step1_result)


def _render_standalone() -> None:
    """صفحه‌ی مستقلِ قدمِ سوم — ورودی دستی و نتیجه در session_state."""
    _render_inputs()

    error = st.session_state.get(_ERROR_KEY)
    if error:
        st.error(error)

    result = st.session_state.get(_RESULT_KEY)
    if result:
        _render_result(result, st.session_state.get(_TEST_CASES_KEY) or [])
        _render_approval()


def _render_in_workflow(workflow: WorkflowState) -> None:
    """قدمِ سوم داخلِ ویزارد: نتیجه‌های قدم اول و دوم خودکار می‌آیند.

    این قدم هیچ ورودیِ دستی ندارد — پس فقط یک دکمه‌ی اجرا لازم است.
    """
    render_propagated_inputs(workflow, 3)
    st.caption(
        "This step needs no further input: the Step 1 test cases and the Step 2 "
        "mapping above are enough."
    )

    if st.button(run_label(workflow, 3), key="workflow_run_3"):
        payload = workflow.step_inputs(3)
        upstream = [RESULT_ARGUMENT[source] for source in (1, 2)]
        if any(name not in payload for name in upstream):
            st.error(
                "The Step 1 and Step 2 results are not available yet. Run them and "
                "approve Step 2 first."
            )
        else:
            result, error = _run(payload[upstream[0]], payload[upstream[1]])
            if error:
                workflow.record_error(3, error)
            else:
                workflow.record_result(3, result)
            # کلِ صفحه (از جمله نوارِ قدم‌ها) با وضعیتِ تازه دوباره کشیده شود.
            st.rerun()

    result = workflow.result(3)
    if result:
        step1_result = workflow.result(1)
        _render_result(
            result, extract_test_cases(step1_result) if step1_result else []
        )


def render_step(workflow: WorkflowState | None = None) -> None:
    """ورودی و نتیجه‌ی قدمِ سوم را نشان می‌دهد.

    workflow=None یعنی صفحه‌ی مستقل (ورودی دستی، نتیجه در session_state).
    workflow یعنی داخلِ ویزارد (ورودی و نتیجه در وضعیتِ جریان).
    """
    if workflow is None:
        _render_standalone()
    else:
        _render_in_workflow(workflow)


def main() -> None:
    st.set_page_config(
        page_title="Step 3 — Scenario Analysis",
        page_icon="🔗",
        layout="wide",
    )

    st.title("Step 3 — Scenario & Dependency Analysis")
    st.caption(
        "Step 1 test cases + Step 2 API mappings → scenarios, execution order and "
        "data dependencies for human review. This step does not fetch or parse "
        "Swagger, does not execute any API and does not generate a Postman "
        "collection."
    )

    render_step()


if __name__ == "__main__":
    main()
