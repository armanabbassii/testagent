"""
scenario_analysis.py — سناریو، ترتیبِ اجرا و وابستگی‌های داده (قدم سوم)

جریانِ قدم سوم:

    نتیجه‌ی قدم ۱ (تست‌کیس‌های کسب‌وکاری)
  + نتیجه‌ی قدم ۲ (عملیات‌های کشف‌شده و نگاشتِ تست‌کیس به API)
        → LLM: معنا، گروه‌بندیِ سناریو، ترتیب، وابستگی و جریانِ داده
        → اعتبارسنجیِ قطعی: ارجاع‌ها، ترتیب، حلقه‌ها، پارامترها و فیلدهای پاسخ
        → نتیجه‌ی ساختاریافته‌ی قدم سوم برای بازبینیِ انسانی

قاعده‌ی اصلی: LLM ساختار را پیشنهاد می‌دهد و پایتون آن را اعتبارسنجی می‌کند.
هیچ تست‌کیس، وابستگی، پارامتر یا فیلدِ پاسخی که وجود ندارد پذیرفته نمی‌شود و
هیچ حلقه‌ای در گرافِ وابستگی مجاز نیست.

این ماژول هیچ API ای را اجرا نمی‌کند، هیچ سندِ Swagger ای را نمی‌خواند و دوباره
کشف نمی‌کند، هیچ مجموعه‌ی Postman ای نمی‌سازد و هیچ متغیرِ زمانِ اجرا استخراج
نمی‌کند — فقط وابستگی را توصیف می‌کند تا قدم چهارم آن را به Postman تبدیل کند.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from typing_extensions import NotRequired, TypedDict

from src.agents.test_case_generator.api_discovery import operation_key
from src.agents.test_case_generator.api_mapping import (
    ApiMappingError,
    extract_test_cases,
)
from src.agents.test_case_generator.json_output import (
    JsonExtractionError,
    extract_json_object,
)
from src.agents.test_case_generator.variable_dependencies import (
    DEFAULT_DEPENDENCY_SCOPE,
    ORIGIN_INFERRED,
    ORIGIN_TASK,
    RequestedVariable,
    VariableInstructionError,
    first_field,
    normalize_response_path,
    requested_variables,
)
from src.config import LLM_MAX_OUTPUT_TOKENS
from src.debug import DebugConfig
from src.llm_client import LLMClient

_PROMPT_PATH = Path(__file__).parent / "prompts" / "step3_scenario_analysis.md"

# مقادیرِ مجازِ اطمینان برای وابستگی‌های استنتاج‌شده
_VALID_CONFIDENCE = ("high", "medium", "low")

# محلی که یک مقدار در پاسخِ تست‌کیسِ مبدأ تولید می‌شود
_SOURCE_LOCATIONS = ("response.body", "response.header")

# محلی که یک مقدار در درخواستِ تست‌کیسِ مقصد مصرف می‌شود
_TARGET_LOCATIONS = ("path", "query", "header", "body")

# نامِ متغیر باید بعداً قابلِ تبدیل به متغیرِ Postman باشد
_VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# مسیرِ ساده‌ی JSON مانند «$.id» یا «$.data.id»
_JSON_PATH_RE = re.compile(r"^\$(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])+$")

# بخشِ INPUT در فایلِ پرامپت
_INPUT_SECTION_RE = re.compile(
    r"^={3,}[ \t]*\nINPUT[ \t]*\n={3,}[ \t]*\n", re.MULTILINE
)

# جای‌نگهدارهای فایلِ پرامپتِ قدم سوم
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(step1_result|step2_result)\s*\}\}")

_PROMPT_PLACEHOLDERS = ("step1_result", "step2_result")


class ScenarioAnalysisError(RuntimeError):
    """ورودیِ نامعتبر قدم سوم یا خروجیی که با قرارداد نمی‌خواند."""


# ── مدل داده ─────────────────────────────────────────────────────────────────

class Scenario(TypedDict):
    """گروهی از تست‌کیس‌ها که یک جریانِ کسب‌وکاری مشترک را می‌سازند."""

    id: str
    title: str
    test_case_ids: list[str]
    reason: str


class ExecutionStep(TypedDict):
    """جایگاهِ یک تست‌کیس در ترتیبِ اجرا و وابستگی‌هایش."""

    test_case_id: str
    order: int
    depends_on: list[str]


class DependencySource(TypedDict):
    """جایی که یک مقدار تولید می‌شود."""

    test_case_id: str
    location: str
    path: str


class DependencyTarget(TypedDict):
    """جایی که یک مقدار مصرف می‌شود."""

    test_case_id: str
    location: str
    parameter: str


class DataDependency(TypedDict):
    """یک مقدار که از یک تست‌کیس به تست‌کیس‌های دیگر جریان دارد."""

    variable_name: str
    source: DependencySource
    targets: list[DependencyTarget]
    confidence: str
    reason: str
    # اسکوپِ متغیرِ Postman — «global» یا «collection». نتیجه‌ی قدیمیِ بدونِ این
    # فیلد همان collection است (variable_dependencies.dependency_scope).
    scope: NotRequired[str]
    # آیا مصرف‌کننده‌ها بدونِ این مقدار نباید اجرا شوند؟ نبودِ فیلد یعنی بله.
    required: NotRequired[bool]
    # «task» = کاربر صریحاً خواسته (قدم ۱)؛ «inferred» = استنتاجِ همین قدم
    origin: NotRequired[str]


class Clarification(TypedDict):
    """ابهامی که مدل نتوانسته قطعی کند — قابلِ خواندن توسط ماشین."""

    type: str
    test_case_id: str
    message: str


class Step3Result(TypedDict):
    """نتیجه‌ی نهاییِ قدم سوم."""

    scenarios: list[Scenario]
    execution_order: list[ExecutionStep]
    data_dependencies: list[DataDependency]
    clarifications: list[Clarification]


# ── ساختِ prompt ─────────────────────────────────────────────────────────────

def split_prompt(text: str) -> tuple[str, str]:
    """فایلِ پرامپتِ قدم سوم را به (system prompt، قالبِ پیامِ کاربر) تقسیم می‌کند.

    هرچه پیش از بخشِ INPUT است دستورِ سیستم است — قواعد و قراردادِ خروجی — و
    هیچ داده‌ی زمانِ اجرا در آن نیست. از بخشِ INPUT به بعد قالبی است که با
    نتیجه‌ی قدم اول و دوم پر می‌شود.

    پرتاب می‌کند:
        ScenarioAnalysisError : بخشِ INPUT یا جای‌نگهدارها کامل نباشند
    """
    matches = list(_INPUT_SECTION_RE.finditer(text))
    if not matches:
        raise ScenarioAnalysisError(
            "Prompt file has no INPUT section — expected a line 'INPUT' between "
            "two '====' separators."
        )

    match = matches[-1]
    system_prompt = text[: match.start()].strip()
    user_template = text[match.end() :].strip()
    if not system_prompt or not user_template:
        raise ScenarioAnalysisError(
            "Prompt file has an empty system prompt or an empty INPUT section."
        )

    found = set(_PLACEHOLDER_RE.findall(user_template))
    missing = set(_PROMPT_PLACEHOLDERS) - found
    if missing:
        raise ScenarioAnalysisError(
            "Prompt INPUT section is missing placeholder(s): "
            + ", ".join(sorted(f"{{{{{name}}}}}" for name in missing))
        )
    return system_prompt, user_template


def render_prompt(
    template: str,
    step1_result: Any,
    step2_result: Any,
) -> str:
    """قالب را با نتیجه‌ی کاملِ قدم اول و دوم پر می‌کند."""
    values = {
        "step1_result": json.dumps(step1_result, ensure_ascii=False, indent=2),
        "step2_result": json.dumps(step2_result, ensure_ascii=False, indent=2),
    }
    return _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], template)


def build_prompt(
    prompt_path: Path | str,
    step1_result: Any,
    step2_result: Any,
) -> tuple[str, str]:
    """(system prompt، پیامِ کاربرِ ساخته‌شده) را از فایلِ پرامپت برمی‌گرداند."""
    text = Path(prompt_path).read_text(encoding="utf-8")
    system_prompt, template = split_prompt(text)
    return system_prompt, render_prompt(template, step1_result, step2_result)


# ── کمکی‌های اعتبارسنجی ──────────────────────────────────────────────────────

def _clean(value: object) -> str:
    """رشته را trim می‌کند؛ هر چیزِ دیگری رشته‌ی خالی می‌شود."""
    return value.strip() if isinstance(value, str) else ""


def _as_int(value: object) -> int | None:
    """عددِ صحیح برمی‌گرداند؛ bool عمداً رد می‌شود چون در پایتون int است."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _string_list(value: object, where: str, errors: list[str]) -> list[str]:
    """لیستی از رشته‌های غیرِتکراری و غیرِخالی می‌سازد."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        errors.append(f"{where}: must be an array of strings.")
        return []

    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"{where}[{index}]: must be a string.")
            continue
        item = item.strip()
        if item and item not in items:
            items.append(item)
    return items


def _first_field(path: str) -> str:
    """نامِ اولین فیلدِ یک JSON path — برای تطبیق با فیلدهای شناخته‌شده‌ی پاسخ."""
    body = path.lstrip("$").lstrip(".")
    for separator in (".", "["):
        body = body.split(separator, 1)[0]
    return body


# ── نمای عملیات‌های قدم دوم ──────────────────────────────────────────────────

def extract_api_mappings(step2_result: Any) -> list[dict]:
    """نگاشت‌های قدم دوم را از نتیجه‌ی آن بیرون می‌کشد.

    پرتاب می‌کند:
        ScenarioAnalysisError : نتیجه‌ی قدم دوم ساختار درستی نداشته باشد
    """
    if not isinstance(step2_result, dict):
        raise ScenarioAnalysisError("Step 2 result must be a JSON object.")

    mappings = step2_result.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise ScenarioAnalysisError(
            "Step 2 result does not contain any test case mapping."
        )
    return [mapping for mapping in mappings if isinstance(mapping, dict)]


def _operation_index(step2_result: Any) -> dict[tuple[str, str], dict]:
    """(متد، مسیرِ نرمال‌شده) → عملیاتِ کاملِ کشف‌شده در قدم دوم.

    دو شکلِ ورودی پذیرفته می‌شود:

      * شکلِ کاملِ نتیجه‌ی قدم دوم — ``services`` (کاتالوگِ کشف‌شده)؛
      * قراردادِ فشرده‌ی قدم ۲ → ۳ — تعریفِ عملیات همراهِ خودِ نگاشت می‌آید و
        فقط عملیات‌هایی را در بر می‌گیرد که نگاشتی به آن‌ها اشاره کرده است.

    ویزارد شکلِ فشرده را می‌فرستد تا کاتالوگِ کامل به پرامپتِ این قدم نرسد.
    """
    result = step2_result or {}
    index: dict[tuple[str, str], dict] = {}

    services = result.get("services")
    if isinstance(services, list):
        for service in services:
            if not isinstance(service, dict):
                continue
            for api in service.get("apis") or []:
                if not isinstance(api, dict):
                    continue
                index.setdefault(
                    operation_key(_clean(api.get("method")), _clean(api.get("path"))),
                    api,
                )
        return index

    for mapping in result.get("mappings") or []:
        if not isinstance(mapping, dict):
            continue
        operation = mapping.get("operation")
        if not isinstance(operation, dict):
            continue
        index.setdefault(
            operation_key(
                _clean(operation.get("method")), _clean(operation.get("path"))
            ),
            operation,
        )
    return index


def _mapped_operations(step2_result: Any) -> dict[str, dict]:
    """test_case_id → عملیاتی که قدم دوم برایش انتخاب کرده (فقط نگاشت‌های حل‌شده).

    شکلِ کاملِ نتیجه‌ی قدم دوم عملیات را زیرِ ``api`` می‌گذارد؛ قراردادِ فشرده
    آن را روی خودِ نگاشت (``method`` / ``path`` / ``operation_id``) پهن می‌کند.
    نگاشتِ حل‌نشده در هر دو شکل نادیده گرفته می‌شود.
    """
    mapped: dict[str, dict] = {}
    for mapping in (step2_result or {}).get("mappings") or []:
        if not isinstance(mapping, dict):
            continue
        case_id = _clean(mapping.get("test_case_id"))
        if not case_id:
            continue
        api = mapping.get("api")
        if isinstance(api, dict):
            mapped.setdefault(case_id, api)
            continue
        method = _clean(mapping.get("method"))
        path = _clean(mapping.get("path"))
        if method and path:
            mapped.setdefault(
                case_id,
                {
                    "method": method,
                    "path": path,
                    "operation_id": _clean(mapping.get("operation_id")),
                },
            )
    return mapped


def _operation_for(api: dict | None, index: dict[tuple[str, str], dict]) -> dict | None:
    """عملیاتِ کاملِ یک نگاشت را از میان عملیات‌های کشف‌شده پیدا می‌کند."""
    if not isinstance(api, dict):
        return None
    return index.get(operation_key(_clean(api.get("method")), _clean(api.get("path"))))


def _response_fields(operation: dict) -> set[str]:
    """فیلدهای پاسخی که سند واقعاً افشا کرده است."""
    fields: set[str] = set()
    for response in (operation.get("responses") or {}).values():
        if isinstance(response, dict):
            for name in response.get("fields") or []:
                fields.add(str(name))
    return fields


def _parameters_by_location(operation: dict) -> dict[str, set[str]]:
    """پارامترهای عملیات به تفکیکِ محلِ مصرف."""
    by_location: dict[str, set[str]] = {}
    for param in operation.get("parameters") or []:
        if not isinstance(param, dict):
            continue
        location = _clean(param.get("in"))
        name = _clean(param.get("name"))
        if location and name:
            by_location.setdefault(location, set()).add(name)
    return by_location


def _body_fields(operation: dict) -> set[str]:
    """فیلدهای سطحِ اولِ بدنه‌ی درخواست، اگر نمونه‌ای استخراج شده باشد."""
    body = operation.get("request_body")
    return {str(name) for name in body} if isinstance(body, dict) else set()


# ── تشخیصِ حلقه ──────────────────────────────────────────────────────────────

def find_cycle(edges: dict[str, set[str]]) -> list[str] | None:
    """یک حلقه در گرافِ وابستگی پیدا می‌کند و مسیرش را برمی‌گرداند.

    رنگ‌آمیزیِ DFS: یالِ رو به گرهِ خاکستری یعنی حلقه. خروجی مثال:
    ``['TC-001', 'TC-002', 'TC-001']``
    """
    white, grey, black = 0, 1, 2
    color = {node: white for node in edges}
    stack: list[str] = []
    depth: dict[str, int] = {}

    def visit(node: str) -> list[str] | None:
        color[node] = grey
        depth[node] = len(stack)
        stack.append(node)

        for neighbour in sorted(edges.get(node, ())):
            if color.get(neighbour) == grey:
                return stack[depth[neighbour] :] + [neighbour]
            if color.get(neighbour) == white:
                found = visit(neighbour)
                if found:
                    return found

        stack.pop()
        depth.pop(node, None)
        color[node] = black
        return None

    for node in sorted(edges):
        if color[node] == white:
            found = visit(node)
            if found:
                return found
    return None


# ── اعتبارسنجی ───────────────────────────────────────────────────────────────

def _validate_scenarios(
    raw: object, known_ids: list[str], errors: list[str]
) -> list[Scenario]:
    scenarios: list[Scenario] = []
    if not isinstance(raw, list) or not raw:
        errors.append("'scenarios' must be a non-empty array.")
        return scenarios

    seen_ids: set[str] = set()
    for position, item in enumerate(raw):
        where = f"scenarios[{position}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: expected an object.")
            continue

        scenario_id = _clean(item.get("id"))
        if not scenario_id:
            errors.append(f"{where}: 'id' is missing or empty.")
        elif scenario_id in seen_ids:
            errors.append(f"{where}: duplicate scenario id {scenario_id!r}.")
        else:
            seen_ids.add(scenario_id)

        title = _clean(item.get("title"))
        if not title:
            errors.append(f"{where}: 'title' is missing or empty.")

        reason = _clean(item.get("reason"))
        if not reason:
            errors.append(f"{where}: 'reason' is missing or empty.")

        case_ids = _string_list(
            item.get("test_case_ids"), f"{where}.test_case_ids", errors
        )
        if not case_ids:
            errors.append(f"{where}: 'test_case_ids' must be a non-empty array.")
        for case_id in case_ids:
            if case_id not in known_ids:
                errors.append(
                    f"{where}.test_case_ids: unknown test case id {case_id!r}."
                )

        scenarios.append(
            Scenario(
                id=scenario_id,
                title=title,
                test_case_ids=[c for c in case_ids if c in known_ids],
                reason=reason,
            )
        )
    return scenarios


def _validate_execution_order(
    raw: object, known_ids: list[str], errors: list[str]
) -> tuple[list[ExecutionStep], dict[str, int]]:
    """ترتیبِ اجرا را اعتبارسنجی می‌کند و نگاشتِ «تست‌کیس → ترتیبِ معتبر» را برمی‌گرداند."""
    steps: list[ExecutionStep] = []
    if not isinstance(raw, list) or not raw:
        errors.append("'execution_order' must be a non-empty array.")
        return steps, {}

    seen_cases: set[str] = set()
    valid_order: dict[str, int] = {}
    order_owner: dict[int, str] = {}

    for position, item in enumerate(raw):
        where = f"execution_order[{position}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: expected an object.")
            continue

        case_id = _clean(item.get("test_case_id"))
        if not case_id:
            errors.append(f"{where}: 'test_case_id' is missing or empty.")
        elif case_id not in known_ids:
            errors.append(f"{where}: unknown test_case_id {case_id!r}.")
        elif case_id in seen_cases:
            errors.append(f"{where}: duplicate entry for {case_id!r}.")
        else:
            seen_cases.add(case_id)

        order = _as_int(item.get("order"))
        if order is None:
            errors.append(
                f"{where}: 'order' must be a positive integer, "
                f"got {item.get('order')!r}."
            )
        elif order < 1:
            errors.append(f"{where}: 'order' must be >= 1, got {order}.")
        elif order in order_owner:
            errors.append(
                f"{where}: duplicate order {order} — already used by "
                f"{order_owner[order]!r}; orders must be unique."
            )
        else:
            order_owner[order] = case_id
            if case_id:
                valid_order[case_id] = order

        steps.append(
            ExecutionStep(
                test_case_id=case_id,
                order=order if order is not None else 0,
                depends_on=_string_list(
                    item.get("depends_on"), f"{where}.depends_on", errors
                ),
            )
        )

    ordered_ids = {step["test_case_id"] for step in steps if step["test_case_id"]}

    for step in steps:
        case_id = step["test_case_id"]
        for dependency in step["depends_on"]:
            if dependency not in known_ids:
                errors.append(
                    f"execution_order: unknown dependency {dependency!r} "
                    f"declared by {case_id!r}."
                )
            elif dependency == case_id:
                errors.append(
                    f"execution_order: {case_id!r} depends on itself."
                )
            elif dependency not in ordered_ids:
                errors.append(
                    f"execution_order: {case_id!r} depends on {dependency!r}, "
                    f"which has no entry in 'execution_order'."
                )
            elif dependency in valid_order and case_id in valid_order:
                if valid_order[dependency] >= valid_order[case_id]:
                    errors.append(
                        f"execution_order: {dependency!r} "
                        f"(order {valid_order[dependency]}) must be executed before "
                        f"{case_id!r} (order {valid_order[case_id]})."
                    )
    return steps, valid_order


def _validate_source(
    raw: object,
    where: str,
    known_ids: list[str],
    mapped: dict[str, dict],
    index: dict[tuple[str, str], dict],
    errors: list[str],
) -> DependencySource | None:
    if not isinstance(raw, dict):
        errors.append(f"{where}: expected an object.")
        return None

    case_id = _clean(raw.get("test_case_id"))
    if not case_id:
        errors.append(f"{where}: 'test_case_id' is missing or empty.")
    elif case_id not in known_ids:
        errors.append(f"{where}: unknown test case id {case_id!r}.")
    elif case_id not in mapped:
        errors.append(
            f"{where}: test case {case_id!r} has no resolved API mapping in "
            f"Step 2, so it cannot be the source of a data dependency."
        )

    location = _clean(raw.get("location"))
    if location not in _SOURCE_LOCATIONS:
        errors.append(
            f"{where}: 'location' must be one of {_SOURCE_LOCATIONS}, "
            f"got {raw.get('location')!r}."
        )

    path = _clean(raw.get("path"))
    if not path:
        errors.append(f"{where}: 'path' is missing or empty.")
    elif not _JSON_PATH_RE.match(path):
        errors.append(
            f"{where}: 'path' {path!r} is not a JSON path such as '$.id'."
        )
    else:
        operation = _operation_for(mapped.get(case_id), index)
        if operation is not None:
            known_fields = _response_fields(operation)
            field = _first_field(path)
            if known_fields and field and field not in known_fields:
                errors.append(
                    f"{where}: {field!r} is not a response field of the operation "
                    f"mapped to {case_id!r} ({sorted(known_fields)})."
                )

    return DependencySource(test_case_id=case_id, location=location, path=path)


def _validate_targets(
    raw: object,
    where: str,
    known_ids: list[str],
    mapped: dict[str, dict],
    index: dict[tuple[str, str], dict],
    errors: list[str],
) -> list[DependencyTarget]:
    targets: list[DependencyTarget] = []
    if not isinstance(raw, list) or not raw:
        errors.append(f"{where}: must be a non-empty array.")
        return targets

    seen: set[str] = set()
    for position, item in enumerate(raw):
        item_where = f"{where}[{position}]"
        if not isinstance(item, dict):
            errors.append(f"{item_where}: expected an object.")
            continue

        case_id = _clean(item.get("test_case_id"))
        if not case_id:
            errors.append(f"{item_where}: 'test_case_id' is missing or empty.")
        elif case_id not in known_ids:
            errors.append(f"{item_where}: unknown test case id {case_id!r}.")
        elif case_id not in mapped:
            errors.append(
                f"{item_where}: test case {case_id!r} has no resolved API mapping "
                f"in Step 2."
            )
        elif case_id in seen:
            errors.append(f"{item_where}: duplicate target {case_id!r}.")
        else:
            seen.add(case_id)

        location = _clean(item.get("location"))
        if location not in _TARGET_LOCATIONS:
            errors.append(
                f"{item_where}: 'location' must be one of {_TARGET_LOCATIONS}, "
                f"got {item.get('location')!r}."
            )

        parameter = _clean(item.get("parameter"))
        if not parameter:
            errors.append(f"{item_where}: 'parameter' is missing or empty.")
        elif location in _TARGET_LOCATIONS and case_id in mapped:
            operation = _operation_for(mapped.get(case_id), index)
            if operation is not None:
                if location == "body":
                    known = _body_fields(operation)
                else:
                    known = _parameters_by_location(operation).get(location, set())
                if known and parameter not in known:
                    errors.append(
                        f"{item_where}: {parameter!r} is not a {location} parameter "
                        f"of the operation mapped to {case_id!r}."
                    )

        targets.append(
            DependencyTarget(
                test_case_id=case_id, location=location, parameter=parameter
            )
        )
    return targets


def _validate_data_dependencies(
    raw: object,
    known_ids: list[str],
    mapped: dict[str, dict],
    index: dict[tuple[str, str], dict],
    errors: list[str],
) -> list[DataDependency]:
    dependencies: list[DataDependency] = []
    if not isinstance(raw, list):
        errors.append("'data_dependencies' must be an array.")
        return dependencies

    seen_variables: set[str] = set()
    for position, item in enumerate(raw):
        where = f"data_dependencies[{position}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: expected an object.")
            continue

        variable = _clean(item.get("variable_name"))
        if not variable:
            errors.append(f"{where}: 'variable_name' is missing or empty.")
        elif not _VARIABLE_NAME_RE.match(variable):
            errors.append(
                f"{where}: 'variable_name' {variable!r} cannot be used as a "
                f"variable name."
            )
        elif variable in seen_variables:
            errors.append(f"{where}: duplicate variable_name {variable!r}.")
        else:
            seen_variables.add(variable)

        confidence = _clean(item.get("confidence")).lower()
        if confidence not in _VALID_CONFIDENCE:
            errors.append(
                f"{where}: 'confidence' must be one of {_VALID_CONFIDENCE}, "
                f"got {item.get('confidence')!r}."
            )

        reason = _clean(item.get("reason"))
        if not reason:
            errors.append(f"{where}: 'reason' is missing or empty.")

        source = _validate_source(
            item.get("source"), f"{where}.source", known_ids, mapped, index, errors
        )
        targets = _validate_targets(
            item.get("targets"), f"{where}.targets", known_ids, mapped, index, errors
        )

        if source and source["test_case_id"]:
            for target in targets:
                if target["test_case_id"] == source["test_case_id"]:
                    errors.append(
                        f"{where}: a target must not be the source test case "
                        f"({source['test_case_id']!r})."
                    )

        dependencies.append(
            DataDependency(
                variable_name=variable,
                source=source
                or DependencySource(test_case_id="", location="", path=""),
                targets=targets,
                confidence=confidence
                if confidence in _VALID_CONFIDENCE
                else "low",
                reason=reason,
                scope=DEFAULT_DEPENDENCY_SCOPE,
                required=True,
                origin=ORIGIN_INFERRED,
            )
        )
    return dependencies


# ── متغیرهای درخواستیِ کاربر (قدم اول) ───────────────────────────────────────

def _matching_dependency(variable: RequestedVariable, raw: list) -> int | None:
    """وابستگیِ پیشنهادیِ LLM که همان مقدارِ درخواستی را توصیف می‌کند.

    به ترتیب: هم‌نامی؛ همان مبدأ با همان مسیر؛ همان مبدأ با مقصدهایی که با
    مصرف‌کننده‌های کاربر هم‌پوشانی دارند (همان مقدار با نام/مسیرِ حدسیِ دیگر)؛
    و وقتی کاربر مسیری نگفته، تنها وابستگیِ همان مبدأ. پیشنهادِ مطابق فقط
    «کمک» است: نام، اسکوپ، مبدأ، مسیر و مصرف‌کننده‌هایی که کاربر گفته همیشه
    برنده‌اند — و چون کنار گذاشته می‌شود، متغیرِ دومی برای همان مقدار نمی‌سازد.
    """
    for position, item in enumerate(raw):
        if isinstance(item, dict) and _clean(item.get("variable_name")) == variable["name"]:
            return position

    consumers = {consumer["test_case_id"] for consumer in variable["consumers"]}
    same_source: list[int] = []
    overlapping: list[int] = []
    for position, item in enumerate(raw):
        source = item.get("source") if isinstance(item, dict) else None
        if not isinstance(source, dict):
            continue
        if _clean(source.get("test_case_id")) != variable["source_test_case_id"]:
            continue
        same_source.append(position)
        targets = {
            _clean(target.get("test_case_id"))
            for target in item.get("targets") or []
            if isinstance(target, dict)
        }
        if consumers & targets:
            overlapping.append(position)
        if not variable["response_path"]:
            continue
        try:
            path = normalize_response_path(source.get("path"))
        except VariableInstructionError:
            continue
        if path == variable["response_path"]:
            return position
    if overlapping:
        return overlapping[0]
    if not variable["response_path"] and len(same_source) == 1:
        return same_source[0]
    return None


def _describe(operation: dict | None) -> str:
    if not operation:
        return "its mapped operation"
    return f"{_clean(operation.get('method')).upper()} {_clean(operation.get('path'))}"


def _requested_target(
    label: str,
    source_id: str,
    consumer: dict,
    hint: dict,
    mapped: dict[str, dict],
    index: dict[tuple[str, str], dict],
    clarifications: list[Clarification],
) -> DependencyTarget | None:
    """محلِ مصرفِ یک متغیرِ درخواستی را با قراردادِ عملیاتِ مقصد تطبیق می‌دهد.

    محل/پارامترِ کاربر برنده است؛ مقدارِ خالی از پیشنهادِ LLM و در نهایت فقط
    وقتی از قرارداد پر می‌شود که بی‌ابهام باشد (مثلاً تنها پارامترِ مسیر). هر
    چیزی که قابلِ تعیین یا تأیید نباشد به ابهام تبدیل می‌شود، نه حدس.
    """
    case_id = consumer["test_case_id"]
    if not case_id or case_id == source_id:
        return None
    if case_id not in mapped:
        clarifications.append(
            Clarification(
                type="data_dependency",
                test_case_id=case_id,
                message=(
                    f"{label}: consumer {case_id!r} has no resolved API mapping in "
                    f"Step 2, so the value cannot be wired into its request."
                ),
            )
        )
        return None

    operation = _operation_for(mapped.get(case_id), index)
    hint_location = _clean(hint.get("location"))
    hint_parameter = _clean(hint.get("parameter"))

    location = consumer["location"] or (
        hint_location if hint_location in _TARGET_LOCATIONS else ""
    )
    parameter = consumer["parameter"]
    if not parameter and hint_location == location:
        parameter = hint_parameter

    by_location = _parameters_by_location(operation) if operation else {}
    body_fields = _body_fields(operation) if operation else set()
    if operation is not None:
        if not location and not parameter:
            # فقط وقتی عملیات دقیقاً یک ورودی (مسیر/query/بدنه) دارد، محل بی‌ابهام است
            # — مثلاً GET /voucher/{id}. با بیش از یک ورودی حدس زده نمی‌شود.
            inputs = [
                (place, name)
                for place in ("path", "query")
                for name in sorted(by_location.get(place, set()))
            ] + [("body", name) for name in sorted(body_fields)]
            if len(inputs) == 1:
                location, parameter = inputs[0]
        if not location and parameter:
            candidates = [
                place
                for place in ("path", "query", "header")
                if parameter in by_location.get(place, set())
            ]
            if parameter.split(".", 1)[0] in body_fields:
                candidates.append("body")
            if len(candidates) == 1:
                location = candidates[0]
        if location == "path" and not parameter:
            names = sorted(by_location.get("path", set()))
            if len(names) == 1:
                parameter = names[0]

    if not location or not parameter:
        clarifications.append(
            Clarification(
                type="data_dependency",
                test_case_id=case_id,
                message=(
                    f"{label}: cannot determine where {case_id!r} "
                    f"({_describe(operation)}) uses the value — name the parameter "
                    f"(for example the path parameter) in the task or in the "
                    f"Step 1 'variables' and rerun Step 3."
                ),
            )
        )
        return None

    if operation is not None:
        if location == "body":
            known, checked = body_fields, parameter.split(".", 1)[0]
        elif location == "header":
            known = {name.lower() for name in by_location.get("header", set())}
            checked = parameter.lower()
        else:
            known, checked = by_location.get(location, set()), parameter
        if known and checked not in known:
            clarifications.append(
                Clarification(
                    type="data_dependency",
                    test_case_id=case_id,
                    message=(
                        f"{label}: {parameter!r} is not a {location} parameter of "
                        f"{_describe(operation)} (mapped to {case_id!r}); the value "
                        f"was not wired into that request."
                    ),
                )
            )
            return None
        if not known and location in ("query", "header"):
            # سند هیچ پارامتری در این محل ندارد؛ چیزی برای مقایسه نیست. انتخابِ
            # صریحِ کاربر نگه داشته می‌شود ولی بی‌صدا نمی‌ماند.
            clarifications.append(
                Clarification(
                    type="data_dependency",
                    test_case_id=case_id,
                    message=(
                        f"{label}: {_describe(operation)} documents no {location} "
                        f"parameters; {parameter!r} is used as specified but cannot "
                        f"be verified against the API contract."
                    ),
                )
            )

    return DependencyTarget(test_case_id=case_id, location=location, parameter=parameter)


def _requested_dependency(
    variable: RequestedVariable,
    hint: dict,
    mapped: dict[str, dict],
    index: dict[tuple[str, str], dict],
    clarifications: list[Clarification],
) -> DataDependency | None:
    """یک متغیرِ درخواستیِ قدم اول را به یک data_dependency تبدیل می‌کند."""
    name = variable["name"]
    source_id = variable["source_test_case_id"]
    label = f"Requested variable '{name}'"

    if source_id not in mapped:
        clarifications.append(
            Clarification(
                type="data_dependency",
                test_case_id=source_id,
                message=(
                    f"{label}: the source test case {source_id!r} has no resolved API "
                    f"mapping in Step 2, so nothing can extract the value."
                ),
            )
        )
        return None

    hint_source = hint.get("source") if isinstance(hint.get("source"), dict) else {}
    if _clean(hint_source.get("test_case_id")) != source_id:
        hint_source = {}

    # مسیرِ کاربر حفظ می‌شود؛ فقط وقتی کاربر مسیری نگفته، پیشنهادِ LLM.
    location, path = "response.body", variable["response_path"]
    from_task = bool(path)
    if not path and hint_source:
        try:
            hint_path = normalize_response_path(hint_source.get("path"))
        except VariableInstructionError:
            hint_path = ""
        hint_location = _clean(hint_source.get("location"))
        if hint_path and hint_location in _SOURCE_LOCATIONS:
            location, path = hint_location, hint_path

    if not path:
        clarifications.append(
            Clarification(
                type="data_dependency",
                test_case_id=source_id,
                message=(
                    f"{label}: neither the task nor the scenario analysis identifies "
                    f"where the value is in the response of {source_id!r}; give its "
                    f"response path (for example data.id) and rerun Step 3."
                ),
            )
        )
        return None

    operation = _operation_for(mapped.get(source_id), index)
    if operation is not None and location == "response.body":
        fields = _response_fields(operation)
        given = "specified in the task" if from_task else "proposed by the scenario analysis"
        if not fields:
            clarifications.append(
                Clarification(
                    type="response_path",
                    test_case_id=source_id,
                    message=(
                        f"{label}: the response schema of {_describe(operation)} "
                        f"documents no fields, so '{path}' ({given}) cannot be "
                        f"verified against the API contract; it is used exactly as "
                        f"given and its existence is checked when the request runs."
                    ),
                )
            )
        elif first_field(path) not in fields:
            clarifications.append(
                Clarification(
                    type="response_path",
                    test_case_id=source_id,
                    message=(
                        f"{label}: '{path}' ({given}) does not start with a documented "
                        f"response field of {_describe(operation)} "
                        f"({', '.join(sorted(fields))}); it was kept as given — "
                        f"confirm the path."
                    ),
                )
            )

    hint_targets = {
        _clean(target.get("test_case_id")): target
        for target in hint.get("targets") or []
        if isinstance(target, dict) and _clean(target.get("test_case_id"))
    }
    consumers = variable["consumers"] or [
        {"test_case_id": case_id, "location": "", "parameter": ""}
        for case_id in hint_targets
    ]
    targets: list[DependencyTarget] = []
    for consumer in consumers:
        target = _requested_target(
            label,
            source_id,
            consumer,
            hint_targets.get(consumer["test_case_id"], {}),
            mapped,
            index,
            clarifications,
        )
        if target is not None:
            targets.append(target)

    instruction = variable["instruction"]
    reason = (
        f"Requested in the task: {instruction}"
        if instruction
        else _clean(hint.get("reason")) or "Requested explicitly in the Step 1 result."
    )
    return DataDependency(
        variable_name=name,
        source=DependencySource(test_case_id=source_id, location=location, path=path),
        targets=targets,
        confidence="high",
        reason=reason,
        scope=variable["scope"],
        required=variable["required"],
        origin=ORIGIN_TASK,
    )


def _resolve_requested_variables(
    requested: list[RequestedVariable],
    raw_dependencies: list,
    mapped: dict[str, dict],
    index: dict[tuple[str, str], dict],
) -> tuple[list[DataDependency], list, list[Clarification]]:
    """متغیرهای درخواستی را قطعی می‌سازد و پیشنهادهای مطابقِ LLM را کنار می‌گذارد.

    برمی‌گرداند: (وابستگی‌های درخواستی، پیشنهادهای باقی‌مانده‌ی LLM، ابهام‌ها)
    """
    remaining = list(raw_dependencies)
    resolved: list[DataDependency] = []
    clarifications: list[Clarification] = []
    for variable in requested:
        position = _matching_dependency(variable, remaining)
        hint = remaining.pop(position) if position is not None else {}
        dependency = _requested_dependency(
            variable, hint if isinstance(hint, dict) else {}, mapped, index,
            clarifications,
        )
        if dependency is not None:
            resolved.append(dependency)
    return resolved, remaining, clarifications


def _without_claimed_targets(
    requested: list[DataDependency],
    inferred: list[DataDependency],
    clarifications: list[Clarification],
) -> list[DataDependency]:
    """وابستگیِ استنتاجی نمی‌تواند محلی را که کاربر صریحاً تعیین کرده بگیرد.

    بدونِ این، دو متغیر برای یک پارامتر رقابت می‌کردند و فقط یکی واقعاً در
    درخواست می‌نشست. هم‌نامی با متغیرِ درخواستی هم پذیرفته نمی‌شود.
    """
    names = {dependency["variable_name"] for dependency in requested}
    claimed = {
        (target["test_case_id"], target["location"], target["parameter"])
        for dependency in requested
        for target in dependency["targets"]
    }

    kept: list[DataDependency] = []
    for dependency in inferred:
        name = dependency["variable_name"]
        source_id = dependency["source"]["test_case_id"]
        if name in names:
            clarifications.append(
                Clarification(
                    type="data_dependency",
                    test_case_id=source_id,
                    message=(
                        f"The scenario analysis proposed a second dependency named "
                        f"'{name}'; the variable requested in the task was kept."
                    ),
                )
            )
            continue
        targets = [
            target
            for target in dependency["targets"]
            if (target["test_case_id"], target["location"], target["parameter"])
            not in claimed
        ]
        if len(targets) != len(dependency["targets"]):
            clarifications.append(
                Clarification(
                    type="data_dependency",
                    test_case_id=source_id,
                    message=(
                        f"The inferred variable '{name}' targeted a parameter that a "
                        f"variable requested in the task already fills; the requested "
                        f"variable was kept there."
                    ),
                )
            )
        if targets:
            kept.append({**dependency, "targets": targets})
    return kept


def _validate_clarifications(
    raw: object, known_ids: list[str], errors: list[str]
) -> list[Clarification]:
    clarifications: list[Clarification] = []
    if not isinstance(raw, list):
        errors.append("'clarifications' must be an array.")
        return clarifications

    for position, item in enumerate(raw):
        where = f"clarifications[{position}]"
        if not isinstance(item, dict):
            errors.append(
                f"{where}: expected an object with 'type' and 'message'."
            )
            continue

        kind = _clean(item.get("type"))
        if not kind:
            errors.append(f"{where}: 'type' is missing or empty.")

        message = _clean(item.get("message"))
        if not message:
            errors.append(f"{where}: 'message' is missing or empty.")

        case_id = _clean(item.get("test_case_id"))
        if case_id and case_id not in known_ids:
            errors.append(f"{where}: unknown test_case_id {case_id!r}.")

        clarifications.append(
            Clarification(type=kind, test_case_id=case_id, message=message)
        )
    return clarifications


def validate_scenario_payload(
    data: object,
    *,
    test_cases: list[dict],
    step2_result: Any,
    requested: list[RequestedVariable] | None = None,
) -> tuple[
    list[Scenario], list[ExecutionStep], list[DataDependency], list[Clarification]
]:
    """خروجیِ JSON مدل را اعتبارسنجی و نرمال می‌کند.

    همه‌ی خطاها یک‌جا جمع و در یک پیام گزارش می‌شوند. علاوه بر ساختار، ارجاع‌ها
    به تست‌کیس‌ها، ترتیبِ سازگار با وابستگی‌ها، نبودِ حلقه در گرافِ وابستگی و
    وجودِ پارامترها/فیلدهای پاسخ در عملیات‌های قدم دوم بررسی می‌شوند.

    ``requested`` متغیرهایی است که کاربر در قدم اول صریحاً خواسته. آن‌ها به‌صورتِ
    قطعی به وابستگی تبدیل می‌شوند (با origin «task») و پیشنهادِ LLM فقط
    جاهای خالی را پر می‌کند؛ آنچه قابلِ تأیید نیست ابهام می‌شود، نه خطا.

    پرتاب می‌کند:
        ScenarioAnalysisError : خروجی با قرارداد نخواند
    """
    if not isinstance(data, dict):
        raise ScenarioAnalysisError("LLM output for Step 3 is not a JSON object.")

    errors: list[str] = []
    known_ids = [str(case.get("id", "")) for case in test_cases]
    position = {case_id: index for index, case_id in enumerate(known_ids)}
    index = _operation_index(step2_result)
    mapped = _mapped_operations(step2_result)

    scenarios = _validate_scenarios(data.get("scenarios"), known_ids, errors)
    steps, valid_order = _validate_execution_order(
        data.get("execution_order"), known_ids, errors
    )
    raw_dependencies = data.get("data_dependencies")
    requested_dependencies: list[DataDependency] = []
    requested_notes: list[Clarification] = []
    if requested and isinstance(raw_dependencies, list):
        requested_dependencies, raw_dependencies, requested_notes = (
            _resolve_requested_variables(requested, raw_dependencies, mapped, index)
        )
    inferred = _validate_data_dependencies(
        raw_dependencies, known_ids, mapped, index, errors
    )
    dependencies = requested_dependencies + _without_claimed_targets(
        requested_dependencies, inferred, requested_notes
    )
    clarifications = _validate_clarifications(
        data.get("clarifications"), known_ids, errors
    )
    clarifications.extend(requested_notes)

    # گرافِ وابستگی: یال از تست‌کیسِ وابسته به تست‌کیسی که باید پیش از آن اجرا شود.
    edges: dict[str, set[str]] = {}
    for step in steps:
        case_id = step["test_case_id"]
        if not case_id:
            continue
        node = edges.setdefault(case_id, set())
        for dependency in step["depends_on"]:
            if dependency:
                node.add(dependency)

    # جریانِ داده هم یک وابستگیِ ترتیبی است: مبدأ باید پیش از مقصد اجرا شود.
    for dependency in dependencies:
        source = dependency["source"]["test_case_id"]
        if not source:
            continue
        for target in dependency["targets"]:
            target_id = target["test_case_id"]
            if target_id and target_id != source:
                edges.setdefault(target_id, set()).add(source)

    cycle = find_cycle(edges)
    if cycle:
        errors.append("circular dependency detected: " + " → ".join(cycle) + ".")

    if errors:
        raise ScenarioAnalysisError(
            "LLM output does not match the required Step 3 scenario format:\n  - "
            + "\n  - ".join(errors)
        )

    end = len(known_ids)
    scenarios.sort(
        key=lambda scenario: (
            position.get(scenario["test_case_ids"][0], end)
            if scenario["test_case_ids"]
            else end,
            scenario["id"],
        )
    )
    steps.sort(
        key=lambda step: (step["order"], position.get(step["test_case_id"], end))
    )
    dependencies.sort(
        key=lambda dependency: (
            position.get(dependency["source"]["test_case_id"], end),
            dependency["variable_name"],
        )
    )
    return scenarios, steps, dependencies, clarifications


def build_result(
    scenarios: list[Scenario],
    execution_order: list[ExecutionStep],
    data_dependencies: list[DataDependency],
    clarifications: list[Clarification],
) -> Step3Result:
    """نتیجه‌ی نهاییِ ساختاریافته‌ی قدم سوم را می‌سازد."""
    return Step3Result(
        scenarios=scenarios,
        execution_order=execution_order,
        data_dependencies=data_dependencies,
        clarifications=clarifications,
    )


# ── عاملِ تحلیل ──────────────────────────────────────────────────────────────

class ScenarioAnalysisAgent:
    """سناریوها، ترتیبِ اجرا و وابستگی‌های داده را از نتیجه‌ی قدم اول و دوم می‌سازد.

    تنها وظیفه‌ی LLM اینجا تفسیرِ معنا، گروه‌بندی، استنتاجِ وابستگی و تولیدِ
    دلیل/ابهام است. همه‌ی ارجاع‌ها پس از آن به‌صورتِ قطعی اعتبارسنجی می‌شوند.
    """

    name = "scenario_analysis"

    def __init__(
        self,
        debug_config: DebugConfig | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        prompt_path: Path | str | None = None,
    ) -> None:
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
        self._prompt_path = Path(prompt_path) if prompt_path else _PROMPT_PATH
        self._temperature = temperature
        # None یعنی «مقدارِ مشترکِ پروژه»؛ عددِ صریح همیشه برنده است.
        # مقدارِ مشترک همان ۸۱۹۲ ای است که این قدم از قبل داشت.
        self._max_tokens = LLM_MAX_OUTPUT_TOKENS if max_tokens is None else max_tokens

    def analyze(
        self,
        step1_result: Any,
        step2_result: Any,
        user_id: str,
    ) -> tuple[
        list[Scenario], list[ExecutionStep], list[DataDependency], list[Clarification]
    ]:
        """(سناریوها، ترتیبِ اجرا، وابستگی‌های داده، ابهام‌ها) را برمی‌گرداند."""
        try:
            test_cases = extract_test_cases(step1_result)
        except ApiMappingError as exc:
            raise ScenarioAnalysisError(str(exc)) from exc
        try:
            requested = requested_variables(step1_result)
        except VariableInstructionError as exc:
            raise ScenarioAnalysisError(str(exc)) from exc

        system_prompt, user_message = build_prompt(
            self._prompt_path, step1_result, step2_result
        )
        self._log.info(
            "شروع تحلیلِ سناریو و وابستگی",
            user_id=user_id,
            test_cases=len(test_cases),
        )
        self._log.trace(
            "prompt تحلیلِ سناریو ساخته شد",
            system_chars=len(system_prompt),
            user_chars=len(user_message),
        )

        llm = LLMClient(user_id=user_id, agent_name=self.name)
        raw = llm.chat(
            user_message=user_message,
            system_prompt=system_prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        self._log.trace("پاسخ LLM دریافت شد", response_chars=len(raw or ""))

        try:
            data = extract_json_object(raw)
        except JsonExtractionError as exc:
            raise ScenarioAnalysisError(str(exc)) from exc

        return validate_scenario_payload(
            data,
            test_cases=test_cases,
            step2_result=step2_result,
            requested=requested,
        )


class Step3ScenarioAnalysisGenerator:
    """قدم سوم: سناریو، ترتیبِ اجرا و وابستگی‌های داده از نتیجه‌ی قدم اول و دوم.

    پارامترها:
        debug_config : تنظیماتِ لاگِ پروژه
        temperature  : دمای LLM — صفر، چون این تحلیل باید تکرارپذیر باشد
        max_tokens   : سقفِ توکنِ پاسخ (None یعنی مقدارِ مشترکِ LLM_MAX_OUTPUT_TOKENS)
        prompt_path  : مسیرِ فایلِ پرامپت (برای تست قابلِ جایگزینی است)
    """

    name = "step3_scenario_analysis"

    def __init__(
        self,
        debug_config: DebugConfig | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        prompt_path: Path | str | None = None,
    ) -> None:
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
        self._agent = ScenarioAnalysisAgent(
            debug_config=debug_config,
            temperature=temperature,
            max_tokens=max_tokens,
            prompt_path=prompt_path,
        )

    def generate(
        self,
        step1_result: Any,
        step2_result: Any,
        user_id: str,
    ) -> Step3Result:
        """نتیجه‌ی کاملِ قدم سوم را می‌سازد.

        این متد هیچ API ای اجرا نمی‌کند و قدم چهارم را نیز اجرا نمی‌کند؛ تأییدِ
        انسانی در لایه‌ی UI و در session نگه داشته می‌شود.

        پرتاب می‌کند:
            ScenarioAnalysisError : ورودیِ نامعتبر یا خروجیِ ناسازگار با قرارداد
        """
        self._validate_inputs(step1_result, step2_result)
        self._log.info("شروع تحلیلِ سناریو", user_id=user_id)

        scenarios, execution_order, data_dependencies, clarifications = (
            self._agent.analyze(step1_result, step2_result, user_id)
        )

        counts = Counter(
            dependency["confidence"] for dependency in data_dependencies
        )
        self._log.info(
            "تحلیلِ سناریو کامل شد",
            scenarios=len(scenarios),
            ordered=len(execution_order),
            dependencies=len(data_dependencies),
            high=counts.get("high", 0),
            medium=counts.get("medium", 0),
        )
        if clarifications:
            self._log.warning(
                "ابهام‌هایی برای بازبینیِ انسانی گزارش شد",
                clarifications=len(clarifications),
            )

        return build_result(
            scenarios, execution_order, data_dependencies, clarifications
        )

    @staticmethod
    def _validate_inputs(step1_result: Any, step2_result: Any) -> None:
        """شکلِ ورودیِ قدم اول و دوم را پیش از هر کاری بررسی می‌کند."""
        try:
            extract_test_cases(step1_result)
        except ApiMappingError as exc:
            raise ScenarioAnalysisError(str(exc)) from exc
        extract_api_mappings(step2_result)


__all__ = [
    "Clarification",
    "DataDependency",
    "DependencySource",
    "DependencyTarget",
    "ExecutionStep",
    "Scenario",
    "ScenarioAnalysisAgent",
    "ScenarioAnalysisError",
    "Step3Result",
    "Step3ScenarioAnalysisGenerator",
    "build_prompt",
    "build_result",
    "extract_api_mappings",
    "extract_test_cases",
    "find_cycle",
    "render_prompt",
    "split_prompt",
    "validate_scenario_payload",
]
