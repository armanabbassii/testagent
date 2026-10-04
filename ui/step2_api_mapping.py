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

اجرا (از ریشه‌ی پروژه):

    uv sync --group ui
    uv run --group ui streamlit run ui/step2_api_mapping.py
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
from ui.formatting import (  # noqa: E402
    mapping_counts,
    mapping_details,
    service_details,
)

# شناسه‌ی کاربری که به‌عنوان هدر x-user-id به سرویسِ LLM فرستاده می‌شود
_UI_USER_ID = "step2-api-mapping-ui"

_RESULT_KEY = "api_mapping_result"
_ERROR_KEY = "api_mapping_error"
_TEST_CASES_KEY = "api_mapping_test_cases"
_APPROVED_KEY = "api_mapping_approved"


def _analyze(step1_json: str, services_raw: str) -> None:
    """کشف و نگاشت را اجرا می‌کند و نتیجه یا خطا را در session_state می‌گذارد.

    هر نتیجه‌ی تازه تأییدِ قبلی را باطل می‌کند تا تأییدِ کهنه روی نتیجه‌ی
    جدید نماند.
    """
    st.session_state[_APPROVED_KEY] = False

    try:
        step1_result = json.loads(step1_json)
    except json.JSONDecodeError as exc:
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = f"Step 1 result is not valid JSON: {exc}"
        return

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
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = f"Invalid input: {exc}"
    except ApiMappingError as exc:
        # سندِ Swagger خوانده نشد یا نگاشت با قرارداد نخواند
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = f"API mapping failed:\n\n{exc}"
    except Exception as exc:  # noqa: BLE001 — خطای شبکه/سرویس نباید UI را بترکاند
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = (
            f"API mapping failed: {type(exc).__name__}: {exc}"
        )
    else:
        st.session_state[_RESULT_KEY] = result
        st.session_state[_TEST_CASES_KEY] = extract_test_cases(step1_result)
        st.session_state[_ERROR_KEY] = None


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
    """فرمِ ورودی را می‌سازد و در صورت ارسال، کشف و نگاشت را اجرا می‌کند."""
    with st.form("api_mapping_form"):
        step1_json = st.text_area(
            "Step 1 result (JSON) *",
            height=220,
            placeholder=(
                "Paste the JSON produced by Step 1 — the 'Raw JSON' section at the "
                "bottom of the Step 1 page is exactly this."
            ),
        )
        services_raw = st.text_area(
            "Swagger / OpenAPI sources *",
            height=140,
            placeholder=(
                "One Swagger/OpenAPI URL or local file path per line, e.g.\n"
                "https://podium-admin.sandpod.ir/api/swagger-ui/index.html"
                "?urls.primaryName=Admin#/voucher-admin-controller/getVoucherDetails"
            ),
        )
        submitted = st.form_submit_button("Analyze APIs & Map Test Cases")

    if not submitted:
        return

    if not step1_json.strip():
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = "The Step 1 result is required."
        return

    if not split_sources(services_raw):
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = "At least one Swagger source is required."
        return

    _analyze(step1_json, services_raw)


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

    _render_inputs()

    error = st.session_state.get(_ERROR_KEY)
    if error:
        st.error(error)

    result = st.session_state.get(_RESULT_KEY)
    if result:
        _render_result(result, st.session_state.get(_TEST_CASES_KEY) or [])
        _render_approval()


main()
