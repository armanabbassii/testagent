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

اجرا (از ریشه‌ی پروژه):

    uv sync --group ui
    uv run --group ui streamlit run ui/step1_task_analysis.py
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

from src.agents.test_case_generator.task_analysis import (  # noqa: E402
    TaskAnalysisError,
    TaskAnalysisGenerator,
)
from src.debug import DebugConfig  # noqa: E402
from ui.formatting import (  # noqa: E402
    case_details,
    case_overview,
    markdown_bullets,
    type_counts,
)

# شناسه‌ی کاربری که به‌عنوان هدر x-user-id به سرویسِ LLM فرستاده می‌شود
_UI_USER_ID = "step1-task-analysis-ui"

_RESULT_KEY = "analysis_result"
_ERROR_KEY = "analysis_error"


def _analyze(task_description: str, developed_services: str) -> None:
    """تحلیل را اجرا می‌کند و نتیجه یا خطا را در session_state می‌گذارد.

    نتیجه در session_state نگه داشته می‌شود تا با هر rerun (مثلاً باز کردنِ
    یک تست‌کیس) از بین نرود.
    """
    try:
        with st.spinner("Analyzing task..."):
            result = TaskAnalysisGenerator(
                debug_config=DebugConfig.from_env()
            ).generate(
                task_description=task_description,
                user_id=_UI_USER_ID,
                developed_services=developed_services,
            )
    except ValueError as exc:
        # ورودیِ نامعتبر — مثلاً توصیفِ تسکِ خالی که پیش از این‌جا گرفته می‌شود
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = f"Invalid input: {exc}"
    except TaskAnalysisError as exc:
        # خروجیِ مدل با قرارداد نخوانده است — پیامِ کاملِ اعتبارسنجی نشان داده می‌شود
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = (
            "The model output did not match the required test case format.\n\n"
            f"{exc}"
        )
    except Exception as exc:  # noqa: BLE001 — خطای شبکه/سرویس نباید UI را بترکاند
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = (
            f"Task analysis failed: {type(exc).__name__}: {exc}"
        )
    else:
        st.session_state[_RESULT_KEY] = result
        st.session_state[_ERROR_KEY] = None


def _render_result(result: dict) -> None:
    """نتیجه‌ی تحلیل را برای بازبینیِ انسانی نمایش می‌دهد."""
    st.subheader("Task Summary")
    st.write(result["task_summary"])

    counts = type_counts(result)
    columns = st.columns(len(counts) + 1)
    columns[0].metric("Test Cases", len(result["test_cases"]))
    for column, case_type in zip(columns[1:], counts):
        column.metric(case_type.replace("_", " ").title(), counts[case_type])

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
    """فرمِ ورودی را می‌سازد و در صورت ارسال، تحلیل را اجرا می‌کند."""
    with st.form("task_analysis_form"):
        task_description = st.text_area(
            "Task Description *",
            height=220,
            placeholder=(
                "Paste the development task here — the business and functional "
                "behaviour described in the task is the source of truth."
            ),
        )
        developed_services = st.text_area(
            "Developed Services",
            height=160,
            placeholder=(
                "APIs/endpoints implemented for this task — plain text, paths, "
                "HTTP methods, Swagger URLs, or a mixture. Multiple services allowed."
            ),
        )
        submitted = st.form_submit_button("Analyze Task")

    if not submitted:
        return

    if not task_description.strip():
        st.session_state[_RESULT_KEY] = None
        st.session_state[_ERROR_KEY] = "Task Description is required."
        return

    _analyze(task_description, developed_services)


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

    _render_inputs()

    error = st.session_state.get(_ERROR_KEY)
    if error:
        st.error(error)

    result = st.session_state.get(_RESULT_KEY)
    if result:
        _render_result(result)


main()
