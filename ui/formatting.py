"""
ui/formatting.py — کمک‌تابع‌های نمایشیِ نتیجه‌ی تحلیل تسک

این ماژول هیچ منطقِ کسب‌وکاری ندارد و Streamlit را import نمی‌کند: فقط
ساختارِ خروجیِ TaskAnalysisGenerator (قدم ۱) و نتیجه‌ی نگاشتِ API (قدم ۲) را به
شکلِ قابلِ نمایش تبدیل می‌کند. به همین دلیل مستقل از UI هم قابلِ تست است.

منطقِ تحلیل، اعتبارسنجی، کشفِ Swagger و نگاشت همه در
src/agents/test_case_generator/ می‌ماند؛ این‌جا هیچ‌کدام تکرار نمی‌شوند.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.agents.test_case_generator.task_analysis import REQUIRES_MAPPING

# ترتیبِ ثابتِ انواع در خلاصه‌ی نتیجه
TYPE_ORDER = ("positive", "negative", "boundary", "state_transition")


def format_related_service(service: Any) -> str:
    """نگاشتِ سرویسِ یک تست‌کیس را به یک رشته‌ی خوانا تبدیل می‌کند.

    اگر متد یا مسیر تعیین نشده باشد (مقدارِ نگاشت‌نشده یا خالی)، همان متنِ
    استانداردِ «نگاشت لازم است» برگردانده می‌شود — هیچ مسیر یا متدی حدس زده
    نمی‌شود.
    """
    if not isinstance(service, dict):
        return REQUIRES_MAPPING

    method = str(service.get("method") or "").strip()
    path = str(service.get("path") or "").strip()
    name = str(service.get("service") or "").strip()

    if not method or not path:
        return REQUIRES_MAPPING
    if method == REQUIRES_MAPPING or path == REQUIRES_MAPPING:
        return REQUIRES_MAPPING

    return f"{method} {path} ({name})" if name else f"{method} {path}"


def markdown_bullets(items: Any, empty: str = "—") -> str:
    """یک لیستِ رشته‌ای را به متنِ Markdown با بولت تبدیل می‌کند.

    ورودیِ خالی یا خالی‌از‌محتوا، متنِ جانشینِ `empty` را برمی‌گرداند تا در UI
    جای خالی نماند.
    """
    if not isinstance(items, list):
        return empty

    cleaned = [str(item).strip() for item in items if str(item or "").strip()]
    if not cleaned:
        return empty
    return "\n".join(f"- {item}" for item in cleaned)


def type_counts(result: Any) -> dict[str, int]:
    """تعدادِ تست‌کیس‌ها را به تفکیکِ نوع برمی‌گرداند (همه‌ی کلیدها همیشه حاضرند)."""
    counts = {case_type: 0 for case_type in TYPE_ORDER}
    for case in (result or {}).get("test_cases", []):
        case_type = case.get("type")
        if case_type in counts:
            counts[case_type] += 1
    return counts


def case_overview(result: Any) -> list[dict[str, str]]:
    """یک ردیف برای هر تست‌کیس می‌سازد — برای جدولِ مرورِ سریع."""
    return [
        {
            "ID": case.get("id", ""),
            "Title": case.get("title", ""),
            "Type": case.get("type", ""),
            "Priority": case.get("priority", ""),
            "Related Service": format_related_service(case.get("related_service")),
        }
        for case in (result or {}).get("test_cases", [])
    ]


# ── نمایشِ کاملِ تست‌کیس ──────────────────────────────────────────────────────
# جدولِ case_overview فقط مرورِ سریع است. توابعِ زیر بلوکِ کاملِ هر تست‌کیس را
# می‌سازند و ساختارشان برای همه‌ی تست‌کیس‌ها یکسان است — صرف‌نظر از نوعشان —
# تا هیچ تست‌کیسی خلاصه‌تر از بقیه نمایش داده نشود.

_EMPTY_VALUE = "—"


def _case_field(case: Any, key: str) -> str:
    """مقدارِ متنیِ یک فیلد را trim می‌کند؛ غایب یا غیرِ رشته → رشته‌ی خالی."""
    if not isinstance(case, dict):
        return ""
    value = case.get(key)
    return value.strip() if isinstance(value, str) else ""


def case_heading(case: Any) -> str:
    """عنوانِ یک تست‌کیس: «ID — Title».

    اگر یکی از دو مقدار نباشد فقط همان یکی برمی‌گردد تا خطِ تیره‌ی اضافه ساخته
    نشود.
    """
    case_id = _case_field(case, "id")
    title = _case_field(case, "title")
    if case_id and title:
        return f"{case_id} — {title}"
    return case_id or title


def case_meta(case: Any) -> str:
    """خطِ نوع و اولویتِ یک تست‌کیس."""
    case_type = _case_field(case, "type") or _EMPTY_VALUE
    priority = _case_field(case, "priority") or _EMPTY_VALUE
    return f"Type: {case_type}\nPriority: {priority}"


def case_sections(case: Any) -> list[dict[str, str]]:
    """بخش‌های محتواییِ یک تست‌کیس را با ترتیبِ ثابت می‌سازد.

    فیلدهای خالی جای خالی نمی‌گذارند: لیست‌ها متنِ جانشین می‌گیرند و متن‌های
    غایب به `_EMPTY_VALUE` تبدیل می‌شوند.
    """
    if not isinstance(case, dict):
        case = {}
    return [
        {
            "label": "Preconditions",
            "body": markdown_bullets(case.get("preconditions"), empty="None"),
        },
        {"label": "Steps", "body": markdown_bullets(case.get("steps"))},
        {
            "label": "Expected Result",
            "body": _case_field(case, "expected_result") or _EMPTY_VALUE,
        },
        {
            "label": "Related Service",
            "body": format_related_service(case.get("related_service")),
        },
    ]


def case_details(result: Any) -> list[dict[str, Any]]:
    """برای هر تست‌کیس یک بلوکِ کاملِ نمایشی می‌سازد.

    ترتیبِ `result["test_cases"]` حفظ می‌شود و هر بلوک دقیقاً ساختارِ یکسان
    دارد — بنابراین صفحه‌ی Streamlit فقط لازم است روی همین لیست حلقه بزند.
    """
    return [
        {
            "heading": case_heading(case),
            "meta": case_meta(case),
            "sections": case_sections(case),
        }
        for case in (result or {}).get("test_cases", [])
    ]


# ── نمایشِ نتیجه‌ی قدم دوم (کشفِ API و نگاشت) ─────────────────────────────────

_NOT_RESOLVED = "Not resolved"


def api_label(api: Any) -> str:
    """برچسبِ خوانای یک عملیاتِ API: «GET /admin/voucher/{id}»."""
    if not isinstance(api, dict):
        return ""
    method = _case_field(api, "method")
    path = _case_field(api, "path")
    if method and path:
        return f"{method} {path}"
    return method or path


def api_catalogue(service: Any) -> list[dict[str, str]]:
    """عملیات‌های یک سرویس را برای فهرستِ خوانا آماده می‌کند."""
    if not isinstance(service, dict):
        return []
    catalogue = []
    for api in service.get("apis") or []:
        if not isinstance(api, dict):
            continue
        catalogue.append(
            {
                "label": api_label(api),
                "operation": _case_field(api, "operation_id"),
                "summary": _case_field(api, "summary"),
            }
        )
    return catalogue


def service_details(result: Any) -> list[dict[str, Any]]:
    """برای هر سرویسِ کشف‌شده یک بلوکِ نمایشی می‌سازد.

    چیزهایی که سند اجازه‌ی تعیینشان را نداده، خالی می‌مانند و در `unresolved`
    گزارش می‌شوند — هیچ مقداری حدس زده نمی‌شود.
    """
    services = []
    for service in (result or {}).get("services", []):
        if not isinstance(service, dict):
            continue
        authorization = service.get("authorization")
        services.append(
            {
                "name": _case_field(service, "name"),
                "source_url": _case_field(service, "source_url"),
                "base_url": _case_field(service, "base_url"),
                "authorization": (
                    _case_field(authorization, "value")
                    if isinstance(authorization, dict)
                    else ""
                ),
                "apis": api_catalogue(service),
                "unresolved": [
                    str(item).strip()
                    for item in (service.get("unresolved") or [])
                    if str(item).strip()
                ],
            }
        )
    return services


def mapping_details(result: Any, test_cases: Any = None) -> list[dict[str, Any]]:
    """برای هر نگاشتِ قدم دوم یک بلوکِ نمایشی می‌سازد.

    عنوانِ هر بلوک از خودِ تست‌کیسِ قدم اول می‌آید (اگر داده شده باشد) تا
    بازبین لازم نباشد بین دو صفحه رفت‌وبرگشت کند.
    """
    titles = {
        str(case.get("id", "")): str(case.get("title", "") or "")
        for case in (test_cases or [])
        if isinstance(case, dict)
    }

    details = []
    for mapping in (result or {}).get("mappings", []):
        if not isinstance(mapping, dict):
            continue
        case_id = str(mapping.get("test_case_id", "") or "")
        api = mapping.get("api")
        resolved = isinstance(api, dict)
        details.append(
            {
                "heading": case_heading(
                    {"id": case_id, "title": titles.get(case_id, "")}
                ),
                "test_case_id": case_id,
                "api": api_label(api) if resolved else "",
                "operation": _case_field(api, "operation_id") if resolved else "",
                "confidence": _case_field(mapping, "confidence"),
                "reason": _case_field(mapping, "reason"),
                "clarification": _case_field(mapping, "clarification"),
                "resolved": resolved,
            }
        )
    return details


def mapping_counts(result: Any) -> dict[str, int]:
    """تعدادِ نگاشت‌های حل‌شده و حل‌نشده — برای خلاصه‌ی بالای صفحه."""
    counts = {"resolved": 0, "unresolved": 0}
    for mapping in (result or {}).get("mappings", []):
        if not isinstance(mapping, dict):
            continue
        counts["resolved" if isinstance(mapping.get("api"), dict) else "unresolved"] += 1
    return counts


# ── نمایشِ نتیجه‌ی قدم سوم (سناریو، ترتیبِ اجرا و وابستگی‌های داده) ─────────────


def _case_titles(test_cases: Any) -> dict[str, str]:
    """نگاشتِ شناسه‌ی تست‌کیس به عنوانش — از نتیجه‌ی قدم اول."""
    return {
        str(case.get("id", "")): str(case.get("title", "") or "")
        for case in (test_cases or [])
        if isinstance(case, dict)
    }


def _case_heading_for(case_id: Any, titles: dict[str, str]) -> str:
    """عنوانِ خوانای یک تست‌کیس با شناسه‌اش: «TC-001 — Create Voucher»."""
    key = str(case_id or "")
    return case_heading({"id": key, "title": titles.get(key, "")})


def scenario_details(result: Any, test_cases: Any = None) -> list[dict[str, Any]]:
    """برای هر سناریو یک بلوکِ نمایشی می‌سازد.

    ترتیبِ تست‌کیس‌ها همان ترتیبی است که تحلیل تعیین کرده و شماره‌گذاری از یک
    شروع می‌شود تا بازبین بتواند سناریو را خط‌به‌خط مرور کند.
    """
    titles = _case_titles(test_cases)
    details = []
    for scenario in (result or {}).get("scenarios", []):
        if not isinstance(scenario, dict):
            continue
        scenario_id = _case_field(scenario, "id")
        title = _case_field(scenario, "title")
        cases = []
        for position, case_id in enumerate(scenario.get("test_case_ids") or [], start=1):
            cases.append(
                {
                    "position": position,
                    "test_case_id": str(case_id),
                    "heading": _case_heading_for(case_id, titles),
                }
            )
        details.append(
            {
                "id": scenario_id,
                "title": title,
                "heading": case_heading({"id": scenario_id, "title": title}),
                "reason": _case_field(scenario, "reason"),
                "cases": cases,
            }
        )
    return details


def execution_steps(result: Any, test_cases: Any = None) -> list[dict[str, Any]]:
    """برای هر تست‌کیسِ دارای ترتیب، یک بلوکِ نمایشی می‌سازد."""
    titles = _case_titles(test_cases)
    steps = []
    for step in (result or {}).get("execution_order", []):
        if not isinstance(step, dict):
            continue
        case_id = str(step.get("test_case_id", "") or "")
        depends_on = [
            {
                "test_case_id": str(dependency),
                "heading": _case_heading_for(dependency, titles),
            }
            for dependency in step.get("depends_on") or []
        ]
        steps.append(
            {
                "test_case_id": case_id,
                "order": step.get("order", ""),
                "heading": _case_heading_for(case_id, titles),
                "depends_on": depends_on,
                "depends_on_label": ", ".join(
                    item["heading"] for item in depends_on
                ),
            }
        )
    return steps


def execution_arrow(result: Any, test_cases: Any = None) -> str:
    """ترتیبِ اجرا را در یک خط نشان می‌دهد: «TC-001 → TC-002 → TC-003»."""
    return " → ".join(
        step["heading"] or step["test_case_id"]
        for step in execution_steps(result, test_cases)
    )


def dependency_details(result: Any, test_cases: Any = None) -> list[dict[str, Any]]:
    """برای هر وابستگیِ داده یک بلوکِ نمایشی می‌سازد.

    برچسب‌ها همان چیزی را می‌گویند که قدم چهارم لازم دارد: مقدار از کجا
    می‌آید و در کجای درخواستِ مقصدها مصرف می‌شود.
    """
    titles = _case_titles(test_cases)
    details = []
    for dependency in (result or {}).get("data_dependencies", []):
        if not isinstance(dependency, dict):
            continue

        source = dependency.get("source")
        source = source if isinstance(source, dict) else {}
        source_case = str(source.get("test_case_id", "") or "")
        source_location = _case_field(source, "location")
        source_path = _case_field(source, "path").lstrip("$").lstrip(".")

        targets = []
        for target in dependency.get("targets") or []:
            if not isinstance(target, dict):
                continue
            target_case = str(target.get("test_case_id", "") or "")
            location = _case_field(target, "location")
            parameter = _case_field(target, "parameter")
            targets.append(
                {
                    "test_case_id": target_case,
                    "heading": _case_heading_for(target_case, titles),
                    "location": location,
                    "parameter": parameter,
                    "label": f"{target_case} → {location}.{parameter}".rstrip("."),
                }
            )

        details.append(
            {
                "variable_name": _case_field(dependency, "variable_name"),
                "confidence": _case_field(dependency, "confidence"),
                "reason": _case_field(dependency, "reason"),
                "source": {
                    "test_case_id": source_case,
                    "heading": _case_heading_for(source_case, titles),
                    "location": source_location,
                    "path": source_path,
                    # منبعِ غایب برچسبِ خالی می‌گیرد، نه « → » بی‌محتوا
                    "label": (
                        f"{source_case} → {source_location}.{source_path}".rstrip(".")
                        if source_case
                        else ""
                    ),
                },
                "targets": targets,
            }
        )
    return details


def clarification_details(result: Any, test_cases: Any = None) -> list[dict[str, Any]]:
    """برای هر ابهامِ قدم سوم یک بلوکِ نمایشی می‌سازد."""
    titles = _case_titles(test_cases)
    details = []
    for clarification in (result or {}).get("clarifications", []):
        if not isinstance(clarification, dict):
            continue
        case_id = _case_field(clarification, "test_case_id")
        details.append(
            {
                "type": _case_field(clarification, "type"),
                "test_case_id": case_id,
                "heading": _case_heading_for(case_id, titles) if case_id else "",
                "message": _case_field(clarification, "message"),
            }
        )
    return details


def step3_counts(result: Any) -> dict[str, int]:
    """شمارشِ خلاصه‌ی نتیجه‌ی قدم سوم — برای متریک‌های بالای صفحه."""
    result = result or {}
    return {
        "scenarios": len(result.get("scenarios") or []),
        "ordered": len(result.get("execution_order") or []),
        "dependencies": len(result.get("data_dependencies") or []),
        "clarifications": len(result.get("clarifications") or []),
    }


# ── نمایشِ نتیجه‌ی قدم چهارم (ساختِ Postman Collection) ──────────────────────
# این‌جا هیچ ساختاری از کالکشن بازسازی نمی‌شود: کالکشن همان چیزی است که
# PostmanBuilder ساخته و فقط برای نمایش/دانلود آماده می‌شود.

_UNSAFE_FILENAME_RE = re.compile(r"[^a-z0-9]+")


def step4_counts(result: Any) -> dict[str, int]:
    """شمارشِ خلاصه‌ی نتیجه‌ی قدم چهارم — برای متریک‌های بالای صفحه."""
    result = result or {}
    return {
        "scenarios": len(result.get("scenarios") or []),
        "requests": len(result.get("requests") or []),
        "unresolved": len(result.get("unresolved") or []),
        "clarifications": len(result.get("clarifications") or []),
    }


def step4_request_rows(result: Any) -> list[dict[str, str]]:
    """یک ردیف برای هر درخواستِ ساخته‌شده — با سناریویی که در آن اجرا می‌شود."""
    rows = []
    for request in (result or {}).get("requests") or []:
        if not isinstance(request, dict):
            continue
        rows.append(
            {
                "Request": _case_field(request, "name"),
                "Method": _case_field(request, "method"),
                "Path": _case_field(request, "path"),
                "Scenario": _case_field(request, "scenario") or _EMPTY_VALUE,
            }
        )
    return rows


def step4_unresolved_rows(result: Any) -> list[dict[str, str]]:
    """تست‌کیس‌هایی که به درخواست تبدیل نشدند — هیچ‌کدام پنهان نمی‌شود."""
    rows = []
    for item in (result or {}).get("unresolved") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "Test case": case_heading(
                    {
                        "id": _case_field(item, "test_case_id"),
                        "title": _case_field(item, "title"),
                    }
                ),
                "Reason": _case_field(item, "reason"),
            }
        )
    return rows


def step4_warnings(result: Any) -> list[str]:
    """هشدارهای قدم چهارم — مثلاً ناسازگاریِ ترتیبِ اجرا با یک وابستگیِ داده."""
    return [
        str(warning).strip()
        for warning in (result or {}).get("warnings") or []
        if str(warning or "").strip()
    ]


def collection_filename(collection_name: Any) -> str:
    """نامِ فایلِ خروجی از نامِ کالکشن — با همان قراردادِ نام‌گذاریِ پروژه."""
    slug = _UNSAFE_FILENAME_RE.sub(
        "_", str(collection_name or "").strip().lower()
    ).strip("_")
    return f"{slug or 'postman_collection'}.postman_collection.json"


def collection_json(collection: Any) -> str:
    """کالکشن را برای نمایش/دانلود به JSON خوانا تبدیل می‌کند.

    اگر کالکشن قابلِ سریال‌سازی نباشد، یک JSON معتبر با پیامِ خطا برگردانده
    می‌شود تا صفحه‌ی UI نشکند و مشکل پنهان نماند.
    """
    try:
        return json.dumps(collection, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        return json.dumps(
            {"error": f"the collection could not be serialized: {exc}"},
            ensure_ascii=False,
            indent=2,
        )


# ── نمایشِ نتیجه‌ی قدم پنجم (بازبینیِ نهایی و صادرات) ────────────────────────
# قدم پنجم چیزی تولید نمی‌کند؛ این توابع فقط گزارشِ آن را برای نمایش آماده
# می‌کنند. کالکشنِ صادرشده همان شیءِ دست‌نخورده‌ی قدم چهارم است و هیچ فراداده‌ای
# به آن اضافه نمی‌شود — به همین دلیل collection_json و collection_filename قدم
# چهارم این‌جا دوباره استفاده می‌شوند.

_STATUS_LABELS = {
    "ready": "Ready",
    "needs_review": "Needs review",
    "blocked": "Blocked",
}

_METRIC_KEYS = (
    "test_cases",
    "mapped_test_cases",
    "generated_requests",
    "scenarios",
    "data_dependencies",
    "clarifications",
    "issues",
    "warnings",
)


def _review_section(result: Any) -> dict:
    """بخشِ review نتیجه‌ی قدم پنجم — اگر نباشد یک دیکشنریِ خالی."""
    review = (result or {}).get("review")
    return review if isinstance(review, dict) else {}


def _count(value: Any) -> int:
    """شمارشِ معتبر — هر چیزِ دیگری صفر، تا جدول ناقص نماند."""
    return value if isinstance(value, int) and value > 0 else 0


def _step_label(step: Any) -> str:
    """«Step 3» — قدمِ نامعتبر جای‌نگهدارِ خالی می‌گیرد."""
    return f"Step {step}" if isinstance(step, int) and step > 0 else _EMPTY_VALUE


def step5_status(result: Any) -> str:
    """وضعیتِ بازبینی (ready / needs_review / blocked) — ناشناخته دست‌نخورده."""
    return _case_field(_review_section(result), "status")


def step5_status_label(result: Any) -> str:
    """برچسبِ خوانای وضعیت — وضعیتِ ناشناخته بدونِ تغییر نمایش داده می‌شود."""
    status = step5_status(result)
    return _STATUS_LABELS.get(status, status or _EMPTY_VALUE)


def step5_summary(result: Any) -> str:
    """خلاصه‌ی متنیِ قطعیِ قدم پنجم — همان متنِ تولیدشده، بدونِ بازنویسی."""
    return _case_field(_review_section(result), "summary")


def step5_metrics(result: Any) -> dict[str, int]:
    """متریک‌های قطعیِ قدم پنجم — هر کلیدِ غایب صفر می‌شود."""
    metrics = (result or {}).get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    return {key: _count(metrics.get(key)) for key in _METRIC_KEYS}


def step5_issue_rows(result: Any) -> list[dict[str, str]]:
    """issue های بازدارنده — با قدمِ منبع، کد و تست‌کیسِ درگیر."""
    rows = []
    for issue in _review_section(result).get("issues") or []:
        if not isinstance(issue, dict):
            continue
        rows.append(
            {
                "Step": _step_label(issue.get("step")),
                "Code": _case_field(issue, "code") or _EMPTY_VALUE,
                "Test case": _case_field(issue, "test_case_id") or _EMPTY_VALUE,
                "Message": _case_field(issue, "message"),
            }
        )
    return rows


def step5_review_item_rows(result: Any) -> list[dict[str, str]]:
    """مواردِ حل‌نشده — ابهام‌های قدم‌های ۱ تا ۳ و تست‌کیس‌های بدونِ درخواست."""
    rows = []
    section = _review_section(result)
    for item in section.get("unresolved_clarifications") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "Step": _step_label(item.get("source_step")),
                "Kind": _case_field(item, "kind") or _EMPTY_VALUE,
                "Test case": _case_field(item, "test_case_id") or _EMPTY_VALUE,
                "Message": _case_field(item, "message"),
            }
        )
    return rows


def step5_warnings(result: Any) -> list[str]:
    """هشدارهای بازبینی — همه، بدونِ پنهان‌کردن یا خلاصه‌کردن."""
    return [
        str(warning).strip()
        for warning in _review_section(result).get("warnings") or []
        if str(warning or "").strip()
    ]


def postman_collection(result: Any) -> dict:
    """کالکشنِ صادرشده — همان شیءِ دست‌نخورده‌ی قدم چهارم."""
    artifacts = (result or {}).get("artifacts")
    if not isinstance(artifacts, dict):
        return {}
    collection = artifacts.get("postman_collection")
    return collection if isinstance(collection, dict) else {}


def collection_name(collection: Any) -> str:
    """نامِ کالکشن از خودِ کالکشن — برای نامِ فایلِ صادرات."""
    if not isinstance(collection, dict):
        return ""
    return _case_field(collection.get("info"), "name")
