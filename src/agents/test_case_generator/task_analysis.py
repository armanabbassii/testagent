"""
test_case_generator/task_analysis.py — تحلیلِ تسک و تولیدِ تست‌کیس ساختاریافته

قدمِ اولِ جریانِ انسان-در-حلقه:

    Task Description + Developed Services → LLM → تست‌کیس‌های ساختاریافته

این ماژول عمداً کارهای زیر را انجام نمی‌دهد:
  - تحلیلِ Swagger یا دریافتِ سندِ OpenAPI   (قدمِ بعدیِ جریان است)
  - ساختِ Postman Collection یا اسکریپت      (قدمِ بعدیِ جریان است)
  - اجرای API، اجرای تست، یا فراخوانیِ سرویس‌های اپلیکیشن
  - ساختِ Authorization header یا متغیرهای Postman

منبعِ حقیقت، «توصیفِ تسک» است. «سرویس‌های توسعه‌داده‌شده» فقط زمینه‌اند تا
مشخص شود هر تست‌کیس به کدام API مربوط است؛ نه منبعِ قواعدِ کسب‌وکار. اگر
نگاشتِ API قابلِ تعیین نباشد، به‌جای حدس‌زدن مقدارِ REQUIRES_MAPPING
برگردانده می‌شود.

خروجیِ LLM سخت‌گیرانه اعتبارسنجی می‌شود: اگر با قرارداد نخواند،
TaskAnalysisError پرتاب می‌شود تا نتیجه‌ی ناقص بی‌سروصدا به گامِ بعد نرود.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from typing import Any

from typing_extensions import NotRequired, TypedDict

from src.agents.test_case_generator.auth_intent import (
    DEFAULT_AUTH_INTENT,
    AuthIntentError,
    normalize_auth_intent,
)
from src.agents.test_case_generator.environment_decisions import (
    extract_environment_decisions,
)
from src.agents.test_case_generator.json_output import (
    JsonExtractionError,
    extract_json_object,
)
from src.agents.test_case_generator.variable_dependencies import (
    RequestedVariable,
    validate_requested_variables,
)
from src.config import LLM_MAX_OUTPUT_TOKENS
from src.debug import DebugConfig
from src.llm_client import LLMClient, truncation_message
from src.task_sources.sources import TaskSource, sources_from_text

_PROMPT_PATH = Path(__file__).parent / "prompts" / "step1_task_analysis.md"

# پرامپت‌های مسیرِ مرحله‌ای: اول برنامه‌ی فشرده، بعد جزئیات در دسته‌های کوچک.
_PLAN_PROMPT_PATH = Path(__file__).parent / "prompts" / "step1_task_plan.md"
_DETAILS_PROMPT_PATH = Path(__file__).parent / "prompts" / "step1_test_case_details.md"

_VALID_TYPES = ("positive", "negative", "boundary", "state_transition")
_VALID_PRIORITIES = ("high", "medium", "low")

# مقداری که وقتی نگاشتِ API قابلِ تعیین نیست استفاده می‌شود
REQUIRES_MAPPING = "Unknown - requires API mapping"

# نشانه‌هایی که یعنی مدل نتوانسته نگاشت را تعیین کند (نه یک مسیرِ واقعی)
_UNKNOWN_MARKERS = (
    "unknown",
    "requires api mapping",
    "not available",
    "n/a",
    "none",
    "tbd",
)

# جای‌نگهدارهای فایل‌های پرامپت
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

# بخشِ INPUT در فایلِ پرامپت: از این‌جا به بعد قالبِ ورودیِ کاربر است
_INPUT_SECTION_RE = re.compile(
    r"^={3,}[ \t]*\nINPUT[ \t]*\n={3,}[ \t]*\n", re.MULTILINE
)

# وقتی کاربر سرویسی معرفی نکرده باشد
_EMPTY_SERVICES = "(none provided)"

# وقتی پاسخِ کاملِ یک‌فراخوانی هیچ تست‌کیسی ندارد — ورودیِ تولیدِ مرحله‌ای
_EMPTY_SINGLE_RESPONSE = (
    "The single-call LLM response was complete but contained no test cases, so "
    "it could not be used."
)

# ── تشخیصِ «هیچ مرحله‌ای تست‌کیس نساخت» ─────────────────────────────────────
# وقتی هم پاسخِ یک‌فراخوانی و هم برنامه‌ی مرحله‌ای تست‌کیس ندارند، پیامِ خطا
# می‌گوید کدام مرحله چه برگرداند و مدل چرا تست‌کیسی نساخت (خلاصه و چند
# clarification از خودِ پاسخ، کوتاه‌شده). پاسخِ خام، استدلالِ مدل و پرامپت هرگز
# در پیام یا لاگ نمی‌آیند؛ لاگ فقط وضعیت، شناسه و تعداد را ثبت می‌کند.
STAGE_SINGLE_CALL = "single-call analysis"
STAGE_PLAN = "staged generation (test case plan)"

_DIAG_SUMMARY_CHARS = 300
_DIAG_CLARIFICATIONS = 3
_DIAG_CLARIFICATION_CHARS = 200


@dataclass(frozen=True)
class GenerationAttempt:
    """یک مرحله که هیچ تست‌کیسِ قابلِ استفاده‌ای برنگرداند."""

    stage: str
    # missing | null | empty | not an array (<type>) | truncated (…)
    test_cases_state: str
    response_id: str = ""
    task_summary: str = ""
    clarifications: tuple[str, ...] = ()


def cases_state(data: dict) -> str:
    """چرا ``test_cases`` قابلِ استفاده نیست: نبودن، null، خالی یا غیرِ آرایه."""
    if "test_cases" not in data:
        return "missing"
    value = data["test_cases"]
    if value is None:
        return "null"
    if isinstance(value, list):
        return "empty" if not value else "present"
    return f"not an array ({type(value).__name__})"


def _response_id(raw: object) -> str:
    """شناسه‌ی پاسخِ ارائه‌دهنده، اگر کلاینت گزارش کرده باشد."""
    value = getattr(raw, "response_id", "")
    return value if isinstance(value, str) else ""


def _shorten(value: object, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _empty_attempt(stage: str, data: dict, raw: object) -> GenerationAttempt:
    clarifications = data.get("clarifications")
    texts = (
        tuple(str(item) for item in clarifications if isinstance(item, str) and item.strip())
        if isinstance(clarifications, list)
        else ()
    )
    summary = data.get("task_summary")
    return GenerationAttempt(
        stage=stage,
        test_cases_state=cases_state(data),
        response_id=_response_id(raw),
        task_summary=summary.strip() if isinstance(summary, str) else "",
        clarifications=texts,
    )


def no_test_cases_diagnostics(attempts: list[GenerationAttempt]) -> str:
    """بخشِ تشخیصیِ پیامِ خطا — مرحله‌ها، وضعیت، شناسه، خلاصه و clarificationها.

    خلاصه و clarificationها از آخرین مرحله‌ای می‌آیند که آن‌ها را داشت (معمولاً
    برنامه‌ی مرحله‌ای)، کوتاه‌شده و حداکثر _DIAG_CLARIFICATIONS مورد.
    """
    lines = ["", "", "No test cases were produced by any generation stage:"]
    for attempt in attempts:
        state = attempt.test_cases_state
        described = state if state.startswith("truncated") else f"'test_cases' was {state}"
        reference = f" (response id: {attempt.response_id})" if attempt.response_id else ""
        lines.append(f"  - {attempt.stage}: {described}{reference}.")

    source = next((a for a in reversed(attempts) if a.task_summary), None)
    if source is not None:
        lines.append(
            f"Model task summary ({source.stage}): "
            f"{_shorten(source.task_summary, _DIAG_SUMMARY_CHARS)}"
        )
    source = next((a for a in reversed(attempts) if a.clarifications), None)
    if source is not None:
        shown = source.clarifications[:_DIAG_CLARIFICATIONS]
        lines.append(
            f"Model clarifications ({source.stage}; {len(shown)} of "
            f"{len(source.clarifications)}):"
        )
        lines += [f"  - {_shorten(item, _DIAG_CLARIFICATION_CHARS)}" for item in shown]
    else:
        lines.append("The model gave no clarifications.")
    return "\n".join(lines)


# حداکثر تعداد تست‌کیس در هر فراخوانیِ «گسترش» (مسیرِ مرحله‌ای).
#
# چرا لازم است: خروجیِ این قدم یک لیستِ باز است و تعدادش را مدل تعیین می‌کند؛
# پس برخلافِ قدم دوم نمی‌شود ورودی را از قبل دسته‌بندی کرد. وقتی پاسخِ کامل به
# سقفِ توکن می‌خورد، اول یک «برنامه‌ی فشرده» ساخته می‌شود و بعد جزئیاتِ هر
# دسته جداگانه تولید می‌شود. حجمِ هر پاسخِ گسترش با این عدد محدود می‌ماند.
#
# چرا ۸: هر تست‌کیسِ کامل (preconditions + چند step + expected_result) حدودِ
# ۲۰۰–۳۵۰ توکن است؛ ۸ تست‌کیس ≈ ۱٬۶۰۰–۲٬۸۰۰ توکن، با حاشیه‌ی کافی زیرِ
# ۸٬۱۹۲. مقدار با batch_size در سازنده قابلِ تغییر است.
DEFAULT_TEST_CASE_BATCH_SIZE = 8


class TaskAnalysisError(RuntimeError):
    """ورودیِ نامعتبر یا خروجیِ LLM مغایر با قراردادِ تست‌کیس."""


class RelatedService(TypedDict):
    """نگاشتِ یک تست‌کیس به سرویس/API — مقدارها ممکن است REQUIRES_MAPPING باشند."""

    method: str
    path: str
    service: str


class TaskTestCase(TypedDict):
    id: str
    title: str
    type: str
    priority: str
    preconditions: list[str]
    steps: list[str]
    expected_result: str
    related_service: RelatedService
    # رفتارِ اعتبارنامه‌ی همین تست‌کیس — مستقل از قراردادِ احراز هویتِ API.
    # مقادیرِ مجاز در auth_intent.py تعریف شده‌اند.
    auth_intent: str
    # ردیابیِ نیازمندی و طراحیِ سطحِ نیازمندی — همه اختیاری؛ نتیجه‌ی قدیمیِ
    # بدونِ این فیلدها همان رفتارِ قبلی را دارد.
    requirement_refs: NotRequired[list[str]]
    checks: NotRequired[list[dict]]
    input_intent: NotRequired[list[dict]]


class TaskAnalysisResult(TypedDict):
    task_summary: str
    identified_requirements: list[str]
    test_cases: list[TaskTestCase]
    clarifications: list[str]
    # متغیرهایی که کاربر صراحتاً خواسته (استخراج از پاسخ، ذخیره، مصرف) — از
    # این‌جا به بعد داده‌ی ساختاریافته است، نه متن. نتیجه‌ی قدیمیِ بدونِ این
    # فیلد یعنی «هیچ متغیری خواسته نشده».
    variables: list[RequestedVariable]
    # تصمیم‌های محیطِ سنجیده‌شده (environment_decisions.py) — نتیجه‌ی قدیمیِ
    # بدونِ این فیلدها یعنی «هیچ تصمیمی»، پس همه‌ی تست‌کیس‌ها base_url می‌گیرند.
    environment_decisions: NotRequired[list[dict]]
    rejected_environment_decisions: NotRequired[list[dict]]
    environment_sources: NotRequired[list[dict]]
    environment_warnings: NotRequired[list[str]]
    # نیازمندی‌های ساختاریافته (رفتاری یا پیاده‌سازی)، تست‌کیس‌های کنارگذاشته
    # با دلیل و پوشش‌دهنده، و هشدارهای ردیابی — همه اختیاری و افزایشی.
    requirements: NotRequired[list[dict]]
    dropped_test_cases: NotRequired[list[dict]]
    traceability_warnings: NotRequired[list[str]]


class PlannedTestCase(TypedDict):
    """یک تست‌کیس در برنامه — فقط «چه چیزی» بدونِ جزئیاتِ اجرایی.

    عنوان و نوع و اولویت همان چیزی است که فراخوانیِ گسترش باید حفظ کند؛
    preconditions/steps/expected_result عمداً اینجا نیستند تا حجمِ برنامه
    چند برابر کوچک‌تر از خروجیِ کامل بماند.
    """

    id: str
    title: str
    type: str
    priority: str
    related_service: RelatedService
    # در برنامه هم نگه داشته می‌شود تا تولیدِ مرحله‌ای آن را از دست ندهد.
    auth_intent: str
    # ردیابی و طراحیِ سطحِ نیازمندی همین‌جا قطعی می‌شود (اختیاری).
    requirement_refs: NotRequired[list[str]]
    checks: NotRequired[list[dict]]
    input_intent: NotRequired[list[dict]]


class TaskAnalysisPlan(TypedDict):
    """برنامه‌ی فشرده‌ی تست‌کیس‌ها — مرحله‌ی اولِ تولیدِ مرحله‌ای."""

    task_summary: str
    identified_requirements: list[str]
    test_cases: list[PlannedTestCase]
    clarifications: list[str]
    # در برنامه می‌آید چون شناسه‌ی تست‌کیس‌ها همین‌جا قطعی می‌شود.
    variables: list[RequestedVariable]
    # خروجیِ خامِ مدل برای تصمیم‌های محیط — بعد از ساختِ نتیجه سنجیده می‌شود.
    environment_decisions: NotRequired[Any]
    requirements: NotRequired[list[dict]]
    dropped_test_cases: NotRequired[list[dict]]


class TestCaseDetails(TypedDict):
    """جزئیاتِ اجراییِ یک تست‌کیسِ برنامه‌ریزی‌شده — مرحله‌ی دوم."""

    id: str
    preconditions: list[str]
    steps: list[str]
    expected_result: str


# ── ساختِ prompt ─────────────────────────────────────────────────────────────

def _split_input_section(
    text: str, placeholders: tuple[str, ...]
) -> tuple[str, str]:
    """فایلِ پرامپت را به (system prompt، قالبِ پیامِ کاربر) تقسیم می‌کند.

    هرچه پیش از بخشِ INPUT است دستورِ سیستم است — یعنی قواعدِ تولید و قراردادِ
    خروجی. از بخشِ INPUT به بعد قالبی است که با ورودیِ کاربر پر می‌شود.
    """
    matches = list(_INPUT_SECTION_RE.finditer(text))
    if not matches:
        raise TaskAnalysisError(
            "Prompt file has no INPUT section — expected a line 'INPUT' between "
            "two '====' separators."
        )

    match = matches[-1]
    system_prompt = text[: match.start()].strip()
    user_template = text[match.end() :].strip()
    if not system_prompt or not user_template:
        raise TaskAnalysisError(
            "Prompt file has an empty system prompt or an empty INPUT section."
        )

    found = set(_PLACEHOLDER_RE.findall(user_template))
    missing = set(placeholders) - found
    if missing:
        raise TaskAnalysisError(
            "Prompt INPUT section is missing placeholder(s): "
            + ", ".join(sorted(f"{{{{{name}}}}}" for name in missing))
        )
    return system_prompt, user_template


def _render(template: str, values: dict[str, str]) -> str:
    """جای‌گذاری در یک پاس؛ جای‌نگهدارِ ناشناخته دست‌نخورده می‌ماند."""
    return _PLACEHOLDER_RE.sub(
        lambda match: values.get(match.group(1), match.group(0)), template
    )


def split_prompt(text: str) -> tuple[str, str]:
    """فایلِ پرامپتِ اصلی را تقسیم می‌کند.

    پرتاب می‌کند:
        TaskAnalysisError : بخشِ INPUT پیدا نشود، خالی باشد، یا جای‌نگهدارها را نداشته باشد
    """
    return _split_input_section(text, ("task_description", "developed_services"))


def split_plan_prompt(text: str) -> tuple[str, str]:
    """فایلِ پرامپتِ «برنامه‌ی تست‌کیس» را تقسیم می‌کند."""
    return _split_input_section(text, ("task_description", "developed_services"))


def split_details_prompt(text: str) -> tuple[str, str]:
    """فایلِ پرامپتِ «گسترشِ تست‌کیس» را تقسیم می‌کند."""
    return _split_input_section(text, ("task_description", "test_case_plan"))


def render_prompt(
    template: str,
    task_description: str,
    developed_services: str = "",
) -> str:
    """قالب را با ورودیِ کاربر پر می‌کند.

    جای‌گذاری در یک پاس انجام می‌شود؛ بنابراین اگر متنِ خودِ کاربر شامل
    «{{...}}» باشد به‌عنوان جای‌نگهدار تفسیر نمی‌شود.
    """
    values = {
        "task_description": (task_description or "").strip(),
        "developed_services": (developed_services or "").strip() or _EMPTY_SERVICES,
    }
    return _render(template, values)


def render_plan_prompt(
    template: str,
    task_description: str,
    developed_services: str = "",
) -> str:
    """قالبِ پرامپتِ برنامه را پر می‌کند — همان ورودی‌های پرامپتِ اصلی."""
    return render_prompt(template, task_description, developed_services)


def render_details_prompt(
    template: str,
    task_description: str,
    planned_cases: list[PlannedTestCase],
) -> str:
    """قالبِ پرامپتِ گسترش را با توصیفِ تسک و دسته‌ی برنامه‌ریزی‌شده پر می‌کند."""
    values = {
        "task_description": (task_description or "").strip(),
        "test_case_plan": json.dumps(planned_cases, ensure_ascii=False, indent=2),
    }
    return _render(template, values)


def build_prompt(
    prompt_path: Path | str,
    task_description: str,
    developed_services: str = "",
) -> tuple[str, str]:
    """(system prompt، پیامِ کاربرِ ساخته‌شده) را از فایلِ پرامپت برمی‌گرداند."""
    text = Path(prompt_path).read_text(encoding="utf-8")
    system_prompt, template = split_prompt(text)
    return system_prompt, render_prompt(
        template, task_description, developed_services
    )


def build_plan_prompt(
    prompt_path: Path | str,
    task_description: str,
    developed_services: str = "",
) -> tuple[str, str]:
    """(system prompt، پیامِ کاربر) برای مرحله‌ی «برنامه‌ی تست‌کیس»."""
    text = Path(prompt_path).read_text(encoding="utf-8")
    system_prompt, template = split_plan_prompt(text)
    return system_prompt, render_plan_prompt(
        template, task_description, developed_services
    )


def build_details_prompt(
    prompt_path: Path | str,
    task_description: str,
    planned_cases: list[PlannedTestCase],
) -> tuple[str, str]:
    """(system prompt، پیامِ کاربر) برای گسترشِ یک دسته از برنامه."""
    text = Path(prompt_path).read_text(encoding="utf-8")
    system_prompt, template = split_details_prompt(text)
    return system_prompt, render_details_prompt(
        template, task_description, planned_cases
    )


# ── اعتبارسنجیِ خروجی ────────────────────────────────────────────────────────

def _clean(value: object) -> str:
    """رشته را trim می‌کند؛ هر چیزِ دیگری رشته‌ی خالی می‌شود."""
    return value.strip() if isinstance(value, str) else ""


def _is_unknown_marker(value: str) -> bool:
    lowered = _clean(value).lower()
    return not lowered or lowered in _UNKNOWN_MARKERS or "unknown" in lowered


def _string_list(
    value: object,
    where: str,
    errors: list[str],
    *,
    required: bool,
) -> list[str]:
    """یک لیستِ رشته‌ای را اعتبارسنجی و نرمال می‌کند.

    یک رشته‌ی تنها هم پذیرفته و به لیستِ یک‌عضوی تبدیل می‌شود — محتوا نه ساخته
    می‌شود و نه از دست می‌رود، فقط شکلِ خروجیِ مدل تحمل می‌شود.
    """
    if value is None:
        if required:
            errors.append(f"{where}: is required and must be a non-empty array of strings.")
        return []

    if isinstance(value, str):
        value = [value]

    if not isinstance(value, list):
        errors.append(
            f"{where}: must be an array of strings, got {type(value).__name__}."
        )
        return []

    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(
                f"{where}[{index}]: must be a string, got {type(item).__name__}."
            )
            continue
        if item.strip():
            items.append(item.strip())

    if required and not items:
        errors.append(f"{where}: must contain at least one entry.")
    return items


def _validate_related_service(
    value: object,
    where: str,
    errors: list[str],
) -> RelatedService:
    """نگاشتِ سرویس را اعتبارسنجی و نرمال می‌کند.

    سه شکل پذیرفته می‌شود:
      - شیءِ {"method", "path", "service"}            → همان (method به حروف بزرگ)
      - رشته‌ی نگاشت‌نشده ("Unknown - requires API mapping") → شیءِ نگاشت‌نشده
      - غایب / null / شیءِ خالی                       → شیءِ نگاشت‌نشده

    حدس‌زدن مسیر یا متد ممنوع است؛ اگر یکی از آن‌ها نباشد خطا ثبت می‌شود.
    """
    unmapped = RelatedService(method=REQUIRES_MAPPING, path=REQUIRES_MAPPING, service="")

    if value is None:
        return unmapped

    if isinstance(value, str):
        if _is_unknown_marker(value):
            return unmapped
        errors.append(
            f"{where}: must be an object with 'method' and 'path', or the string "
            f"'{REQUIRES_MAPPING}' — got the bare string {value!r}."
        )
        return unmapped

    if not isinstance(value, dict):
        errors.append(
            f"{where}: must be an object or a string, got {type(value).__name__}."
        )
        return unmapped

    raw_method = _clean(value.get("method"))
    raw_path = _clean(value.get("path"))
    if not raw_method and not raw_path:
        return unmapped

    if raw_method:
        method = REQUIRES_MAPPING if _is_unknown_marker(raw_method) else raw_method.upper()
    else:
        # نگاشت مشخص نشده است — مقدارِ sentinel جای حدس‌زدن می‌نشیند
        method = REQUIRES_MAPPING
        errors.append(
            f"{where}: 'method' is missing — use '{REQUIRES_MAPPING}' if it is unknown."
        )

    if raw_path:
        path = REQUIRES_MAPPING if _is_unknown_marker(raw_path) else raw_path
    else:
        path = REQUIRES_MAPPING
        errors.append(
            f"{where}: 'path' is missing — use '{REQUIRES_MAPPING}' if it is unknown."
        )

    raw_service = value.get("service")
    if raw_service is None:
        service = ""
    elif isinstance(raw_service, str):
        service = "" if _is_unknown_marker(raw_service) else raw_service.strip()
    else:
        errors.append(f"{where}: 'service' must be a string when present.")
        service = ""

    return RelatedService(method=method, path=path, service=service)


def _validate_auth_intent(value: object, where: str, errors: list[str]) -> str:
    """قصدِ احراز هویت را نرمال می‌کند؛ غیبتِ فیلد یعنی ``inherit``.

    فیلد برای نتیجه‌های ذخیره‌شده‌ی قدیمی اجباری نیست. مقدارِ ناشناخته ولی
    خطاست و به‌جای حدس زدن، به مدل گزارش می‌شود.
    """
    try:
        intent = normalize_auth_intent(value)
    except AuthIntentError as exc:
        errors.append(f"{where}: {exc}")
        return DEFAULT_AUTH_INTENT
    return intent or DEFAULT_AUTH_INTENT


def _validate_test_case(
    case: object,
    where: str,
    errors: list[str],
) -> TaskTestCase | None:
    """یک تست‌کیس را اعتبارسنجی و نرمال می‌کند؛ در صورت خرابی None برمی‌گرداند."""
    if not isinstance(case, dict):
        errors.append(f"{where}: expected an object, got {type(case).__name__}.")
        return None

    case_id = _clean(case.get("id"))
    if not case_id:
        errors.append(f"{where}: 'id' is missing or empty.")
    where = f"{where} ('{case_id or '?'}')"

    title = _clean(case.get("title"))
    if not title:
        errors.append(f"{where}: 'title' is missing or empty.")

    case_type = _clean(case.get("type")).lower()
    if case_type not in _VALID_TYPES:
        errors.append(
            f"{where}: 'type' must be one of {_VALID_TYPES}, got {case.get('type')!r}."
        )

    priority = _clean(case.get("priority")).lower()
    if priority not in _VALID_PRIORITIES:
        errors.append(
            f"{where}: 'priority' must be one of {_VALID_PRIORITIES}, "
            f"got {case.get('priority')!r}."
        )

    auth_intent = _validate_auth_intent(
        case.get("auth_intent"), f"{where}.auth_intent", errors
    )

    expected_result = _clean(case.get("expected_result"))
    if not expected_result:
        errors.append(f"{where}: 'expected_result' is missing or empty.")

    preconditions = _string_list(
        case.get("preconditions"), f"{where}.preconditions", errors, required=False
    )
    steps = _string_list(case.get("steps"), f"{where}.steps", errors, required=True)
    related_service = _validate_related_service(
        case.get("related_service"), f"{where}.related_service", errors
    )

    valid = (
        bool(case_id)
        and bool(title)
        and case_type in _VALID_TYPES
        and priority in _VALID_PRIORITIES
        and bool(expected_result)
        and bool(steps)
    )
    if not valid:
        return None

    return TaskTestCase(
        id=case_id,
        title=title,
        type=case_type,
        priority=priority,
        preconditions=preconditions,
        steps=steps,
        expected_result=expected_result,
        related_service=related_service,
        auth_intent=auth_intent,
    )


def _dedupe(cases: list[TaskTestCase]) -> list[TaskTestCase]:
    """تست‌کیس‌های کاملاً تکراری را حذف می‌کند.

    «تکراری» یعنی نوع، عنوان، قدم‌ها، نتیجه‌ی مورد انتظار و قصدِ احراز هویت
    یکسان باشند. اگر دو سناریو در یکی از این‌ها تفاوت داشته باشند، هر دو
    می‌مانند — از جمله دو تستِ یک endpoint با رفتارِ اعتبارنامه‌ی متفاوت.
    """
    seen: set[tuple] = set()
    kept: list[TaskTestCase] = []
    for case in cases:
        key = (
            case["type"],
            case["title"].lower(),
            tuple(step.lower() for step in case["steps"]),
            case["expected_result"].lower(),
            case["auth_intent"],
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(case)
    return kept


# ── ردیابیِ نیازمندی، ضرورت و پوشش ───────────────────────────────────────────
#
# همه‌ی این فیلدها اختیاری‌اند: نبودِ آن‌ها یعنی همان قراردادِ قبلی. وقتی مدل
# آن‌ها را می‌فرستد، قطعی سنجیده می‌شوند تا
#   * هر تست‌کیس به یک نیازمندیِ رفتاری قابلِ ردیابی باشد؛
#   * نیازمندیِ «پیاده‌سازی» (مثلاً ثبتِ یک endpoint در یک سیستمِ دسترسی) به
#     تنهایی تست‌کیس نسازد؛
#   * کنارگذاشتنِ یک تست‌کیس هیچ پوششِ نیازمندی‌ای را بی‌صدا از بین نبرد.

REQUIREMENT_KINDS = ("behavior", "implementation")
REQUIREMENT_SOURCES = ("description", "comment", "attachment", "user")
CHECK_KINDS = (
    "field_present",
    "field_absent",
    "field_value",
    "field_type",
    "list_empty",
    "list_count",
    "pagination",
    "rejected",
    "success",
    "other",
)


def _validate_requirements(raw: object, errors: list[str]) -> list[dict] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        errors.append("'requirements' must be an array.")
        return None
    requirements: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"requirements[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: expected an object.")
            continue
        requirement_id = _clean(item.get("id"))
        text = _clean(item.get("text"))
        kind = _clean(item.get("kind")).lower()
        source = _clean(item.get("source")).lower()
        if not requirement_id:
            errors.append(f"{where}: 'id' is missing or empty.")
            continue
        if requirement_id in seen:
            errors.append(f"{where}: duplicate requirement id {requirement_id!r}.")
            continue
        seen.add(requirement_id)
        if not text:
            errors.append(f"{where} ('{requirement_id}'): 'text' is missing or empty.")
        if kind not in REQUIREMENT_KINDS:
            errors.append(
                f"{where} ('{requirement_id}'): 'kind' must be one of {REQUIREMENT_KINDS}."
            )
        if source and source not in REQUIREMENT_SOURCES:
            errors.append(
                f"{where} ('{requirement_id}'): 'source' must be one of {REQUIREMENT_SOURCES}."
            )
        normalized = {"id": requirement_id, "text": text, "kind": kind}
        if source:
            normalized["source"] = source
        for key in ("source_reference", "evidence"):
            if _clean(item.get(key)):
                normalized[key] = _clean(item.get(key))
        requirements.append(normalized)
    return requirements


def _validate_refs(
    raw: object, where: str, known: dict[str, str] | None, errors: list[str]
) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        errors.append(f"{where}: must be an array of requirement ids.")
        return []
    refs = [item.strip() for item in raw if item.strip()]
    if refs and known is None:
        errors.append(f"{where}: requirement ids are given but 'requirements' is missing.")
        return refs
    for ref in refs:
        if known is not None and ref not in known:
            errors.append(f"{where}: unknown requirement id {ref!r}.")
    return list(dict.fromkeys(refs))


def _validate_checks(raw: object, where: str, errors: list[str]) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"{where}: must be an array.")
        return []
    checks: list[dict] = []
    for index, item in enumerate(raw):
        item_where = f"{where}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_where}: expected an object.")
            continue
        kind = _clean(item.get("kind")).lower()
        description = _clean(item.get("description"))
        if kind not in CHECK_KINDS:
            errors.append(f"{item_where}: 'kind' must be one of {CHECK_KINDS}.")
            continue
        if not description:
            errors.append(f"{item_where}: 'description' is missing or empty.")
            continue
        fields = item.get("fields")
        if fields is not None and (
            not isinstance(fields, list) or not all(isinstance(f, str) for f in fields)
        ):
            errors.append(f"{item_where}: 'fields' must be an array of strings.")
            continue
        check = {"kind": kind, "description": description}
        names = [name.strip() for name in fields or [] if name.strip()]
        if names:
            check["fields"] = names
        for key in ("requirement_ref", "evidence"):
            if _clean(item.get(key)):
                check[key] = _clean(item.get(key))
        checks.append(check)
    return checks


def _validate_input_intent(raw: object, where: str, errors: list[str]) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"{where}: must be an array.")
        return []
    intents: list[dict] = []
    for index, item in enumerate(raw):
        item_where = f"{where}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_where}: expected an object.")
            continue
        intent = _clean(item.get("intent"))
        if not intent:
            errors.append(f"{item_where}: 'intent' is missing or empty.")
            continue
        normalized = {"name": _clean(item.get("name")), "intent": intent}
        value = item.get("value")
        if value is not None and isinstance(value, (str, int, float, bool)):
            normalized["value"] = value
        if _clean(item.get("evidence")):
            normalized["evidence"] = _clean(item.get("evidence"))
        intents.append(normalized)
    return intents


def _case_traceability(
    raw_case: dict, case_id: str, known: dict[str, str] | None, errors: list[str]
) -> dict:
    """فیلدهای اختیاریِ ردیابی و طراحیِ سطحِ نیازمندیِ یک تست‌کیس."""
    where = f"test case {case_id!r}"
    extras: dict = {}
    refs = _validate_refs(
        raw_case.get("requirement_refs"), f"{where}.requirement_refs", known, errors
    )
    if refs:
        extras["requirement_refs"] = refs
        if known is not None and all(known.get(ref) == "implementation" for ref in refs):
            errors.append(
                f"{where}: it is traced only to implementation requirement(s) "
                f"{', '.join(refs)}. An implementation requirement is kept for "
                f"traceability but does not by itself require a test case; trace the "
                f"case to a behaviour requirement or drop it."
            )
    checks = _validate_checks(raw_case.get("checks"), f"{where}.checks", errors)
    if checks:
        extras["checks"] = checks
    intents = _validate_input_intent(
        raw_case.get("input_intent"), f"{where}.input_intent", errors
    )
    if intents:
        extras["input_intent"] = intents
    return extras


def _validate_dropped(
    raw: object,
    kept: list[dict],
    known: dict[str, str] | None,
    errors: list[str],
) -> list[dict] | None:
    """تست‌کیس‌های کنارگذاشته — هر حذف دلیل و پوشش‌دهنده دارد و پوشش را نمی‌کاهد."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        errors.append("'dropped_test_cases' must be an array.")
        return None
    kept_ids = {case["id"] for case in kept}
    covered: set[str] = set()
    for case in kept:
        covered.update(case.get("requirement_refs") or [])
    dropped: list[dict] = []
    for index, item in enumerate(raw):
        where = f"dropped_test_cases[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: expected an object.")
            continue
        title = _clean(item.get("title"))
        reason = _clean(item.get("reason"))
        if not title or not reason:
            errors.append(f"{where}: 'title' and 'reason' are required.")
            continue
        covered_by = item.get("covered_by")
        if covered_by is None:
            covered_by = []
        if not isinstance(covered_by, list) or not all(isinstance(c, str) for c in covered_by):
            errors.append(f"{where}: 'covered_by' must be an array of test case ids.")
            continue
        covered_by = [c.strip() for c in covered_by if c.strip()]
        for case_id in covered_by:
            if case_id not in kept_ids:
                errors.append(f"{where}: 'covered_by' names unknown test case {case_id!r}.")
        refs = _validate_refs(item.get("requirement_refs"), f"{where}.requirement_refs", known, errors)
        lost = [ref for ref in refs if ref not in covered]
        if lost:
            errors.append(
                f"{where} ('{title}'): dropping it would lose coverage of requirement(s) "
                f"{', '.join(lost)} — no kept test case is traced to them. Keep the case "
                f"or trace a kept case to those requirements."
            )
        entry = {"title": title, "reason": reason, "covered_by": covered_by}
        if refs:
            entry["requirement_refs"] = refs
        dropped.append(entry)
    return dropped


def _apply_traceability(
    data: dict, kept: list[TaskTestCase], errors: list[str]
) -> tuple[list[dict] | None, list[dict] | None, list[str]]:
    """فیلدهای ردیابی را می‌سنجد و روی تست‌کیس‌های نگه‌داشته‌شده می‌گذارد.

    برمی‌گرداند: (نیازمندی‌ها، تست‌کیس‌های کنارگذاشته، هشدارها) — None یعنی
    مدل آن بخش را نفرستاده است.
    """
    requirements = _validate_requirements(data.get("requirements"), errors)
    known = (
        {item["id"]: item["kind"] for item in requirements}
        if requirements is not None
        else None
    )
    raw_by_id: dict[str, dict] = {}
    for raw_case in data.get("test_cases") or []:
        if isinstance(raw_case, dict) and _clean(raw_case.get("id")):
            raw_by_id.setdefault(_clean(raw_case.get("id")), raw_case)

    warnings: list[str] = []
    for case in kept:
        extras = _case_traceability(raw_by_id.get(case["id"], {}), case["id"], known, errors)
        case.update(extras)  # type: ignore[typeddict-item]
        if requirements and not extras.get("requirement_refs"):
            warnings.append(
                f"Test case {case['id']!r} is not traced to any requirement."
            )
    dropped = _validate_dropped(data.get("dropped_test_cases"), kept, known, errors)
    return requirements, dropped, warnings


def validate_analysis(data: dict) -> TaskAnalysisResult:
    """خروجیِ JSON مدل را اعتبارسنجی و نرمال می‌کند.

    همه‌ی خطاها یک‌جا جمع و در یک پیام گزارش می‌شوند تا مدل بتواند در یک دور
    همه را اصلاح کند. تست‌کیس‌های تکراری حذف می‌شوند.

    پرتاب می‌کند:
        TaskAnalysisError : خروجی با قرارداد نخواند
    """
    errors: list[str] = []

    task_summary = _clean(data.get("task_summary"))
    if not task_summary:
        errors.append("'task_summary' is missing or empty.")

    requirements = _string_list(
        data.get("identified_requirements"),
        "identified_requirements",
        errors,
        required=False,
    )
    clarifications = _string_list(
        data.get("clarifications"), "clarifications", errors, required=False
    )

    raw_cases = data.get("test_cases")
    cases: list[TaskTestCase] = []
    if not isinstance(raw_cases, list) or not raw_cases:
        errors.append("'test_cases' must be a non-empty array.")
    else:
        for index, raw_case in enumerate(raw_cases):
            case = _validate_test_case(raw_case, f"test_cases[{index}]", errors)
            if case is not None:
                cases.append(case)

    seen_ids: set[str] = set()
    for case in cases:
        if case["id"] in seen_ids:
            errors.append(f"duplicate test case id {case['id']!r} — ids must be unique.")
        seen_ids.add(case["id"])

    kept = _dedupe(cases)
    variables = validate_requested_variables(data.get("variables"), kept, errors)
    structured, dropped, trace_warnings = _apply_traceability(data, kept, errors)

    if errors:
        raise TaskAnalysisError(
            "LLM output does not match the required test case format:\n  - "
            + "\n  - ".join(errors)
        )

    if structured and not requirements:
        requirements = [item["text"] for item in structured if item["text"]]
    result = TaskAnalysisResult(
        task_summary=task_summary,
        identified_requirements=requirements,
        test_cases=kept,
        clarifications=clarifications,
        variables=variables,
    )
    # کلیدهای اختیاری فقط وقتی مدل آن‌ها را فرستاده است.
    if structured is not None:
        result["requirements"] = structured
    if dropped is not None:
        result["dropped_test_cases"] = dropped
    if trace_warnings:
        result["traceability_warnings"] = trace_warnings
    return result


def parse_analysis(raw: str) -> TaskAnalysisResult:
    """پاسخِ خامِ LLM را به نتیجه‌ی تحلیلِ تسک تبدیل می‌کند.

    پرتاب می‌کند:
        TaskAnalysisError : پاسخ خالی/غیرِ JSON باشد یا با قرارداد نخواند
    """
    try:
        data = extract_json_object(raw)
    except JsonExtractionError as exc:
        raise TaskAnalysisError(str(exc)) from exc
    return validate_analysis(data)


def _has_no_test_cases(data: dict) -> bool:
    """آیا پاسخِ JSONِ کامل هیچ تست‌کیسِ قابلِ استفاده‌ای ندارد؟

    نبودنِ کلید، مقدارِ غیرلیستی و لیستِ خالی همه «بدونِ تست‌کیس» هستند. این
    تنها حالتی است که در آن پاسخِ کاملِ یک‌فراخوانی به تولیدِ مرحله‌ای سپرده
    می‌شود — نه پذیرفته و نه به‌عنوان JSONِ نامعتبر گزارش می‌شود.
    """
    cases = data.get("test_cases")
    return not isinstance(cases, list) or not cases


def _validate_planned_case(
    case: object,
    where: str,
    errors: list[str],
) -> PlannedTestCase | None:
    """یک تست‌کیسِ برنامه (بدونِ جزئیاتِ اجرایی) را اعتبارسنجی می‌کند."""
    if not isinstance(case, dict):
        errors.append(f"{where}: expected an object, got {type(case).__name__}.")
        return None

    case_id = _clean(case.get("id"))
    if not case_id:
        errors.append(f"{where}: 'id' is missing or empty.")
    where = f"{where} ('{case_id or '?'}')"

    title = _clean(case.get("title"))
    if not title:
        errors.append(f"{where}: 'title' is missing or empty.")

    case_type = _clean(case.get("type")).lower()
    if case_type not in _VALID_TYPES:
        errors.append(
            f"{where}: 'type' must be one of {_VALID_TYPES}, got {case.get('type')!r}."
        )

    priority = _clean(case.get("priority")).lower()
    if priority not in _VALID_PRIORITIES:
        errors.append(
            f"{where}: 'priority' must be one of {_VALID_PRIORITIES}, "
            f"got {case.get('priority')!r}."
        )

    auth_intent = _validate_auth_intent(
        case.get("auth_intent"), f"{where}.auth_intent", errors
    )

    related_service = _validate_related_service(
        case.get("related_service"), f"{where}.related_service", errors
    )

    valid = (
        bool(case_id)
        and bool(title)
        and case_type in _VALID_TYPES
        and priority in _VALID_PRIORITIES
    )
    if not valid:
        return None

    planned = PlannedTestCase(
        id=case_id,
        title=title,
        type=case_type,
        priority=priority,
        related_service=related_service,
        auth_intent=auth_intent,
    )
    # فیلدهای اختیاریِ ردیابی همان‌طور که آمده‌اند حمل می‌شوند؛ سنجشِ کاملشان
    # در validate_plan و دوباره در validate_analysis انجام می‌شود.
    for key in ("requirement_refs", "checks", "input_intent"):
        if case.get(key) is not None:
            planned[key] = case[key]  # type: ignore[literal-required]
    return planned


def validate_plan(data: dict) -> TaskAnalysisPlan:
    """خروجیِ JSON مرحله‌ی «برنامه» را اعتبارسنجی و نرمال می‌کند.

    برنامه فقط «چه چیزی» را تعیین می‌کند — شناسه، عنوان، نوع، اولویت و سرویسِ
    مرتبط. جزئیاتِ اجرایی در مرحله‌ی بعد و در دسته‌های کوچک تولید می‌شوند.

    پرتاب می‌کند:
        TaskAnalysisError : خروجی با قرارداد نخواند
    """
    errors: list[str] = []

    task_summary = _clean(data.get("task_summary"))
    if not task_summary:
        errors.append("'task_summary' is missing or empty.")

    requirements = _string_list(
        data.get("identified_requirements"),
        "identified_requirements",
        errors,
        required=False,
    )
    clarifications = _string_list(
        data.get("clarifications"), "clarifications", errors, required=False
    )

    raw_cases = data.get("test_cases")
    cases: list[PlannedTestCase] = []
    if not isinstance(raw_cases, list) or not raw_cases:
        errors.append("'test_cases' must be a non-empty array.")
    else:
        for index, raw_case in enumerate(raw_cases):
            case = _validate_planned_case(raw_case, f"test_cases[{index}]", errors)
            if case is not None:
                cases.append(case)

    seen_ids: set[str] = set()
    for case in cases:
        if case["id"] in seen_ids:
            errors.append(f"duplicate test case id {case['id']!r} — ids must be unique.")
        seen_ids.add(case["id"])

    variables = validate_requested_variables(data.get("variables"), cases, errors)
    # همان سنجشِ ردیابی روی کپیِ تست‌کیس‌ها — تا خطا پیش از گسترش برگردد.
    _apply_traceability(data, [dict(case) for case in cases], errors)  # type: ignore[misc]

    if errors:
        raise TaskAnalysisError(
            "LLM output does not match the required test case plan format:\n  - "
            + "\n  - ".join(errors)
        )

    plan = TaskAnalysisPlan(
        task_summary=task_summary,
        identified_requirements=requirements,
        test_cases=cases,
        clarifications=clarifications,
        variables=variables,
    )
    for key in ("requirements", "dropped_test_cases"):
        if data.get(key) is not None:
            plan[key] = data[key]  # type: ignore[literal-required]
    return plan


def validate_details(
    data: dict,
    planned: list[PlannedTestCase],
) -> list[TestCaseDetails]:
    """جزئیاتِ یک دسته را اعتبارسنجی می‌کند و به ترتیبِ برنامه برمی‌گرداند.

    قرارداد سخت‌گیرانه است: دقیقاً همان شناسه‌های برنامه، هر کدام یک‌بار.
    شناسه‌ی ناشناخته، تکراری یا گم‌شده خطاست — هیچ تست‌کیسی نه اضافه می‌شود و
    نه بی‌سروصدا حذف می‌گردد.

    پرتاب می‌کند:
        TaskAnalysisError : خروجی با قرارداد نخواند
    """
    errors: list[str] = []
    planned_ids = [case["id"] for case in planned]
    planned_set = set(planned_ids)

    raw_cases = data.get("test_cases")
    by_id: dict[str, TestCaseDetails] = {}
    if not isinstance(raw_cases, list) or not raw_cases:
        errors.append("'test_cases' must be a non-empty array.")
    else:
        for index, raw_case in enumerate(raw_cases):
            if not isinstance(raw_case, dict):
                errors.append(
                    f"test_cases[{index}]: expected an object, got "
                    f"{type(raw_case).__name__}."
                )
                continue

            case_id = _clean(raw_case.get("id"))
            if not case_id:
                errors.append(f"test_cases[{index}]: 'id' is missing or empty.")
                continue

            where = f"test_cases[{index}] ('{case_id}')"
            if case_id not in planned_set:
                errors.append(
                    f"{where}: id is not in the planned test cases — do not add "
                    "or rename test cases."
                )
                continue
            if case_id in by_id:
                errors.append(f"{where}: duplicate detail for the same id.")
                continue

            preconditions = _string_list(
                raw_case.get("preconditions"),
                f"{where}.preconditions",
                errors,
                required=False,
            )
            steps = _string_list(
                raw_case.get("steps"), f"{where}.steps", errors, required=True
            )
            expected_result = _clean(raw_case.get("expected_result"))
            if not expected_result:
                errors.append(f"{where}: 'expected_result' is missing or empty.")

            if steps and expected_result:
                by_id[case_id] = TestCaseDetails(
                    id=case_id,
                    preconditions=preconditions,
                    steps=steps,
                    expected_result=expected_result,
                )

    missing = [case_id for case_id in planned_ids if case_id not in by_id]
    if missing:
        errors.append(
            "missing details for planned test case id(s): "
            + ", ".join(repr(case_id) for case_id in missing)
            + " — every planned test case must be expanded."
        )

    if errors:
        raise TaskAnalysisError(
            "LLM output does not match the required test case format:\n  - "
            + "\n  - ".join(errors)
        )

    return [by_id[case_id] for case_id in planned_ids]


def _assemble_result(
    plan: TaskAnalysisPlan,
    details: list[TestCaseDetails],
) -> TaskAnalysisResult:
    """برنامه و جزئیات را به قراردادِ نهاییِ TaskAnalysisResult تبدیل می‌کند."""
    expected = [case["id"] for case in plan["test_cases"]]
    actual = [detail["id"] for detail in details]
    if actual != expected:
        raise TaskAnalysisError(
            "Internal error: expanded details do not match the planned test cases "
            f"({len(actual)} detail(s) for {len(expected)} planned case(s), or "
            "their order changed). No partial result can be used."
        )

    details_by_id = {detail["id"]: detail for detail in details}
    test_cases: list[dict] = []
    for planned in plan["test_cases"]:
        detail = details_by_id[planned["id"]]
        test_cases.append(
            {
                "id": planned["id"],
                "title": planned["title"],
                "type": planned["type"],
                "priority": planned["priority"],
                "preconditions": detail["preconditions"],
                "steps": detail["steps"],
                "expected_result": detail["expected_result"],
                "related_service": planned["related_service"],
                "auth_intent": planned["auth_intent"],
                **{
                    key: planned[key]  # type: ignore[literal-required]
                    for key in ("requirement_refs", "checks", "input_intent")
                    if key in planned
                },
            }
        )

    assembled = {
        "task_summary": plan["task_summary"],
        "identified_requirements": plan["identified_requirements"],
        "test_cases": test_cases,
        "clarifications": plan["clarifications"],
        "variables": plan.get("variables") or [],
    }
    for key in ("requirements", "dropped_test_cases"):
        if key in plan:
            assembled[key] = plan[key]  # type: ignore[literal-required]
    return validate_analysis(assembled)


def _batches(items: list, size: int) -> list[list]:
    """اقلام را به دسته‌های پیوسته‌ی حداکثر `size`تایی تقسیم می‌کند."""
    return [items[start : start + size] for start in range(0, len(items), size)]


# ── تولید ────────────────────────────────────────────────────────────────────

class TaskAnalysisGenerator:
    """از توصیفِ تسک، تست‌کیس‌های ساختاریافته برای بازبینیِ انسانی می‌سازد.

    مسیرِ عادی یک فراخوانی است. اگر پاسخ به سقفِ توکن بخورد، تولیدِ مرحله‌ای
    جایگزین می‌شود: اول یک «برنامه‌ی فشرده» (بدونِ preconditions/steps/
    expected_result) و بعد جزئیات در دسته‌های کوچک. هیچ پاسخِ بریده‌ای ترمیم یا
    پذیرفته نمی‌شود.

    پارامترها:
        debug_config        : تنظیماتِ لاگِ پروژه
        temperature         : دمای LLM — پایین، چون خروجی باید ساختاریافته باشد
        max_tokens          : سقفِ توکنِ پاسخ (None یعنی مقدارِ مشترکِ LLM_MAX_OUTPUT_TOKENS)
        prompt_path         : مسیرِ فایلِ پرامپتِ اصلی (برای تست قابلِ جایگزینی است)
        plan_prompt_path    : مسیرِ پرامپتِ مرحله‌ی «برنامه‌ی تست‌کیس»
        details_prompt_path : مسیرِ پرامپتِ مرحله‌ی «گسترشِ تست‌کیس»
        batch_size          : حداکثر تعداد تست‌کیس در هر فراخوانیِ گسترش
    """

    # نامی که در LLMClient و observation استفاده می‌شود
    name = "task_analysis"

    def __init__(
        self,
        debug_config: DebugConfig | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        prompt_path: Path | str | None = None,
        plan_prompt_path: Path | str | None = None,
        details_prompt_path: Path | str | None = None,
        batch_size: int | None = None,
    ) -> None:
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
        self._prompt_path = Path(prompt_path) if prompt_path else _PROMPT_PATH
        self._plan_prompt_path = (
            Path(plan_prompt_path) if plan_prompt_path else _PLAN_PROMPT_PATH
        )
        self._details_prompt_path = (
            Path(details_prompt_path) if details_prompt_path else _DETAILS_PROMPT_PATH
        )
        self._temperature = temperature
        # None یعنی «مقدارِ مشترکِ پروژه»؛ عددِ صریح همیشه برنده است.
        self._max_tokens = LLM_MAX_OUTPUT_TOKENS if max_tokens is None else max_tokens
        self._batch_size = (
            DEFAULT_TEST_CASE_BATCH_SIZE if batch_size is None else batch_size
        )
        if self._batch_size < 1:
            raise ValueError("batch_size must be at least 1.")

    def generate(
        self,
        task_description: str,
        user_id: str,
        developed_services: str = "",
        sources: tuple[TaskSource, ...] | None = None,
    ) -> TaskAnalysisResult:
        """تست‌کیس‌ها را از توصیفِ تسک تولید می‌کند.

        ``sources`` منابعی است که شاهدِ تصمیم‌های محیط در آن‌ها سنجیده می‌شود
        (Description/Comments/پیوست‌های Jira)؛ None یعنی یک منبعِ description با
        همین متن. پرامپت از آن ساخته نمی‌شود — مدل همان task_description را می‌بیند.

        پرتاب می‌کند:
            ValueError           : توصیفِ تسک خالی باشد
            TaskAnalysisError    : خروجیِ LLM خالی/نامعتبر/مغایرِ قرارداد باشد
        """
        if not (task_description or "").strip():
            raise ValueError("task_description is empty — nothing to analyze.")

        system_prompt, user_message = build_prompt(
            self._prompt_path, task_description, developed_services
        )
        self._log.info(
            "شروع تحلیل تسک",
            user_id=user_id,
            task_chars=len(task_description),
            services_chars=len(developed_services or ""),
        )
        self._log.trace(
            "prompt ساخته شد",
            system_chars=len(system_prompt),
            user_chars=len(user_message),
        )

        llm = LLMClient(user_id=user_id, agent_name=self.name)
        self._log.debug("ارسال تسک به LLM")
        raw = self._chat(llm, system_prompt, user_message)
        self._log.trace("پاسخ LLM دریافت شد", response_chars=len(raw or ""))

        # پاسخِ بریده را نه ترمیم می‌کنیم و نه حدس می‌زنیم. به‌جای خطا، تولیدِ
        # مرحله‌ای اجرا می‌شود؛ خودِ پاسخِ ناقص هرگز خوانده نمی‌شود.
        truncated = truncation_message(raw)
        if truncated:
            self._log.warning(
                "پاسخِ یک‌مرحله‌ای بریده شد — تولیدِ مرحله‌ای آغاز می‌شود",
                finish_reason="length",
                batch_size=self._batch_size,
            )
            result, raw_decisions = self._generate_staged(
                llm,
                task_description,
                developed_services,
                truncated,
                single_call=GenerationAttempt(
                    stage=STAGE_SINGLE_CALL,
                    test_cases_state="truncated (finish_reason='length'; nothing was read)",
                    response_id=_response_id(raw),
                ),
            )
        else:
            result, raw_decisions = self._analyze_single_call(
                llm, raw, task_description, developed_services
            )

        result.update(
            extract_environment_decisions(
                raw_decisions,
                sources if sources is not None else sources_from_text(task_description),
                [case["id"] for case in result["test_cases"]],
            )
        )
        self._log_result(result)
        return result

    def _analyze_single_call(
        self,
        llm: LLMClient,
        raw: str,
        task_description: str,
        developed_services: str,
    ) -> tuple[TaskAnalysisResult, Any]:
        """پاسخِ کاملِ مسیرِ یک‌فراخوانی را اعتبارسنجی می‌کند.

        خروجی: (نتیجه، environment_decisionsِ خامِ همان پاسخی که پذیرفته شد).

        اگر پاسخ JSONِ کامل باشد ولی هیچ تست‌کیسی نداشته باشد، «نتیجه‌ی خالی»
        پذیرفته نمی‌شود؛ به‌جای آن همان تولیدِ مرحله‌ای اجرا می‌شود که برای
        پاسخِ بریده استفاده می‌شد. اگر برنامه‌ی مرحله‌ی اول هم خالی یا نامعتبر
        باشد، خطای همان مرحله بالا می‌رود و هیچ لیستِ خالی‌ای برنمی‌گردد.
        """
        try:
            data = extract_json_object(raw)
        except JsonExtractionError as exc:
            raise TaskAnalysisError(str(exc)) from exc

        if _has_no_test_cases(data):
            attempt = _empty_attempt(STAGE_SINGLE_CALL, data, raw)
            self._log.warning(
                "پاسخِ یک‌فراخوانی هیچ تست‌کیسی نداشت — تولیدِ مرحله‌ای آغاز می‌شود",
                reason="empty_test_cases",
                test_cases=attempt.test_cases_state,
                response_id=attempt.response_id,
                clarifications=len(attempt.clarifications),
                batch_size=self._batch_size,
            )
            return self._generate_staged(
                llm,
                task_description,
                developed_services,
                _EMPTY_SINGLE_RESPONSE,
                single_call=attempt,
            )
        return validate_analysis(data), data.get("environment_decisions")

    def _chat(self, llm: LLMClient, system_prompt: str, user_message: str) -> str:
        """همه‌ی فراخوانی‌ها همان temperature و سقفِ توکن را می‌گیرند."""
        return llm.chat(
            user_message=user_message,
            system_prompt=system_prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )

    def _generate_staged(
        self,
        llm: LLMClient,
        task_description: str,
        developed_services: str,
        fallback_reason: str,
        single_call: GenerationAttempt | None = None,
    ) -> tuple[TaskAnalysisResult, Any]:
        """برنامه‌ی فشرده می‌سازد و جزئیات را دسته‌به‌دسته گسترش می‌دهد.

        ``single_call`` می‌گوید پاسخِ یک‌فراخوانی چرا کنار گذاشته شد — فقط برای
        پیامِ خطا اگر برنامه هم تست‌کیسی نداشته باشد.
        """
        plan = self._plan(
            llm, task_description, developed_services, fallback_reason, single_call
        )
        details = self._expand_all(llm, task_description, plan["test_cases"])
        return _assemble_result(plan, details), plan.get("environment_decisions")

    def _plan(
        self,
        llm: LLMClient,
        task_description: str,
        developed_services: str,
        fallback_reason: str,
        single_call: GenerationAttempt | None = None,
    ) -> TaskAnalysisPlan:
        system_prompt, user_message = build_plan_prompt(
            self._plan_prompt_path, task_description, developed_services
        )
        raw = self._chat(llm, system_prompt, user_message)

        truncated = truncation_message(raw)
        if truncated:
            self._log.error("برنامه‌ی تست‌کیس نیز بریده شد", finish_reason="length")
            raise TaskAnalysisError(
                f"{fallback_reason}\n\n"
                "A staged generation was attempted because the single-call "
                "response could not be used, but the compact test case plan also "
                "reached the output token limit. No partial test case list was "
                "used."
            )

        try:
            data = extract_json_object(raw)
        except JsonExtractionError as exc:
            raise TaskAnalysisError(str(exc)) from exc
        if not _has_no_test_cases(data):
            plan = validate_plan(data)
            plan["environment_decisions"] = data.get("environment_decisions")
            return plan

        # هیچ مرحله‌ای تست‌کیس نساخت: همان اعتبارسنجی خطا می‌دهد؛ پیامش فقط با
        # تشخیصِ مرحله‌ها کامل می‌شود و خطای اصلی به‌عنوانِ علت حفظ می‌شود.
        attempts = [a for a in (single_call, _empty_attempt(STAGE_PLAN, data, raw)) if a]
        try:
            return validate_plan(data)
        except TaskAnalysisError as exc:
            self._log.error(
                "هیچ مرحله‌ای تست‌کیس تولید نکرد",
                stages=[a.stage for a in attempts],
                test_cases=[a.test_cases_state for a in attempts],
                response_ids=[a.response_id for a in attempts],
                clarifications=[len(a.clarifications) for a in attempts],
            )
            raise TaskAnalysisError(f"{exc}{no_test_cases_diagnostics(attempts)}") from exc

    def _expand_all(
        self,
        llm: LLMClient,
        task_description: str,
        planned: list[PlannedTestCase],
    ) -> list[TestCaseDetails]:
        batches = _batches(planned, self._batch_size)
        self._log.info(
            "تولیدِ مرحله‌ای: گسترشِ برنامه در دسته‌ها",
            planned=len(planned),
            batches=len(batches),
            batch_size=self._batch_size,
        )

        details: list[TestCaseDetails] = []
        for number, batch in enumerate(batches, start=1):
            try:
                details.extend(self._expand(llm, task_description, batch))
            except TaskAnalysisError as exc:
                if len(batches) == 1:
                    raise
                first = (number - 1) * self._batch_size + 1
                last = first + len(batch) - 1
                raise TaskAnalysisError(
                    f"{exc}\n\nExpanding the test case plan failed for planned "
                    f"test cases {first}-{last} of {len(planned)} (batch "
                    f"{number} of {len(batches)}, batch size "
                    f"{self._batch_size}). No partial result can be used."
                ) from exc
        return details

    def _expand(
        self,
        llm: LLMClient,
        task_description: str,
        batch: list[PlannedTestCase],
    ) -> list[TestCaseDetails]:
        system_prompt, user_message = build_details_prompt(
            self._details_prompt_path, task_description, batch
        )
        raw = self._chat(llm, system_prompt, user_message)

        # ترتیب مهم است: اول بریدگی، بعد JSON. پاسخِ بریده هرگز به‌عنوان
        # «JSON نامعتبر» گزارش نمی‌شود و هرگز ترمیم نمی‌گردد.
        truncated = truncation_message(raw)
        if truncated:
            self._log.error("پاسخِ گسترش بریده شد", finish_reason="length")
            raise TaskAnalysisError(truncated)

        try:
            data = extract_json_object(raw)
        except JsonExtractionError as exc:
            raise TaskAnalysisError(str(exc)) from exc
        return validate_details(data, batch)

    def _log_result(self, result: TaskAnalysisResult) -> None:
        counts = Counter(case["type"] for case in result["test_cases"])
        self._log.info(
            "تحلیل تسک کامل شد",
            total=len(result["test_cases"]),
            positive=counts.get("positive", 0),
            negative=counts.get("negative", 0),
            boundary=counts.get("boundary", 0),
            state_transition=counts.get("state_transition", 0),
            clarifications=len(result["clarifications"]),
        )
        if not counts.get("positive"):
            self._log.warning(
                "هیچ تست‌کیسِ مثبتی تولید نشد — خروجی برای بازبینی انسانی است",
                total=len(result["test_cases"]),
            )
