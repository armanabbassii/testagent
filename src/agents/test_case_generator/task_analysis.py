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

import re
from collections import Counter
from pathlib import Path

from typing_extensions import TypedDict

from src.agents.test_case_generator.json_output import (
    JsonExtractionError,
    extract_json_object,
)
from src.debug import DebugConfig
from src.llm_client import LLMClient

_PROMPT_PATH = Path(__file__).parent / "prompts" / "step1_task_analysis.md"

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

# جای‌نگهدارهای فایلِ پرامپت
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(task_description|developed_services)\s*\}\}")

# بخشِ INPUT در فایلِ پرامپت: از این‌جا به بعد قالبِ ورودیِ کاربر است
_INPUT_SECTION_RE = re.compile(
    r"^={3,}[ \t]*\nINPUT[ \t]*\n={3,}[ \t]*\n", re.MULTILINE
)

# وقتی کاربر سرویسی معرفی نکرده باشد
_EMPTY_SERVICES = "(none provided)"


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


# ── ساختِ prompt ─────────────────────────────────────────────────────────────

def split_prompt(text: str) -> tuple[str, str]:
    """فایلِ پرامپت را به (system prompt، قالبِ پیامِ کاربر) تقسیم می‌کند.

    هرچه پیش از بخشِ INPUT است دستورِ سیستم است — یعنی قواعدِ تولید و قراردادِ
    خروجی. از بخشِ INPUT به بعد قالبی است که با ورودیِ کاربر پر می‌شود.

    پرتاب می‌کند:
        TaskAnalysisError : بخشِ INPUT پیدا نشود، خالی باشد، یا جای‌نگهدارها را نداشته باشد
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
    missing = {"task_description", "developed_services"} - found
    if missing:
        raise TaskAnalysisError(
            "Prompt INPUT section is missing placeholder(s): "
            + ", ".join(sorted(f"{{{{{name}}}}}" for name in missing))
        )
    return system_prompt, user_template


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
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


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


# ── تولید ────────────────────────────────────────────────────────────────────

class TaskAnalysisGenerator:
    """از توصیفِ تسک، تست‌کیس‌های ساختاریافته برای بازبینیِ انسانی می‌سازد.

    پارامترها:
        debug_config : تنظیماتِ لاگِ پروژه
        temperature  : دمای LLM — پایین، چون خروجی باید ساختاریافته باشد
        max_tokens   : سقفِ توکنِ پاسخ
        prompt_path  : مسیرِ فایلِ پرامپت (برای تست قابلِ جایگزینی است)
    """

    # نامی که در LLMClient و observation استفاده می‌شود
    name = "task_analysis"

    def __init__(
        self,
        debug_config: DebugConfig | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        prompt_path: Path | str | None = None,
    ) -> None:
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
        self._prompt_path = Path(prompt_path) if prompt_path else _PROMPT_PATH
        self._temperature = temperature
        self._max_tokens = max_tokens

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
        raw = llm.chat(
            user_message=user_message,
            system_prompt=system_prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        self._log.trace("پاسخ LLM دریافت شد", response_chars=len(raw or ""))

        result = parse_analysis(raw)

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
        return result
