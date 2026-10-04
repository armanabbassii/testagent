"""
test_case_generator/testcase_generator.py — تولید تست‌کیس ساختاریافته از ApiSpec

مسئولیت این ماژول فقط یک قدم از زنجیره است:

    ApiSpec / list[EndpointSpec] → LLM → تست‌کیس‌های ساختاریافته (dict معتبر)

این ماژول عمداً کارهای زیر را انجام نمی‌دهد:
  - دریافت یا تحلیل سند Swagger   (وظیفه‌ی swagger_analyzer.py)
  - ساخت فایل Postman Collection  (وظیفه‌ی postman_builder.py)
  - ذخیره‌ی فایل یا اجرای درخواست API

خروجی، دیکشنری معتبرشده‌ای است که مستقیماً به postman_builder داده می‌شود.
اگر LLM خروجی نامعتبر بدهد، TestCaseGenerationError پرتاب می‌شود — خروجی خراب
هرگز بی‌سروصدا پذیرفته نمی‌شود.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from typing_extensions import NotRequired, TypedDict

from src.agents.test_case_generator.json_output import (
    JsonExtractionError,
    extract_json_object,
)
from src.agents.test_case_generator.scenario_parser import (
    NegativeCase,
    Scenario,
    ScenarioStep,
    StepParams,
)
from src.agents.test_case_generator.swagger_analyzer import ApiSpec, EndpointSpec
from src.debug import DebugConfig
from src.llm_client import LLMClient

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# هدر احراز هویتی که طبق rules.md باید در هر درخواست مجاز حاضر باشد
_AUTH_HEADER = "Authorization"
_AUTH_VALUE = "Bearer {{token}}"

_VALID_TYPES = ("positive", "negative")

# نامِ پیش‌فرضِ متغیرِ base URL در کالکشن — postman_builder همین را استفاده می‌کند
DEFAULT_BASE_URL_VAR = "baseUrl"

# قرارداد خروجی — چون system.md و rules.md فرمت JSON را توصیف نمی‌کنند،
# این بخش به system prompt اضافه می‌شود.
_OUTPUT_CONTRACT = """\
## Output Format (strict)

Return ONLY valid JSON. No markdown fences, no explanation before or after the JSON.

```
{
  "controllers": [
    {
      "name": "<controller name exactly as given>",
      "test_cases": [
        {
          "name": "<method> <path> - <scenario> (Positive|Negative)",
          "type": "positive" | "negative",
          "method": "<HTTP method exactly as given>",
          "path": "<path exactly as given>",
          "description": "<short description of the scenario>",
          "headers": {"Authorization": "Bearer {{token}}"},
          "query_params": {},
          "path_params": {},
          "body": {} | null,
          "expected_status": <integer>,
          "assertions": [{"type": "status_code", "expected": <integer>}],
          "save_variables": [{"json_path": "$.id", "variable": "resourceId"}],
          "assumptions": []
        }
      ]
    }
  ]
}
```

Field requirements:

- `name` is required and must be meaningful.
- `type` is exactly `positive` or `negative`.
- `method` and `path` must be copied verbatim from an endpoint listed above.
  Never output an endpoint that was not given to you.
- `headers` must contain `"Authorization": "Bearer {{token}}"` for every
  authorized request. Only an unauthorized negative case may omit it. Never emit
  a real token — `{{token}}` stays a Postman variable.
- `expected_status` is an integer.
- `assertions` always contains at least `{"type": "status_code", "expected": <expected_status>}`.
- `body` is a JSON object or `null` (use `null` when the request has no body).
- `query_params`, `path_params` are JSON objects (`{}` when empty).
- `save_variables`, `assumptions` are arrays (`[]` when empty).
- Put any assumption you had to make (undocumented status code, invented field for
  a negative case) as a string inside `assumptions`.
"""


# ── مدل خروجی ────────────────────────────────────────────────────────────────

class SaveVariable(TypedDict):
    """مقداری از پاسخ که باید در یک متغیر Postman ذخیره شود."""
    json_path: str
    variable: str
    # global → pm.globals.set ، collection → pm.collectionVariables.set
    # اگر نیاید، رفتارِ قبلی (collection) حفظ می‌شود.
    scope: NotRequired[str]
    # منبعِ مقدار: "body" (پیش‌فرض) یا "header".
    # با "header"، json_path نامِ هدر است، نه یک مسیرِ JSON.
    source: NotRequired[str]


class Assertion(TypedDict):
    """یک بررسی روی پاسخ — همیشه دست‌کم یک assertion از نوع status_code وجود دارد."""
    type: str
    expected: NotRequired[object]
    json_path: NotRequired[str]


class TestCase(TypedDict):
    """یک سناریوی تست روی یک endpoint مشخص."""
    name: str
    type: str                       # positive | negative
    method: str
    path: str
    description: str
    headers: dict[str, str]
    query_params: dict
    path_params: dict
    body: dict | None
    expected_status: int
    assertions: list[Assertion]
    save_variables: list[SaveVariable]
    assumptions: list[str]
    # نامِ متغیرِ base URL برای این درخواست — وقتی یک سناریو چند منبعِ Swagger
    # (چند سرویس) دارد، هر قدم می‌تواند میزبانِ خودش را داشته باشد.
    base_url_var: NotRequired[str]


class ControllerTestCases(TypedDict):
    """تست‌کیس‌های یک controller."""
    name: str
    test_cases: list[TestCase]


class TestCaseSuite(TypedDict):
    """خروجی کامل تولیدکننده — ورودی postman_builder."""
    controllers: list[ControllerTestCases]


class TestCaseGenerationError(RuntimeError):
    """خروجی LLM خالی، غیر JSON، یا مغایر با قرارداد خروجی بوده است."""


# ── ساخت prompt ──────────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    """system.md + rules.md + قرارداد خروجی JSON را ترکیب می‌کند."""
    parts = [
        (_PROMPTS_DIR / "system.md").read_text(encoding="utf-8"),
        (_PROMPTS_DIR / "rules.md").read_text(encoding="utf-8"),
        _OUTPUT_CONTRACT,
    ]
    return "\n\n---\n\n".join(parts)


def _format_endpoint(endpoint: EndpointSpec) -> str:
    """یک EndpointSpec را به بلوک متنی خوانا برای LLM تبدیل می‌کند."""
    lines = [f"### {endpoint.method} {endpoint.path}"]

    if endpoint.operation_id:
        lines.append(f"- operationId: {endpoint.operation_id}")
    if endpoint.summary:
        lines.append(f"- summary: {endpoint.summary}")
    lines.append(f"- requires auth: {'yes' if endpoint.security else 'no'}")
    lines.append(f"- success status: {endpoint.success_status}")
    if endpoint.error_statuses:
        lines.append(
            "- error statuses: "
            + ", ".join(str(code) for code in endpoint.error_statuses)
        )

    if endpoint.params:
        lines.append("- parameters:")
        for param in endpoint.params:
            required = "required" if param.required else "optional"
            example = f", example: {param.example}" if param.example is not None else ""
            lines.append(
                f"  - {param.name} (in: {param.location}, {required}, "
                f"type: {param.schema_type}{example})"
            )

    if endpoint.required_headers:
        lines.append("- required headers: " + ", ".join(endpoint.required_headers))

    if endpoint.request_body is not None:
        required = "required" if endpoint.request_body_required else "optional"
        body = json.dumps(endpoint.request_body, ensure_ascii=False, indent=2)
        lines.append(
            f"- request body ({endpoint.request_content_type}, {required}):\n"
            f"```json\n{body}\n```"
        )

    if endpoint.response_fields:
        lines.append("- response fields: " + ", ".join(endpoint.response_fields))

    return "\n".join(lines)


def _format_endpoints(endpoints: list[EndpointSpec]) -> str:
    """endpoint ها را بر اساس controller گروه‌بندی و به متن تبدیل می‌کند."""
    groups: dict[str, list[EndpointSpec]] = {}
    for ep in endpoints:
        groups.setdefault(ep.controller, []).append(ep)

    parts: list[str] = []
    for controller in sorted(groups):
        parts.append(f"## Controller: {controller}")
        parts.extend(_format_endpoint(ep) for ep in groups[controller])
    return "\n\n".join(parts)


def _build_user_message(
    endpoints: list[EndpointSpec],
    title: str,
    version: str,
    base_url: str,
    global_security: bool,
) -> str:
    header = "\n".join([
        f"# API: {title} (version {version})",
        f"Base URL: {base_url or 'unknown'}",
        f"Global auth required: {'yes' if global_security else 'no'}",
        f"Total endpoints: {len(endpoints)}",
    ])
    return (
        "Generate test cases for the following API endpoints.\n\n"
        f"{header}\n\n{_format_endpoints(endpoints)}"
    )


# ── استخراج و اعتبارسنجی خروجی ───────────────────────────────────────────────

def _extract_json(raw: str) -> dict:
    """متن خام LLM را به dict تبدیل می‌کند — fence و متن اضافه را تحمل می‌کند.

    منطقِ استخراج در json_output.py است تا در مسیرِ سناریو و مسیرِ تحلیلِ تسک
    تکرار نشود؛ این‌جا فقط به خطای این ماژول ترجمه می‌شود.
    """
    try:
        return extract_json_object(raw)
    except JsonExtractionError as exc:
        raise TestCaseGenerationError(str(exc)) from exc


def _validate_test_case(
    case: object,
    known_endpoints: set[str],
    where: str,
    errors: list[str],
) -> TestCase | None:
    """یک تست‌کیس را اعتبارسنجی و نرمال‌سازی می‌کند.

    ظرف‌های اختیاری که غایب باشند با مقدار خالی پر می‌شوند (query_params،
    path_params، save_variables، assumptions، description). بقیه‌ی تخلف‌ها به
    errors اضافه می‌شوند و باعث شکست کل تولید می‌شوند.
    """
    if not isinstance(case, dict):
        errors.append(f"{where}: expected an object, got {type(case).__name__}.")
        return None

    name = case.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{where}: 'name' is missing or empty.")
        return None
    where = f"{where} ('{name}')"

    case_type = case.get("type")
    if case_type not in _VALID_TYPES:
        errors.append(f"{where}: 'type' must be one of {_VALID_TYPES}, got {case_type!r}.")

    method = case.get("method")
    path = case.get("path")
    if not isinstance(method, str) or not isinstance(path, str):
        errors.append(f"{where}: 'method' and 'path' must both be strings.")
        return None
    method = method.upper()
    if f"{method} {path}" not in known_endpoints:
        errors.append(
            f"{where}: '{method} {path}' is not one of the endpoints from the "
            f"Swagger specification."
        )

    headers = case.get("headers", {})
    if not isinstance(headers, dict):
        errors.append(f"{where}: 'headers' must be an object.")
        headers = {}
    auth = headers.get(_AUTH_HEADER)
    if auth is not None and auth not in (_AUTH_VALUE, ""):
        errors.append(
            f"{where}: '{_AUTH_HEADER}' must be exactly '{_AUTH_VALUE}' — "
            f"a literal token must never be generated."
        )
    elif auth is None and case_type == "positive":
        errors.append(f"{where}: a positive test case must send '{_AUTH_HEADER}'.")

    expected_status = case.get("expected_status")
    if not isinstance(expected_status, int) or isinstance(expected_status, bool):
        errors.append(f"{where}: 'expected_status' must be an integer.")
        expected_status = 0

    assertions = case.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        errors.append(f"{where}: 'assertions' must be a non-empty array.")
        assertions = []
    elif not any(
        isinstance(a, dict) and a.get("type") == "status_code" for a in assertions
    ):
        errors.append(f"{where}: 'assertions' must include a 'status_code' assertion.")

    body = case.get("body")
    if body is not None and not isinstance(body, dict):
        errors.append(f"{where}: 'body' must be an object or null.")
        body = None

    query_params = case.get("query_params", {})
    if not isinstance(query_params, dict):
        errors.append(f"{where}: 'query_params' must be an object.")
        query_params = {}

    path_params = case.get("path_params", {})
    if not isinstance(path_params, dict):
        errors.append(f"{where}: 'path_params' must be an object.")
        path_params = {}

    save_variables = case.get("save_variables", [])
    if not isinstance(save_variables, list):
        errors.append(f"{where}: 'save_variables' must be an array.")
        save_variables = []

    assumptions = case.get("assumptions", [])
    if not isinstance(assumptions, list):
        errors.append(f"{where}: 'assumptions' must be an array.")
        assumptions = []

    return TestCase(
        name=name,
        type=case_type if case_type in _VALID_TYPES else "positive",
        method=method,
        path=path,
        description=str(case.get("description", "")),
        headers=headers,
        query_params=query_params,
        path_params=path_params,
        body=body,
        expected_status=expected_status,
        assertions=assertions,
        save_variables=save_variables,
        assumptions=assumptions,
    )


def _validate_suite(data: dict, endpoints: list[EndpointSpec]) -> TestCaseSuite:
    """کل خروجی LLM را اعتبارسنجی می‌کند و در صورت تخلف خطا می‌دهد."""
    controllers = data.get("controllers")
    if not isinstance(controllers, list) or not controllers:
        raise TestCaseGenerationError(
            "LLM output has no non-empty 'controllers' array."
        )

    known = {f"{ep.method.upper()} {ep.path}" for ep in endpoints}
    errors: list[str] = []
    result: list[ControllerTestCases] = []

    for c_index, controller in enumerate(controllers):
        where = f"controllers[{c_index}]"
        if not isinstance(controller, dict):
            errors.append(f"{where}: expected an object.")
            continue

        c_name = controller.get("name")
        if not isinstance(c_name, str) or not c_name.strip():
            errors.append(f"{where}: 'name' is missing or empty.")
            c_name = "default"

        raw_cases = controller.get("test_cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            errors.append(f"{where} ('{c_name}'): 'test_cases' must be a non-empty array.")
            continue

        cases: list[TestCase] = []
        for t_index, raw_case in enumerate(raw_cases):
            case = _validate_test_case(
                raw_case, known, f"{where}.test_cases[{t_index}]", errors
            )
            if case is not None:
                cases.append(case)

        result.append(ControllerTestCases(name=c_name, test_cases=cases))

    if errors:
        raise TestCaseGenerationError(
            "LLM output does not match the required test case format:\n  - "
            + "\n  - ".join(errors)
        )

    return TestCaseSuite(controllers=result)


# ── حالتِ سناریو: Scenario + Swagger → تست‌کیس ────────────────────────────────
#
# در حالتِ سناریو، کاربر جریان را صریحاً تعریف کرده است: ترتیبِ قدم‌ها، کدهای
# وضعیتِ مورد انتظار، متغیرهایی که استخراج می‌شوند و دقیقاً کدام سناریوی منفی
# خواسته شده است. این‌ها «قرارداد» هستند، نه حدس — پس اسکلتِ تست‌کیس‌ها به‌صورت
# قطعی (deterministic) از Scenario + Swagger ساخته می‌شود.
#
# LLM فقط در یک گامِ اختیاری و کاملاً افزایشی استفاده می‌شود (بدنه‌های واقع‌گرایانه‌تر
# برای کیس‌های منفی، توضیحات، و assertion های فیلدِ پاسخ). اگر آن گام شکست بخورد،
# همان اسکلتِ قطعی خروجی می‌شود.

# کدهای وضعیتِ پیش‌فرضِ هر نوع سناریوی منفی — فقط وقتی استفاده می‌شوند که نه
# سناریو و نه Swagger کد را مشخص نکرده باشند.
_DEFAULT_NEGATIVE_STATUS: dict[str, int] = {
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "duplicate": 409,
    "missing_required_field": 400,
    "invalid_request": 400,
    "invalid_body": 400,
    "invalid_parameter": 400,
    "validation_error": 400,
}

# پارامترِ مسیر به شکلِ {name} داخلِ path
_PATH_PARAM_RE = re.compile(r"\{([^{}]+)\}")

# فیلدی که postman_builder می‌تواند به دسترسیِ امنِ JS تبدیلش کند
_SIMPLE_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# سقفِ assertion های فیلدِ پاسخ برای هر تست‌کیسِ مثبت
_MAX_RESPONSE_ASSERTIONS = 3

# شناسه‌ای که انتظار می‌رود وجود نداشته باشد (برای سناریوی not_found)
_NON_EXISTENT_ID = 999999999


@dataclass
class ResolvedStep:
    """یک قدمِ سناریو که به عملیاتِ واقعیِ Swagger گره خورده است.

    این ساختار ورودیِ generate_for_scenario است: agent.py قدم را با
    swagger_analyzer.find_endpoint پیدا می‌کند و نتیجه را این‌جا می‌گذارد.
    """
    step: ScenarioStep
    endpoint: EndpointSpec
    source_name: str
    base_url: str
    base_url_var: str = DEFAULT_BASE_URL_VAR


@dataclass
class _StepRequest:
    """قسمتِ مشترکِ همه‌ی درخواست‌های یک قدم — پیش از تفکیکِ مثبت/منفی."""
    path_params: dict = field(default_factory=dict)
    query_params: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    body: dict | None = None
    assumptions: list[str] = field(default_factory=list)


# ---- کمکی‌های عمومی ---------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """override را روی base می‌نشاند؛ dict های تودرتو ادغام می‌شوند."""
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = value
    return result


def _invalid_value(value: object) -> object:
    """یک مقدارِ هم‌نوع‌نبودن (type violation) برای مقدارِ داده‌شده می‌سازد."""
    return -1 if isinstance(value, str) else "invalid"


# ---- جای‌گذاریِ پارامترها ----------------------------------------------------

def _resolve_param_location(
    name: str,
    declared: str,
    endpoint: EndpointSpec,
    placeholders: set[str],
    swagger_locations: dict[str, str],
) -> tuple[str, str]:
    """محلِ واقعیِ یک پارامتر را تعیین می‌کند — Swagger حرفِ آخر را می‌زند.

    سناریو جریان را توصیف می‌کند و ممکن است یک مقدار را «path» بنویسد در حالی
    که Swagger همان پارامتر را query تعریف کرده باشد. در این حالت محلِ Swagger
    برنده است و دلیلش به‌عنوان assumption ثبت می‌شود.

    برمی‌گرداند: (محلِ واقعی، توضیحِ اختیاری برای assumptions)
    """
    if name in placeholders:
        return "path", ""

    swagger_location = swagger_locations.get(name)
    if swagger_location in ("query", "header"):
        if swagger_location != declared:
            return swagger_location, (
                f"The scenario declares '{name}' as a {declared} parameter, but the "
                f"Swagger specification defines it as a {swagger_location} parameter; "
                f"it is sent as {swagger_location}."
            )
        return swagger_location, ""

    if declared == "path":
        return "query", (
            f"The scenario declares '{name}' as a path parameter, but the documented "
            f"path '{endpoint.path}' has no '{{{name}}}' placeholder; it is sent as a "
            f"query parameter instead."
        )

    return declared, ""


def _place_params(
    params: StepParams, endpoint: EndpointSpec
) -> tuple[dict, dict, dict, list[str]]:
    """مقادیرِ سناریو را در محلِ درستِ درخواست می‌نشاند.

    مقادیر دست‌نخورده منتقل می‌شوند؛ یک متغیرِ Postman مثل "{{voucherId}}" باید
    تا خروجیِ نهایی به همان شکل باقی بماند و هرگز به مقدارِ واقعی تبدیل نشود.

    برمی‌گرداند: (path_params, query_params, headers, assumptions)
    """
    placeholders = {m.group(1) for m in _PATH_PARAM_RE.finditer(endpoint.path)}
    swagger_locations = {p.name: p.location for p in endpoint.params if p.name}

    path_params: dict = {}
    query_params: dict = {}
    headers: dict = {}
    assumptions: list[str] = []

    for declared, values in (
        ("path", params.path),
        ("query", params.query),
        ("header", params.header),
    ):
        for raw_name, value in values.items():
            name = str(raw_name)
            location, note = _resolve_param_location(
                name, declared, endpoint, placeholders, swagger_locations
            )
            if note:
                assumptions.append(note)
            if location == "path":
                path_params[name] = value
            elif location == "header":
                headers[name] = value
            else:
                query_params[name] = value

    return path_params, query_params, headers, assumptions


def _initial_body(endpoint: EndpointSpec, overrides: dict) -> dict | None:
    """بدنه‌ی درخواست را از نمونه‌ی Swagger می‌سازد و override های سناریو را می‌نشاند."""
    sample = endpoint.request_body
    body = copy.deepcopy(sample) if isinstance(sample, dict) else None
    if overrides:
        body = _deep_merge(body or {}, dict(overrides))
    return body


def _build_step_request(resolved: ResolvedStep) -> _StepRequest:
    """قسمتِ مشترکِ درخواست‌های یک قدم را می‌سازد."""
    path_params, query_params, headers, assumptions = _place_params(
        resolved.step.params, resolved.endpoint
    )
    return _StepRequest(
        path_params=path_params,
        query_params=query_params,
        headers=headers,
        body=_initial_body(resolved.endpoint, resolved.step.params.body),
        assumptions=assumptions,
    )


# ---- ساختِ تست‌کیسِ مثبت ------------------------------------------------------

def _response_field_assertions(endpoint: EndpointSpec) -> list[Assertion]:
    """چند assertionِ «وجودِ فیلد» از روی فیلدهای پاسخِ مستندشده می‌سازد."""
    return [
        Assertion(type="json_field", json_path=f"$.{name}")
        for name in endpoint.response_fields[:_MAX_RESPONSE_ASSERTIONS]
        if isinstance(name, str) and _SIMPLE_FIELD_RE.match(name)
    ]


def _build_positive_case(resolved: ResolvedStep, base: _StepRequest) -> TestCase:
    """سناریوی مثبتِ یک قدم را به یک تست‌کیس تبدیل می‌کند."""
    step, endpoint = resolved.step, resolved.endpoint
    positive = step.positive
    assumptions = list(base.assumptions)

    if positive is not None and positive.expected_status is not None:
        expected_status = positive.expected_status
    else:
        expected_status = endpoint.success_status
        assumptions.append(
            f"The scenario does not declare an expected status for this step; "
            f"{expected_status} comes from the Swagger specification."
        )

    assertions: list[Assertion] = [
        Assertion(type="status_code", expected=expected_status)
    ]
    assertions.extend(_response_field_assertions(endpoint))

    save_variables = [
        SaveVariable(
            json_path=extract.json_path,
            variable=extract.variable,
            scope=extract.scope,
        )
        for extract in (positive.extract if positive else [])
    ]

    return TestCase(
        name=f"{endpoint.method} {endpoint.path} — {step.name} (Positive)",
        type="positive",
        method=endpoint.method,
        path=endpoint.path,
        description=step.description
        or f"Positive case for scenario step '{step.name}'.",
        headers={_AUTH_HEADER: _AUTH_VALUE, **base.headers},
        query_params=dict(base.query_params),
        path_params=dict(base.path_params),
        body=copy.deepcopy(base.body),
        expected_status=expected_status,
        assertions=assertions,
        save_variables=save_variables,
        assumptions=assumptions,
        base_url_var=resolved.base_url_var,
    )


# ---- ساختِ تست‌کیسِ منفی ------------------------------------------------------

def _pick_required_field(endpoint: EndpointSpec, body: dict) -> str:
    """فیلدی از بدنه که حذفش سناریوی «فیلد اجباریِ جاافتاده» را می‌سازد.

    اول سراغِ فیلدهای اجباریِ مستندشده در Swagger می‌رود؛ فقط اگر هیچ‌کدام در
    بدنه نبودند، به اولین فیلد برمی‌گردد.
    """
    for name in endpoint.request_body_required_fields:
        if name in body:
            return name
    return next(iter(body), "")


def _invalidate_first_param(path_params: dict, query_params: dict) -> str:
    """اولین پارامتر را به یک مقدارِ نامعتبر تغییر می‌دهد و نامش را برمی‌گرداند."""
    for bucket in (path_params, query_params):
        for name, value in bucket.items():
            bucket[name] = _invalidate_param_value(value)
            return str(name)
    return ""


def _invalidate_param_value(value: object) -> object:
    """مقدارِ نامعتبر برای یک پارامتر — متغیرهای Postman هم جایگزین می‌شوند."""
    if isinstance(value, str) and "{{" in value:
        # یک متغیر Postman قابلِ «بدشکل کردن» نیست؛ مقدارِ آشکارا نامعتبر می‌گذاریم
        return "invalid"
    return _invalid_value(value)


def _apply_negative_type(
    neg_type: str,
    endpoint: EndpointSpec,
    path_params: dict,
    query_params: dict,
    headers: dict,
    body: dict | None,
) -> tuple[dict | None, list[str]]:
    """درخواست را طوری تغییر می‌دهد که سناریوی منفیِ خواسته‌شده را بسازد.

    dict های پارامتر و هدر در جا تغییر می‌کنند؛ بدنه چون می‌تواند جایگزین یا حذف
    شود به‌صورت مقدارِ بازگشتی برمی‌گردد.

    برمی‌گرداند: (بدنه‌ی نهایی، توضیحاتی که باید در assumptions ثبت شود)
    """
    notes: list[str] = []

    if neg_type == "unauthorized":
        headers.pop(_AUTH_HEADER, None)
        notes.append(
            "The Authorization header is intentionally not sent so that the request "
            "asserts the unauthorized response."
        )
        return body, notes

    if neg_type == "forbidden":
        notes.append(
            "Sends a valid {{token}} that is expected to lack permission for this "
            "operation."
        )
        return body, notes

    if neg_type == "duplicate":
        notes.append(
            "Repeats the same valid payload; run it after the positive case so the "
            "resource already exists."
        )
        return body, notes

    if neg_type == "not_found":
        replaced = [
            name
            for bucket in (path_params, query_params)
            for name in bucket
            if str(name).lower().endswith("id")
        ]
        for bucket in (path_params, query_params):
            for name in list(bucket):
                if str(name).lower().endswith("id"):
                    bucket[name] = _NON_EXISTENT_ID
        if replaced:
            notes.append(
                f"Identifier parameter(s) {', '.join(replaced)} are replaced with "
                f"{_NON_EXISTENT_ID}, which is expected not to exist."
            )
        else:
            notes.append(
                "The operation documents no identifier parameter to point at a "
                "missing resource; the request is sent unchanged."
            )
        return body, notes

    if neg_type == "missing_required_field":
        if isinstance(body, dict) and body:
            removed = _pick_required_field(endpoint, body)
            if removed:
                notes.append(f"Required request body field '{removed}' is omitted.")
                return {k: v for k, v in body.items() if k != removed}, notes

        required_param = next(
            (
                p.name
                for p in endpoint.params
                if p.required and (p.name in path_params or p.name in query_params)
            ),
            "",
        )
        if required_param:
            path_params.pop(required_param, None)
            query_params.pop(required_param, None)
            notes.append(f"Required parameter '{required_param}' is omitted.")
        else:
            notes.append(
                "The specification documents no required field or parameter that this "
                "request sends, so nothing could be omitted."
            )
        return body, notes

    # invalid_request / invalid_body / invalid_parameter / validation_error
    prefer_params = neg_type == "invalid_parameter"

    if not prefer_params and isinstance(body, dict) and body:
        name = next(iter(body))
        body[name] = _invalid_value(body[name])
        notes.append(
            f"Request body field '{name}' carries a value of the wrong type."
        )
        return body, notes

    changed = _invalidate_first_param(path_params, query_params)
    if changed:
        notes.append(f"Parameter '{changed}' carries an invalid value.")
        return body, notes

    if isinstance(body, dict) and body:
        name = next(iter(body))
        body[name] = _invalid_value(body[name])
        notes.append(f"Request body field '{name}' carries a value of the wrong type.")
        return body, notes

    notes.append(
        "The operation documents no parameter or body field to make invalid; the "
        "request is sent unchanged."
    )
    return body, notes


def _negative_expected_status(
    negative: NegativeCase, endpoint: EndpointSpec
) -> tuple[int, str]:
    """کدِ وضعیتِ مورد انتظارِ یک سناریوی منفی — سناریو، بعد Swagger، بعد پیش‌فرض."""
    if negative.expected_status is not None:
        return negative.expected_status, ""

    default = _DEFAULT_NEGATIVE_STATUS.get(negative.type, 400)
    if default in endpoint.error_statuses:
        return default, (
            f"The scenario does not declare an expected status; {default} comes from "
            f"the Swagger specification."
        )
    if default == 400 and 422 in endpoint.error_statuses:
        return 422, (
            "The specification documents 422 (and not 400) for this operation, so 422 "
            "is expected."
        )
    return default, (
        f"Status {default} is not documented for this operation in Swagger; it is "
        f"assumed from the negative case type '{negative.type}'."
    )


def _build_negative_case(
    resolved: ResolvedStep, negative: NegativeCase, base: _StepRequest
) -> TestCase:
    """یک سناریوی منفیِ صریحِ سناریو را به یک درخواستِ مستقل تبدیل می‌کند.

    این تست‌کیس هرگز درخواستِ مثبت را تغییر نمی‌دهد — یک کپیِ مستقل است.
    """
    step, endpoint = resolved.step, resolved.endpoint

    path_params = dict(base.path_params)
    query_params = dict(base.query_params)
    headers = {_AUTH_HEADER: _AUTH_VALUE, **base.headers}
    body = copy.deepcopy(base.body)
    assumptions = list(base.assumptions)

    # override هایی که خودِ سناریو برای این کیسِ منفی داده است
    if not negative.params.is_empty():
        n_path, n_query, n_headers, n_assumptions = _place_params(
            negative.params, endpoint
        )
        path_params.update(n_path)
        query_params.update(n_query)
        headers.update(n_headers)
        if negative.params.body:
            body = _deep_merge(body or {}, dict(negative.params.body))
        assumptions.extend(n_assumptions)

    body, type_notes = _apply_negative_type(
        negative.type, endpoint, path_params, query_params, headers, body
    )
    assumptions.extend(type_notes)

    expected_status, status_note = _negative_expected_status(negative, endpoint)
    if status_note:
        assumptions.append(status_note)

    return TestCase(
        name=f"{endpoint.method} {endpoint.path} — {negative.name} (Negative)",
        type="negative",
        method=endpoint.method,
        path=endpoint.path,
        description=negative.description
        or (
            f"Negative case '{negative.name}' ({negative.type}) for scenario step "
            f"'{step.name}'."
        ),
        headers=headers,
        query_params=query_params,
        path_params=path_params,
        body=body,
        expected_status=expected_status,
        # پاسخِ خطا شکلِ پاسخِ موفق را ندارد → فقط کدِ وضعیت assert می‌شود
        assertions=[Assertion(type="status_code", expected=expected_status)],
        # فقط مسیرِ موفق است که متغیر می‌سازد
        save_variables=[],
        assumptions=assumptions,
        base_url_var=resolved.base_url_var,
    )


# ---- اسکلتِ قطعیِ کلِ سناریو ---------------------------------------------------

def _build_scenario_baseline(steps: list[ResolvedStep]) -> TestCaseSuite:
    """از قدم‌های resolve شده یک TestCaseSuite قطعی می‌سازد.

    هر قدم یک folder می‌شود و ترتیبِ folder ها دقیقاً ترتیبِ `steps` سناریو است؛
    Postman هم ترتیبِ item ها را حفظ می‌کند. پس متغیری که در یک قدم استخراج
    می‌شود، پیش از قدم‌های بعدی ساخته شده است.
    """
    controllers: list[ControllerTestCases] = []
    for index, resolved in enumerate(steps, start=1):
        base = _build_step_request(resolved)
        cases: list[TestCase] = [_build_positive_case(resolved, base)]
        cases.extend(
            _build_negative_case(resolved, negative, base)
            for negative in resolved.step.negative
        )
        controllers.append(
            ControllerTestCases(name=f"{index}. {resolved.step.name}", test_cases=cases)
        )
    return TestCaseSuite(controllers=controllers)


# ---- گامِ اختیاریِ غنی‌سازی با LLM ---------------------------------------------

_SCENARIO_REFINEMENT_CONTRACT = """\
## Scenario mode — what you are asked for

The scenario below has already been turned into a complete, valid set of test
cases. The flow, the order of the steps, the HTTP methods and paths, the expected
status codes, the Authorization handling and the response-variable extraction are
already decided and are NOT yours to change.

Your only job is to improve the request bodies and the descriptions, and to
suggest response-field assertions, using the Swagger information given per case.

## Output Format (strict)

Return ONLY valid JSON. No markdown fences, no explanation before or after the JSON.

```
{
  "cases": [
    {
      "id": "<the id given for the case, copied verbatim>",
      "description": "<one clear sentence describing what this request checks>",
      "body": {} | null,
      "assertions": [{"type": "json_field", "json_path": "$.someField"}]
    }
  ]
}
```

Rules:

- Only use ids that were given to you. Never invent a case.
- `body` must stay consistent with the documented request schema. Return `null`
  when the request should not carry a body, and omit the field when you have
  nothing to improve.
- Never replace a Postman variable with a real value. A value such as
  `{{voucherId}}` or `{{token}}` must be copied verbatim.
- Never generate a real token, credential, password, or personal data.
- For a negative case, change only what the case's intent requires — an
  unauthorized case keeps a valid body, a missing-field case keeps everything
  except the omitted field.
- `assertions` may only contain field assertions on the success response of a
  positive case. Never return a `status_code` assertion — the expected status is
  already fixed. Use at most 3 assertions per case, each with a simple
  `$.field` or `$.field.subfield` path.
- Omit any case you have nothing to add to. An empty `"cases": []` is a valid
  answer.
"""


def _build_scenario_system_prompt() -> str:
    """system.md + rules.md + قراردادِ خروجیِ گامِ غنی‌سازیِ سناریو."""
    parts = [
        (_PROMPTS_DIR / "system.md").read_text(encoding="utf-8"),
        (_PROMPTS_DIR / "rules.md").read_text(encoding="utf-8"),
        _SCENARIO_REFINEMENT_CONTRACT,
    ]
    return "\n\n---\n\n".join(parts)


def _case_id(step_index: int, case_index: int) -> str:
    return f"{step_index}.{case_index}"


def _format_scenario_case(
    case_id: str, resolved: ResolvedStep, case: TestCase
) -> str:
    """یک تست‌کیسِ ساخته‌شده را برای بازبینیِ LLM به متن تبدیل می‌کند."""
    endpoint = resolved.endpoint
    lines = [
        f"### case id: {case_id}",
        f"- step: {resolved.step.name} (swagger source: {resolved.source_name})",
        f"- kind: {case['type']}",
        f"- request: {case['method']} {case['path']}",
        f"- expected status: {case['expected_status']}",
        f"- current description: {case['description']}",
    ]

    if case["path_params"]:
        lines.append(f"- path params: {json.dumps(case['path_params'], ensure_ascii=False)}")
    if case["query_params"]:
        lines.append(f"- query params: {json.dumps(case['query_params'], ensure_ascii=False)}")

    if endpoint.request_body is not None:
        lines.append(
            "- documented request body sample:\n```json\n"
            + json.dumps(endpoint.request_body, ensure_ascii=False, indent=2)
            + "\n```"
        )
    if endpoint.request_body_required_fields:
        lines.append(
            "- required body fields: "
            + ", ".join(endpoint.request_body_required_fields)
        )

    body = case["body"]
    if body is None:
        lines.append("- current body: none")
    else:
        lines.append(
            "- current body:\n```json\n"
            + json.dumps(body, ensure_ascii=False, indent=2)
            + "\n```"
        )

    if case["type"] == "positive" and endpoint.response_fields:
        lines.append("- documented response fields: " + ", ".join(endpoint.response_fields))
    if case["assumptions"]:
        lines.append("- intent: " + " ".join(case["assumptions"]))

    return "\n".join(lines)


def _build_scenario_user_message(
    scenario: Scenario, steps: list[ResolvedStep], suite: TestCaseSuite
) -> str:
    """پیامِ کاربر برای گامِ غنی‌سازی را می‌سازد."""
    header = [
        f"# Scenario: {scenario.name}",
        scenario.description or "(no description)",
        "",
        "Steps, in execution order:",
    ]
    header.extend(
        f"  {index}. {resolved.step.name} — {resolved.endpoint.method} {resolved.endpoint.path}"
        for index, resolved in enumerate(steps, start=1)
    )

    blocks: list[str] = []
    for step_index, (resolved, controller) in enumerate(
        zip(steps, suite["controllers"]), start=1
    ):
        for case_index, case in enumerate(controller["test_cases"]):
            blocks.append(
                _format_scenario_case(
                    _case_id(step_index, case_index), resolved, case
                )
            )

    return (
        "Improve the request bodies, descriptions and response assertions of the "
        "following generated test cases.\n\n"
        + "\n".join(header)
        + "\n\n"
        + "\n\n".join(blocks)
    )


def _parse_refinements(raw: str) -> dict[str, dict]:
    """پاسخِ گامِ غنی‌سازی را به نگاشتِ case id → اصلاحات تبدیل می‌کند."""
    data = _extract_json(raw)
    cases = data.get("cases")
    if not isinstance(cases, list):
        return {}

    result: dict[str, dict] = {}
    for entry in cases:
        if not isinstance(entry, dict):
            continue
        case_id = entry.get("id")
        if isinstance(case_id, str) and case_id.strip():
            result[case_id.strip()] = entry
    return result


def _refined_assertions(entry: dict, case: TestCase) -> list[Assertion]:
    """assertion های پیشنهادیِ LLM را فیلتر می‌کند.

    فقط assertion های فیلدِ پاسخ روی کیسِ مثبت پذیرفته می‌شوند؛ کدِ وضعیت از
    قبل قطعی است و هرگز از LLM گرفته نمی‌شود.
    """
    if case["type"] != "positive":
        return []

    suggested = entry.get("assertions")
    if not isinstance(suggested, list):
        return []

    existing = {
        a.get("json_path")
        for a in case["assertions"]
        if isinstance(a, dict) and a.get("json_path")
    }
    accepted: list[Assertion] = []
    for item in suggested:
        if not isinstance(item, dict) or item.get("type") == "status_code":
            continue
        json_path = item.get("json_path")
        if not isinstance(json_path, str) or json_path in existing:
            continue
        if not all(
            _SIMPLE_FIELD_RE.match(segment)
            for segment in json_path.lstrip("$").lstrip(".").split(".")
            if segment
        ):
            continue
        existing.add(json_path)
        accepted.append(Assertion(type="json_field", json_path=json_path))
        if len(accepted) + len(case["assertions"]) - 1 >= _MAX_RESPONSE_ASSERTIONS:
            break
    return accepted


def _apply_refinements(
    steps: list[ResolvedStep], suite: TestCaseSuite, refinements: dict[str, dict]
) -> int:
    """اصلاحاتِ LLM را روی اسکلتِ قطعی اعمال می‌کند — فقط و فقط افزایشی.

    method، path، کدِ وضعیت، هدرها، ترتیب و متغیرهای استخراجی هرگز از LLM گرفته
    نمی‌شوند. بعد از اعمالِ بدنه‌ی پیشنهادی، مقادیری که خودِ سناریو تعیین کرده
    دوباره روی آن نشانده می‌شوند تا متغیرهایی مثل {{voucherId}} حفظ شوند.

    برمی‌گرداند: تعداد تست‌کیس‌هایی که واقعاً تغییر کردند.
    """
    changed = 0
    for step_index, (resolved, controller) in enumerate(
        zip(steps, suite["controllers"]), start=1
    ):
        negatives = resolved.step.negative
        for case_index, case in enumerate(controller["test_cases"]):
            entry = refinements.get(_case_id(step_index, case_index))
            if not entry:
                continue

            touched = False

            description = entry.get("description")
            if isinstance(description, str) and description.strip():
                case["description"] = description.strip()
                touched = True

            # بدنه فقط وقتی پذیرفته می‌شود که این درخواست اصلاً بدنه می‌گیرد
            body = entry.get("body")
            if isinstance(body, dict) and isinstance(case["body"], dict):
                merged = body
                # مقادیرِ صریحِ سناریو همیشه برنده‌اند
                overrides = dict(resolved.step.params.body)
                if case_index > 0 and case_index - 1 < len(negatives):
                    overrides = _deep_merge(
                        overrides, dict(negatives[case_index - 1].params.body)
                    )
                if overrides:
                    merged = _deep_merge(merged, overrides)
                case["body"] = merged
                touched = True

            extra = _refined_assertions(entry, case)
            if extra:
                case["assertions"].extend(extra)
                touched = True

            if touched:
                changed += 1

    return changed


# ── تولیدکننده ───────────────────────────────────────────────────────────────

class TestCaseGenerator:
    """از اطلاعات تحلیل‌شده‌ی Swagger، تست‌کیس ساختاریافته تولید می‌کند.

    پارامترها:
        debug_config    : تنظیمات لاگ پروژه
        temperature     : دمای LLM — پیش‌فرض پایین چون خروجی باید ساختاریافته باشد
        max_tokens      : سقف توکن پاسخ
        enrich_with_llm : در حالتِ سناریو، آیا گامِ اختیاریِ غنی‌سازی با LLM اجرا شود
    """

    name = "testcase_generator"

    def __init__(
        self,
        debug_config: DebugConfig | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        enrich_with_llm: bool = True,
    ) -> None:
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._enrich_with_llm = enrich_with_llm

    def generate(self, spec: ApiSpec, user_id: str) -> TestCaseSuite:
        """برای تمام endpoint های یک ApiSpec تست‌کیس تولید می‌کند."""
        return self.generate_for_endpoints(
            endpoints=spec.endpoints,
            user_id=user_id,
            title=spec.title,
            version=spec.version,
            base_url=spec.base_url,
            global_security=spec.global_security,
        )

    def generate_for_endpoints(
        self,
        endpoints: list[EndpointSpec],
        user_id: str,
        title: str = "API",
        version: str = "1.0.0",
        base_url: str = "",
        global_security: bool = False,
    ) -> TestCaseSuite:
        """برای زیرمجموعه‌ای از endpoint ها تست‌کیس تولید می‌کند.

        برای spec های بزرگ می‌توان endpoint ها را دسته‌دسته پاس داد تا پاسخ LLM
        در سقف توکن جا شود.

        پرتاب می‌کند:
            ValueError                : لیست endpoint خالی باشد
            TestCaseGenerationError   : خروجی LLM خالی/نامعتبر/مغایر قرارداد باشد
        """
        if not endpoints:
            raise ValueError("endpoints is empty — nothing to generate test cases for.")

        self._log.info(
            "شروع تولید تست‌کیس", title=title, endpoint_count=len(endpoints)
        )

        system_prompt = _build_system_prompt()
        user_message = _build_user_message(
            endpoints, title, version, base_url, global_security
        )
        self._log.trace(
            "prompt ساخته شد",
            system_chars=len(system_prompt),
            user_chars=len(user_message),
        )

        llm = LLMClient(user_id=user_id, agent_name=self.name)
        self._log.debug("ارسال به LLM")
        raw = llm.chat(
            user_message=user_message,
            system_prompt=system_prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        self._log.trace("پاسخ LLM دریافت شد", response_chars=len(raw or ""))

        suite = _validate_suite(_extract_json(raw), endpoints)

        total = sum(len(c["test_cases"]) for c in suite["controllers"])
        positives = sum(
            1
            for c in suite["controllers"]
            for tc in c["test_cases"]
            if tc["type"] == "positive"
        )
        self._log.info(
            "تولید تست‌کیس کامل شد",
            controllers=len(suite["controllers"]),
            total=total,
            positive=positives,
            negative=total - positives,
        )
        return suite

    # ---- حالتِ سناریو --------------------------------------------------------

    def generate_for_scenario(
        self,
        scenario: Scenario,
        steps: list[ResolvedStep],
        user_id: str,
    ) -> TestCaseSuite:
        """از یک سناریو و قدم‌های resolve شده‌ی آن، تست‌کیس تولید می‌کند.

        برخلاف حالتِ Swagger، این‌جا جریان را کاربر تعیین کرده است؛ پس اسکلتِ
        تست‌کیس‌ها قطعی ساخته می‌شود: ترتیبِ قدم‌ها، کدهای وضعیت، برداشتنِ هدرِ
        Authorization در کیسِ unauthorized و استخراجِ متغیرها همه از سناریو و
        Swagger می‌آیند، نه از LLM.

        بعد از آن، اگر enrich_with_llm روشن باشد، یک گامِ اختیاریِ غنی‌سازی اجرا
        می‌شود که فقط می‌تواند توضیحات، بدنه‌ی درخواست و assertion های فیلدِ پاسخ
        را بهتر کند. شکستِ این گام خروجی را از بین نمی‌برد.

        پرتاب می‌کند:
            ValueError : لیست قدم‌ها خالی باشد
        """
        if not steps:
            raise ValueError("steps is empty — nothing to generate test cases for.")

        self._log.info(
            "شروع تولید تست‌کیس از سناریو",
            scenario=scenario.name,
            steps=len(steps),
        )

        suite = _build_scenario_baseline(steps)

        if self._enrich_with_llm:
            self._refine_scenario_suite(scenario, steps, suite, user_id)

        total = sum(len(c["test_cases"]) for c in suite["controllers"])
        positives = sum(
            1
            for c in suite["controllers"]
            for tc in c["test_cases"]
            if tc["type"] == "positive"
        )
        self._log.info(
            "تولید تست‌کیس از سناریو کامل شد",
            scenario=scenario.name,
            steps=len(suite["controllers"]),
            total=total,
            positive=positives,
            negative=total - positives,
        )
        return suite

    def _refine_scenario_suite(
        self,
        scenario: Scenario,
        steps: list[ResolvedStep],
        suite: TestCaseSuite,
        user_id: str,
    ) -> None:
        """گامِ اختیاریِ غنی‌سازی — suite را در جا و فقط افزایشی بهتر می‌کند.

        هر خطایی (LLM، JSON نامعتبر، پاسخِ خالی) فقط لاگ می‌شود؛ اسکلتِ قطعی
        همان‌طور که هست خروجی می‌ماند.
        """
        try:
            system_prompt = _build_scenario_system_prompt()
            user_message = _build_scenario_user_message(scenario, steps, suite)
            self._log.trace(
                "prompt غنی‌سازیِ سناریو ساخته شد",
                system_chars=len(system_prompt),
                user_chars=len(user_message),
            )

            llm = LLMClient(user_id=user_id, agent_name=self.name)
            self._log.debug("ارسال سناریو به LLM برای غنی‌سازی")
            raw = llm.chat(
                user_message=user_message,
                system_prompt=system_prompt,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            refinements = _parse_refinements(raw)
            changed = _apply_refinements(steps, suite, refinements)
            self._log.info(
                "غنی‌سازیِ سناریو اعمال شد",
                suggested=len(refinements),
                applied=changed,
            )
        except Exception as exc:  # noqa: BLE001 — غنی‌سازی نباید تولید را بشکند
            self._log.warning(
                "غنی‌سازیِ سناریو نافرجام ماند؛ خروجیِ قطعی حفظ شد",
                error=str(exc),
            )
