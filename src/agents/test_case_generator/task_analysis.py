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
from pathlib import Path

from typing_extensions import TypedDict

from src.agents.test_case_generator.json_output import (
    JsonExtractionError,
    extract_json_object,
)
from src.config import LLM_MAX_OUTPUT_TOKENS
from src.debug import DebugConfig
from src.llm_client import LLMClient, truncation_message

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


class TaskAnalysisResult(TypedDict):
    task_summary: str
    identified_requirements: list[str]
    test_cases: list[TaskTestCase]
    clarifications: list[str]


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


class TaskAnalysisPlan(TypedDict):
    """برنامه‌ی فشرده‌ی تست‌کیس‌ها — مرحله‌ی اولِ تولیدِ مرحله‌ای."""

    task_summary: str
    identified_requirements: list[str]
    test_cases: list[PlannedTestCase]
    clarifications: list[str]


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
    )


def _dedupe(cases: list[TaskTestCase]) -> list[TaskTestCase]:
    """تست‌کیس‌های کاملاً تکراری را حذف می‌کند.

    «تکراری» یعنی نوع، عنوان، قدم‌ها و نتیجه‌ی مورد انتظار یکسان باشند. اگر دو
    سناریو در یکی از این‌ها تفاوت داشته باشند، هر دو می‌مانند.
    """
    seen: set[tuple] = set()
    kept: list[TaskTestCase] = []
    for case in cases:
        key = (
            case["type"],
            case["title"].lower(),
            tuple(step.lower() for step in case["steps"]),
            case["expected_result"].lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(case)
    return kept


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

    if errors:
        raise TaskAnalysisError(
            "LLM output does not match the required test case format:\n  - "
            + "\n  - ".join(errors)
        )

    return TaskAnalysisResult(
        task_summary=task_summary,
        identified_requirements=requirements,
        test_cases=_dedupe(cases),
        clarifications=clarifications,
    )


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

    return PlannedTestCase(
        id=case_id,
        title=title,
        type=case_type,
        priority=priority,
        related_service=related_service,
    )


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

    if errors:
        raise TaskAnalysisError(
            "LLM output does not match the required test case plan format:\n  - "
            + "\n  - ".join(errors)
        )

    return TaskAnalysisPlan(
        task_summary=task_summary,
        identified_requirements=requirements,
        test_cases=cases,
        clarifications=clarifications,
    )


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
            }
        )

    return validate_analysis(
        {
            "task_summary": plan["task_summary"],
            "identified_requirements": plan["identified_requirements"],
            "test_cases": test_cases,
            "clarifications": plan["clarifications"],
        }
    )


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
    ) -> TaskAnalysisResult:
        """تست‌کیس‌ها را از توصیفِ تسک تولید می‌کند.

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
            result = self._generate_staged(
                llm, task_description, developed_services, truncated
            )
        else:
            result = self._analyze_single_call(
                llm, raw, task_description, developed_services
            )

        self._log_result(result)
        return result

    def _analyze_single_call(
        self,
        llm: LLMClient,
        raw: str,
        task_description: str,
        developed_services: str,
    ) -> TaskAnalysisResult:
        """پاسخِ کاملِ مسیرِ یک‌فراخوانی را اعتبارسنجی می‌کند.

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
            self._log.warning(
                "پاسخِ یک‌فراخوانی هیچ تست‌کیسی نداشت — تولیدِ مرحله‌ای آغاز می‌شود",
                reason="empty_test_cases",
                batch_size=self._batch_size,
            )
            return self._generate_staged(
                llm,
                task_description,
                developed_services,
                _EMPTY_SINGLE_RESPONSE,
            )
        return validate_analysis(data)

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
    ) -> TaskAnalysisResult:
        """برنامه‌ی فشرده می‌سازد و جزئیات را دسته‌به‌دسته گسترش می‌دهد."""
        plan = self._plan(llm, task_description, developed_services, fallback_reason)
        details = self._expand_all(llm, task_description, plan["test_cases"])
        return _assemble_result(plan, details)

    def _plan(
        self,
        llm: LLMClient,
        task_description: str,
        developed_services: str,
        fallback_reason: str,
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
        return validate_plan(data)

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
