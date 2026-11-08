"""
src/testcase_box/preparation.py — آماده‌سازیِ payloadِ اجرا برای یک نسخه (M4، تابعِ خالص)

    result = prepare_case(
        version,
        host_intent=HostIntent.NORMAL,
        profile=ExecutionProfile("staging", resolved_variables={"base_url", "userId"}),
        approval_state=store.script_approval_state(version.id),
    )

ترکیبِ چهار بررسی، به این ترتیب:
  1. readiness ِ M3 (assess) همان‌طور که هست — هیچ قاعده‌ای ضعیف نمی‌شود و هیچ تعمیری
     (Bearer، اعتبارنامه، URL) انجام نمی‌شود؛
  2. نیتِ میزبان: NORMAL دقیقاً {{base_url}} و VDI دقیقاً {{base_url_vdi}} می‌خواهد.
     میزبان هرگز عوض نمی‌شود — Base Path ِ سرویس هنگامِ تولید در مسیر پخته شده و فقط با
     همان میزبان معنا دارد. ارجاعِ دیگر به متغیرِ میزبانِ مقابل (بیرون از host ِ URL، در
     درخواست یا اسکریپت) هم مسدودکننده است و بازنویسی نمی‌شود؛
  3. پروفایلِ اجرا (فقط نام‌ها، profiles.py) در برابرِ زمان‌بندیِ واقعیِ Newman:
     * set در اسکریپتِ prerequest پیش از resolve ِ درخواست اجرا می‌شود (postman-runtime:
       item.command ← prerequest، سپس request.command ← resolveVariables) پس تأمین‌کننده است؛
     * set در اسکریپتِ test پس از ارسال است و هرگز درخواستِ همان case را تأمین نمی‌کند؛
     * token را runtime تزریق می‌کند؛ {{$name}} ِ شناخته‌شده را Postman می‌سازد و ناشناخته
       literal ارسال می‌شود (مسدودکننده)؛
     * متغیرِ caseِ دیگر در M4 حل نمی‌شود — فقط اگر در پروفایل صریحاً آمده باشد؛
  4. ساختِ نسخه‌ی مستقلِ payload. تنها تبدیل‌های مجاز (همه در transformations ثبت می‌شوند):
     * AUTH_VALUE_CANONICALIZED — مقدارِ فعالِ Authorization که پس از trim دقیقاً {{token}}
       است، دقیقاً {{token}} می‌شود («Bearer » هرگز اضافه نمی‌شود)؛
     * METHOD_UPPERCASED — method به شکلِ متعارف (trim و حروفِ بزرگ؛ SDK هم بزرگ می‌کند).
     بقیه — URL (raw، host، path، query ِ تکراری/غیرفعال، encoding، variable)، هدرها، بدنه،
     اسکریپت‌ها، description و response — بی‌تغییر کپی می‌شوند؛ رشته‌ای بازسازی نمی‌شود.

وضعیت (اولویت): NOT_PREPARABLE (هر ERROR) ← NEEDS_SCRIPT_REVIEW (REVIEW) ←
NEEDS_CONFIGURATION (CONFIGURATION) ← PREPARED. فقط PREPARED payload و
prepared_fingerprint دارد؛ نتیجه‌ی ناقص هرگز برگردانده نمی‌شود.

PREPARED یعنی «payload با پروفایلِ داده‌شده قراردادِ آماده‌سازیِ M4 را برآورده می‌کند».
موفقیتِ اجرا، امنیتِ اسکریپت یا ثابت ماندنِ درخواست را تضمین نمی‌کند: اسکریپتِ prerequest
می‌تواند url/method/headers/body/auth را تغییر دهد (ALLOWED_REQUEST_MUTATIONS ِ
postman-runtime). SCRIPT_MUTATES_REQUEST فقط شکل‌های مستقیم را می‌بیند
(pm.request.<…> = …، pm.request.<…>.add/upsert/remove/…(…)، set ِ متغیرِ میزبان یا token)؛
alias (const r = pm.request)، دسترسیِ کروشه‌ای، eval و نامِ محاسبه‌شده دیده نمی‌شوند — نبودنِ
این هشدار ضمانتِ امنیتی نیست. unset هم مثلِ M3 یک «نوشتن» شمرده می‌شود.

خالص: بی‌پایگاه‌داده، بی‌فایل، بی‌شبکه، بی‌متغیرِ محیطی و بی‌توکن؛ نسخه و اثرانگشت‌های
ذخیره‌شده تغییر نمی‌کنند. source_fingerprint همان الگوریتمِ M2 است (sha256_json ِ کلِ
payload) و prepared_fingerprint همان الگوریتم روی خروجیِ دقیق. پیام‌ها مقدار یا متنِ
اسکریپت ندارند.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.testcase_box.contract import (
    HOST_VARIABLES,
    KIND_EXTERNAL,
    KIND_HOST,
    KIND_TOKEN,
    SEVERITY_CONFIGURATION,
    SEVERITY_ERROR,
    SEVERITY_REVIEW,
    TOKEN_REFERENCE,
    TOKEN_VARIABLE,
    Finding,
    finding,
    ordered,
    safe_name,
)
from src.testcase_box.models import CaseVersion, from_json, sha256_json, to_json
from src.testcase_box.payload import PayloadReport, inspect_payload
from src.testcase_box.profiles import (
    DYNAMIC_VARIABLES,
    HOST_VARIABLE_FOR_INTENT,
    ExecutionProfile,
    HostIntent,
)
from src.testcase_box.readiness import assess
from src.testcase_box.scripts import ScriptReport, inspect_scripts, script_texts

STATUS_PREPARED = "PREPARED"
STATUS_NEEDS_SCRIPT_REVIEW = "NEEDS_SCRIPT_REVIEW"
STATUS_NEEDS_CONFIGURATION = "NEEDS_CONFIGURATION"
STATUS_NOT_PREPARABLE = "NOT_PREPARABLE"

TRANSFORM_AUTH = "AUTH_VALUE_CANONICALIZED"
TRANSFORM_METHOD = "METHOD_UPPERCASED"

# منبعِ مقدارِ هر متغیر در لحظه‌ی استفاده
SOURCE_PROFILE = "PROFILE"
SOURCE_EMPTY = "EMPTY"
SOURCE_RUNTIME = "RUNTIME"
SOURCE_SCRIPT = "SCRIPT"
SOURCE_CONFLICT = "CONFLICT"
SOURCE_UNAVAILABLE = "UNAVAILABLE"

_MUTATING_CALLS = (
    "add|addHeader|addQueryParams|append|assimilate|authorizeUsing|clear|insert|insertAfter|"
    "populate|prepend|remove|removeHeader|removeQueryParams|repopulate|update|upsert|upsertHeader"
)
# pm.request با زنجیره‌ی اختیاریِ .member، سپس انتسابِ ساده (نه == یا =>) یا فراخوانیِ متدِ تغییردهنده
_REQUEST_MUTATION_RE = re.compile(
    r"\bpm\s*\.\s*request(?:\s*\.\s*[A-Za-z_$][\w$]*)*?"
    r"(?:\s*=(?![=>])|\s*\.\s*(?:" + _MUTATING_CALLS + r")\s*\()"
)
# ارجاع در یک ورودیِ فهرستی؛ ورودیِ disabled ارسال نمی‌شود
_ENTRY_LOCATION_RE = re.compile(
    r"^\$\.request\.(header|url\.query|body\.urlencoded|body\.formdata)\[(\d+)\]\."
)


@dataclass(frozen=True)
class VariableAvailability:
    name: str
    kind: str
    source: str
    locations: tuple[str, ...]


@dataclass(frozen=True)
class PreparationResult:
    """نتیجه‌ی قطعیِ آماده‌سازی. prepared_payload فقط برای PREPARED وجود دارد و نسخه‌ای
    مستقل است (تغییرش نسخه‌ی ذخیره‌شده را عوض نمی‌کند). PREPARED ضمانتِ اجرا نیست."""

    version_id: int
    profile_id: str
    host_intent: str | None
    status: str
    readiness_status: str
    selected_host_variable: str | None
    findings: tuple[Finding, ...]
    required_variables: tuple[VariableAvailability, ...]
    provided_variables: tuple[str, ...]
    transformations: tuple[str, ...]
    prepared_payload: dict | None
    source_fingerprint: str
    prepared_fingerprint: str | None

    @property
    def prepared(self) -> bool:
        return self.status == STATUS_PREPARED


def prepare_case(
    version: CaseVersion,
    *,
    host_intent: HostIntent,
    profile: ExecutionProfile,
    approval_state: str,
) -> PreparationResult:
    if not isinstance(version, CaseVersion):
        raise TypeError("version must be a CaseVersion.")
    if not isinstance(profile, ExecutionProfile):
        raise TypeError("profile must be an ExecutionProfile.")
    payload = version.payload
    readiness = assess(version, approval_state)
    payload_report = inspect_payload(payload)
    script_report = inspect_scripts(payload, version.script_fingerprint)
    findings: list[Finding] = list(readiness.findings)

    source_fingerprint = sha256_json(payload)
    if version.fingerprint != source_fingerprint:
        findings.append(finding("SOURCE_FINGERPRINT_DIFFERS", "$"))

    intent = _intent(host_intent)
    selected = None
    if intent is None:
        findings.append(finding("HOST_INTENT_INVALID", "$"))
    elif readiness.host_variable is not None:
        expected = HOST_VARIABLE_FOR_INTENT[intent]
        if readiness.host_variable != expected:
            findings.append(
                finding(
                    "HOST_INTENT_MISMATCH",
                    "$.request.url.host",
                    intent=intent.value,
                    expected=expected,
                    actual=readiness.host_variable,
                )
            )
        else:
            selected = expected
            if selected in profile.empty_variables:
                findings.append(finding("PROFILE_HOST_EMPTY", "$.request.url.host", name=selected))
            elif selected not in profile.resolved_variables:
                findings.append(
                    finding("PROFILE_HOST_MISSING", "$.request.url.host", name=selected)
                )
    if TOKEN_VARIABLE in profile.resolved_variables | profile.empty_variables:
        findings.append(finding("PROFILE_TOKEN_IGNORED", "$", name=TOKEN_VARIABLE))

    variables = _variables(payload, payload_report, script_report, profile, selected, findings)
    findings.extend(_mutations(payload, script_report))
    result = ordered(findings)
    status = _status(result)

    prepared_payload, transformations, prepared_fingerprint = None, (), None
    if status == STATUS_PREPARED:
        prepared_payload, transformations = _prepare(payload)
        prepared_fingerprint = sha256_json(prepared_payload)
    return PreparationResult(
        version_id=version.id,
        profile_id=profile.profile_id,
        host_intent=intent.value if intent is not None else None,
        status=status,
        readiness_status=readiness.status,
        selected_host_variable=selected,
        findings=result,
        required_variables=variables,
        provided_variables=readiness.provided_variables,
        transformations=transformations,
        prepared_payload=prepared_payload,
        source_fingerprint=source_fingerprint,
        prepared_fingerprint=prepared_fingerprint,
    )


def _intent(value: Any) -> HostIntent | None:
    """فقط عضوِ HostIntent یا دقیقاً مقدارِ رشته‌ای‌اش؛ هیچ حدسی (حروفِ کوچک، نامِ میزبان)."""
    if isinstance(value, str) and value in tuple(HostIntent):
        return HostIntent(value)
    return None


def _status(findings: tuple[Finding, ...]) -> str:
    severities = {item.severity for item in findings}
    if SEVERITY_ERROR in severities:
        return STATUS_NOT_PREPARABLE
    if SEVERITY_REVIEW in severities:
        return STATUS_NEEDS_SCRIPT_REVIEW
    if SEVERITY_CONFIGURATION in severities:
        return STATUS_NEEDS_CONFIGURATION
    return STATUS_PREPARED


def _disabled(payload: Any, location: str) -> bool:
    """ارجاع در هدر/query/فرمِ disabled است؟ (آن ورودی ارسال نمی‌شود.)"""
    match = _ENTRY_LOCATION_RE.match(location)
    if match is None or not isinstance(payload, dict):
        return False
    node: Any = payload.get("request")
    for key in match.group(1).split("."):
        node = node.get(key) if isinstance(node, dict) else None
    index = int(match.group(2))
    if not isinstance(node, list) or index >= len(node) or not isinstance(node[index], dict):
        return False
    return node[index].get("disabled") is True


def _variables(
    payload: Any,
    payload_report: PayloadReport,
    script_report: ScriptReport,
    profile: ExecutionProfile,
    selected: str | None,
    findings: list[Finding],
) -> tuple[VariableAvailability, ...]:
    """منبعِ هر متغیر در لحظه‌ی استفاده؛ یافته‌ها به findings اضافه می‌شوند."""
    set_before_request = {w.name for w in script_report.writes if w.listen == "prerequest"}
    set_anywhere = {w.name for w in script_report.writes}
    opposite = next((n for n in HOST_VARIABLES if n != selected), None) if selected else None
    # name → [(location، از درخواست است؟، اسکریپتِ همین case پیش از استفاده تأمینش می‌کند؟)]
    uses: dict[str, list[tuple[str, bool, bool]]] = {}

    for reference in payload_report.references:
        name, location = reference.name, reference.location
        if name.startswith("$"):
            if name not in DYNAMIC_VARIABLES and not _disabled(payload, location):
                findings.append(finding("DYNAMIC_VARIABLE_UNKNOWN", location, name=safe_name(name)))
            continue
        if _disabled(payload, location) and name != opposite:
            continue
        uses.setdefault(name, []).append((location, True, name in set_before_request))
    for read in script_report.reads:
        if read.name.startswith("$"):
            continue
        satisfiers = set_before_request if read.listen == "prerequest" else set_anywhere
        uses.setdefault(read.name, []).append((read.location, False, read.name in satisfiers))
    if opposite is not None:
        for write in script_report.writes:
            if write.name == opposite:
                uses.setdefault(opposite, []).append((write.location, False, True))

    result = []
    for name in sorted(uses):
        locations = tuple(sorted({location for location, _, _ in uses[name]}))
        if name == TOKEN_VARIABLE:
            result.append(VariableAvailability(name, KIND_TOKEN, SOURCE_RUNTIME, locations))
            continue
        if name in HOST_VARIABLES:
            if name == opposite:
                for location in locations:
                    findings.append(finding("HOST_REFERENCE_CONFLICT", location, name=name))
                source = SOURCE_CONFLICT
            else:
                source = _profile_source(name, profile)  # یافته‌اش یک بار، روی url.host
            result.append(VariableAvailability(name, KIND_HOST, source, locations))
            continue
        pending = [(location, from_request) for location, from_request, ok in uses[name] if not ok]
        if not pending:
            source = SOURCE_SCRIPT
        else:
            source = _profile_source(name, profile)
            for location, from_request in pending:
                if source == SOURCE_EMPTY:
                    findings.append(finding("VARIABLE_EMPTY", location, name=safe_name(name)))
                elif source == SOURCE_UNAVAILABLE:
                    code = "VARIABLE_UNAVAILABLE" if from_request else "SCRIPT_VARIABLE_UNAVAILABLE"
                    findings.append(finding(code, location, name=safe_name(name)))
        result.append(VariableAvailability(name, KIND_EXTERNAL, source, locations))
    return tuple(result)


def _profile_source(name: str, profile: ExecutionProfile) -> str:
    if name in profile.resolved_variables:
        return SOURCE_PROFILE
    if name in profile.empty_variables:
        return SOURCE_EMPTY
    return SOURCE_UNAVAILABLE


def _mutations(payload: Any, script_report: ScriptReport) -> list[Finding]:
    """تشخیصِ ایستا و قطعیِ شکل‌های مستقیمِ تغییرِ درخواست در prerequest — کامل نیست."""
    findings = []
    for script in script_texts(payload):
        if script.listen == "prerequest" and _REQUEST_MUTATION_RE.search(script.text):
            findings.append(finding("SCRIPT_MUTATES_REQUEST", script.location, target="request"))
    for write in script_report.writes:
        if write.listen != "prerequest":
            continue
        if write.name in HOST_VARIABLES:
            target = "host variable"
        elif write.name == TOKEN_VARIABLE:
            target = "token"
        else:
            continue
        findings.append(finding("SCRIPT_MUTATES_REQUEST", write.location, target=target))
    return findings


def _prepare(payload: dict) -> tuple[dict, tuple[str, ...]]:
    """نسخه‌ی مستقل با دو تبدیلِ مجاز. فقط برای PREPARED صدا زده می‌شود، پس شکلِ
    request/method/header را M3 تضمین کرده است."""
    prepared = from_json(to_json(payload))
    applied = set()
    request = prepared["request"]
    canonical = request["method"].strip().upper()
    if canonical != request["method"]:
        request["method"] = canonical
        applied.add(TRANSFORM_METHOD)
    for header in request.get("header") or []:
        value = header.get("value")
        if (
            header.get("disabled") is not True
            and header["key"].strip().lower() == "authorization"
            and isinstance(value, str)
            and value != TOKEN_REFERENCE
            and value.strip() == TOKEN_REFERENCE
        ):
            header["value"] = TOKEN_REFERENCE
            applied.add(TRANSFORM_AUTH)
    return prepared, tuple(sorted(applied))
