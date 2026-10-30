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
from src.agents.test_case_generator.api_mapping import extract_test_cases  # noqa: E402
from src.agents.test_case_generator.source_selection import (  # noqa: E402
    EVIDENCE_LABELS,
    SEMANTIC_NOT_RUN,
    SourceSelection,
)
from src.agents.test_case_generator.swagger_snapshots import (  # noqa: E402
    SELECTION_AUTO,
    SWAGGER_SOURCES,
    SwaggerSnapshotError,
    enabled_sources,
    expand_selection,
    selection_for_keys,
    snapshot_available,
    snapshot_fingerprint,
    snapshot_path,
    source_for,
)
from ui.components import render_propagated_inputs, run_step_key  # noqa: E402
from ui.pipeline import (  # noqa: E402
    execute_step,
    explicit_selection,
    plan_swagger_sources,
    run_api_mapping,
)
from ui.formatting import (  # noqa: E402
    mapping_counts,
    mapping_details,
    service_details,
)
from ui.swagger_refresh import (  # noqa: E402
    is_refreshing,
    refresh_status,
    run_pending_refresh,
    start_refresh,
)
from ui.workflow import (  # noqa: E402
    INPUT_SWAGGER_SELECTION,
    INPUT_SWAGGER_SNAPSHOTS,
    WorkflowState,
    run_label,
)

_SPINNER = "Discovering APIs and mapping test cases..."

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
    step1_result: dict,
    services_raw: str,
    snapshot_keys: list[str] | None = None,
) -> tuple[dict | None, str | None]:
    """کشف و نگاشت را اجرا می‌کند و (نتیجه، خطا) را برمی‌گرداند.

    ``snapshot_keys`` مسیرِ عادیِ ویزارد است: از snapshotهای محلی خوانده
    می‌شود و هیچ درخواستی به شبکه زده نمی‌شود. بدونِ آن، همان مسیرِ قبلیِ
    ``services_raw`` (URL یا مسیرِ فایل) اجرا می‌شود.

    هیچ چیزی در session_state نوشته نمی‌شود: نگه‌داشتنِ نتیجه کارِ فراخوان است —
    صفحه‌ی مستقل آن را در session_state می‌گذارد و ویزارد در وضعیتِ جریان.
    فراخوانیِ Generator در ui/pipeline.py است (مشترک با ویزارد و Run All).
    """
    with st.spinner(_SPINNER):
        return run_api_mapping(step1_result, services_raw, snapshot_keys=snapshot_keys)


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


def _render_result(result: dict, test_cases: list[dict], selection: str = "") -> None:
    """نتیجه‌ی قدم دوم را برای بازبینیِ انسانی نمایش می‌دهد.

    اولویتِ نمایش با نگاشت‌هاست: کاربر باید «TC-001 → GET /admin/voucher/{id}»
    را ببیند، نه هزاران عملیاتِ بی‌ربط. کاتالوگِ کاملِ کشف‌شده عمداً جمع‌شده
    می‌ماند — برای اعتبارسنجی و اشکال‌زدایی لازم است، ولی خروجیِ اصلی نیست.
    """
    counts = mapping_counts(result)
    columns = st.columns(3)
    columns[0].metric("Services", len(result["services"]))
    columns[1].metric("Mapped", counts["resolved"])
    columns[2].metric("Not resolved", counts["unresolved"])

    if selection:
        st.caption(f"Swagger sources: **{selection}**")

    _render_mappings(result, test_cases)

    if result["clarifications"]:
        st.subheader("Clarifications")
        st.caption(
            "These points could not be settled automatically — they need a human "
            "answer before the mapping can be trusted."
        )
        for item in result["clarifications"]:
            st.warning(item)

    st.caption(
        f"The full discovered catalogue ({sum(len(service['apis']) for service in result['services'])} "
        "operations) is kept for validation and debugging. It is not part of the "
        "input for the next step."
    )
    with st.expander("Full discovered catalogue (debug)"):
        _render_services(result)

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


def _render_update_buttons() -> None:
    """دکمه‌ی Update هر منبعِ Swagger — سندِ تازه را می‌گیرد و snapshot را جایگزین می‌کند.

    هر دکمه فقط فایلِ منبعِ خودش را عوض می‌کند. کلیک فقط وضعیت را «در حالِ
    بارگذاری» می‌کند و صفحه دوباره کشیده می‌شود تا همان دکمه غیرفعال دیده شود؛
    درخواست در اجرای بعد فرستاده می‌شود. دکمه‌ی منبعِ دیگر فعال می‌ماند. نگاشت
    خودکار اجرا نمی‌شود.
    """
    st.caption(
        "Fetch the latest Swagger document of a source and replace its local "
        "snapshot. The file is only replaced when the download is complete and "
        "valid. Step 2 is not run automatically."
    )
    columns = st.columns(len(SWAGGER_SOURCES))
    for column, source in zip(columns, SWAGGER_SOURCES):
        with column:
            loading = is_refreshing(st.session_state, source.key)
            if st.button(
                f"Fetching {source.label}..." if loading else f"Update {source.label}",
                key=f"workflow_update_{source.key}",
                disabled=loading,
                help=f"Fetch {source.remote_url} and replace data/swagger/{source.filename}",
                width="stretch",
            ):
                start_refresh(st.session_state, source.key)
                st.rerun()
            st.caption(f"`{source.filename}`")
            status = refresh_status(st.session_state, source.key)
            if status is not None:
                (st.success if status.ok else st.error)(status.message)

    # درخواست‌ها بعد از کشیدنِ همه‌ی دکمه‌ها فرستاده می‌شوند تا دکمه‌ی منبعِ دیگر
    # در طولِ انتظار دیده و قابلِ کلیک بماند.
    for column, source in zip(columns, SWAGGER_SOURCES):
        if not is_refreshing(st.session_state, source.key):
            continue
        with column, st.spinner(f"Fetching {source.label} Swagger from {source.remote_url}..."):
            run_pending_refresh(st.session_state, source.key)
        st.rerun()


_MODE_AUTO = "Automatic (recommended)"
_MODE_MANUAL = "Choose manually"


def _stored_manual_keys(workflow: WorkflowState) -> list[str]:
    """کلیدهای انتخابِ دستیِ ذخیره‌شده — یا [] (خودکار یا مقدارِ نامعتبر)."""
    try:
        return expand_selection(explicit_selection(workflow))
    except SwaggerSnapshotError:
        return []


def _render_selection(plan: SourceSelection) -> None:
    """هر منبعِ ثبت‌شده: فعال؟ snapshot دارد؟ برای این اجرا انتخاب شد؟ چرا؟

    «ثبت‌شده»، «فعال»، «انتخاب‌شده» و «دارای عملیاتِ مرتبط» جدا نشان داده
    می‌شوند تا معلوم باشد قدم ۲ دقیقاً کدام snapshot را می‌خواند و چرا.
    """
    rows = []
    for source in SWAGGER_SOURCES:
        evidence = plan.evidence_for(source.key)
        rows.append(
            {
                "Source": source.label,
                "Enabled": "yes" if source.enabled else "no",
                "Snapshot": source.filename if snapshot_available(source.key) else "⚠ missing",
                "Selected": "✅" if source.key in plan.keys and not plan.error else "",
                "Evidence": "; ".join(
                    f"{EVIDENCE_LABELS[item.kind]}: {item.detail}" for item in evidence
                ),
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")

    unresolved = plan.urls.unresolved
    unassigned = [match.path for match in plan.endpoints if not match.owners]
    if unresolved or unassigned:
        st.caption(
            "Not assigned to a source: "
            + ", ".join([*unresolved, *unassigned])
        )
    for warning in plan.warnings:
        st.warning(warning)
    if plan.semantic == SEMANTIC_NOT_RUN:
        # پیش‌نمایش LLM را صدا نمی‌زند؛ تحلیلِ متنِ تسک با دکمه‌ی Run انجام می‌شود.
        st.info(
            "The task description (Persian, English or mixed) is analysed when Step 2 "
            "runs. This preview uses only Swagger URLs, endpoint paths and English "
            "keyword matching."
            + (f"\n\nPreview result: {plan.error}" if plan.error else "")
        )
    elif plan.error:
        st.error(plan.error)


def _render_in_workflow(workflow: WorkflowState) -> None:
    """قدمِ دوم داخلِ ویزارد: نتیجه‌ی قدم اول خودکار می‌آید، منابعِ Swagger خودکار.

    منابع با source_selection.select_sources از URLِ Swagger، مسیرِ endpoint و
    متنِ تسک (در برابرِ متادیتای snapshotها) انتخاب می‌شوند؛ فقط همان snapshotها
    خوانده می‌شوند و هیچ درخواستی به شبکه زده نمی‌شود. کاربر می‌تواند منابع را
    دستی انتخاب کند؛ انتخابِ دستی جایگزین نمی‌شود و فقط با URLِ Swaggerِ صریح
    سنجیده می‌شود. انتخاب در وضعیتِ جریان می‌ماند و عوض‌کردنش نتیجه را کهنه
    اعلام می‌کند.
    """
    render_propagated_inputs(workflow, 2)

    st.subheader("Swagger sources")
    manual_keys = _stored_manual_keys(workflow)
    modes = [_MODE_AUTO, _MODE_MANUAL]
    mode = st.radio(
        "Swagger source selection",
        options=modes,
        index=1 if manual_keys else 0,
        horizontal=True,
        key="workflow_swagger_selection",
        help=(
            "Automatic chooses the sources from the Swagger URLs, endpoint paths and "
            "task description of Step 1, checked against the local snapshots. Run All "
            "always uses Automatic."
        ),
    )
    selection = SELECTION_AUTO
    if mode == _MODE_MANUAL:
        options = [source.key for source in enabled_sources()]
        chosen = st.multiselect(
            "Sources to read",
            options=options,
            default=[key for key in manual_keys if key in options],
            format_func=lambda key: source_for(key).label,
            key="workflow_swagger_manual",
        )
        selection = selection_for_keys(chosen)

    # بعد از اجرای خودکار، همان انتخابی که نتیجه‌ی جاری با آن ساخته شده نشان داده
    # می‌شود؛ وگرنه پیش‌نمایشِ قطعی (بدونِ LLM) از همان انتخابی که Run اجرا می‌کند.
    used = workflow.swagger_selection
    if (
        selection == SELECTION_AUTO
        and isinstance(used, SourceSelection)
        and workflow.has_result(2)
        and not workflow.is_out_of_date(2)
    ):
        st.caption("Sources used by the current Step 2 result:")
        plan = used
    else:
        preview = WorkflowState(inputs=dict(workflow.inputs))
        preview.set_input(INPUT_SWAGGER_SELECTION, selection)
        plan = plan_swagger_sources(preview)
    _render_selection(plan)
    if plan.keys and not plan.error:
        st.caption(
            "Only these snapshots are read: "
            + ", ".join(f"`data/swagger/{snapshot_path(key).name}`" for key in plan.keys)
            + ". No request is sent to the Swagger host."
        )

    _render_update_buttons()

    # محتوای snapshotهایی که آخرین نگاشت از آن‌ها خوانده است. اگر بعد از اجرا
    # عوض شده باشند (Update یا ویرایشِ دستی)، پاورقیِ ویزارد هشدار می‌دهد که
    # نتیجه متعلق به سندِ قدیمی است.
    workflow.set_input(INPUT_SWAGGER_SNAPSHOTS, snapshot_fingerprint(list(plan.keys)))

    if st.button(run_label(workflow, 2), key=run_step_key(2)):
        if mode == _MODE_MANUAL and not selection:
            st.error("Choose at least one Swagger source, or switch to Automatic.")
        else:
            # انتخابِ دستی (یا «خودکار») ذخیره می‌شود؛ execute_step منابع را تعیین
            # و مسیرها و اثرانگشتِ snapshotها را ثبت می‌کند.
            workflow.set_input(INPUT_SWAGGER_SELECTION, selection)
            with st.spinner(_SPINNER):
                outcome = execute_step(2, workflow)
            if outcome.recorded:
                # کلِ صفحه (از جمله نوارِ قدم‌ها) با وضعیتِ تازه دوباره کشیده شود.
                st.rerun()
            st.error(outcome.error)

    result = workflow.result(2)
    if result:
        step1_result = workflow.result(1)
        used = workflow.swagger_selection
        _render_result(
            result,
            extract_test_cases(step1_result) if step1_result else [],
            used.summary() if isinstance(used, SourceSelection) else "",
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
