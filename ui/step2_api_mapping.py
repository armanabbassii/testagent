"""
ui/step2_api_mapping.py — رابطِ ساده‌ی Streamlit برای قدمِ دومِ جریان

    Step 1 Test Cases + Developed Services (Swagger)
        → کشفِ API از سندِ Swagger
        → نگاشتِ هر تست‌کیس به یک عملیاتِ واقعی
        → بازبینی و تأییدِ انسانی

این لایه کاملاً نازک است: ورودی می‌گیرد، Step2ApiMappingGenerator را صدا
می‌زند، خطا را نشان می‌دهد و نتیجه را نمایش می‌دهد. هیچ منطقِ کسب‌وکاری اینجا
نیست — دریافت و parse سندِ Swagger، اعتبارسنجیِ نگاشت و فراخوانیِ LLM همه در
src/agents/test_case_generator/ می‌مانند.

این صفحه به جریانِ Swagger-first کاری ندارد و هیچ API ای را اجرا نمی‌کند، هیچ
Postman ای نمی‌سازد و هیچ وابستگیِ سناریویی تولید نمی‌کند.

همین صفحه در ویزاردِ ui/app.py هم استفاده می‌شود: render_step کارِ نمایش و اجرا
را انجام می‌دهد و main فقط پوسته‌ی صفحه‌ی مستقل است. داخلِ ویزارد، نتیجه‌ی قدم
اول خودکار از وضعیتِ جریان می‌آید و فقط منابعِ Swagger از کاربر پرسیده می‌شود.

اجرا (از ریشه‌ی پروژه):

    uv sync --group ui
    uv run --group ui streamlit run ui/step2_api_mapping.py

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

from src.agents.test_case_generator.api_discovery import split_sources  # noqa: E402
from src.agents.test_case_generator.api_mapping import (  # noqa: E402
    ApiMappingError,
    Step2ApiMappingGenerator,
    extract_test_cases,
)
from src.debug import DebugConfig  # noqa: E402
from ui.components import render_propagated_inputs  # noqa: E402
from ui.formatting import (  # noqa: E402
    mapping_counts,
    mapping_details,
    service_details,
)
from ui.workflow import (  # noqa: E402
    INPUT_SWAGGER_SOURCES,
    RESULT_ARGUMENT,
    WorkflowState,
    run_label,
)

# شناسه‌ی کاربری که به‌عنوان هدر x-user-id به سرویسِ LLM فرستاده می‌شود
_UI_USER_ID = "step2-api-mapping-ui"

_RESULT_KEY = "api_mapping_result"
_ERROR_KEY = "api_mapping_error"
_TEST_CASES_KEY = "api_mapping_test_cases"
_APPROVED_KEY = "api_mapping_approved"

_STEP1_PLACEHOLDER = (
    "Paste the JSON produced by Step 1 — the 'Raw JSON' section at the bottom of "
    "the Step 1 page is exactly this."
)
_SOURCES_PLACEHOLDER = (
    "One Swagger/OpenAPI URL or local file path per line, e.g.\n"
    "https://podium-admin.sandpod.ir/api/swagger-ui/index.html"
    "?urls.primaryName=Admin#/voucher-admin-controller/getVoucherDetails"
)


def _run(
    step1_result: dict, services_raw: str
) -> tuple[dict | None, str | None]:
    """کشف و نگاشت را اجرا می‌کند و (نتیجه، خطا) را برمی‌گرداند.

    هیچ چیزی در session_state نوشته نمی‌شود: نگه‌داشتنِ نتیجه کارِ فراخوان است —
    صفحه‌ی مستقل آن را در session_state می‌گذارد و ویزارد در وضعیتِ جریان.
    """
    sources = split_sources(services_raw)

    try:
        with st.spinner("Discovering APIs and mapping test cases..."):
            result = Step2ApiMappingGenerator(
                debug_config=DebugConfig.from_env()
            ).generate(
                step1_result=step1_result,
                service_sources=sources,
                user_id=_UI_USER_ID,
            )
    except ValueError as exc:
        # ورودیِ نامعتبر — مثلاً فهرستِ خالیِ سرویس‌ها
        return None, f"Invalid input: {exc}"
    except ApiMappingError as exc:
        # سندِ Swagger خوانده نشد یا نگاشت با قرارداد نخواند
        return None, f"API mapping failed:\n\n{exc}"
    except Exception as exc:  # noqa: BLE001 — خطای شبکه/سرویس نباید UI را بترکاند
        return None, f"API mapping failed: {type(exc).__name__}: {exc}"

    return result, None


def _store(result: dict | None, error: str | None) -> None:
    """نتیجه یا خطا را در session_state می‌گذارد (حالتِ صفحه‌ی مستقل).

    هر نتیجه‌ی تازه تأییدِ قبلی را باطل می‌کند تا تأییدِ کهنه روی نتیجه‌ی
    جدید نماند.
    """
    st.session_state[_APPROVED_KEY] = False
    st.session_state[_RESULT_KEY] = result
    st.session_state[_ERROR_KEY] = error


def _render_services(result: dict) -> None:
    """سرویس‌های کشف‌شده و عملیات‌های واقعیِ هرکدام را نشان می‌دهد."""
    st.subheader("Discovered Services")

    for service in service_details(result):
        st.markdown(f"#### {service['name'] or '—'}")
        st.markdown(f"**Swagger:** {service['source_url'] or '—'}")
        st.markdown(f"**Base URL:** {service['base_url'] or '—'}")
        if service["authorization"]:
            st.markdown(f"**Authorization:** `{service['authorization']}`")
        for note in service["unresolved"]:
            st.warning(note)

    st.subheader("Discovered APIs")
    for service in service_details(result):
        st.markdown(f"**{service['name'] or '—'}**")
        if not service["apis"]:
            st.caption("No operation was discovered for this service.")
            continue
        for api in service["apis"]:
            st.markdown(f"`{api['label']}`")
            if api["operation"]:
                st.caption(f"Operation: {api['operation']}")


def _render_mappings(result: dict, test_cases: list[dict]) -> None:
    """هر تست‌کیس را با نگاشتِ کاملش نشان می‌دهد — هیچ‌کدام پنهان نمی‌شود."""
    st.subheader("Test Case Mapping")

    for detail in mapping_details(result, test_cases):
        st.markdown(f"#### {detail['heading']}")
        st.markdown(f"**Mapped API:** {detail['api'] or 'Not resolved'}")
        st.markdown(f"**Operation:** {detail['operation'] or '—'}")
        st.markdown(f"**Confidence:** {detail['confidence'] or '—'}")
        st.markdown(f"**Reason:** {detail['reason'] or '—'}")
        if detail["clarification"]:
            st.warning(f"Clarification: {detail['clarification']}")
        st.divider()


def _render_result(result: dict, test_cases: list[dict]) -> None:
    """نتیجه‌ی قدم دوم را برای بازبینیِ انسانی نمایش می‌دهد."""
    counts = mapping_counts(result)
    columns = st.columns(3)
    columns[0].metric("Services", len(result["services"]))
    columns[1].metric("Mapped", counts["resolved"])
    columns[2].metric("Not resolved", counts["unresolved"])

    _render_services(result)
    _render_mappings(result, test_cases)

    if result["clarifications"]:
        st.subheader("Clarifications")
        st.caption(
            "These points could not be settled automatically — they need a human "
            "answer before the mapping can be trusted."
        )
        for item in result["clarifications"]:
            st.warning(item)

    with st.expander("Raw JSON (input for the next step of the workflow)"):
        st.json(result)


def _render_approval() -> None:
    """تأییدِ انسانیِ قدم دوم — نتیجه در همین نشست نگه داشته می‌شود."""
    st.subheader("Review")

    if st.session_state.get(_APPROVED_KEY):
        st.success(
            "Step 2 approved — this mapping is held for the next workflow step."
        )
        return

    if st.button("Approve Step 2"):
        st.session_state[_APPROVED_KEY] = True
        st.success(
            "Step 2 approved — this mapping is held for the next workflow step."
        )


def _render_inputs() -> None:
    """فرمِ ورودیِ صفحه‌ی مستقل را می‌سازد و در صورت ارسال، کشف و نگاشت را اجرا می‌کند."""
    with st.form("api_mapping_form"):
        step1_json = st.text_area(
            "Step 1 result (JSON) *",
            height=220,
            placeholder=_STEP1_PLACEHOLDER,
        )
        services_raw = st.text_area(
            "Swagger / OpenAPI sources *",
            height=140,
            placeholder=_SOURCES_PLACEHOLDER,
        )
        submitted = st.form_submit_button("Analyze APIs & Map Test Cases")

    if not submitted:
        return

    if not step1_json.strip():
        _store(None, "The Step 1 result is required.")
        return

    if not split_sources(services_raw):
        _store(None, "At least one Swagger source is required.")
        return

    try:
        step1_result = json.loads(step1_json)
    except json.JSONDecodeError as exc:
        _store(None, f"Step 1 result is not valid JSON: {exc}")
        return

    result, error = _run(step1_result, services_raw)
    _store(result, error)
    if error is None:
        st.session_state[_TEST_CASES_KEY] = extract_test_cases(step1_result)


def _render_standalone() -> None:
    """صفحه‌ی مستقلِ قدمِ دوم — ورودی دستی و نتیجه در session_state."""
    _render_inputs()

    error = st.session_state.get(_ERROR_KEY)
    if error:
        st.error(error)

    result = st.session_state.get(_RESULT_KEY)
    if result:
        _render_result(result, st.session_state.get(_TEST_CASES_KEY) or [])
        _render_approval()


def _render_in_workflow(workflow: WorkflowState) -> None:
    """قدمِ دوم داخلِ ویزارد: نتیجه‌ی قدم اول خودکار می‌آید، فقط Swagger پرسیده می‌شود.

    منابعِ Swagger یک‌بار در وضعیتِ جریان می‌مانند؛ اجرای دوباره همان‌ها را
    از قبل پر می‌کند و کاربر فقط در صورت نیاز عوضشان می‌کند.
    """
    render_propagated_inputs(workflow, 2)

    with st.form("api_mapping_form"):
        services_raw = st.text_area(
            "Swagger / OpenAPI sources *",
            value=workflow.input_value(INPUT_SWAGGER_SOURCES),
            height=140,
            placeholder=_SOURCES_PLACEHOLDER,
        )
        submitted = st.form_submit_button(run_label(workflow, 2))

    if submitted:
        payload = workflow.step_inputs(2)
        upstream = RESULT_ARGUMENT[1]
        if upstream not in payload:
            st.error(
                "The Step 1 result is not available yet. Run Step 1 and approve "
                "it first."
            )
        elif not split_sources(services_raw):
            st.error("At least one Swagger source is required.")
        else:
            workflow.set_input(INPUT_SWAGGER_SOURCES, services_raw)
            result, error = _run(payload[upstream], services_raw)
            if error:
                workflow.record_error(2, error)
            else:
                workflow.record_result(2, result)
            # کلِ صفحه (از جمله نوارِ قدم‌ها) با وضعیتِ تازه دوباره کشیده شود.
            st.rerun()

    result = workflow.result(2)
    if result:
        step1_result = workflow.result(1)
        _render_result(
            result, extract_test_cases(step1_result) if step1_result else []
        )


def render_step(workflow: WorkflowState | None = None) -> None:
    """ورودی و نتیجه‌ی قدمِ دوم را نشان می‌دهد.

    workflow=None یعنی صفحه‌ی مستقل (ورودی دستی، نتیجه در session_state).
    workflow یعنی داخلِ ویزارد (ورودی و نتیجه در وضعیتِ جریان).
    """
    if workflow is None:
        _render_standalone()
    else:
        _render_in_workflow(workflow)


def main() -> None:
    st.set_page_config(
        page_title="Step 2 — API Mapping",
        page_icon="🔗",
        layout="wide",
    )

    st.title("Step 2 — Swagger Analysis & API Mapping")
    st.caption(
        "Step 1 test cases + Swagger sources → discovered API operations → a "
        "reviewable test case ↔ API mapping. This step does not execute any API, "
        "does not generate a Postman collection and does not build scenario "
        "dependencies."
    )

    render_step()


if __name__ == "__main__":
    main()
