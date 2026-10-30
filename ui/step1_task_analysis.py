"""
ui/step1_task_analysis.py — رابطِ ساده‌ی Streamlit برای قدمِ اولِ جریان

    Task Description + Developed Services
        → TaskAnalysisGenerator
        → تست‌کیس‌های ساختاریافته
        → بازبینیِ انسانی

این لایه کاملاً نازک است: ورودی می‌گیرد، همان TaskAnalysisGenerator موجود را
صدا می‌زند، خطا را نشان می‌دهد و نتیجه را نمایش می‌دهد. هیچ منطقِ کسب‌وکاری
اینجا نیست — ساختِ prompt، فراخوانیِ LLM، استخراج و اعتبارسنجیِ JSON و تولیدِ
تست‌کیس همه در src/agents/test_case_generator/task_analysis.py می‌مانند.

این صفحه به جریانِ Swagger-first کاری ندارد: نه سواگر تحلیل می‌کند، نه
Postman می‌سازد، نه API ای را اجرا می‌کند و نه قدم‌های بعدیِ جریان را
اجرا می‌کند.

همین صفحه در ویزاردِ ui/app.py هم استفاده می‌شود: render_step کارِ نمایش و اجرا
را انجام می‌دهد و main فقط پوسته‌ی صفحه‌ی مستقل است. هیچ منطقِ تازه‌ای اینجا
اضافه نشده — فقط محلِ نگه‌داشتنِ نتیجه (session_state یا وضعیتِ ویزارد) عوض
می‌شود.

اجرا (از ریشه‌ی پروژه):

    uv sync --group ui
    uv run --group ui streamlit run ui/step1_task_analysis.py

یا از داخلِ ویزارد: uv run --group ui streamlit run ui/app.py
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

from ui.components import RUN_ALL_KEY, request_run_all, run_step_key  # noqa: E402
from ui.formatting import (  # noqa: E402
    MESSAGE_ERROR,
    MESSAGE_INFO,
    MESSAGE_SUCCESS,
    MESSAGE_WARNING,
    case_details,
    case_overview,
    jira_attachment_rows,
    jira_status_messages,
    markdown_bullets,
    requested_variable_rows,
    type_counts,
)
from ui.pipeline import (  # noqa: E402
    execute_step,
    jira_input_problem,
    load_jira_task,
    run_task_analysis,
)
from ui.workflow import (  # noqa: E402
    INPUT_DEVELOPED_SERVICES,
    INPUT_JIRA_ISSUE,
    INPUT_TASK_DESCRIPTION,
    WorkflowState,
    run_label,
)

_SPINNER = "Analyzing task..."

_SOURCE_MANUAL = "Manual description"
_SOURCE_JIRA = "Jira issue"
_SOURCE_ICONS = {
    _SOURCE_MANUAL: ":material/edit_note:",
    _SOURCE_JIRA: ":material/confirmation_number:",
}
_SOURCE_KEY = "workflow_task_source"
RUN_ALL_LABEL = "Run All — Steps 1 to 5"
_JIRA_REFERENCE_KEY = "workflow_jira_reference"

_RESULT_KEY = "analysis_result"
_ERROR_KEY = "analysis_error"

_TASK_PLACEHOLDER = (
    "Paste the development task here — the business and functional behaviour "
    "described in the task is the source of truth."
)
_JIRA_TASK_PLACEHOLDER = "Fetch a Jira issue above — its content appears here."
_SERVICES_PLACEHOLDER = (
    "APIs/endpoints implemented for this task — plain text, paths, HTTP methods, "
    "Swagger URLs, or a mixture. Multiple services allowed."
)


def _run(
    task_description: str, developed_services: str
) -> tuple[dict | None, str | None]:
    """تحلیل را اجرا می‌کند و (نتیجه، خطا) را برمی‌گرداند.

    هیچ چیزی در session_state نوشته نمی‌شود: نگه‌داشتنِ نتیجه کارِ فراخوان است —
    صفحه‌ی مستقل آن را در session_state می‌گذارد و ویزارد در وضعیتِ جریان.
    فراخوانیِ Generator در ui/pipeline.py است (مشترک با ویزارد و Run All).
    """
    with st.spinner(_SPINNER):
        return run_task_analysis(task_description, developed_services)


def _store(result: dict | None, error: str | None) -> None:
    """نتیجه یا خطا را در session_state می‌گذارد (حالتِ صفحه‌ی مستقل).

    نتیجه در session_state نگه داشته می‌شود تا با هر rerun (مثلاً باز کردنِ
    یک تست‌کیس) از بین نرود.
    """
    st.session_state[_RESULT_KEY] = result
    st.session_state[_ERROR_KEY] = error


def _render_result(result: dict) -> None:
    """نتیجه‌ی تحلیل را برای بازبینیِ انسانی نمایش می‌دهد."""
    st.subheader("Task Summary")
    st.write(result["task_summary"])

    counts = type_counts(result)
    columns = st.columns(len(counts) + 1)
    columns[0].metric("Test Cases", len(result["test_cases"]), border=True)
    for column, case_type in zip(columns[1:], counts):
        column.metric(case_type.replace("_", " ").title(), counts[case_type], border=True)

    st.subheader("Identified Requirements")
    st.markdown(
        markdown_bullets(
            result["identified_requirements"],
            empty="No requirements were identified.",
        )
    )

    if result["clarifications"]:
        st.subheader("Clarifications")
        st.caption(
            "The task description does not settle these points — they need a "
            "human answer before the scenarios can be trusted."
        )
        for item in result["clarifications"]:
            st.warning(item)

    variables = requested_variable_rows(result)
    if variables:
        st.subheader("Requested Variables")
        st.caption(
            "Values the task explicitly asks to extract, check and save. Step 3 "
            "matches them to the API contract and Step 4 generates the scripts."
        )
        st.dataframe(variables, hide_index=True)

    st.subheader("Test Cases")
    st.dataframe(case_overview(result), hide_index=True)

    # همه‌ی تست‌کیس‌ها با همان ساختارِ کامل نمایش داده می‌شوند — نه فقط اولی.
    for detail in case_details(result):
        st.markdown(f"#### {detail['heading']}")
        st.markdown(detail["meta"])
        for section in detail["sections"]:
            st.markdown(f"**{section['label']}**")
            st.markdown(section["body"])
        st.divider()

    with st.expander("Raw JSON (input for the next step of the workflow)"):
        st.json(result)


def _render_inputs() -> None:
    """فرمِ ورودیِ صفحه‌ی مستقل را می‌سازد و در صورت ارسال، تحلیل را اجرا می‌کند."""
    with st.form("task_analysis_form"):
        task_description = st.text_area(
            "Task Description *",
            height=220,
            placeholder=_TASK_PLACEHOLDER,
        )
        developed_services = st.text_area(
            "Developed Services",
            height=160,
            placeholder=_SERVICES_PLACEHOLDER,
        )
        submitted = st.form_submit_button("Analyze Task")

    if not submitted:
        return

    if not task_description.strip():
        _store(None, "Task Description is required.")
        return

    _store(*_run(task_description, developed_services))


def _render_standalone() -> None:
    """صفحه‌ی مستقلِ قدمِ اول — ورودی دستی و نتیجه در session_state."""
    _render_inputs()

    error = st.session_state.get(_ERROR_KEY)
    if error:
        st.error(error)

    result = st.session_state.get(_RESULT_KEY)
    if result:
        _render_result(result)


_MESSAGE_ICONS = {
    MESSAGE_SUCCESS: ":material/check_circle:",
    MESSAGE_ERROR: ":material/error:",
    MESSAGE_WARNING: ":material/warning:",
    MESSAGE_INFO: ":material/info:",
}


def _render_jira_status(result) -> None:
    """وضعیتِ هر مرحله‌ی دریافت، جدولِ پیوست‌ها و پیش‌نمایشِ متنِ ساخته‌شده."""
    show = {
        MESSAGE_SUCCESS: st.success,
        MESSAGE_ERROR: st.error,
        MESSAGE_WARNING: st.warning,
        MESSAGE_INFO: st.info,
    }
    for level, text in jira_status_messages(result):
        show[level](text, icon=_MESSAGE_ICONS[level])
    task = result.task_input
    if task is None:
        return
    if task.attachments:
        with st.expander(
            f"Attachments · {len(task.attachments)}",
            icon=":material/attach_file:",
            expanded=bool(task.unprocessed_attachments()),
        ):
            st.dataframe(jira_attachment_rows(task), hide_index=True, width="stretch")
    with st.expander(
        "Extracted Jira content (what Step 1 receives as the Task Description)",
        icon=":material/visibility:",
    ):
        # متنِ خام، نه Markdownِ رندرشده: محتوای issue نامطمئن است.
        st.code(task.to_task_description(), language="markdown", wrap_lines=True)


def _render_jira_source(workflow: WorkflowState) -> None:
    """کلید/نشانیِ issue و دکمه‌ی دریافت. دریافت هرگز خودش قدمی را اجرا نمی‌کند."""
    st.session_state.setdefault(_JIRA_REFERENCE_KEY, workflow.input_value(INPUT_JIRA_ISSUE))
    with st.container(border=True):
        st.markdown(":material/confirmation_number: **Jira issue**")
        with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
            reference = st.text_input(
                "Jira issue key or URL",
                key=_JIRA_REFERENCE_KEY,
                placeholder="PROJ-123 or https://jira.example.com/browse/PROJ-123",
            )
            fetch = st.button("Fetch issue", icon=":material/cloud_download:")
        st.caption(
            "Signs in with the Jira settings in .env and reads the description, all "
            "comments and the readable attachments. Fetching does not run any step."
        )
        if fetch:
            with st.spinner("Signing in to Jira and reading the issue..."):
                result = load_jira_task(workflow, reference)
            if result.ok:
                # کادرِ توصیف با متنِ تازه دوباره کشیده شود.
                st.rerun()
            _render_jira_status(result)
            return

        if workflow.task_import is not None:
            _render_jira_status(workflow.task_import)
            problem = jira_input_problem(workflow, reference) if reference.strip() else ""
            if problem:
                st.warning(problem, icon=":material/sync_problem:")


def _render_in_workflow(workflow: WorkflowState) -> None:
    """قدمِ اول داخلِ ویزارد: ورودی‌ها در وضعیتِ جریان می‌مانند و نتیجه هم آنجا.

    نتیجه‌ی تازه، قدم‌های پایین‌دستی را کهنه می‌کند و آن‌ها را پاک نمی‌کند؛
    پیامش در بالای ویزارد نشان داده می‌شود.

    «Run All» دکمه‌ی دومِ همین فرم است تا همان متن‌های واردشده را بگیرد: فقط
    ورودی‌ها را ثبت و اجرای کامل را درخواست می‌کند؛ خودِ اجرا در ui/app.py با
    ui.pipeline.run_all انجام می‌شود.

    منبعِ تسک دستی یا یک issueِ Jira است. Jira فقط کادرِ «Task Description» را
    پر می‌کند (قابلِ ویرایش)؛ اجرا همان مسیرِ دستی است. اگر کلیدِ issue بعد از
    دریافت عوض شود، اجرا تا دریافتِ issueِ تازه ممکن نیست.
    """
    with st.container(horizontal=True, vertical_alignment="center", gap="medium"):
        source = st.segmented_control(
            "Task source",
            (_SOURCE_MANUAL, _SOURCE_JIRA),
            default=_SOURCE_JIRA if workflow.input_value(INPUT_JIRA_ISSUE) else _SOURCE_MANUAL,
            format_func=lambda option: f"{_SOURCE_ICONS[option]} {option}",
            key=_SOURCE_KEY,
            required=True,
        )
        st.caption(
            "Write the task yourself, or import it from a Jira issue and edit it below."
        )
    from_jira = source == _SOURCE_JIRA
    if from_jira:
        _render_jira_source(workflow)

    with st.form("task_analysis_form"):
        task_description = st.text_area(
            "Task description *",
            value=workflow.input_value(INPUT_TASK_DESCRIPTION),
            height=320 if from_jira else 220,
            placeholder=_JIRA_TASK_PLACEHOLDER if from_jira else _TASK_PLACEHOLDER,
            help=(
                "Built from the fetched Jira issue: description, all comments and the "
                "readable attachments, each section labelled with its origin. You can "
                "edit it before running."
                if from_jira
                else None
            ),
        )
        developed_services = st.text_area(
            "Developed services",
            value=workflow.input_value(INPUT_DEVELOPED_SERVICES),
            height=160,
            placeholder=_SERVICES_PLACEHOLDER,
            help="Kept separate from the task description. Optional.",
        )
        # یک ردیف: اجرای قدمِ اول (ثانویه) کنارِ Run All (اصلی). روی صفحه‌ی باریک
        # دو دکمه زیرِ هم می‌روند.
        with st.container(horizontal=True, gap="small"):
            submitted = st.form_submit_button(
                run_label(workflow, 1),
                key=run_step_key(1),
                icon=":material/play_arrow:",
                width="stretch",
            )
            run_all_requested = st.form_submit_button(
                RUN_ALL_LABEL,
                key=RUN_ALL_KEY,
                type="primary",
                icon=":material/fast_forward:",
                disabled=workflow.run_in_progress,
                width="stretch",
                help=(
                    "Runs Steps 1–5 one after another without stopping for review. "
                    "Step 2 chooses its Swagger sources automatically."
                ),
            )
    st.caption(
        "Step 2 chooses the local Swagger snapshots automatically: from Swagger URLs "
        "(?urls.primaryName=CRM or …/v3/api-docs/CRM), example endpoints "
        "(https://…/api/admin/voucher/12 or GET /provider/serviceSet/{id}) and the "
        "task description, checked against the snapshots. An example endpoint is the "
        "most reliable hint."
    )

    if submitted or run_all_requested:
        jira_problem = (
            jira_input_problem(workflow, st.session_state.get(_JIRA_REFERENCE_KEY, ""))
            if from_jira
            else ""
        )
        if not task_description.strip():
            # ورودیِ نامعتبر وضعیت را دست نمی‌زند — نتیجه‌ی قبلی سرِ جایش می‌ماند.
            st.error("Task Description is required.")
        elif jira_problem:
            st.error(jira_problem)
        else:
            workflow.set_input(INPUT_TASK_DESCRIPTION, task_description)
            workflow.set_input(INPUT_DEVELOPED_SERVICES, developed_services)
            workflow.set_input(
                INPUT_JIRA_ISSUE, workflow.task_import.issue_key if from_jira else ""
            )
            if run_all_requested:
                request_run_all()
            else:
                with st.spinner(_SPINNER):
                    execute_step(1, workflow)
            # کلِ صفحه (از جمله نوارِ قدم‌ها) با وضعیتِ تازه دوباره کشیده شود.
            st.rerun()

    result = workflow.result(1)
    if result:
        _render_result(result)


def render_step(workflow: WorkflowState | None = None) -> None:
    """ورودی و نتیجه‌ی قدمِ اول را نشان می‌دهد.

    workflow=None یعنی صفحه‌ی مستقل (ورودی دستی، نتیجه در session_state).
    workflow یعنی داخلِ ویزارد (ورودی و نتیجه در وضعیتِ جریان).
    """
    if workflow is None:
        _render_standalone()
    else:
        _render_in_workflow(workflow)


def main() -> None:
    st.set_page_config(
        page_title="Step 1 — Task Analysis",
        page_icon="🧪",
        layout="wide",
    )

    st.title("Step 1 — Task Analysis & Test Case Generation")
    st.caption(
        "Task Description + Developed Services → structured test cases for human "
        "review. This step does not analyze Swagger, does not generate a Postman "
        "collection, and does not execute any API."
    )

    render_step()


if __name__ == "__main__":
    main()
