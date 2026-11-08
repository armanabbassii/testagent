"""
test_design.py — اعتبارسنجیِ قطعیِ «طراحیِ تست» هر تست‌کیس

قدم سوم علاوه بر سناریو و وابستگی می‌تواند برای هر تست‌کیس یک طراحیِ تست
پیشنهاد دهد (بخشِ اختیاریِ «test_design»):

  * request_values : مقدارِ مشخصی که باید در path/query/header/body فرستاده شود،
                     همراهِ منبعِ آن مقدار (value_source)؛
  * assertions     : بررسی‌های پاسخ که مستقیماً از نیازمندی می‌آیند؛
  * pending        : تست‌کیسی که رفتارِ موردِ انتظارش هنوز روشن نیست؛
  * unverified_expectations : انتظاری که با قرارداد/داده‌ی موجود قابلِ اثبات
                     نیست و باید به‌عنوانِ محدودیت گزارش شود.

قاعده‌ی اصلی همان قاعده‌ی کلِ جریان است: LLM پیشنهاد می‌دهد و این ماژول — بدونِ
هیچ LLM، شبکه یا فایل — آن را در برابرِ عملیاتِ واقعیِ Swagger می‌سنجد. هر مقدار
یا assertionی که قابلِ اثبات نباشد کنار گذاشته و به یک ابهام تبدیل می‌شود؛ هیچ
مقداری حدس زده نمی‌شود و هیچ خطایی کلِ جریان را متوقف نمی‌کند.

همین تابع در قدم سوم (پس از پاسخِ LLM)، قدم چهارم (پیش از ساختِ درخواست — چون
نتیجه‌ی قدم سوم در UI قابلِ ویرایش است) و قدم پنجم استفاده می‌شود؛ خروجیِ آن
idempotent است: نرمال‌کردنِ دوباره‌ی یک طراحیِ معتبر همان را برمی‌گرداند.

این ماژول به هیچ endpoint، نامِ فیلد یا تسکِ خاصی وابسته نیست.
"""

from __future__ import annotations

import math
import re
from typing_extensions import NotRequired, TypedDict

# ── واژگانِ قرارداد ─────────────────────────────────────────────────────────

LOCATIONS = ("path", "query", "header", "body")

SOURCE_SCHEMA_DEFAULT = "schema_default"
SOURCE_USER_OR_REQUIREMENT = "user_or_requirement"
SOURCE_SCHEMA_CONSTRAINT = "schema_constraint"
SOURCE_DEPENDENCY = "dependency"
SOURCE_NONEXISTENT_ID = "nonexistent_id"
SOURCE_RUNTIME_VARIABLE = "runtime_variable"
VALUE_SOURCES = (
    SOURCE_SCHEMA_DEFAULT,
    SOURCE_USER_OR_REQUIREMENT,
    SOURCE_SCHEMA_CONSTRAINT,
    SOURCE_DEPENDENCY,
    SOURCE_NONEXISTENT_ID,
    SOURCE_RUNTIME_VARIABLE,
)

KIND_STATUS = "status"
KIND_STATUS_CLASS = "status_class"
KIND_FIELD_PRESENT = "field_present"
KIND_FIELD_ABSENT = "field_absent"
KIND_FIELD_EQUALS = "field_equals"
KIND_FIELD_TYPE = "field_type"
KIND_ARRAY_LENGTH = "array_length"
ASSERTION_KINDS = (
    KIND_STATUS,
    KIND_STATUS_CLASS,
    KIND_FIELD_PRESENT,
    KIND_FIELD_ABSENT,
    KIND_FIELD_EQUALS,
    KIND_FIELD_TYPE,
    KIND_ARRAY_LENGTH,
)
_FIELD_KINDS = (
    KIND_FIELD_PRESENT,
    KIND_FIELD_ABSENT,
    KIND_FIELD_EQUALS,
    KIND_FIELD_TYPE,
    KIND_ARRAY_LENGTH,
)

PATH_SOURCE_SWAGGER = "swagger"
PATH_SOURCES = (PATH_SOURCE_SWAGGER, "requirement", "attachment", "user")

SCOPE_PATH = "path"
SCOPE_ANYWHERE = "anywhere"

STATUS_CLASS_SUCCESS = "2xx"
STATUS_CLASS_CLIENT_ERROR = "4xx"
STATUS_CLASSES = (STATUS_CLASS_SUCCESS, STATUS_CLASS_CLIENT_ERROR)

VALUE_TYPES = ("string", "number", "integer", "boolean", "object", "array", "null")

ROOT_PATH = "$"

# نوعِ ابهام‌هایی که این ماژول می‌سازد (قراردادِ Clarificationِ قدم سوم)
CLARIFICATION_TEST_DATA = "test_data"
CLARIFICATION_ASSERTION = "assertion"
CLARIFICATION_PENDING = "pending_expectation"

_RUNTIME_VARIABLE_RE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")
# یک بخشِ مسیرِ پاسخ: نامِ کلید و حداکثر یک «[]» پشتِ آن
_PATH_SEGMENT_RE = re.compile(r"^[^.\[\]\s]+(\[\])?$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_INTEGER_RE = re.compile(r"^-?\d+$")
_INT_BOUNDS = {"int32": (-(2**31), 2**31 - 1), "int64": (-(2**63), 2**63 - 1)}


# ── مدل داده ─────────────────────────────────────────────────────────────────


class RequestValue(TypedDict):
    """یک مقدارِ ورودیِ مشخص برای درخواستِ یک تست‌کیس."""

    location: str
    name: str
    value: object
    value_source: str
    evidence: NotRequired[str]


class DesignAssertion(TypedDict):
    """یک بررسیِ پاسخ که از نیازمندی می‌آید."""

    kind: str
    json_path: NotRequired[str]
    scope: NotRequired[str]
    path_source: NotRequired[str]
    expected: NotRequired[object]
    min: NotRequired[int]
    max: NotRequired[int]
    value_type: NotRequired[str]
    requirement_ref: NotRequired[str]
    evidence: NotRequired[str]


class UnverifiedExpectation(TypedDict):
    """انتظاری که با قرارداد یا داده‌ی موجود قابلِ اثبات نیست."""

    expectation: str
    reason: str


class TestDesign(TypedDict):
    """طراحیِ تستِ نرمال‌شده‌ی یک تست‌کیس."""

    test_case_id: str
    request_values: list[RequestValue]
    assertions: list[DesignAssertion]
    pending: bool
    pending_reason: str
    unverified_expectations: list[UnverifiedExpectation]


# ── کمکی‌ها ──────────────────────────────────────────────────────────────────


def _clean(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _number(value: object) -> float | None:
    """مقدارِ عددی — عدد یا رشته‌ی عددی؛ bool عدد حساب نمی‌شود."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _clarification(kind: str, case_id: str, message: str) -> dict:
    return {"type": kind, "test_case_id": case_id, "message": message}


def operation_parameter(operation: dict, location: str, name: str) -> dict | None:
    """پارامترِ مستندِ عملیات در یک محل؛ نامِ هدر حساس به حروف نیست."""
    for parameter in operation.get("parameters") or []:
        if not isinstance(parameter, dict):
            continue
        if _clean(parameter.get("in")) != location:
            continue
        documented = _clean(parameter.get("name"))
        if location == "header":
            if documented.lower() == name.lower():
                return parameter
        elif documented == name:
            return parameter
    return None


def _body_has_field(body: object, field_path: str) -> bool:
    node = body
    for part in field_path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def normalize_json_path(path: str) -> str:
    """«$.a.b» → «a.b»؛ «$» → «$»."""
    text = _clean(path)
    if text in ("$", "$."):
        return ROOT_PATH
    if text.startswith("$."):
        return text[2:]
    if text.startswith("$["):
        return text[1:]
    return text


def path_segments_valid(path: str) -> bool:
    """مسیرِ نقطه‌ای با حداکثر یک «[]»؛ مسیرِ ریشه‌ی آرایه («[].x») هم معتبر است."""
    if path == ROOT_PATH:
        return True
    if path.count("[]") > 1:
        return False
    body = path
    if body.startswith("[]"):
        if not body.startswith("[]."):
            return False
        body = body[3:]
    return bool(body) and all(_PATH_SEGMENT_RE.match(part) for part in body.split("."))


def documented_response_paths(operation: dict) -> set[str]:
    """مسیرهای مستندِ پاسخ‌های موفق (فیلدهای سطحِ اول + مسیرهای تودرتو)."""
    paths: set[str] = set()
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return paths
    for status, response in responses.items():
        if not str(status).strip().startswith("2") or not isinstance(response, dict):
            continue
        for key in ("fields", "field_paths"):
            for item in response.get(key) or []:
                if _clean(item):
                    paths.add(_clean(item))
    return paths


def _path_is_documented(path: str, documented: set[str]) -> bool:
    if path == ROOT_PATH:
        return any(item.startswith("[]") for item in documented)
    if path in documented:
        return True
    # مسیرِ آرایه (مثلاً «result.items») وقتی مستند است که عضوی زیرِ آن مستند باشد
    return any(item.startswith(path + "[]") for item in documented)


def documented_statuses(operation: dict) -> set[int]:
    statuses: set[int] = set()
    responses = operation.get("responses")
    if isinstance(responses, dict):
        for key in responses:
            text = str(key).strip()
            if text.isdigit():
                statuses.add(int(text))
    return statuses


def is_rejection(assertion: dict) -> bool:
    """assertionی که ردِ درخواست توسطِ سرور (خطای سمتِ کلاینت) را بررسی می‌کند."""
    if assertion.get("kind") == KIND_STATUS_CLASS:
        return assertion.get("expected") == STATUS_CLASS_CLIENT_ERROR
    if assertion.get("kind") == KIND_STATUS:
        expected = assertion.get("expected")
        return isinstance(expected, int) and 400 <= expected < 500
    return False


# ── بررسیِ نوع و قیدِ مقدار ──────────────────────────────────────────────────


def type_problem(value: object, parameter: dict) -> str:
    """ناسازگاریِ مقدار با نوع/فرمتِ مستند — رشته‌ی خالی یعنی سازگار."""
    declared = _clean(parameter.get("type")).lower()
    constraints = (
        parameter.get("constraints") if isinstance(parameter.get("constraints"), dict) else {}
    )
    fmt = _clean(constraints.get("format")).lower()
    if value is None or (isinstance(value, str) and not value.strip()):
        return "the value is empty"
    if declared == "integer":
        text = str(value).strip() if not isinstance(value, bool) else ""
        if isinstance(value, float) and value.is_integer():
            text = str(int(value))
        if not _INTEGER_RE.match(text):
            return f"the documented type is integer but the value is {value!r}"
        bounds = _INT_BOUNDS.get(fmt)
        if bounds and not bounds[0] <= int(text) <= bounds[1]:
            return f"the value {text} is outside the {fmt} range"
        return ""
    if declared == "number":
        if _number(value) is None:
            return f"the documented type is number but the value is {value!r}"
        return ""
    if declared == "boolean":
        if isinstance(value, bool) or str(value).strip().lower() in ("true", "false"):
            return ""
        return f"the documented type is boolean but the value is {value!r}"
    if fmt == "uuid" and not _UUID_RE.match(str(value).strip()):
        return f"the documented format is uuid but the value is {value!r}"
    return ""


def constraint_violations(value: object, constraints: dict) -> list[str]:
    """قیدهای مستندی که این مقدار نقض می‌کند."""
    violations: list[str] = []
    number = _number(value)
    if number is not None:
        minimum = _number(constraints.get("minimum"))
        maximum = _number(constraints.get("maximum"))
        exclusive_min = constraints.get("exclusiveMinimum")
        exclusive_max = constraints.get("exclusiveMaximum")
        if minimum is not None:
            if exclusive_min is True and number <= minimum:
                violations.append(f"exclusive minimum {constraints['minimum']}")
            elif number < minimum:
                violations.append(f"minimum {constraints['minimum']}")
        if maximum is not None:
            if exclusive_max is True and number >= maximum:
                violations.append(f"exclusive maximum {constraints['maximum']}")
            elif number > maximum:
                violations.append(f"maximum {constraints['maximum']}")
        for key, compare in (("exclusiveMinimum", "le"), ("exclusiveMaximum", "ge")):
            limit = constraints.get(key)
            limit_number = _number(limit) if not isinstance(limit, bool) else None
            if limit_number is None:
                continue
            if (compare == "le" and number <= limit_number) or (
                compare == "ge" and number >= limit_number
            ):
                violations.append(f"{key} {limit}")
    if isinstance(value, str):
        min_length = _non_negative_int(constraints.get("minLength"))
        max_length = _non_negative_int(constraints.get("maxLength"))
        if min_length is not None and len(value) < min_length:
            violations.append(f"minLength {min_length}")
        if max_length is not None and len(value) > max_length:
            violations.append(f"maxLength {max_length}")
        pattern = constraints.get("pattern")
        if isinstance(pattern, str) and pattern:
            try:
                if not re.search(pattern, value):
                    violations.append(f"pattern {pattern}")
            except re.error:
                pass
    enum = constraints.get("enum")
    if isinstance(enum, list) and enum:
        if str(value) not in {str(item) for item in enum}:
            violations.append("enum " + ", ".join(str(item) for item in enum))
    return violations


def _boundary_problem(value: object, constraints: dict) -> str:
    """مقدارِ «schema_constraint» باید واقعاً روی مرز یا آن‌سوی یک قیدِ مستند باشد."""
    number = _number(value)
    numeric_limits = [
        _number(constraints.get(key))
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
        if not isinstance(constraints.get(key), bool)
    ]
    numeric_limits = [limit for limit in numeric_limits if limit is not None]
    if number is not None and numeric_limits:
        for limit in numeric_limits:
            if number in (limit, limit - 1, limit + 1):
                return ""
        if constraint_violations(value, constraints):
            return ""
        listed = ", ".join(f"{limit:g}" for limit in numeric_limits)
        return f"the value {value!r} is not on or beyond a documented limit ({listed})"
    length_limits = [_non_negative_int(constraints.get(key)) for key in ("minLength", "maxLength")]
    length_limits = [limit for limit in length_limits if limit is not None]
    if isinstance(value, str) and length_limits:
        if len(value) in {n for limit in length_limits for n in (limit, limit - 1, limit + 1)}:
            return ""
        if constraint_violations(value, constraints):
            return ""
        return f"the length of {value!r} is not on or beyond a documented length limit"
    if isinstance(constraints.get("enum"), list) and constraints.get("enum"):
        return ""
    return "the parameter documents no constraint, so the value cannot be a boundary"


# ── نرمال‌سازی ───────────────────────────────────────────────────────────────


def _dependency_targets(dependencies: list[dict], case_id: str) -> set[tuple[str, str]]:
    targets: set[tuple[str, str]] = set()
    for dependency in dependencies or []:
        if not isinstance(dependency, dict):
            continue
        for target in dependency.get("targets") or []:
            if isinstance(target, dict) and _clean(target.get("test_case_id")) == case_id:
                location = _clean(target.get("location"))
                parameter = _clean(target.get("parameter"))
                targets.add((location, parameter.lower() if location == "header" else parameter))
    return targets


def _normalize_value(
    raw: object,
    case_id: str,
    operation: dict,
    dependency_targets: set[tuple[str, str]],
    notes: list[dict],
) -> tuple[RequestValue | None, str]:
    """(مقدارِ معتبر یا None، نقضِ قیدِ مستند در صورتِ وجود)."""
    where = f"{case_id}: request value"
    if not isinstance(raw, dict):
        notes.append(
            _clarification(CLARIFICATION_TEST_DATA, case_id, f"{where} is not an object; ignored.")
        )
        return None, ""
    location = _clean(raw.get("location")).lower()
    name = _clean(raw.get("name"))
    source = _clean(raw.get("value_source"))
    evidence = _clean(raw.get("evidence"))
    value = raw.get("value")
    label = f"{location} '{name}'" if name else location or "(no location)"

    def reject(reason: str) -> tuple[None, str]:
        notes.append(
            _clarification(
                CLARIFICATION_TEST_DATA,
                case_id,
                f"The proposed value for {label} was not used: {reason}.",
            )
        )
        return None, ""

    if location not in LOCATIONS or not name:
        return reject("the location must be path, query, header or body and the name must be set")
    if source not in VALUE_SOURCES:
        return reject(f"value_source {source!r} is not one of " + ", ".join(VALUE_SOURCES))
    if not _is_scalar(value):
        return reject("only scalar values are supported")
    if location == "header" and name.lower() == "authorization":
        return reject("the Authorization header is managed by the authentication contract")

    parameter: dict | None = None
    if location == "body":
        if not _body_has_field(operation.get("request_body"), name):
            return reject("the documented request body has no such field")
    else:
        parameter = operation_parameter(operation, location, name)
        if parameter is None:
            return reject("the operation documents no such parameter")

    constraints = (
        parameter.get("constraints")
        if parameter is not None and isinstance(parameter.get("constraints"), dict)
        else {}
    )
    item = RequestValue(location=location, name=name, value=value, value_source=source)
    if evidence:
        item["evidence"] = evidence

    if source == SOURCE_DEPENDENCY:
        key = (location, name.lower() if location == "header" else name)
        if key not in dependency_targets:
            return reject("no Step 3 data dependency delivers a value to this parameter")
        item["value"] = None
        return item, ""

    if source == SOURCE_RUNTIME_VARIABLE:
        if not isinstance(value, str) or not _RUNTIME_VARIABLE_RE.match(value.strip()):
            return reject("a runtime variable must be written as {{name}}")
        item["value"] = "{{" + _RUNTIME_VARIABLE_RE.match(value.strip()).group(1) + "}}"
        return item, ""

    if source == SOURCE_SCHEMA_DEFAULT:
        if "default" not in constraints:
            return reject(
                "Swagger documents no default for it; a default must come from Swagger or a quoted requirement"
            )
        if str(value) != str(constraints["default"]):
            return reject(f"the documented default is {constraints['default']!r}")
    elif source == SOURCE_USER_OR_REQUIREMENT:
        if not evidence:
            return reject(
                "a value taken from the user or the requirement needs the quoted evidence"
            )
    elif source == SOURCE_SCHEMA_CONSTRAINT:
        problem = _boundary_problem(value, constraints)
        if problem:
            return reject(problem)
    elif source == SOURCE_NONEXISTENT_ID:
        if location not in ("path", "query"):
            return reject("a non-existent identifier belongs in the path or the query")
        if value is None or (isinstance(value, str) and not value.strip()):
            return reject("a non-existent identifier must be a well-formed, non-empty value")

    violation = ""
    if parameter is not None:
        problem = type_problem(value, parameter)
        if problem:
            violation = problem
        else:
            broken = constraint_violations(value, constraints)
            if broken:
                violation = "it violates the documented " + "; ".join(broken)
        if violation and source == SOURCE_NONEXISTENT_ID:
            return reject(f"a non-existent identifier must still be well-formed ({violation})")
    return item, violation


def _normalize_assertion(
    raw: object, case_id: str, case_type: str, operation: dict, notes: list[dict]
) -> DesignAssertion | None:
    if not isinstance(raw, dict):
        notes.append(
            _clarification(
                CLARIFICATION_ASSERTION,
                case_id,
                f"{case_id}: an assertion is not an object; ignored.",
            )
        )
        return None
    kind = _clean(raw.get("kind"))

    def reject(reason: str) -> None:
        notes.append(
            _clarification(
                CLARIFICATION_ASSERTION,
                case_id,
                f"The proposed {kind or 'unknown'} assertion was not generated: {reason}.",
            )
        )
        return None

    if kind not in ASSERTION_KINDS:
        return reject("the kind must be one of " + ", ".join(ASSERTION_KINDS))

    item = DesignAssertion(kind=kind)
    for key in ("requirement_ref", "evidence"):
        if _clean(raw.get(key)):
            item[key] = _clean(raw.get(key))

    if kind == KIND_STATUS:
        expected = raw.get("expected")
        status = expected if isinstance(expected, int) and not isinstance(expected, bool) else None
        if status is None and isinstance(expected, str) and expected.strip().isdigit():
            status = int(expected.strip())
        if status is None:
            return reject("the expected status must be an integer")
        if status not in documented_statuses(operation):
            return reject(
                f"status {status} is not documented for this operation; an exact status "
                f"is never invented"
            )
        if case_type == "positive" and not 200 <= status < 300:
            return reject("a positive test case cannot expect an error status")
        item["expected"] = status
        return item

    if kind == KIND_STATUS_CLASS:
        expected = _clean(raw.get("expected")).lower()
        if expected not in STATUS_CLASSES:
            return reject("the status class must be 2xx or 4xx")
        if case_type == "positive" and expected != STATUS_CLASS_SUCCESS:
            return reject("a positive test case cannot expect a client error")
        item["expected"] = expected
        return item

    scope = _clean(raw.get("scope")).lower() or SCOPE_PATH
    path = normalize_json_path(_clean(raw.get("json_path")))
    path_source = _clean(raw.get("path_source")).lower()
    evidence = item.get("evidence", "")

    if scope not in (SCOPE_PATH, SCOPE_ANYWHERE):
        return reject("the scope must be 'path' or 'anywhere'")
    if scope == SCOPE_ANYWHERE:
        if kind not in (KIND_FIELD_PRESENT, KIND_FIELD_ABSENT):
            return reject("'anywhere' is only allowed for the presence or absence of a key")
        if not path or "." in path or "[]" in path or not _PATH_SEGMENT_RE.match(path):
            return reject("an 'anywhere' assertion names a single key, not a path")
        if not evidence:
            return reject(
                "searching the whole response is allowed only when the quoted requirement "
                "is about the key anywhere in the response"
            )
        item["json_path"] = path
        item["scope"] = SCOPE_ANYWHERE
    else:
        if not path:
            return reject("an exact response path is required")
        if not path_segments_valid(path):
            return reject(
                f"the response path {path!r} is not supported (dotted keys, at most one [])"
            )
        if path == ROOT_PATH and kind != KIND_ARRAY_LENGTH:
            return reject("only a list length can be asserted on the response root")
        if path_source not in PATH_SOURCES:
            return reject("path_source must be swagger, requirement, attachment or user")
        if path_source == PATH_SOURCE_SWAGGER:
            if not _path_is_documented(path, documented_response_paths(operation)):
                return reject(f"the path {path!r} is not documented in the Swagger response")
        elif not evidence:
            return reject(
                "a path taken from the requirement, an attachment or the user needs the quoted evidence"
            )
        item["json_path"] = path
        item["scope"] = SCOPE_PATH
        item["path_source"] = path_source

    if kind == KIND_FIELD_EQUALS:
        if "expected" not in raw or not _is_scalar(raw.get("expected")):
            return reject("field_equals needs a scalar expected value")
        item["expected"] = raw.get("expected")
    elif kind == KIND_FIELD_TYPE:
        value_type = _clean(raw.get("value_type")).lower()
        if value_type not in VALUE_TYPES:
            return reject("value_type must be one of " + ", ".join(VALUE_TYPES))
        item["value_type"] = value_type
    elif kind == KIND_ARRAY_LENGTH:
        if path.endswith("[]") or ("[]" in path and path != ROOT_PATH):
            return reject("the length is asserted on the list itself, not on its items")
        bounds = {}
        for key in ("expected", "min", "max"):
            if raw.get(key) is None:
                continue
            number = _non_negative_int(raw.get(key))
            if number is None:
                return reject(f"{key} must be a non-negative integer")
            bounds[key] = number
        if not bounds:
            return reject("array_length needs expected, min or max")
        if "min" in bounds and "max" in bounds and bounds["min"] > bounds["max"]:
            return reject("min is greater than max")
        item.update(bounds)  # type: ignore[typeddict-item]
    return item


def normalize_test_design(
    raw: object,
    *,
    test_cases: list[dict],
    operations: dict[str, dict | None],
    dependencies: list[dict] | None = None,
) -> tuple[list[TestDesign], list[dict]]:
    """طراحیِ تستِ پیشنهادی را می‌سنجد و (طراحی‌های معتبر، ابهام‌ها) برمی‌گرداند.

    ``operations`` برای هر test_case_id عملیاتِ کاملِ نگاشت‌شده (یا None) است.
    ورودیِ نامعتبر هرگز استثنا نمی‌دهد؛ هر بخشِ غیرقابلِ اثبات کنار گذاشته و
    با دلیلش گزارش می‌شود.
    """
    notes: list[dict] = []
    if raw is None:
        return [], notes
    if not isinstance(raw, list):
        notes.append(
            _clarification(
                CLARIFICATION_TEST_DATA, "", "'test_design' must be an array; it was ignored."
            )
        )
        return [], notes

    cases = {_clean(case.get("id")): case for case in test_cases if isinstance(case, dict)}
    designs: list[TestDesign] = []
    seen: set[str] = set()

    for position, entry in enumerate(raw):
        if not isinstance(entry, dict):
            notes.append(
                _clarification(
                    CLARIFICATION_TEST_DATA,
                    "",
                    f"test_design[{position}] is not an object; ignored.",
                )
            )
            continue
        case_id = _clean(entry.get("test_case_id"))
        if case_id not in cases:
            notes.append(
                _clarification(
                    CLARIFICATION_TEST_DATA,
                    "",
                    f"test_design[{position}] names an unknown test case {case_id!r}; ignored.",
                )
            )
            continue
        if case_id in seen:
            notes.append(
                _clarification(
                    CLARIFICATION_TEST_DATA,
                    case_id,
                    f"A second test design for {case_id} was ignored.",
                )
            )
            continue
        seen.add(case_id)

        operation = operations.get(case_id)
        if not isinstance(operation, dict):
            notes.append(
                _clarification(
                    CLARIFICATION_TEST_DATA,
                    case_id,
                    f"{case_id} has no resolved operation, so its test design cannot be validated and was ignored.",
                )
            )
            continue
        case_type = _clean(cases[case_id].get("type")) or "positive"

        assertions: list[DesignAssertion] = []
        raw_assertions = entry.get("assertions")
        for item in raw_assertions if isinstance(raw_assertions, list) else []:
            normalized = _normalize_assertion(item, case_id, case_type, operation, notes)
            if normalized is not None and normalized not in assertions:
                assertions.append(normalized)

        targets = _dependency_targets(dependencies or [], case_id)
        values: list[RequestValue] = []
        violations: list[str] = []
        used: set[tuple[str, str]] = set()
        raw_values = entry.get("request_values")
        for item in raw_values if isinstance(raw_values, list) else []:
            normalized, violation = _normalize_value(item, case_id, operation, targets, notes)
            if normalized is None:
                continue
            key = (normalized["location"], normalized["name"].lower())
            if key in used:
                notes.append(
                    _clarification(
                        CLARIFICATION_TEST_DATA,
                        case_id,
                        f"A second value for {normalized['location']} '{normalized['name']}' was ignored.",
                    )
                )
                continue
            used.add(key)
            values.append(normalized)
            if violation:
                violations.append(f"{normalized['location']} '{normalized['name']}': {violation}")

        pending = bool(entry.get("pending"))
        pending_reason = _clean(entry.get("pending_reason"))
        if pending and not pending_reason:
            pending_reason = "Step 3 marked this test case as pending without a reason."

        # قاعده‌ی عمومیِ تعارض: مقداری که قیدِ مستند را نقض می‌کند فقط وقتی مجاز
        # است که خودِ تست‌کیس ردِ درخواست را بررسی کند. در غیرِ این صورت رفتارِ
        # موردِ انتظار تعریف نشده است و تست‌کیس تا روشن‌شدنِ آن معلق می‌ماند.
        if violations:
            deliberate = case_type in ("negative", "boundary") and any(
                is_rejection(assertion) for assertion in assertions
            )
            if not deliberate:
                reason = (
                    "The input needed for this test case conflicts with the documented "
                    "contract (" + "; ".join(violations) + ") and no requirement defines "
                    "the expected behaviour for that conflict."
                )
                pending = True
                if reason not in pending_reason:
                    pending_reason = f"{pending_reason} {reason}".strip()

        unverified: list[UnverifiedExpectation] = []
        raw_unverified = entry.get("unverified_expectations")
        for item in raw_unverified if isinstance(raw_unverified, list) else []:
            if not isinstance(item, dict):
                continue
            expectation = _clean(item.get("expectation"))
            reason = _clean(item.get("reason"))
            if expectation and reason:
                unverified.append(UnverifiedExpectation(expectation=expectation, reason=reason))

        if pending:
            notes.append(_clarification(CLARIFICATION_PENDING, case_id, pending_reason))
        for item in unverified:
            notes.append(
                _clarification(
                    CLARIFICATION_PENDING,
                    case_id,
                    f"Unverified expectation: {item['expectation']} — {item['reason']}",
                )
            )

        if not (values or assertions or pending or unverified):
            continue
        designs.append(
            TestDesign(
                test_case_id=case_id,
                request_values=values,
                assertions=assertions,
                pending=pending,
                pending_reason=pending_reason if pending else "",
                unverified_expectations=unverified,
            )
        )

    order = {case_id: index for index, case_id in enumerate(cases)}
    designs.sort(key=lambda design: order.get(design["test_case_id"], len(order)))
    return designs, notes


def designs_by_case(designs: object) -> dict[str, dict]:
    """test_case_id → طراحی (برای مصرف در قدم ۴ و ۵)."""
    index: dict[str, dict] = {}
    for design in designs if isinstance(designs, list) else []:
        if isinstance(design, dict) and _clean(design.get("test_case_id")):
            index.setdefault(_clean(design.get("test_case_id")), design)
    return index


def runtime_variables(designs: object) -> set[str]:
    """نامِ متغیرهای زمانِ اجرایی که طراحی‌ها لازم دارند (بدونِ {{ }})."""
    names: set[str] = set()
    for design in designs if isinstance(designs, list) else []:
        if not isinstance(design, dict):
            continue
        for value in design.get("request_values") or []:
            if isinstance(value, dict) and value.get("value_source") == SOURCE_RUNTIME_VARIABLE:
                match = _RUNTIME_VARIABLE_RE.match(_clean(value.get("value")))
                if match:
                    names.add(match.group(1))
    return names


def to_builder_assertion(assertion: dict) -> dict | None:
    """assertionِ طراحی → قراردادِ Assertionِ PostmanBuilder."""
    kind = assertion.get("kind")
    if kind == KIND_STATUS:
        return {"type": "status_code", "expected": assertion.get("expected")}
    if kind == KIND_STATUS_CLASS:
        return {"type": "status_class", "expected": assertion.get("expected")}
    if kind not in _FIELD_KINDS:
        return None
    result: dict = {"type": kind, "json_path": assertion.get("json_path", "")}
    if assertion.get("scope") == SCOPE_ANYWHERE:
        result["scope"] = SCOPE_ANYWHERE
    for key in ("expected", "min", "max", "value_type"):
        if key in assertion:
            result[key] = assertion[key]
    return result


__all__ = [
    "ASSERTION_KINDS",
    "LOCATIONS",
    "PATH_SOURCES",
    "STATUS_CLASSES",
    "VALUE_SOURCES",
    "VALUE_TYPES",
    "DesignAssertion",
    "RequestValue",
    "TestDesign",
    "UnverifiedExpectation",
    "constraint_violations",
    "designs_by_case",
    "documented_response_paths",
    "documented_statuses",
    "is_rejection",
    "normalize_json_path",
    "normalize_test_design",
    "operation_parameter",
    "path_segments_valid",
    "runtime_variables",
    "to_builder_assertion",
    "type_problem",
]
