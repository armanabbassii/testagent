"""
variable_dependencies.py — قراردادِ مشترکِ «متغیرِ درخواستیِ کاربر» بینِ قدم‌ها

کاربر ممکن است در توصیفِ تسک صریحاً بخواهد:

    مقدارِ result.body.id از پاسخِ «ایجادِ بن» استخراج و بررسی شود،
    به‌عنوانِ global variableِ voucherId ذخیره شود،
    و درخواستِ «دریافتِ بن» همان مقدار را در مسیرش بفرستد.

این خواسته فقط یک‌بار — در قدم اول و توسطِ LLM — از متنِ آزاد تشخیص داده
می‌شود و از آن به بعد **داده‌ی ساختاریافته** است، نه متن:

  * قدم ۱  →  ``variables`` در نتیجه‌ی تحلیلِ تسک (``RequestedVariable``)
  * قدم ۳  →  همان خواسته با قراردادِ API تطبیق داده می‌شود و به یک
              ``data_dependency`` با ``origin="task"`` تبدیل می‌شود
  * قدم ۴  →  اسکریپتِ استخراج/اعتبارسنجی و ارجاعِ ``{{name}}`` به‌صورتِ قطعی
              ساخته می‌شود
  * قدم ۵  →  سیم‌کشیِ همان وابستگی در کالکشنِ نهایی دوباره بررسی می‌شود

این ماژول فقط قرارداد، نرمال‌سازی و اعتبارسنجیِ ساختاری را نگه می‌دارد تا
هیچ قدمی قواعدِ نام/اسکوپ/مسیر را دوباره پیاده نکند. هیچ LLM، شبکه یا Postmanی
اینجا نیست و هیچ نام یا مسیرِ خاصی (voucher، id، …) هاردکد نشده است.
"""

from __future__ import annotations

import re
from typing import Any

from typing_extensions import TypedDict

# اسکوپِ ذخیره‌ی متغیر در Postman
SCOPE_GLOBAL = "global"
SCOPE_COLLECTION = "collection"
VARIABLE_SCOPES = (SCOPE_GLOBAL, SCOPE_COLLECTION)

# متغیری که کاربر خواسته ولی اسکوپش را نگفته — درخواستِ این جریان «global» است.
DEFAULT_REQUESTED_SCOPE = SCOPE_GLOBAL

# وابستگیِ استنتاج‌شده توسطِ قدم سوم (یا نتیجه‌ی قدیمیِ بدونِ این فیلد) همان
# رفتارِ قبلی را نگه می‌دارد: متغیرِ کالکشن.
DEFAULT_DEPENDENCY_SCOPE = SCOPE_COLLECTION

# منشأِ یک data_dependency در نتیجه‌ی قدم سوم
ORIGIN_TASK = "task"
ORIGIN_INFERRED = "inferred"

# جایی که مقدار در درخواستِ مصرف‌کننده قرار می‌گیرد
CONSUMER_LOCATIONS = ("path", "query", "header", "body")

# نامِ متغیر باید یک شناسه‌ی سالمِ Postman/JavaScript باشد
VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# مسیرِ نرمال‌شده‌ی JSON: «$.id»، «$.result.body.id»، «$.items[0].id»
JSON_PATH_RE = re.compile(r"^\$(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])+$")

# یک قطعه از مسیر — نام یا اندیسِ آرایه
_PATH_TOKEN_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


class VariableInstructionError(ValueError):
    """دستورِ متغیرِ درخواستی با قرارداد نمی‌خواند."""


class VariableConsumer(TypedDict):
    """درخواستی که مقدارِ متغیر را مصرف می‌کند.

    ``location`` و ``parameter`` می‌توانند خالی باشند: کاربر معمولاً فقط می‌گوید
    «از همین مقدار در دریافتِ بن استفاده شود»؛ نامِ دقیقِ پارامتر را قدم سوم از
    قراردادِ API (قدم دوم) پیدا می‌کند.
    """

    test_case_id: str
    location: str
    parameter: str


class RequestedVariable(TypedDict):
    """یک متغیر که کاربر صراحتاً خواسته است — خروجیِ قدم اول."""

    name: str
    scope: str
    source_test_case_id: str
    # مسیرِ مقدار در بدنه‌ی پاسخ، همان‌طور که کاربر گفته (نرمال‌شده با «$.»)؛
    # خالی یعنی کاربر مسیر را نگفته و قدم سوم آن را از قرارداد تعیین می‌کند.
    response_path: str
    required: bool
    consumers: list[VariableConsumer]
    # جمله‌ی کاربر که این خواسته از آن آمده — برای بازبینیِ انسانی
    instruction: str


def _clean(value: object) -> str:
    """رشته را trim می‌کند؛ هر چیزِ دیگری رشته‌ی خالی می‌شود."""
    return value.strip() if isinstance(value, str) else ""


def normalize_response_path(value: object) -> str:
    """مسیرِ پاسخ را به شکلِ قراردادیِ «$.a.b» درمی‌آورد.

    «result.body.id»، «$.result.body.id» و «$result.body.id» همه به
    «$.result.body.id» تبدیل می‌شوند. مقدارِ خالی رشته‌ی خالی برمی‌گرداند.

    پرتاب می‌کند:
        VariableInstructionError : مسیر یک JSON pathِ ساده نباشد
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise VariableInstructionError(
            f"response path must be a string, got {type(value).__name__}."
        )
    text = value.strip()
    if not text:
        return ""

    body = text[1:] if text.startswith("$") else text
    if body and not body.startswith((".", "[")):
        body = "." + body
    normalized = "$" + body
    if not JSON_PATH_RE.match(normalized):
        raise VariableInstructionError(
            f"response path {value!r} is not a simple JSON path such as "
            f"'data.id' or '$.data.items[0].id'."
        )
    return normalized


def path_tokens(json_path: str) -> list[str]:
    """قطعه‌های یک مسیرِ نرمال‌شده — نام‌ها به‌صورتِ «.name» و اندیس‌ها «[n]».

    ``"$.result.items[0].id"`` → ``[".result", ".items", "[0]", ".id"]``
    """
    tokens: list[str] = []
    for match in _PATH_TOKEN_RE.finditer(json_path[1:] if json_path.startswith("$") else json_path):
        name, position = match.groups()
        tokens.append(f".{name}" if name is not None else f"[{position}]")
    return tokens


def first_field(json_path: str) -> str:
    """نامِ اولین فیلدِ یک مسیر — برای تطبیق با فیلدهای مستندِ پاسخ."""
    tokens = path_tokens(json_path)
    return tokens[0][1:] if tokens and tokens[0].startswith(".") else ""


def dependency_scope(dependency: dict) -> str:
    """اسکوپِ یک data_dependency — نبودِ فیلد یعنی رفتارِ قبلی (collection)."""
    scope = _clean(dependency.get("scope")).lower()
    return scope if scope in VARIABLE_SCOPES else DEFAULT_DEPENDENCY_SCOPE


def dependency_required(dependency: dict) -> bool:
    """آیا مقدار برای مصرف‌کننده‌ها الزامی است؟ نبودِ فیلد یعنی بله.

    هر data_dependency مقداری را توصیف می‌کند که درخواستِ بعدی به آن نیاز
    دارد؛ مقدارِ «اختیاری» فقط وقتی است که صراحتاً ``required: false`` باشد.
    """
    return dependency.get("required") is not False


def _validate_consumers(
    raw: object,
    where: str,
    known_ids: set[str],
    source_id: str,
    errors: list[str],
) -> list[VariableConsumer]:
    consumers: list[VariableConsumer] = []
    if raw is None:
        return consumers
    if not isinstance(raw, list):
        errors.append(f"{where}: must be an array.")
        return consumers

    seen: set[str] = set()
    for position, item in enumerate(raw):
        item_where = f"{where}[{position}]"
        if isinstance(item, str):
            item = {"test_case_id": item}
        if not isinstance(item, dict):
            errors.append(f"{item_where}: expected an object.")
            continue

        case_id = _clean(item.get("test_case_id"))
        if not case_id:
            errors.append(f"{item_where}: 'test_case_id' is missing or empty.")
            continue
        if case_id not in known_ids:
            errors.append(f"{item_where}: unknown test case id {case_id!r}.")
            continue
        if case_id == source_id:
            errors.append(
                f"{item_where}: {case_id!r} is the source of the variable and "
                f"cannot also consume it."
            )
            continue
        if case_id in seen:
            errors.append(f"{item_where}: duplicate consumer {case_id!r}.")
            continue
        seen.add(case_id)

        location = _clean(item.get("location")).lower()
        if location and location not in CONSUMER_LOCATIONS:
            errors.append(
                f"{item_where}: 'location' must be empty or one of "
                f"{CONSUMER_LOCATIONS}, got {item.get('location')!r}."
            )
            continue

        consumers.append(
            VariableConsumer(
                test_case_id=case_id,
                location=location,
                parameter=_clean(item.get("parameter")),
            )
        )
    return consumers


def validate_requested_variables(
    raw: object,
    test_cases: list[dict],
    errors: list[str],
    where: str = "variables",
) -> list[RequestedVariable]:
    """فهرستِ متغیرهای درخواستیِ قدم اول را اعتبارسنجی و نرمال می‌کند.

    نبودِ فیلد (نتیجه‌های ذخیره‌شده‌ی قدیمی) یعنی «هیچ متغیری خواسته نشده».
    خطاها به ``errors`` اضافه می‌شوند تا با بقیه‌ی خطاهای قرارداد یک‌جا گزارش
    شوند.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"'{where}' must be an array.")
        return []

    types = {
        _clean(case.get("id")): _clean(case.get("type")).lower()
        for case in test_cases
        if isinstance(case, dict)
    }
    known_ids = set(types) - {""}

    variables: list[RequestedVariable] = []
    seen_names: set[str] = set()
    for position, item in enumerate(raw):
        item_where = f"{where}[{position}]"
        if not isinstance(item, dict):
            errors.append(f"{item_where}: expected an object.")
            continue

        valid = True
        name = _clean(item.get("name"))
        if not name:
            errors.append(f"{item_where}: 'name' is missing or empty.")
            valid = False
        elif not VARIABLE_NAME_RE.match(name):
            errors.append(
                f"{item_where}: 'name' {name!r} cannot be used as a Postman "
                f"variable name (letters, digits and underscores only)."
            )
            valid = False
        elif name in seen_names:
            errors.append(f"{item_where}: duplicate variable name {name!r}.")
            valid = False
        else:
            seen_names.add(name)
        item_where = f"{item_where} ({name or '?'})"

        scope = _clean(item.get("scope")).lower() or DEFAULT_REQUESTED_SCOPE
        if scope not in VARIABLE_SCOPES:
            errors.append(
                f"{item_where}: 'scope' must be one of {VARIABLE_SCOPES}, got "
                f"{item.get('scope')!r}."
            )
            valid = False

        source_id = _clean(item.get("source_test_case_id"))
        if not source_id:
            errors.append(f"{item_where}: 'source_test_case_id' is missing or empty.")
            valid = False
        elif source_id not in known_ids:
            errors.append(f"{item_where}: unknown source test case id {source_id!r}.")
            valid = False
        elif types[source_id] == "negative":
            # کیسِ منفی قرار است شکست بخورد؛ اگر منبعِ متغیر باشد، مقدارِ معتبری
            # که سناریوهای مثبت به آن تکیه دارند را خراب یا پاک می‌کند.
            errors.append(
                f"{item_where}: the source {source_id!r} is a negative test case; "
                f"a variable must be extracted from a request that is expected to "
                f"succeed."
            )
            valid = False

        try:
            response_path = normalize_response_path(item.get("response_path"))
        except VariableInstructionError as exc:
            errors.append(f"{item_where}: {exc}")
            response_path = ""
            valid = False

        required = item.get("required", True)
        if required is None:
            required = True
        if not isinstance(required, bool):
            errors.append(f"{item_where}: 'required' must be true or false.")
            valid = False

        consumers = _validate_consumers(
            item.get("consumers"), f"{item_where}.consumers", known_ids, source_id, errors
        )

        instruction = item.get("instruction")
        if instruction is not None and not isinstance(instruction, str):
            errors.append(f"{item_where}: 'instruction' must be a string.")
            valid = False

        if valid:
            variables.append(
                RequestedVariable(
                    name=name,
                    scope=scope,
                    source_test_case_id=source_id,
                    response_path=response_path,
                    required=bool(required),
                    consumers=consumers,
                    instruction=_clean(instruction),
                )
            )
    return variables


def requested_variables(step1_result: Any) -> list[RequestedVariable]:
    """متغیرهای درخواستی را از نتیجه‌ی قدم اول (یا نسخه‌ی ویرایش‌شده‌اش) می‌خواند.

    قدم‌های بعدی همین تابع را صدا می‌زنند تا JSONی که کاربر دستی ویرایش کرده
    با همان قواعدِ قدم اول سنجیده شود.

    پرتاب می‌کند:
        VariableInstructionError : فیلدِ ``variables`` با قرارداد نخواند
    """
    if not isinstance(step1_result, dict):
        return []
    cases = step1_result.get("test_cases")
    errors: list[str] = []
    variables = validate_requested_variables(
        step1_result.get("variables"),
        cases if isinstance(cases, list) else [],
        errors,
    )
    if errors:
        raise VariableInstructionError(
            "Step 1 'variables' do not match the required format:\n  - "
            + "\n  - ".join(errors)
        )
    return variables


__all__ = [
    "CONSUMER_LOCATIONS",
    "DEFAULT_DEPENDENCY_SCOPE",
    "DEFAULT_REQUESTED_SCOPE",
    "JSON_PATH_RE",
    "ORIGIN_INFERRED",
    "ORIGIN_TASK",
    "RequestedVariable",
    "SCOPE_COLLECTION",
    "SCOPE_GLOBAL",
    "VARIABLE_NAME_RE",
    "VARIABLE_SCOPES",
    "VariableConsumer",
    "VariableInstructionError",
    "dependency_required",
    "dependency_scope",
    "first_field",
    "normalize_response_path",
    "path_tokens",
    "requested_variables",
    "validate_requested_variables",
]
