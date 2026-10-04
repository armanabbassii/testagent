"""
final_review.py — بازبینیِ قطعیِ نهایی و آماده‌سازیِ صادرات (قدم پنجمِ جریان HITL)

    نتیجه‌ی قدم ۱ (تست‌کیس‌های کسب‌وکاری)
  + نتیجه‌ی قدم ۲ (عملیات‌های کشف‌شده و نگاشتِ تست‌کیس به API)
  + نتیجه‌ی قدم ۳ (سناریو، ترتیبِ اجرا و وابستگی‌های داده)
  + نتیجه‌ی قدم ۴ (Postman Collection v2.1)
        → وضعیتِ بازبینی (ready / needs_review / blocked)
        → سنجشِ پوششِ تست‌کیس‌ها، سازگاریِ بین‌قدمی و اعتبارِ ساختاری
        → متریک‌ها، هشدارها، مواردِ حل‌نشده و خودِ کالکشنِ دست‌نخورده

این قدم هیچ‌چیز تولید نمی‌کند؛ فقط بازبینی و بسته‌بندی می‌کند:

  - هیچ سندِ Swagger ای دوباره دریافت یا parse نمی‌شود و هیچ API ای کشف نمی‌شود
  - هیچ API و هیچ کالکشنی اجرا نمی‌شود، هیچ LLM ای صدا زده نمی‌شود
  - هیچ تست‌کیس، نگاشت، وابستگی یا درخواستِ تازه‌ای ساخته نمی‌شود
  - هیچ مقدارِ گمشده‌ای حدس زده نمی‌شود (نه status code، نه فیلدِ پاسخ)
  - و هیچ نتیجه‌ای از قدم‌های ۱ تا ۴ بی‌صدا تغییر نمی‌کند: کالکشنِ صادرشده
    دقیقاً همان شیئی است که قدم چهارم ساخته است

نکته‌ی معماری: منطقِ اعتبارسنجی این‌جا دوباره پیاده نشده است. همان توابعِ قدم
چهارم (`validate_collection`، `check_variables_are_available`،
`validate_step3_references` و استخراج‌کننده‌های نتیجه) صدا زده می‌شوند تا یک
قرارداد و یک منطق باقی بماند. تنها چیزی که قدم پنجم اضافه می‌کند سنجشِ
«پوششِ تست‌کیس‌ها با درخواست‌های ساخته‌شده» و «خواندنِ نگاشتِ هر درخواست با
قدم دوم» است — همان دو چیزی که تا حالا کسی بررسی نمی‌کرد.

مرزِ خطا/هشدار/ابهام:

    issue   : خودِ artifact قابلِ اتکا نیست (کالکشنِ نامعتبر، درخواستِ گمشده،
              نگاشتِ ناسازگار، ارجاعِ شکسته در قدم سوم) → وضعیت blocked
    warning : ساختِ کالکشن مشکلی دارد که بازبین باید بداند ولی بازدارنده نیست
              (مثلاً ترتیبِ اجرا با یک وابستگی نمی‌خواند) → وضعیت needs_review
    item    : ابهامِ کسب‌وکاریِ حل‌نشده (قدم‌های ۱–۳) یا تست‌کیسی که قدم چهارم
              صریحاً به درخواست تبدیل نکرده — هیچ‌کدام حدس زده نمی‌شوند و همه
              در `review.unresolved_clarifications` می‌مانند → اگر issue ای
              نباشد وضعیت needs_review می‌شود، نه blocked
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from typing_extensions import TypedDict

from src.agents.test_case_generator.api_discovery import operation_key
from src.agents.test_case_generator.postman_generation import (
    PostmanGenerationError,
    check_variables_are_available,
    extract_mappings,
    extract_services,
    extract_step3_sections,
    extract_test_cases,
    iter_collection_requests,
    mapping_for,
    operation_index,
    validate_collection,
    validate_step3_references,
)
from src.debug import DebugConfig

# ── وضعیت‌ها ─────────────────────────────────────────────────────────────────

#: کالکشن معتبر است و هیچ issue/موردِ باز/هشداری برای بازبینی نمانده.
STATUS_READY = "ready"
#: کالکشن معتبر است ولی ابهام، تست‌کیسِ حل‌نشده یا هشدار دارد — بازبینی لازم است.
STATUS_NEEDS_REVIEW = "needs_review"
#: خودِ artifact قابلِ اتکا نیست (issue بازدارنده دارد) — تا حل نشود صادر نمی‌شود.
STATUS_BLOCKED = "blocked"

_CLARIFICATION = "clarification"
_UNRESOLVED_TEST_CASE = "unresolved_test_case"

#: قراردادِ نام‌گذاریِ درخواستِ قدم چهارم: «<case_id> — <title>»
_SEPARATOR = " — "

# ── قراردادِ خروجی ───────────────────────────────────────────────────────────

class ReviewIssue(TypedDict):
    """یک مشکلِ بازدارنده — تا حل نشود کالکشن قابلِ اتکا نیست."""

    code: str
    step: int
    test_case_id: str
    message: str


class ReviewItem(TypedDict):
    """یک موردِ حل‌نشده که باید انسانی بازبینی شود.

    شاملِ ابهام‌های قدم‌های ۱ تا ۳ و تست‌کیس‌هایی است که قدم چهارم صریحاً به
    درخواست تبدیل نکرده. هیچ‌کدام حدس زده نشده‌اند.
    """

    kind: str
    source_step: int
    test_case_id: str
    message: str


class ReviewSection(TypedDict):
    """بخشِ بازبینی — همان چیزی که صفحه‌ی Streamlit نشان می‌دهد."""

    status: str
    summary: str
    issues: list[ReviewIssue]
    warnings: list[str]
    unresolved_clarifications: list[ReviewItem]


class ReviewMetrics(TypedDict):
    """شمارش‌های قطعی — همه از نتیجه‌ی قدم‌های قبلی، هیچ‌کدام حدسی.

    `clarifications` یعنی همه‌ی مواردِ بازِ بازبینی: ابهام‌های قدم‌های ۱ تا ۳
    به‌علاوه‌ی تست‌کیس‌هایی که قدم چهارم نتوانسته به درخواست تبدیل کند —
    دقیقاً هم‌اندازه‌ی `review.unresolved_clarifications`.
    """

    test_cases: int
    mapped_test_cases: int
    generated_requests: int
    scenarios: int
    data_dependencies: int
    clarifications: int
    issues: int
    warnings: int


class ReviewArtifacts(TypedDict):
    """خروجیِ نهایی — کالکشنِ قدم چهارم، دست‌نخورده و بدونِ هیچ فراداده‌ی قدم پنجم."""

    postman_collection: dict


class Step5Result(TypedDict):
    """نتیجه‌ی قدم پنجم: بازبینی + متریک‌ها + artifact."""

    review: ReviewSection
    metrics: ReviewMetrics
    artifacts: ReviewArtifacts


# ── کمک‌تابع‌های عمومی ──────────────────────────────────────────────────────

def _text(value: object) -> str:
    """رشته‌ی تمیزشده — هر چیزِ دیگری رشته‌ی خالی."""
    return value.strip() if isinstance(value, str) else ""


def _issue(code: str, step: int, test_case_id: str, message: str) -> ReviewIssue:
    """یک issue با شکلِ ثابت — تا ترتیبِ کلیدها همه‌جا یکی بماند."""
    return ReviewIssue(
        code=code, step=step, test_case_id=test_case_id, message=message
    )


def _dedupe(issues: list[ReviewIssue]) -> list[ReviewIssue]:
    """issue تکراری فقط یک بار گزارش می‌شود؛ ترتیبِ اولین‌بار حفظ می‌شود."""
    seen: set[tuple[str, int, str, str]] = set()
    unique: list[ReviewIssue] = []
    for issue in issues:
        key = (issue["code"], issue["step"], issue["test_case_id"], issue["message"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def _counted(count: int, noun: str) -> str:
    """«1 test case» / «3 test cases» — جمعِ ساده و قطعی."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# ── ورودیِ قدم چهارم ────────────────────────────────────────────────────────

def _extract_step4(step4_result: Any) -> tuple[dict, dict | None]:
    """کالکشن و فراداده‌ی قدم چهارم را از هم جدا می‌کند.

    سه حالت پذیرفته می‌شود:
      - نتیجه‌ی کاملِ قدم چهارم ({"collection": {...}, "requests": [...], ...})
      - فقط خودِ کالکشنِ Postman ({"info": ..., "item": [...]})
      - هر چیزِ دیگری → کالکشنِ خالی

    برمی‌گرداند: (collection, metadata) — metadata وقتی None است که فقط خودِ
    کالکشن داده شده باشد.
    """
    if not isinstance(step4_result, dict):
        return {}, None

    collection = step4_result.get("collection")
    if isinstance(collection, dict):
        return collection, step4_result

    if isinstance(step4_result.get("info"), dict) and isinstance(
        step4_result.get("item"), list
    ):
        return step4_result, None

    return {}, None


def _names_by_case(metadata: dict | None) -> dict[str, str]:
    """نگاشتِ «نامِ درخواست → شناسه‌ی تست‌کیس» از فراداده‌ی قدم چهارم."""
    if metadata is None:
        return {}
    rows = metadata.get("requests")
    if not isinstance(rows, list):
        return {}

    by_name: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("name"))
        if name:
            by_name[name] = _text(row.get("test_case_id"))
    return by_name


def _case_id_for(name: str, by_name: dict[str, str], known: set[str]) -> str:
    """شناسه‌ی تست‌کیسِ یک درخواست.

    با فراداده: از همان نگاشتِ قدم چهارم.
    بدونِ فراداده: قراردادِ نام‌گذاریِ قدم چهارم «<case_id> — <title>» است و
    پیشوند فقط وقتی پذیرفته می‌شود که شناسه‌ی واقعیِ قدم اول باشد — هیچ شناسه‌ای
    از خودِ نام حدس زده نمی‌شود.
    """
    if by_name:
        return by_name.get(name, "")
    candidate = name.split(_SEPARATOR, 1)[0].strip()
    return candidate if candidate in known else ""


def _request_path(url: Any) -> str:
    """مسیرِ قالبیِ یک URLِ Postman.

        ["admin", "voucher", ":id"] → "/admin/voucher/{id}"
    """
    if not isinstance(url, dict):
        return ""
    segments = url.get("path")
    if not isinstance(segments, list):
        return ""

    parts: list[str] = []
    for segment in segments:
        text = str(segment)
        parts.append("{" + text[1:] + "}" if text.startswith(":") else text)
    return "/" + "/".join(parts) if parts else ""


@dataclass
class _GeneratedRequest:
    """یک درخواستِ کالکشن به‌همراهِ تست‌کیسی که به آن نسبت داده شده."""

    name: str
    method: str
    path: str
    case_id: str


def _generated_requests(
    collection: dict, by_name: dict[str, str], known: set[str]
) -> list[_GeneratedRequest]:
    """درخواست‌های برگِ کالکشن را با تست‌کیسِ نسبت‌داده‌شده برمی‌گرداند."""
    records: list[_GeneratedRequest] = []
    for item in iter_collection_requests(collection):
        request = item.get("request")
        if not isinstance(request, dict):
            request = {}
        name = _text(item.get("name"))
        records.append(
            _GeneratedRequest(
                name=name,
                method=_text(request.get("method")).upper(),
                path=_request_path(request.get("url")),
                case_id=_case_id_for(name, by_name, known),
            )
        )
    return records


def _dependency_variables(dependencies: list[dict]) -> set[str]:
    """متغیرهایی که قدم سوم به‌عنوان وابستگیِ داده معرفی کرده است."""
    return {
        _text(dependency.get("variable_name"))
        for dependency in dependencies
        if _text(dependency.get("variable_name"))
    }


# ── قاعده‌ی ۱: پوششِ تست‌کیس‌ها ──────────────────────────────────────────────

def _coverage_issues(
    test_cases: list[dict], records: list[_GeneratedRequest], reported: set[str]
) -> list[ReviewIssue]:
    """هر تست‌کیسِ قدم اول باید دقیقاً یک درخواستِ ساخته‌شده داشته باشد.

    تنها استثنا چیزی است که خودِ قدم چهارم صریحاً حل‌نشده اعلام کرده است؛ در آن
    حالت یک موردِ بازبینی ثبت می‌شود (نه یک درخواستِ حدسی) و issue ای ساخته
    نمی‌شود. هر چیزِ دیگری — درخواستِ گمشده، تکراری، یا درخواستی که به هیچ
    تست‌کیسی نمی‌خورد — بازدارنده است.
    """
    counts: dict[str, int] = {}
    for record in records:
        if record.case_id:
            counts[record.case_id] = counts.get(record.case_id, 0) + 1

    issues: list[ReviewIssue] = []
    for case in test_cases:
        case_id = _text(case.get("id"))
        if not case_id:
            continue
        count = counts.get(case_id, 0)
        if count == 0 and case_id not in reported:
            issues.append(
                _issue(
                    "missing_request",
                    4,
                    case_id,
                    f"Test case {case_id!r} has no generated Postman request and "
                    f"Step 4 did not report it as unresolved.",
                )
            )
        elif count > 1:
            issues.append(
                _issue(
                    "duplicate_request",
                    4,
                    case_id,
                    f"Test case {case_id!r} is covered by {count} generated "
                    f"requests; exactly one was expected.",
                )
            )

    for record in records:
        if not record.case_id:
            issues.append(
                _issue(
                    "extra_request",
                    4,
                    "",
                    f"Generated request {record.name!r} cannot be traced back to a "
                    f"Step 1 test case.",
                )
            )
    return issues


# ── قاعده‌ی ۲: سازگاریِ نگاشتِ قدم دوم ──────────────────────────────────────

def _mapping_issues(
    records: list[_GeneratedRequest], mappings: list[dict], services: list[dict]
) -> list[ReviewIssue]:
    """هر درخواست باید با نگاشتِ قدم دوم و با عملیاتِ کشف‌شده بخواند.

    این‌جا هیچ API ای دوباره کشف نمی‌شود: فقط همین نتیجه‌ی قدم دوم خوانده
    می‌شود. مسیرها با همان `operation_key` نرمال می‌شوند تا `{id}` و
    `{voucherId}` یکی حساب شوند.
    """
    index = operation_index(services)
    issues: list[ReviewIssue] = []
    checked: set[str] = set()

    for record in records:
        case_id = record.case_id
        if not case_id or case_id in checked:
            continue
        checked.add(case_id)

        mapping = mapping_for(mappings, case_id)
        if mapping is None:
            issues.append(
                _issue(
                    "missing_mapping",
                    2,
                    case_id,
                    f"Test case {case_id!r} has a generated request but no Step 2 "
                    f"mapping.",
                )
            )
            continue

        api = mapping.get("api")
        if not isinstance(api, dict):
            issues.append(
                _issue(
                    "unresolved_mapping",
                    2,
                    case_id,
                    f"Test case {case_id!r} has a generated request although Step 2 "
                    f"did not resolve an API for it.",
                )
            )
            continue

        expected = operation_key(_text(api.get("method")), _text(api.get("path")))
        actual = operation_key(record.method, record.path)
        if expected != actual:
            issues.append(
                _issue(
                    "inconsistent_mapping",
                    2,
                    case_id,
                    f"Test case {case_id!r} is mapped to {expected[0]} {expected[1]} "
                    f"in Step 2 but its generated request is "
                    f"{actual[0]} {actual[1]}.",
                )
            )
        if expected not in index:
            issues.append(
                _issue(
                    "undiscovered_api",
                    2,
                    case_id,
                    f"Test case {case_id!r} is mapped to {expected[0]} {expected[1]}, "
                    f"which is not among the APIs discovered in Step 2.",
                )
            )
    return issues


# ── قاعده‌ی ۳ و ۴: سازگاریِ قدم سوم و اعتبارِ خودِ کالکشن ────────────────────

def _step3_issues(
    test_cases: list[dict],
    mappings: list[dict],
    scenarios: list[dict],
    execution_order: list[dict],
    dependencies: list[dict],
) -> list[ReviewIssue]:
    """ارجاع‌های قدم سوم با همان اعتبارسنجِ قدم چهارم سنجیده می‌شوند."""
    try:
        validate_step3_references(
            test_cases, mappings, scenarios, execution_order, dependencies
        )
    except PostmanGenerationError as exc:
        return [_issue("inconsistent_step3", 3, "", str(exc))]
    return []


def _collection_issues(
    collection: dict, dependency_variables: set[str]
) -> list[ReviewIssue]:
    """اعتبارِ ساختاریِ کالکشن — با همان دو تابعِ قدم چهارم، بدونِ تکرارِ منطق."""
    issues = [
        _issue("invalid_collection", 4, "", problem)
        for problem in validate_collection(collection)
    ]
    issues.extend(
        _issue("undeclared_variable", 4, "", problem)
        for problem in check_variables_are_available(collection, dependency_variables)
    )
    return issues


def _metadata_issues(
    metadata: dict, records: list[_GeneratedRequest]
) -> list[ReviewIssue]:
    """فراداده‌ی قدم چهارم باید با خودِ کالکشن بخواند.

    اگر کالکشن دست‌کاری شده باشد (درخواستی حذف/عوض شده باشد) صادرات نباید
    بی‌صدا قبول شود.
    """
    rows = metadata.get("requests")
    if not isinstance(rows, list):
        return []

    by_name = {record.name: record for record in records if record.name}
    issues: list[ReviewIssue] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("name"))
        case_id = _text(row.get("test_case_id"))
        if not name:
            continue

        record = by_name.get(name)
        if record is None:
            issues.append(
                _issue(
                    "inconsistent_step4",
                    4,
                    case_id,
                    f"The Step 4 result lists a request named {name!r} that the "
                    f"generated collection does not contain.",
                )
            )
            continue

        method = _text(row.get("method")).upper()
        if method and method != record.method:
            issues.append(
                _issue(
                    "inconsistent_step4",
                    4,
                    case_id,
                    f"Request {name!r} is {method} in the Step 4 result but "
                    f"{record.method} in the generated collection.",
                )
            )

        path = _text(row.get("path"))
        if path and operation_key(method, path) != operation_key(method, record.path):
            issues.append(
                _issue(
                    "inconsistent_step4",
                    4,
                    case_id,
                    f"Request {name!r} is {path} in the Step 4 result but "
                    f"{record.path} in the generated collection.",
                )
            )
    return issues


# ── مواردِ بازِ بازبینی ─────────────────────────────────────────────────────

def _clarification_items(step_result: Any, step: int) -> list[ReviewItem]:
    """ابهام‌های یک قدم.

    قدم‌های ۱ و ۲ ابهام را رشته‌ی ساده نگه می‌دارند و قدم ۳ شیءِ ساخت‌یافته —
    هر دو شکل این‌جا یکی می‌شوند. هیچ ابهامی حذف یا خلاصه نمی‌شود.
    """
    if not isinstance(step_result, dict):
        return []
    clarifications = step_result.get("clarifications")
    if not isinstance(clarifications, list):
        return []

    items: list[ReviewItem] = []
    for entry in clarifications:
        if isinstance(entry, str):
            message = entry.strip()
            kind = _CLARIFICATION
            case_id = ""
        elif isinstance(entry, dict):
            message = _text(entry.get("message"))
            kind = _text(entry.get("type")) or _CLARIFICATION
            case_id = _text(entry.get("test_case_id"))
        else:
            continue
        if not message:
            continue
        items.append(
            ReviewItem(
                kind=kind,
                source_step=step,
                test_case_id=case_id,
                message=message,
            )
        )
    return items


def _unresolved_items(metadata: dict | None) -> list[ReviewItem]:
    """تست‌کیس‌هایی که قدم چهارم نتوانسته به درخواست تبدیل کند — با دلیلش."""
    if metadata is None:
        return []
    rows = metadata.get("unresolved")
    if not isinstance(rows, list):
        return []

    items: list[ReviewItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        message = _text(row.get("reason"))
        if not message:
            continue
        items.append(
            ReviewItem(
                kind=_UNRESOLVED_TEST_CASE,
                source_step=4,
                test_case_id=_text(row.get("test_case_id")),
                message=message,
            )
        )
    return items


def _step4_warnings(metadata: dict | None) -> list[str]:
    """هشدارهای ساختِ قدم چهارم — بدونِ تغییر، فقط تمیزشده."""
    if metadata is None:
        return []
    warnings = metadata.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [
        warning.strip()
        for warning in warnings
        if isinstance(warning, str) and warning.strip()
    ]


# ── وضعیت و خلاصه ───────────────────────────────────────────────────────────

def _status(
    issues: list[ReviewIssue], items: list[ReviewItem], warnings: list[str]
) -> str:
    """وضعیت از دو چیزِ جدا ساخته می‌شود: خودِ artifact و چیزهایی که باید دیده شوند.

    یک ابهامِ عادیِ کسب‌وکاری یا یک هشدارِ قدم چهارم هرگز «blocked» نمی‌سازد؛
    فقط بازبینیِ انسانی لازم می‌کند. «blocked» یعنی خودِ کالکشن قابلِ اتکا نیست.
    «ready» یعنی هیچ issue، هیچ موردِ باز و هیچ هشداری نمانده — وضعیتِ «ready»
    هیچ‌وقت با یک هشدارِ نمایش‌داده‌شده تناقض پیدا نمی‌کند.
    """
    if issues:
        return STATUS_BLOCKED
    if items or warnings:
        return STATUS_NEEDS_REVIEW
    return STATUS_READY


def _summary(metrics: ReviewMetrics, status: str) -> str:
    """خلاصه‌ی متنیِ قطعی — بدونِ LLM، فقط از همان متریک‌ها."""
    lines = [
        "Step 5 review completed.",
        "",
        f"{_counted(metrics['test_cases'], 'test case')} analyzed.",
        f"{_counted(metrics['generated_requests'], 'Postman request')} generated.",
        f"{_counted(metrics['scenarios'], 'scenario')} identified.",
        f"{_counted(metrics['data_dependencies'], 'data dependency')} applied.",
        f"{_counted(metrics['clarifications'], 'unresolved clarification')} remain.",
    ]
    if metrics["issues"]:
        lines.append(f"{_counted(metrics['issues'], 'blocking issue')} found.")
    lines.append("")
    lines.append(
        {
            STATUS_READY: (
                "The Postman collection is structurally valid and no unresolved "
                "review item remains."
            ),
            STATUS_NEEDS_REVIEW: (
                "The Postman collection is structurally valid but requires human "
                "review before execution."
            ),
            STATUS_BLOCKED: (
                "The Postman collection cannot be exported as a usable artifact "
                "until the blocking issues are resolved."
            ),
        }[status]
    )
    return "\n".join(lines)


# ── تولیدکننده ───────────────────────────────────────────────────────────────

class FinalReviewGenerator:
    """قدم پنجم: بازبینیِ قطعیِ نتیجه‌ی قدم چهارم و آماده‌سازیِ صادرات.

    پارامترها:
        debug_config : تنظیماتِ لاگِ پروژه

    این کلاس هیچ‌چیز تولید نمی‌کند و هیچ‌چیز را تغییر نمی‌دهد: فقط می‌خواند،
    می‌سنجد و گزارش می‌کند.
    """

    name = "step5_final_review"

    def __init__(self, debug_config: DebugConfig | None = None) -> None:
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)

    def generate(
        self,
        step1_result: Any,
        step2_result: Any,
        step3_result: Any,
        step4_result: Any,
    ) -> Step5Result:
        """بازبینیِ نهایی را می‌سازد.

        ورودیِ خرابِ هر قدم به یک issue تبدیل می‌شود، نه به استثنا: کارِ این قدم
        دقیقاً نشان‌دادنِ چیزهایی است که درست نیستند.
        """
        issues: list[ReviewIssue] = []

        try:
            test_cases = extract_test_cases(step1_result)
            step1_ok = True
        except PostmanGenerationError as exc:
            test_cases, step1_ok = [], False
            issues.append(_issue("invalid_step1_input", 1, "", str(exc)))

        try:
            services = extract_services(step2_result)
            mappings = extract_mappings(step2_result)
            step2_ok = True
        except PostmanGenerationError as exc:
            services, mappings, step2_ok = [], [], False
            issues.append(_issue("invalid_step2_input", 2, "", str(exc)))

        try:
            scenarios, execution_order, dependencies, _ = extract_step3_sections(
                step3_result
            )
            step3_ok = True
        except PostmanGenerationError as exc:
            scenarios, execution_order, dependencies, step3_ok = [], [], [], False
            issues.append(_issue("invalid_step3_input", 3, "", str(exc)))

        collection, metadata = _extract_step4(step4_result)
        if not collection:
            issues.append(
                _issue(
                    "invalid_step4_input",
                    4,
                    "",
                    "The Step 4 result must contain a Postman collection — either "
                    "the full Step 4 result or the collection itself.",
                )
            )

        warnings = _step4_warnings(metadata)
        if collection and metadata is None:
            warnings.append(
                "Only the Postman collection was provided, without the Step 4 "
                "result around it: coverage was checked from request names and the "
                "unresolved test cases of Step 4 are not available here."
            )

        known = {_text(case.get("id")) for case in test_cases} - {""}
        records = (
            _generated_requests(collection, _names_by_case(metadata), known)
            if collection
            else []
        )

        # هر سنجش فقط وقتی اجرا می‌شود که ورودیِ مربوط به آن واقعاً خوانده شده
        # باشد — وگرنه یک ورودیِ خراب سیلِ issue های بی‌معنی می‌سازد.
        if step3_ok and step1_ok:
            issues.extend(
                _step3_issues(
                    test_cases, mappings, scenarios, execution_order, dependencies
                )
            )
        if collection:
            issues.extend(
                _collection_issues(collection, _dependency_variables(dependencies))
            )
        if step1_ok:
            issues.extend(
                _coverage_issues(test_cases, records, _reported_unresolved(metadata))
            )
        if step2_ok:
            issues.extend(_mapping_issues(records, mappings, services))
        if metadata is not None:
            issues.extend(_metadata_issues(metadata, records))

        unresolved = (
            _clarification_items(step1_result, 1)
            + _clarification_items(step2_result, 2)
            + _clarification_items(step3_result, 3)
            + _unresolved_items(metadata)
        )

        issues = _dedupe(issues)

        metrics = ReviewMetrics(
            test_cases=len(test_cases),
            mapped_test_cases=sum(
                1 for mapping in mappings if isinstance(mapping.get("api"), dict)
            ),
            generated_requests=len(records),
            scenarios=len(scenarios),
            data_dependencies=len(dependencies),
            clarifications=len(unresolved),
            issues=len(issues),
            warnings=len(warnings),
        )
        status = _status(issues, unresolved, warnings)

        self._log.info(
            "بازبینیِ نهاییِ قدم پنجم",
            status=status,
            issues=len(issues),
            warnings=len(warnings),
            unresolved=len(unresolved),
        )

        return Step5Result(
            review=ReviewSection(
                status=status,
                summary=_summary(metrics, status),
                issues=issues,
                warnings=warnings,
                unresolved_clarifications=unresolved,
            ),
            metrics=metrics,
            # کالکشن دقیقاً همان شیئی است که قدم چهارم ساخته — نه دوباره ساخته
            # می‌شود، نه تغییر می‌کند و نه هیچ فراداده‌ی قدم پنجم به آن اضافه
            # می‌شود.
            artifacts=ReviewArtifacts(postman_collection=collection),
        )


def _reported_unresolved(metadata: dict | None) -> set[str]:
    """تست‌کیس‌هایی که خودِ قدم چهارم صریحاً حل‌نشده اعلام کرده است."""
    if metadata is None:
        return set()
    rows = metadata.get("unresolved")
    if not isinstance(rows, list):
        return set()
    return {
        _text(row.get("test_case_id"))
        for row in rows
        if isinstance(row, dict) and _text(row.get("test_case_id"))
    }


__all__ = [
    "STATUS_BLOCKED",
    "STATUS_NEEDS_REVIEW",
    "STATUS_READY",
    "FinalReviewGenerator",
    "ReviewArtifacts",
    "ReviewIssue",
    "ReviewItem",
    "ReviewMetrics",
    "ReviewSection",
    "Step5Result",
]
