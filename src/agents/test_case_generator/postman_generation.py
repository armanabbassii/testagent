"""
postman_generation.py — ساختِ قطعیِ Postman Collection از نتیجه‌ی قدم‌های ۱ تا ۳

قدم چهارمِ جریان HITL:

    نتیجه‌ی قدم ۱ (تست‌کیس‌های کسب‌وکاری)
  + نتیجه‌ی قدم ۲ (عملیات‌های کشف‌شده و نگاشتِ تست‌کیس به API)
  + نتیجه‌ی قدم ۳ (سناریو، ترتیبِ اجرا و وابستگی‌های داده)
        → TestCaseSuite  (قراردادِ موجودِ postman_builder)
        → PostmanBuilder → Postman Collection v2.1

این ماژول هیچ LLM ای صدا نمی‌زند و یک قدمِ کاملاً قطعی است: همان ورودی همیشه
همان کالکشن را می‌سازد. هیچ تصمیمِ کسب‌وکاریِ قدم‌های ۱ تا ۳ اینجا دوباره گرفته
نمی‌شود و هیچ چیزی که در آن سه نتیجه نباشد ساخته نمی‌شود.

کارهایی که این ماژول عمداً انجام نمی‌دهد:
  - تحلیلِ دوباره‌ی تسک، کشفِ دوباره‌ی API، دریافت/parse سندِ Swagger
  - حدسِ نگاشتِ حل‌نشده یا ساختنِ endpointِ اختراعی
  - اجرای API یا اجرای کالکشن، استفاده از توکن/اعتبارنامه‌ی واقعی
  - تبدیلِ ابهامِ حل‌نشده به assertionِ حدسی

نکته‌ی معماری: ساختارِ Postman اینجا دوباره پیاده نشده است. این ماژول یک
`TestCaseSuite` می‌سازد — همان قراردادی که `PostmanBuilder` از قبل می‌شناسد —
و ساختِ خودِ کالکشن را به آن می‌سپارد. بنابراین رفتارهای موجود (متغیرهای
baseUrl/token، «Authorization: Bearer {{token}}»، تبدیلِ {id} به :id،
assertionهای وضعیت، و استخراجِ متغیر از پاسخ) همه دست‌نخورده باقی می‌مانند.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from typing_extensions import TypedDict

from src.agents.test_case_generator.api_discovery import operation_key
from src.agents.test_case_generator.postman_builder import (
    SCHEMA_V21,
    PostmanBuildError,
    PostmanBuilder,
)
from src.agents.test_case_generator.testcase_generator import (
    DEFAULT_BASE_URL_VAR,
    Assertion,
    ControllerTestCases,
    SaveVariable,
    TestCase,
    TestCaseSuite,
)
from src.debug import DebugConfig

_DEFAULT_COLLECTION_NAME = "Generated API Test Collection"

# نامِ پوشه‌ای که تست‌کیس‌های خارج از هر سناریو را نگه می‌دارد
_UNASSIGNED_FOLDER = "Unassigned test cases"

# قراردادِ احراز هویتِ موجودِ پروژه — توکنِ واقعی هرگز ساخته نمی‌شود
_AUTH_HEADER = "Authorization"
_AUTH_VALUE = "Bearer {{token}}"

_COLLECTION_SCOPE = "collection"
_BODY_SOURCE = "body"
_HEADER_SOURCE = "header"

# متدهای معتبرِ HTTP برای اعتبارسنجیِ خروجی
_VALID_METHODS = frozenset(
    {
        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
        "COPY", "LINK", "UNLINK", "PURGE", "LOCK", "UNLOCK", "PROPFIND", "VIEW",
    }
)

# عبارت‌هایی که نشان می‌دهند خودِ تست‌کیس درخواستِ «بدون احراز هویت» را توصیف
# می‌کند. عمداً محافظه‌کارانه است: «unauthorized» یا «invalid token» اینجا نیست،
# چون آن سناریوها معمولاً هدر را می‌فرستند و فقط مقصودشان رد شدن است.
_UNAUTHENTICATED_PHRASES = (
    "without authentication",
    "without authorization",
    "without an authentication",
    "without credentials",
    "without a token",
    "without any token",
    "without token",
    "no authentication",
    "no authorization",
    "no token",
    "missing authentication",
    "missing authorization",
    "missing token",
    "unauthenticated",
    "not authenticated",
    "anonymous",
)

# {{variable}} داخل url/header/body
_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")

# کاراکترهای غیرِمجاز در نامِ متغیرِ base URL
_UNSAFE_NAME_RE = re.compile(r"[^a-z0-9]+")


class PostmanGenerationError(RuntimeError):
    """ورودیِ قدم چهارم ناقص/ناسازگار است یا کالکشنِ ساخته‌شده معتبر نیست."""


# ── مدل خروجی ────────────────────────────────────────────────────────────────

class Step4Request(TypedDict):
    """یک درخواستِ ساخته‌شده — برای بازبینیِ انسانی."""

    test_case_id: str
    title: str
    name: str
    method: str
    path: str
    scenario: str


class Step4Scenario(TypedDict):
    """یک سناریوی قدم سوم به‌همراه تعدادِ درخواست‌های ساخته‌شده‌اش."""

    id: str
    title: str
    request_count: int


class Step4Unresolved(TypedDict):
    """تست‌کیسی که به درخواست تبدیل نشد — با دلیلِ روشن."""

    test_case_id: str
    title: str
    reason: str


class Step4Result(TypedDict):
    """نتیجه‌ی کاملِ قدم چهارم: کالکشن + فراداده‌ی بازبینی."""

    collection_name: str
    collection: dict
    scenarios: list[Step4Scenario]
    requests: list[Step4Request]
    unresolved: list[Step4Unresolved]
    clarifications: list[dict]
    warnings: list[str]


# ── کمکی‌های عمومی ───────────────────────────────────────────────────────────

def _clean(value: object) -> str:
    """رشته را trim می‌کند؛ هر چیزِ دیگری رشته‌ی خالی می‌شود."""
    return value.strip() if isinstance(value, str) else ""


def _as_int(value: object) -> int | None:
    """عددِ صحیح برمی‌گرداند؛ bool عمداً رد می‌شود چون در پایتون int است."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _slug(text: str) -> str:
    """نامِ سرویس را به یک اسلاگِ امنِ متغیر تبدیل می‌کند."""
    return _UNSAFE_NAME_RE.sub("_", (text or "").strip().lower()).strip("_") or "service"


def _unique_name(name: str, used: set[str]) -> str:
    """نامی یکتا می‌سازد — برای پوشه‌هایی که ممکن است هم‌نام شوند."""
    if name not in used:
        used.add(name)
        return name
    index = 2
    while f"{name} ({index})" in used:
        index += 1
    unique = f"{name} ({index})"
    used.add(unique)
    return unique


# ── اعتبارسنجیِ ورودی ────────────────────────────────────────────────────────

def extract_test_cases(step1_result: Any) -> list[dict]:
    """تست‌کیس‌های نتیجه‌ی قدم اول را بیرون می‌کشد و شناسه‌ها را نرمال می‌کند.

    پرتاب می‌کند:
        PostmanGenerationError : نتیجه‌ی قدم اول ساختار درستی نداشته باشد
    """
    if not isinstance(step1_result, dict):
        raise PostmanGenerationError("Step 1 result must be a JSON object.")

    cases = step1_result.get("test_cases")
    if not isinstance(cases, list) or not cases:
        raise PostmanGenerationError(
            "Step 1 result does not contain any test case."
        )

    normalized: list[dict] = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise PostmanGenerationError(f"test_cases[{index}] must be an object.")
        case_id = _clean(case.get("id"))
        if not case_id:
            raise PostmanGenerationError(f"test_cases[{index}] has no 'id'.")
        if case_id in seen:
            raise PostmanGenerationError(
                f"Step 1 result contains duplicate test case id {case_id!r}."
            )
        seen.add(case_id)
        normalized.append({**case, "id": case_id})
    return normalized


def extract_services(step2_result: Any) -> list[dict]:
    """سرویس‌های کشف‌شده‌ی قدم دوم را بیرون می‌کشد."""
    if not isinstance(step2_result, dict):
        raise PostmanGenerationError("Step 2 result must be a JSON object.")

    services = step2_result.get("services")
    if not isinstance(services, list) or not services:
        raise PostmanGenerationError("Step 2 result does not contain any service.")
    return [service for service in services if isinstance(service, dict)]


def extract_mappings(step2_result: Any) -> list[dict]:
    """نگاشت‌های تست‌کیس به API را از نتیجه‌ی قدم دوم بیرون می‌کشد."""
    if not isinstance(step2_result, dict):
        raise PostmanGenerationError("Step 2 result must be a JSON object.")

    mappings = step2_result.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise PostmanGenerationError(
            "Step 2 result does not contain any test case mapping."
        )
    return [mapping for mapping in mappings if isinstance(mapping, dict)]


def extract_step3_sections(
    step3_result: Any,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """چهار بخشِ نتیجه‌ی قدم سوم را بیرون می‌کشد."""
    if not isinstance(step3_result, dict):
        raise PostmanGenerationError("Step 3 result must be a JSON object.")

    sections: list[list[dict]] = []
    for key in ("scenarios", "execution_order", "data_dependencies", "clarifications"):
        value = step3_result.get(key)
        if not isinstance(value, list):
            raise PostmanGenerationError(
                f"Step 3 result field '{key}' must be an array."
            )
        sections.append([item for item in value if isinstance(item, dict)])
    return sections[0], sections[1], sections[2], sections[3]


def validate_step3_references(
    test_cases: list[dict],
    mappings: list[dict],
    scenarios: list[dict],
    execution_order: list[dict],
    dependencies: list[dict],
) -> None:
    """ارجاع‌های نتیجه‌ی قدم سوم را به تست‌کیس‌ها و نگاشت‌های واقعی بررسی می‌کند.

    قدم پنجم هم همین تابع را صدا می‌زند تا «سازگاریِ قدم سوم» را دوباره و با
    همان قواعد بسنجد — منطقِ اعتبارسنجی یک‌جا می‌ماند.

    پرتاب می‌کند:
        PostmanGenerationError : ارجاعی به تست‌کیس/نگاشتِ ناموجود باشد
    """
    known = {_clean(case.get("id")) for case in test_cases}
    resolved = {
        _clean(mapping.get("test_case_id"))
        for mapping in mappings
        if isinstance(mapping.get("api"), dict)
    }
    # وقتی قدم دوم هیچ نگاشتی را حل نکرده باشد، هر مبدأِ وابستگی طبیعتاً
    # حل‌نشده است و فهرست‌کردنِ آن‌ها تنها علتِ واقعی را پنهان می‌کند. در آن
    # حالت خطای گویا را _plan می‌سازد: «هیچ تست‌کیسی به درخواست تبدیل
    # نشد» به‌همراهِ دلیلِ هر تست‌کیس.
    any_mapping_resolved = bool(resolved)

    errors: list[str] = []

    seen_scenarios: set[str] = set()
    for position, scenario in enumerate(scenarios):
        scenario_id = _clean(scenario.get("id"))
        if not scenario_id:
            errors.append(f"scenarios[{position}]: 'id' is missing or empty.")
        elif scenario_id in seen_scenarios:
            errors.append(
                f"scenarios[{position}]: duplicate scenario id {scenario_id!r}."
            )
        else:
            seen_scenarios.add(scenario_id)

        case_ids = scenario.get("test_case_ids")
        if not isinstance(case_ids, list):
            errors.append(f"scenarios[{position}]: 'test_case_ids' must be an array.")
            continue
        for case_id in case_ids:
            case_id = _clean(case_id)
            if case_id and case_id not in known:
                errors.append(
                    f"scenarios[{position}]: unknown test case id {case_id!r}."
                )

    for position, step in enumerate(execution_order):
        case_id = _clean(step.get("test_case_id"))
        if case_id and case_id not in known:
            errors.append(
                f"execution_order[{position}]: unknown test case id {case_id!r}."
            )

    for position, dependency in enumerate(dependencies):
        source = dependency.get("source")
        source_id = _clean(source.get("test_case_id")) if isinstance(source, dict) else ""
        if source_id and source_id not in known:
            errors.append(
                f"data_dependencies[{position}].source: unknown test case id "
                f"{source_id!r}."
            )
        elif source_id and any_mapping_resolved and source_id not in resolved:
            errors.append(
                f"data_dependencies[{position}].source: test case {source_id!r} has "
                f"no resolved API mapping in Step 2, so it cannot produce a variable."
            )
        for target in dependency.get("targets") or []:
            if not isinstance(target, dict):
                continue
            target_id = _clean(target.get("test_case_id"))
            if target_id and target_id not in known:
                errors.append(
                    f"data_dependencies[{position}].targets: unknown test case id "
                    f"{target_id!r}."
                )

    if errors:
        raise PostmanGenerationError(
            "Step 3 result does not reference the Step 1/2 results consistently:\n  - "
            + "\n  - ".join(errors)
        )


# ── تصمیم‌های قطعی ───────────────────────────────────────────────────────────

def case_text(case: dict) -> str:
    """متنِ یک تست‌کیس را برای تشخیصِ معنایی جمع می‌کند."""
    parts = [_clean(case.get("title")), _clean(case.get("expected_result"))]
    for key in ("preconditions", "steps"):
        value = case.get(key)
        if isinstance(value, list):
            parts.extend(_clean(item) for item in value)
    return " ".join(part for part in parts if part).lower()


def case_expects_no_authentication(case: dict) -> bool:
    """آیا خودِ تست‌کیس صریحاً درخواستِ بدونِ احراز هویت را توصیف می‌کند؟

    فقط عبارت‌هایی که «نبودِ» احراز هویت را می‌گویند اینجا مؤثرند. یک کیسِ منفیِ
    کسب‌وکاری (مثلاً اعتبارسنجیِ بدنه) همچنان هدرِ معتبر می‌فرستد.
    """
    text = case_text(case)
    return any(phrase in text for phrase in _UNAUTHENTICATED_PHRASES)


def operation_index(services: list[dict]) -> dict[tuple[str, str], tuple[dict, dict]]:
    """(متد، مسیرِ نرمال‌شده) → (سرویس، عملیات)."""
    index: dict[tuple[str, str], tuple[dict, dict]] = {}
    for service in services:
        for api in service.get("apis") or []:
            if not isinstance(api, dict):
                continue
            key = operation_key(_clean(api.get("method")), _clean(api.get("path")))
            index.setdefault(key, (service, api))
    return index


def mapping_for(mappings: list[dict], case_id: str) -> dict | None:
    """نگاشتِ یک تست‌کیس — اگر چند بار آمده باشد، اولی برنده است."""
    for mapping in mappings:
        if _clean(mapping.get("test_case_id")) == case_id:
            return mapping
    return None


def source_variables(dependencies: list[dict], case_id: str) -> list[SaveVariable]:
    """متغیرهایی که این تست‌کیس باید از پاسخش استخراج کند."""
    saves: list[SaveVariable] = []
    for dependency in dependencies:
        source = dependency.get("source")
        if not isinstance(source, dict):
            continue
        if _clean(source.get("test_case_id")) != case_id:
            continue
        variable = _clean(dependency.get("variable_name"))
        if not variable:
            continue
        is_header = _clean(source.get("location")) == "response.header"
        saves.append(
            SaveVariable(
                json_path=_clean(source.get("path")),
                variable=variable,
                scope=_COLLECTION_SCOPE,
                source=_HEADER_SOURCE if is_header else _BODY_SOURCE,
            )
        )
    return saves


def target_references(dependencies: list[dict], case_id: str) -> dict[str, dict[str, str]]:
    """مقادیری که این تست‌کیس از متغیرهای دیگران مصرف می‌کند، به تفکیکِ محل."""
    buckets: dict[str, dict[str, str]] = {
        "path": {}, "query": {}, "header": {}, "body": {},
    }
    for dependency in dependencies:
        variable = _clean(dependency.get("variable_name"))
        if not variable:
            continue
        reference = "{{" + variable + "}}"
        for target in dependency.get("targets") or []:
            if not isinstance(target, dict):
                continue
            if _clean(target.get("test_case_id")) != case_id:
                continue
            location = _clean(target.get("location"))
            parameter = _clean(target.get("parameter"))
            if location in buckets and parameter:
                buckets[location].setdefault(parameter, reference)
    return buckets


_PARAMETER_LOCATIONS = ("path", "query", "header")


def unfilled_required_parameters(
    operation: dict, path_params: dict, query_params: dict, headers: dict
) -> list[str]:
    """پارامترهای الزامیِ سند که قدم‌های ۱ تا ۳ مقداری برایشان تعیین نکرده‌اند.

    این‌ها در URL/هدر نمی‌آیند — چون هر مقداری برایشان حدس زدن می‌شود — اما
    پنهان هم نمی‌مانند: به‌عنوان assumption روی همان درخواست گزارش می‌شوند.
    """
    filled = {
        "path": set(path_params),
        "query": set(query_params),
        "header": {str(name).lower() for name in headers},
    }

    missing: list[str] = []
    for parameter in operation.get("parameters") or []:
        if not isinstance(parameter, dict) or not parameter.get("required"):
            continue
        location = _clean(parameter.get("in"))
        name = _clean(parameter.get("name"))
        if location not in _PARAMETER_LOCATIONS or not name:
            continue
        reference = name.lower() if location == "header" else name
        if reference not in filled[location]:
            missing.append(f"{location} '{name}'")
    return missing


def _response_statuses(operation: dict) -> list[int]:
    """کدهای وضعیتِ مستندشده‌ی یک عملیات."""
    statuses: list[int] = []
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return statuses
    for key in responses:
        text = str(key).strip()
        if text.isdigit():
            statuses.append(int(text))
    return statuses


def status_assertions(case_type: str, operation: dict) -> tuple[list[Assertion], list[str]]:
    """assertionِ کدِ وضعیت — فقط وقتی قطعی باشد.

    هیچ کدِ وضعیتی اختراع نمی‌شود: برای کیسِ مثبت فقط و فقط وقتی assert می‌شود
    که عملیات دقیقاً یک کدِ موفقِ مستند داشته باشد. کیسِ منفی و عملیاتِ چندموفق
    هیچ assertionی نمی‌گیرند و دلیلش ثبت می‌شود.

    برمی‌گرداند: (assertionها، توضیح‌هایی که باید در assumptions بیاید)
    """
    if case_type != "positive":
        return [], [
            "The expected status of a non-positive test case cannot be derived from "
            "the Step 1/2/3 results, so no status assertion was generated."
        ]

    success = sorted(status for status in _response_statuses(operation) if 200 <= status < 300)
    if len(success) == 1:
        return [Assertion(type="status_code", expected=success[0])], []
    if success:
        listed = ", ".join(str(status) for status in success)
        return [], [
            f"The operation documents several success statuses ({listed}); no status "
            f"assertion was generated because picking one would be a guess."
        ]
    return [], [
        "The operation documents no success status, so no status assertion was "
        "generated."
    ]


# ── ساختِ suite ──────────────────────────────────────────────────────────────

@dataclass
class _Outcome:
    """نتیجه‌ی میانیِ ساخت — پیش از تبدیل به Step4Result."""

    suite: TestCaseSuite
    base_url: str
    extra_variables: list[dict]
    requests: list[dict] = field(default_factory=list)
    scenario_rows: list[dict] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # جایگاهِ هر تست‌کیس در کالکشن — (شماره‌ی پوشه، شماره‌ی درخواست)
    positions: dict[str, tuple[int, int]] = field(default_factory=dict)


def _build_request_case(
    case: dict,
    case_id: str,
    operation: dict,
    service: dict,
    dependencies: list[dict],
    clarifications: list[dict],
    warnings: list[str],
) -> TestCase:
    """یک تست‌کیسِ قدم اول را به یک درخواستِ Postman تبدیل می‌کند."""
    method = _clean(operation.get("method")).upper()
    path = _clean(operation.get("path"))
    title = _clean(case.get("title"))
    case_type = _clean(case.get("type")) or "positive"
    notes: list[str] = []

    # ── احراز هویت ──────────────────────────────────────────────────────────
    headers: dict[str, str] = {}
    authorization = service.get("authorization")
    if isinstance(authorization, dict) and _clean(authorization.get("value")):
        if case_expects_no_authentication(case):
            notes.append(
                "The Authorization header is intentionally not sent: this test case "
                "describes a request made without authentication."
            )
        else:
            header = _clean(authorization.get("header")) or _AUTH_HEADER
            headers[header] = _clean(authorization.get("value")) or _AUTH_VALUE

    # ── بدنه ────────────────────────────────────────────────────────────────
    documented_body = operation.get("request_body")
    body = copy.deepcopy(documented_body) if isinstance(documented_body, dict) else None

    # ── ارجاع‌های وابستگیِ داده ─────────────────────────────────────────────
    references = target_references(dependencies, case_id)
    path_params = dict(references["path"])
    query_params = dict(references["query"])
    for header, value in references["header"].items():
        headers[header] = value

    if references["body"]:
        if body is None:
            body = {}
            notes.append(
                "Swagger documents no request body for this operation; the body "
                "carries only the fields required by the Step 3 data dependencies."
            )
        body.update(references["body"])

    for parameter in path_params:
        if f"{{{parameter}}}" not in path:
            note = (
                f"The data dependency targets path parameter '{parameter}', but the "
                f"documented path '{path}' has no such placeholder."
            )
            notes.append(note)
            warnings.append(f"{case_id}: {note}")

    unfilled = unfilled_required_parameters(operation, path_params, query_params, headers)
    if unfilled:
        notes.append(
            "The operation documents required parameter(s) "
            + ", ".join(unfilled)
            + " that the Step 1/2/3 results do not provide a value for; no value was "
            "invented for them."
        )

    # ── assertionها ─────────────────────────────────────────────────────────
    assertions, status_notes = status_assertions(case_type, operation)
    notes.extend(status_notes)

    # ── استخراجِ متغیر از پاسخ ──────────────────────────────────────────────
    save_variables = source_variables(dependencies, case_id)

    # ── ابهام‌های مربوط به همین تست‌کیس ─────────────────────────────────────
    for clarification in clarifications:
        if _clean(clarification.get("test_case_id")) != case_id:
            continue
        kind = _clean(clarification.get("type")) or "general"
        message = _clean(clarification.get("message"))
        if message:
            notes.append(f"Unresolved clarification ({kind}): {message}")

    expected_status = 0
    if assertions:
        value = assertions[0].get("expected")
        found = _as_int(value)
        if found is not None:
            expected_status = found

    return TestCase(
        name=f"{case_id} — {title}" if title else case_id,
        type=case_type,
        method=method,
        path=path,
        description=_clean(case.get("expected_result")),
        headers=headers,
        query_params=query_params,
        path_params=path_params,
        body=body,
        expected_status=expected_status,
        assertions=assertions,
        save_variables=save_variables,
        assumptions=notes,
        base_url_var="",
    )


def _plan(
    test_cases: list[dict],
    scenarios: list[dict],
    execution_order: list[dict],
    dependencies: list[dict],
    clarifications: list[dict],
    services: list[dict],
    mappings: list[dict],
) -> _Outcome:
    """تست‌کیس‌ها را به درخواست و پوشه تبدیل می‌کند — بدونِ ساختِ کالکشن."""
    index = operation_index(services)
    # جایگاهِ هر تست‌کیس در فهرستِ قدم اول. نامِ `case_position` عمداً با
    # `position` فرق دارد: `sort_key` به این دیکشنری بسته است و حلقه‌های بعدیِ
    # همین تابع (مثلِ حلقه‌ی سرویس‌ها) نامِ `position` را با یک int بازنویسی
    # می‌کنند — closure متغیر را می‌گیرد نه مقدارش، پس آن بازنویسی sort_key را
    # می‌شکند.
    case_position = {
        _clean(case.get("id")): order for order, case in enumerate(test_cases)
    }

    order_of: dict[str, int] = {}
    for step in execution_order:
        case_id = _clean(step.get("test_case_id"))
        value = _as_int(step.get("order"))
        if case_id and value is not None:
            order_of[case_id] = value

    fallback = len(test_cases)

    def sort_key(case_id: str) -> tuple[int, int]:
        return (order_of.get(case_id, fallback), case_position.get(case_id, fallback))

    outcome = _Outcome(suite=TestCaseSuite(controllers=[]), base_url="", extra_variables=[])

    built: dict[str, tuple[TestCase, dict]] = {}
    used_services: list[dict] = []

    for case in test_cases:
        case_id = _clean(case.get("id"))
        title = _clean(case.get("title"))

        mapping = mapping_for(mappings, case_id)
        api = mapping.get("api") if isinstance(mapping, dict) else None
        if not isinstance(api, dict):
            outcome.unresolved.append(
                {
                    "test_case_id": case_id,
                    "title": title,
                    "reason": (
                        "Step 2 did not resolve an API for this test case, so no "
                        "request was generated."
                    ),
                }
            )
            continue

        method = _clean(api.get("method"))
        path = _clean(api.get("path"))
        if not method or not path:
            outcome.unresolved.append(
                {
                    "test_case_id": case_id,
                    "title": title,
                    "reason": "The Step 2 mapping has no method or path.",
                }
            )
            continue

        service, operation = index.get(operation_key(method, path), (None, None))
        if service is None or operation is None:
            outcome.unresolved.append(
                {
                    "test_case_id": case_id,
                    "title": title,
                    "reason": (
                        f"The operation {method} {path} was not found among the APIs "
                        f"discovered in Step 2."
                    ),
                }
            )
            continue

        base_url = _clean(service.get("base_url"))
        if not base_url:
            outcome.unresolved.append(
                {
                    "test_case_id": case_id,
                    "title": title,
                    "reason": (
                        f"Service {_clean(service.get('name')) or '(unnamed)'!r} has no "
                        f"base URL in Step 2, so the request URL cannot be built."
                    ),
                }
            )
            continue

        built[case_id] = (
            _build_request_case(
                case, case_id, operation, service, dependencies, clarifications,
                outcome.warnings,
            ),
            service,
        )
        if not any(service is used for used in used_services):
            used_services.append(service)

    if not built:
        if outcome.unresolved:
            detail = "\n  - ".join(
                f"{item['test_case_id']}: {item['reason']}"
                for item in outcome.unresolved
            )
            message = (
                "No test case could be turned into a Postman request:\n  - " + detail
            )
        else:
            message = "No test case has a resolved API mapping in Step 2."
        raise PostmanGenerationError(message)

    # ── متغیرهای base URL ───────────────────────────────────────────────────
    # یک منبع: همه‌ی درخواست‌ها {{baseUrl}} می‌گیرند — مثلِ رفتارِ قبلیِ پروژه.
    # چند منبع: سرویسِ اول {{baseUrl}} و بقیه {{baseUrl_<service>}}؛ بنابراین هر
    # درخواست میزبانِ درستِ خودش را می‌گیرد و هیچ متغیری بی‌استفاده نمی‌ماند.
    var_of: dict[int, str] = {}
    used_var_names = {DEFAULT_BASE_URL_VAR}

    for position, service in enumerate(used_services):
        if position == 0:
            var_of[id(service)] = DEFAULT_BASE_URL_VAR
            continue
        base = f"{DEFAULT_BASE_URL_VAR}_{_slug(_clean(service.get('name')))}"
        unique = base
        index = 2
        while unique in used_var_names:
            unique = f"{base}_{index}"
            index += 1
        if unique != base:
            outcome.warnings.append(
                f"Two services would share the base URL variable {base!r}; the "
                f"second one was renamed to {unique!r}."
            )
        used_var_names.add(unique)
        var_of[id(service)] = unique

    outcome.base_url = _clean(used_services[0].get("base_url"))
    if len(used_services) > 1:
        outcome.extra_variables = [
            {"key": var_of[id(service)], "value": _clean(service.get("base_url"))}
            for service in used_services[1:]
        ]

    for request_case, service in built.values():
        request_case["base_url_var"] = var_of[id(service)]

    # ── پوشه‌ها: هر سناریو یک پوشه، به ترتیبِ خودِ قدم سوم ────────────────────
    assignment: dict[str, str] = {}
    scenario_titles: dict[str, str] = {}
    for scenario in scenarios:
        scenario_id = _clean(scenario.get("id"))
        scenario_titles[scenario_id] = _clean(scenario.get("title")) or scenario_id
        for case_id in scenario.get("test_case_ids") or []:
            case_id = _clean(case_id)
            if case_id in built and case_id not in assignment:
                assignment[case_id] = scenario_id

    controllers: list[ControllerTestCases] = []
    used_names: set[str] = set()
    folder_of: dict[str, str] = {}

    def add_folder(name: str, members: list[str]) -> None:
        """یک پوشه اضافه می‌کند و جایگاهِ اعضایش را ثبت می‌کند."""
        folder_index = len(controllers)
        controllers.append(
            ControllerTestCases(
                name=name, test_cases=[built[case_id][0] for case_id in members]
            )
        )
        for item_index, case_id in enumerate(members):
            folder_of[case_id] = name
            outcome.positions[case_id] = (folder_index, item_index)

    for scenario in scenarios:
        scenario_id = _clean(scenario.get("id"))
        members = sorted(
            (case_id for case_id, owner in assignment.items() if owner == scenario_id),
            key=sort_key,
        )
        if not members:
            continue
        add_folder(_unique_name(scenario_titles[scenario_id], used_names), members)
        outcome.scenario_rows.append(
            {
                "id": scenario_id,
                "title": scenario_titles[scenario_id],
                "request_count": len(members),
            }
        )

    leftovers = sorted(
        (case_id for case_id in built if case_id not in assignment), key=sort_key
    )
    if leftovers:
        add_folder(_unique_name(_UNASSIGNED_FOLDER, used_names), leftovers)

    outcome.suite = TestCaseSuite(controllers=controllers)

    # فهرستِ درخواست‌ها به ترتیبِ خودِ قدم اول تا بازبین بتواند آن را با ورودی
    # مقایسه کند — نه به ترتیبِ کالکشن.
    for case in test_cases:
        case_id = _clean(case.get("id"))
        if case_id not in built:
            continue
        request_case = built[case_id][0]
        outcome.requests.append(
            {
                "test_case_id": case_id,
                "title": _clean(case.get("title")),
                "name": request_case["name"],
                "method": request_case["method"],
                "path": request_case["path"],
                "scenario": folder_of.get(case_id, ""),
            }
        )

    _check_dependency_order(dependencies, outcome)
    return outcome


def _check_dependency_order(dependencies: list[dict], outcome: _Outcome) -> None:
    """بررسی می‌کند متغیرِ هر وابستگی پیش از مصرف‌شدن استخراج شود.

    ترتیبِ درخواست‌ها تصمیمِ قدم سوم است و اینجا بازچینی نمی‌شود؛ اگر وابستگی‌ای
    با آن ترتیب ناسازگار باشد، به‌صورت هشدار و assumption گزارش می‌شود تا بازبین
    بتواند تصمیم بگیرد.
    """
    positions = outcome.positions

    for dependency in dependencies:
        source = dependency.get("source")
        if not isinstance(source, dict):
            continue
        source_id = _clean(source.get("test_case_id"))
        variable = _clean(dependency.get("variable_name"))
        if not source_id or not variable:
            continue

        targets = [
            _clean(target.get("test_case_id"))
            for target in dependency.get("targets") or []
            if isinstance(target, dict)
        ]

        if source_id not in positions:
            # مبدأ اصلاً درخواستی ندارد (مثلاً نگاشتش حل نشده) → متغیر هرگز
            # ست نمی‌شود؛ این باید دیده شود، نه اینکه بی‌صدا رد شود.
            consumers = sorted({item for item in targets if item in positions})
            if consumers:
                outcome.warnings.append(
                    f"{source_id!r} has no request in this collection, so "
                    f"'{{{{{variable}}}}}' is never extracted; it is consumed by "
                    + ", ".join(repr(item) for item in consumers)
                    + "."
                )
            continue

        for target_id in targets:
            if not target_id or target_id not in positions:
                continue
            if positions[target_id] >= positions[source_id]:
                continue
            note = (
                f"The Step 3 execution order runs {target_id!r} before {source_id!r}, "
                f"but {target_id!r} consumes '{{{{{variable}}}}}' which {source_id!r} "
                f"extracts; the variable is not set when the request runs."
            )
            outcome.warnings.append(note)
            outcome.suite["controllers"][positions[target_id][0]]["test_cases"][
                positions[target_id][1]
            ]["assumptions"].append(note)


# ── اعتبارسنجیِ کالکشن ───────────────────────────────────────────────────────

def iter_collection_requests(collection: dict):
    """همه‌ی درخواست‌های برگِ کالکشن را پیمایش می‌کند.

    اسمِ عمومی است چون قدم پنجم برای بازبینیِ پوششِ تست‌کیس‌ها به همان پیمایشِ
    قدم چهارم نیاز دارد.
    """
    for folder in collection.get("item") or []:
        if not isinstance(folder, dict):
            continue
        for item in folder.get("item") or []:
            if isinstance(item, dict):
                yield item


def validate_collection(collection: Any) -> list[str]:
    """کالکشنِ ساخته‌شده را از نظرِ ساختارِ Postman بررسی می‌کند.

    برمی‌گرداند: فهرستِ مشکلات — فهرستِ خالی یعنی کالکشن معتبر است.
    """
    if not isinstance(collection, dict):
        return ["the generated collection is not a JSON object."]

    problems: list[str] = []

    info = collection.get("info")
    if not isinstance(info, dict):
        problems.append("'info' is missing or is not an object.")
    else:
        if not _clean(info.get("name")):
            problems.append("'info.name' is missing or empty.")
        if _clean(info.get("schema")) != SCHEMA_V21:
            problems.append(f"'info.schema' must be {SCHEMA_V21}.")

    variables = collection.get("variable")
    if not isinstance(variables, list):
        problems.append("'variable' must be an array.")
    else:
        keys: list[str] = []
        for variable in variables:
            if not isinstance(variable, dict):
                problems.append("a collection variable is not an object.")
                continue
            key = _clean(variable.get("key"))
            if not key:
                problems.append("a collection variable has no key.")
                continue
            if key in keys:
                problems.append(f"duplicate collection variable {key!r}.")
            keys.append(key)
        if DEFAULT_BASE_URL_VAR not in keys:
            problems.append(f"the collection does not define {DEFAULT_BASE_URL_VAR!r}.")

    items = collection.get("item")
    if not isinstance(items, list) or not items:
        problems.append("'item' must be a non-empty array.")
    else:
        names: list[str] = []
        for folder in items:
            if not isinstance(folder, dict):
                problems.append("a top-level item is not an object.")
                continue
            folder_name = _clean(folder.get("name"))
            if not folder_name:
                problems.append("a folder has no name.")
            cases = folder.get("item")
            if not isinstance(cases, list):
                problems.append(f"folder {folder_name!r} has no 'item' array.")
                continue
            for item in cases:
                if not isinstance(item, dict):
                    problems.append(f"folder {folder_name!r} contains a non-object item.")
                    continue
                name = _clean(item.get("name"))
                if not name:
                    problems.append(f"a request in folder {folder_name!r} has no name.")
                elif name in names:
                    problems.append(f"duplicate request name {name!r}.")
                else:
                    names.append(name)

                request = item.get("request")
                if not isinstance(request, dict):
                    problems.append(f"request {name!r} has no 'request' object.")
                    continue
                method = _clean(request.get("method")).upper()
                if method not in _VALID_METHODS:
                    problems.append(f"request {name!r} has an invalid method {method!r}.")
                url = request.get("url")
                if not isinstance(url, dict):
                    problems.append(f"request {name!r} has no 'url' object.")
                    continue
                if not _clean(url.get("raw")):
                    problems.append(f"request {name!r} has an empty URL.")
                if not url.get("host"):
                    problems.append(f"request {name!r} has no URL host.")

    try:
        json.dumps(collection, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        problems.append(f"the collection cannot be serialized to JSON: {exc}")

    return problems


def template_variables(value: Any) -> set[str]:
    """نامِ همه‌ی {{variable}} های داخلِ یک مقدار (رشته/لیست/dict)."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(_TEMPLATE_VAR_RE.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            found.update(template_variables(item))
    elif isinstance(value, list):
        for item in value:
            found.update(template_variables(item))
    return found


def check_variables_are_available(
    collection: dict, dependency_variables: set[str]
) -> list[str]:
    """هر {{variable}} مصرف‌شده باید تعریف‌شده باشد — یا متغیرِ کالکشن یا
    متغیری که یک اسکریپتِ استخراج در همین کالکشن می‌سازد."""
    declared = {
        _clean(variable.get("key"))
        for variable in collection.get("variable") or []
        if isinstance(variable, dict)
    }
    available = declared | set(dependency_variables)

    problems: list[str] = []
    for item in iter_collection_requests(collection):
        request = item.get("request")
        if not isinstance(request, dict):
            continue
        used = template_variables(request.get("url")) | template_variables(
            request.get("header")
        )
        used |= template_variables(request.get("body"))
        for variable in sorted(used - available):
            problems.append(
                f"request {_clean(item.get('name'))!r} uses {{{{{variable}}}}} which "
                f"is neither a collection variable nor extracted by any request."
            )
    return problems


# ── تولیدکننده ───────────────────────────────────────────────────────────────

class Step4PostmanGenerator:
    """قدم چهارم: تبدیلِ قطعیِ نتیجه‌ی قدم‌های ۱ تا ۳ به یک Postman Collection.

    پارامترها:
        debug_config    : تنظیماتِ لاگِ پروژه
        collection_name : نامِ کالکشن؛ خالی باشد از خلاصه‌ی تسکِ قدم اول ساخته
                          می‌شود.
        postman_builder : برای تزریق در تست‌ها
    """

    name = "step4_postman_generation"

    def __init__(
        self,
        debug_config: DebugConfig | None = None,
        collection_name: str = "",
        postman_builder: PostmanBuilder | None = None,
    ) -> None:
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
        self._collection_name = _clean(collection_name)
        self._builder = postman_builder or PostmanBuilder(debug_config)

    def generate(
        self,
        step1_result: Any,
        step2_result: Any,
        step3_result: Any,
    ) -> Step4Result:
        """کالکشنِ Postman را از سه نتیجه‌ی قبلی می‌سازد.

        هیچ API ای اجرا نمی‌شود، هیچ سندِ Swagger ای خوانده نمی‌شود و هیچ LLM ای
        صدا زده نمی‌شود.

        پرتاب می‌کند:
            PostmanGenerationError : ورودی ناسازگار یا کالکشنِ نامعتبر
        """
        test_cases = extract_test_cases(step1_result)
        services = extract_services(step2_result)
        mappings = extract_mappings(step2_result)
        scenarios, execution_order, dependencies, clarifications = (
            extract_step3_sections(step3_result)
        )
        validate_step3_references(
            test_cases, mappings, scenarios, execution_order, dependencies
        )

        outcome = _plan(
            test_cases, scenarios, execution_order, dependencies, clarifications,
            services, mappings,
        )

        collection_name = self._collection_name or self._collection_name_from(step1_result)
        self._log.info(
            "شروع ساختِ کالکشنِ Postman",
            collection_name=collection_name,
            requests=len(outcome.requests),
            unresolved=len(outcome.unresolved),
        )

        try:
            collection = self._builder.build(
                suite=outcome.suite,
                base_url=outcome.base_url,
                collection_name=collection_name,
                extra_variables=outcome.extra_variables,
                description=_collection_description(
                    outcome.unresolved, clarifications, outcome.warnings
                ),
            )
        except PostmanBuildError as exc:
            raise PostmanGenerationError(str(exc)) from exc

        problems = validate_collection(collection)
        dependency_variables = {
            _clean(dependency.get("variable_name"))
            for dependency in dependencies
            if _clean(dependency.get("variable_name"))
        }
        problems.extend(check_variables_are_available(collection, dependency_variables))
        if problems:
            raise PostmanGenerationError(
                "The generated Postman collection is not valid:\n  - "
                + "\n  - ".join(problems)
            )

        if outcome.warnings:
            self._log.warning(
                "هشدارهایی در ساختِ کالکشن", warnings=len(outcome.warnings)
            )
        self._log.info(
            "ساختِ کالکشن کامل شد",
            folders=len(collection["item"]),
            requests=len(outcome.requests),
            unresolved=len(outcome.unresolved),
        )

        return Step4Result(
            collection_name=collection_name,
            collection=collection,
            scenarios=outcome.scenario_rows,
            requests=outcome.requests,
            unresolved=outcome.unresolved,
            clarifications=clarifications,
            warnings=outcome.warnings,
        )

    @staticmethod
    def _collection_name_from(step1_result: Any) -> str:
        """نامِ کالکشن از خلاصه‌ی تسکِ قدم اول — مثلِ قراردادِ موجودِ پروژه."""
        summary = _clean((step1_result or {}).get("task_summary")) if isinstance(
            step1_result, dict
        ) else ""
        if not summary:
            return _DEFAULT_COLLECTION_NAME
        first_line = summary.splitlines()[0].strip().rstrip(".")
        if len(first_line) > 80:
            first_line = first_line[:77].rstrip() + "..."
        return f"{first_line} API Tests" if first_line else _DEFAULT_COLLECTION_NAME


def _collection_description(
    unresolved: list[dict], clarifications: list[dict], warnings: list[str]
) -> str:
    """توضیحاتِ سطحِ کالکشن — ابهام‌ها و درخواست‌های ساخته‌نشده را پنهان نمی‌کند."""
    lines = [
        "Generated deterministically from the Step 1 (task analysis), Step 2 (API "
        "discovery and mapping) and Step 3 (scenario and dependency analysis) results."
    ]

    if clarifications:
        lines.append("")
        lines.append(
            "Unresolved clarifications — no assertion was guessed for these:"
        )
        for item in clarifications:
            kind = _clean(item.get("type")) or "general"
            case_id = _clean(item.get("test_case_id"))
            message = _clean(item.get("message"))
            prefix = f"{kind} ({case_id})" if case_id else kind
            lines.append(f"- {prefix}: {message}")

    if unresolved:
        lines.append("")
        lines.append("Test cases without a generated request:")
        for item in unresolved:
            lines.append(f"- {item['test_case_id']}: {item['reason']}")

    if warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in warnings)

    return "\n".join(lines)


__all__ = [
    "PostmanGenerationError",
    "Step4PostmanGenerator",
    "Step4Request",
    "Step4Result",
    "Step4Scenario",
    "Step4Unresolved",
    "case_expects_no_authentication",
    "case_text",
    "check_variables_are_available",
    "extract_mappings",
    "extract_services",
    "extract_step3_sections",
    "extract_test_cases",
    "iter_collection_requests",
    "mapping_for",
    "operation_index",
    "source_variables",
    "status_assertions",
    "target_references",
    "template_variables",
    "unfilled_required_parameters",
    "validate_collection",
    "validate_step3_references",
]
