"""
src/testcase_box/contract.py — قراردادِ payload و مدلِ نتیجه‌ی readiness

payloadِ هر نسخه دقیقاً یک itemِ درخواستِ Postman v2.1 است (نه folder، نه collection).
این ماژول ثابت‌ها، کدهای پایدارِ مسئله، شدت‌ها و رکوردهای نتیجه را تعریف می‌کند؛
بررسی‌ها در payload.py و scripts.py و ترکیبشان در readiness.py است.

ثابت‌ها عمداً این‌جا کپی شده‌اند (نه import): importِ host_selection کلِ گرافِ agent و
importِ بسته‌ی newman موتورِ اجرا را بار می‌کند. تست‌ها هم‌خوانیِ آن‌ها را با منبعِ
اصلی (host_selection، newman.models، newman.redaction) بررسی می‌کنند.

تشخیصِ اعتبارنامه اکتشافی است (نام‌ها و شکل‌ها)، مرزِ امنیتیِ کامل نیست. پیام‌ها هرگز
مقدارِ هدر، query، بدنه، اعتبارنامه یا متنِ اسکریپت را در خود ندارند — فقط کد، مسیرِ
فیلد، نام‌های امن و شمارش.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── ثابت‌های قرارداد (هم‌خوان با host_selection و newman؛ تست‌ها بررسی می‌کنند) ──
BASE_URL = "base_url"
BASE_URL_VDI = "base_url_vdi"
HOST_VARIABLES = (BASE_URL, BASE_URL_VDI)
TOKEN_VARIABLE = "token"
TOKEN_REFERENCE = "{{" + TOKEN_VARIABLE + "}}"
SUPPORTED_METHODS = ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT")
SCRIPT_EVENTS = ("prerequest", "test")

# همان الگوی postman-collection (Substitutor.REGEX_EXTRACT_VARS): بی‌trim، بی‌تودرتو
VARIABLE_RE = re.compile(r"\{\{([^{}]*?)}}")

# هم‌خوان با src/test_execution/newman/redaction.py
SECRET_KEY_RE = re.compile(
    r"token|passw|pwd|secret|authori[sz]ation|cookie|api[-_]?key|session|jwt|bearer|"
    r"credential|private[-_]?key|access[-_]?key|otp",
    re.IGNORECASE,
)
SENSITIVE_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token"}
)
CORS_HEADERS = frozenset(
    {
        "access-control-allow-credentials",
        "access-control-allow-headers",
        "access-control-allow-methods",
        "access-control-allow-origin",
        "access-control-allow-private-network",
        "access-control-expose-headers",
        "access-control-max-age",
        "access-control-request-headers",
        "access-control-request-method",
        "access-control-request-private-network",
    }
)


def is_secret_key(name: object) -> bool:
    return isinstance(name, str) and bool(SECRET_KEY_RE.search(name))


def is_sensitive_header(name: object) -> bool:
    if not isinstance(name, str):
        return False
    lowered = name.strip().lower()
    if lowered in CORS_HEADERS:
        return False
    return lowered in SENSITIVE_HEADERS or is_secret_key(name)


def is_variable_reference(value: object) -> bool:
    """دقیقاً یک ارجاعِ «{{name}}» (پس از trimِ دو طرف)."""
    return isinstance(value, str) and bool(VARIABLE_RE.fullmatch(value.strip()))


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.$:\-]{1,64}$")


def safe_name(value: object) -> str:
    """نام (هدر، متغیر، فیلد) فقط اگر شکلِ شناسه دارد در پیام می‌آید؛ وگرنه جای‌نگه‌دار."""
    return value if isinstance(value, str) and _SAFE_NAME_RE.match(value) else "<unnamed>"


# ── شدت و وضعیت ─────────────────────────────────────────────────────────────
SEVERITY_ERROR = "ERROR"  # مسدودکننده → NOT_EXECUTABLE
SEVERITY_REVIEW = "REVIEW"  # بازبینی/تأییدِ اسکریپت لازم است → NEEDS_SCRIPT_REVIEW
# فقط کدهای آماده‌سازیِ M4 (preparation.py): پروفایلِ اجرا متغیرِ لازم را تأمین نمی‌کند →
# NEEDS_CONFIGURATION. status_for ِ M3 آن را نمی‌بیند چون M3 هرگز تولیدش نمی‌کند.
SEVERITY_CONFIGURATION = "CONFIGURATION"
SEVERITY_WARNING = "WARNING"
SEVERITY_INFO = "INFO"
SEVERITY_RANK = {
    SEVERITY_ERROR: 0,
    SEVERITY_REVIEW: 1,
    SEVERITY_CONFIGURATION: 2,
    SEVERITY_WARNING: 3,
    SEVERITY_INFO: 4,
}

# READY یعنی «مطابقِ قراردادِ M3»؛ موفقیت یا امنیتِ اجرای واقعی را تضمین نمی‌کند —
# متغیرهای بیرونی هنوز در برابرِ پروفایلِ اجرا سنجیده نشده‌اند.
STATUS_READY = "READY"
STATUS_NEEDS_SCRIPT_REVIEW = "NEEDS_SCRIPT_REVIEW"
STATUS_NOT_EXECUTABLE = "NOT_EXECUTABLE"

KIND_HOST = "HOST"
KIND_TOKEN = "TOKEN"
KIND_EXTERNAL = "EXTERNAL"

# ── کدهای مسئله: code → (شدت، الگوی پیامِ امن) ───────────────────────────────
ISSUES: dict[str, tuple[str, str]] = {
    # ساختار
    "PAYLOAD_NOT_OBJECT": (SEVERITY_ERROR, "The payload must be a JSON object (one request item)."),
    "PAYLOAD_IS_COLLECTION": (
        SEVERITY_ERROR,
        "The payload is a Postman collection; only a single request item is supported.",
    ),
    "PAYLOAD_IS_FOLDER": (
        SEVERITY_ERROR,
        "The payload is a folder ('item'); only a single request item is supported.",
    ),
    "FIELD_UNSUPPORTED": (SEVERITY_ERROR, "Field '{field}' is not supported in the V1 contract."),
    "FIELD_MALFORMED": (SEVERITY_ERROR, "Field '{field}' has an unsupported value type."),
    "REQUEST_MISSING": (SEVERITY_ERROR, "The item has no 'request'."),
    "REQUEST_NOT_OBJECT": (
        SEVERITY_ERROR,
        "'request' must be an object; the string form is not supported.",
    ),
    "METHOD_MISSING": (SEVERITY_ERROR, "The request has no HTTP method."),
    "METHOD_UNSUPPORTED": (
        SEVERITY_ERROR,
        "The HTTP method is not one of " + ", ".join(SUPPORTED_METHODS) + ".",
    ),
    # URL
    "URL_MISSING": (SEVERITY_ERROR, "The request has no 'url'."),
    "URL_NOT_STRUCTURED": (
        SEVERITY_ERROR,
        "'url' must be a structured object; the string form is not supported.",
    ),
    "URL_MALFORMED": (SEVERITY_ERROR, "URL field '{field}' has an unsupported shape."),
    "URL_RAW_MISSING": (SEVERITY_ERROR, "'url.raw' is missing or empty."),
    "URL_PROTOCOL_UNSUPPORTED": (
        SEVERITY_ERROR,
        "An explicit URL scheme is not supported; the host variable carries it.",
    ),
    "URL_PORT_UNSUPPORTED": (
        SEVERITY_ERROR,
        "An explicit URL port is not supported; the host variable carries it.",
    ),
    "URL_HASH_UNSUPPORTED": (SEVERITY_ERROR, "A URL fragment ('#') is not supported."),
    "URL_COMPONENTS_CONFLICT": (
        SEVERITY_ERROR,
        "'url.raw' and the structured URL disagree on the {component}.",
    ),
    "QUERY_MALFORMED": (SEVERITY_ERROR, "Query parameter '{field}' has an unsupported shape."),
    "PATH_VARIABLE_EMPTY": (SEVERITY_WARNING, "Path variable ':{name}' has no value."),
    # میزبان
    "HOST_MISSING": (SEVERITY_ERROR, "The URL has no host; use {{base_url}} or {{base_url_vdi}}."),
    "HOST_LITERAL": (
        SEVERITY_ERROR,
        "The host is not a supported host variable; use {{base_url}} or {{base_url_vdi}}.",
    ),
    "HOST_VARIABLE_UNSUPPORTED": (
        SEVERITY_ERROR,
        "Host variable '{name}' is not supported; use {{base_url}} or {{base_url_vdi}}.",
    ),
    "HOST_CONFLICT": (SEVERITY_ERROR, "'url.raw' and 'url.host' name different hosts."),
    "HOST_MIXED_REFERENCES": (
        SEVERITY_WARNING,
        "The request also references the other host variable '{name}'.",
    ),
    # هدر و بدنه
    "HEADER_MALFORMED": (SEVERITY_ERROR, "Header entry '{field}' has an unsupported shape."),
    "HEADER_DUPLICATE": (SEVERITY_WARNING, "Header '{name}' is set {count} times."),
    "BODY_MALFORMED": (SEVERITY_ERROR, "Body field '{field}' has an unsupported shape."),
    "BODY_MODE_UNSUPPORTED": (SEVERITY_ERROR, "Body mode '{mode}' is not supported."),
    "BODY_FILE_UPLOAD": (SEVERITY_ERROR, "File uploads in form data are not supported."),
    # احراز هویت و اعتبارنامه
    "AUTH_HEADER_MISSING": (
        SEVERITY_WARNING,
        "No 'Authorization: {{token}}' header; the request will be sent without a token.",
    ),
    "AUTH_DUPLICATE_BEARER": (
        SEVERITY_ERROR,
        "'Authorization' must be exactly {{token}}; the runtime already adds 'Bearer '.",
    ),
    "AUTH_AMBIGUOUS": (SEVERITY_ERROR, "More than one authorization source is configured."),
    "AUTH_BLOCK_UNSUPPORTED": (
        SEVERITY_ERROR,
        "Request auth type '{type}' is not supported; use the header Authorization: {{token}}.",
    ),
    "AUTH_INHERIT": (
        SEVERITY_WARNING,
        "Auth 'inherit' resolves to no authentication for a standalone item.",
    ),
    "AUTH_VARIABLE_UNSUPPORTED": (
        SEVERITY_ERROR,
        "'Authorization' must reference only {{token}}.",
    ),
    "LITERAL_CREDENTIAL": (SEVERITY_ERROR, "A literal credential is stored in {where}."),
    "TOKEN_OUTSIDE_AUTH": (
        SEVERITY_ERROR,
        "{{token}} may appear only in the Authorization header.",
    ),
    "CREDENTIAL_LIKE_VALUE": (
        SEVERITY_WARNING,
        "Field '{name}' looks like a credential and holds a literal value.",
    ),
    # متغیرها
    "VARIABLE_DYNAMIC": (
        SEVERITY_INFO,
        "Postman dynamic variable '{name}' is generated at runtime.",
    ),
    "VARIABLE_MALFORMED": (SEVERITY_WARNING, "An unclosed or empty '{{' reference was found."),
    # اسکریپت‌ها (بازبینیِ ایستا — اجرای واقعی را امن نمی‌کند)
    "SCRIPT_MALFORMED": (SEVERITY_ERROR, "A script entry has an unsupported shape."),
    "SCRIPT_EVENT_UNSUPPORTED": (
        SEVERITY_ERROR,
        "Only 'prerequest' and 'test' script events are supported.",
    ),
    "SCRIPT_SEND_REQUEST": (
        SEVERITY_ERROR,
        "Scripts may not call sendRequest in V1; approval cannot clear this.",
    ),
    "SCRIPT_EXTERNAL_SOURCE": (SEVERITY_ERROR, "Scripts loaded from 'src' are not supported."),
    "SCRIPT_PACKAGES_UNSUPPORTED": (SEVERITY_ERROR, "Script packages are not supported."),
    "SCRIPT_FINGERPRINT_MISMATCH": (
        SEVERITY_ERROR,
        "The stored script fingerprint does not match the scripts in the payload.",
    ),
    "SCRIPT_TOKEN_ACCESS": (SEVERITY_WARNING, "A script reads the token or request headers."),
    "SCRIPT_DYNAMIC_VARIABLE": (
        SEVERITY_WARNING,
        "A script reads or writes a variable with a computed name.",
    ),
    "SCRIPT_REVIEW_REQUIRED": (SEVERITY_REVIEW, "The scripts need an explicit review decision."),
    "SCRIPT_REJECTED": (SEVERITY_REVIEW, "The scripts were rejected by a reviewer."),
    "SCRIPT_APPROVAL_REVOKED": (SEVERITY_REVIEW, "The script approval was revoked."),
    "SCRIPT_APPROVED": (
        SEVERITY_INFO,
        "The scripts are approved (static review only; not a runtime safety guarantee).",
    ),
    # آماده‌سازیِ اجرا (M4) — میزبان، پروفایل و زمان‌بندیِ متغیرها
    "HOST_INTENT_INVALID": (SEVERITY_ERROR, "The host intent must be NORMAL or VDI."),
    "HOST_INTENT_MISMATCH": (
        SEVERITY_ERROR,
        "The {intent} host intent requires {{{{{expected}}}}}, but the case uses "
        "{{{{{actual}}}}}; the host is never rewritten.",
    ),
    "HOST_REFERENCE_CONFLICT": (
        SEVERITY_ERROR,
        "The case references '{name}', which conflicts with the selected host; "
        "it is never rewritten.",
    ),
    "DYNAMIC_VARIABLE_UNKNOWN": (
        SEVERITY_ERROR,
        "'{name}' is not a Postman dynamic variable and would be sent literally.",
    ),
    "PROFILE_HOST_MISSING": (
        SEVERITY_CONFIGURATION,
        "The execution profile does not resolve host variable '{name}'.",
    ),
    "PROFILE_HOST_EMPTY": (
        SEVERITY_CONFIGURATION,
        "Host variable '{name}' resolves to an empty value in the execution profile.",
    ),
    "VARIABLE_UNAVAILABLE": (
        SEVERITY_CONFIGURATION,
        "Variable '{name}' is not available when the request is sent.",
    ),
    "VARIABLE_EMPTY": (
        SEVERITY_WARNING,
        "Variable '{name}' resolves to an empty value in the execution profile.",
    ),
    "SCRIPT_VARIABLE_UNAVAILABLE": (
        SEVERITY_WARNING,
        "A script reads variable '{name}', which is not available when the script runs.",
    ),
    "SCRIPT_MUTATES_REQUEST": (
        SEVERITY_WARNING,
        "A pre-request script may change the {target}; the sent request may differ from "
        "the prepared payload (static detection only).",
    ),
    "PROFILE_TOKEN_IGNORED": (
        SEVERITY_INFO,
        "The profile lists '{name}'; the runtime always injects the token, so it is ignored.",
    ),
    # برنامه‌ریزیِ اجرا (M7.4، run_planning.py) — همه مسدودکننده
    "SELECTION_INVALID": (SEVERITY_ERROR, "The selected test case id is not a positive integer."),
    "SELECTION_DUPLICATE": (
        SEVERITY_ERROR,
        "The test case is selected more than once; a run contains each case once.",
    ),
    "CASE_NOT_FOUND": (SEVERITY_ERROR, "The selected test case does not exist."),
    "CASE_ARCHIVED": (
        SEVERITY_ERROR,
        "The test case is archived; archived cases run only in a rerun of a pinned version.",
    ),
    "PINNED_VERSION_MISSING": (SEVERITY_ERROR, "The pinned test case version does not exist."),
    "PINNED_VERSION_INVALID": (
        SEVERITY_ERROR,
        "The pinned version does not belong to the test case it is pinned for.",
    ),
    "VERSION_INTEGRITY_FAILED": (
        SEVERITY_ERROR,
        "The stored version fingerprint does not match its payload; the version is not trusted.",
    ),
    "EXECUTION_SCRIPTS_PRESENT": (
        SEVERITY_ERROR,
        "The test case contains scripts; V1 executes only script-free test cases "
        "(script approval does not change this).",
    ),
    "EXECUTION_EVENTS_PRESENT": (
        SEVERITY_ERROR,
        "The test case has script event entries at {where}; V1 executes only cases without "
        "events and never removes them.",
    ),
    "EXECUTION_SCRIPT_STRUCTURE_INVALID": (
        SEVERITY_ERROR,
        "Script presence cannot be ruled out at {where}; the case is not executed.",
    ),
    "EXECUTION_NOT_PREPARED": (
        SEVERITY_ERROR,
        "The test case could not be prepared for execution ({status}).",
    ),
    "SOURCE_FINGERPRINT_DIFFERS": (
        SEVERITY_INFO,
        "The stored version fingerprint differs from the canonical payload fingerprint.",
    ),
}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    location: str
    message: str

    @property
    def blocking(self) -> bool:
        return self.severity == SEVERITY_ERROR


def finding(code: str, location: str, **params: object) -> Finding:
    """شدت و پیام همیشه از جدولِ ISSUES می‌آیند — کدِ ناشناخته KeyError است."""
    severity, template = ISSUES[code]
    return Finding(code, severity, location, template.format(**params) if params else template)


def ordered(findings) -> tuple[Finding, ...]:
    """فقط یافته‌ی کاملاً یکسان یک بار می‌آید؛ مرتب بر اساسِ (شدت، code، location، message).
    دو یافته‌ی متفاوت در یک مکان (مثلاً دو کلیدِ شبیهِ اعتبارنامه در یک بدنه) هر دو می‌مانند."""
    return tuple(
        sorted(
            set(findings),
            key=lambda f: (SEVERITY_RANK[f.severity], f.code, f.location, f.message),
        )
    )


def status_for(findings) -> str:
    """ERROR → NOT_EXECUTABLE؛ در غیرِ این صورت REVIEW → NEEDS_SCRIPT_REVIEW؛ وگرنه READY.
    WARNING و INFO (از جمله SCRIPT_APPROVED) هرگز وضعیت را تغییر نمی‌دهند."""
    severities = {item.severity for item in findings}
    if SEVERITY_ERROR in severities:
        return STATUS_NOT_EXECUTABLE
    if SEVERITY_REVIEW in severities:
        return STATUS_NEEDS_SCRIPT_REVIEW
    return STATUS_READY


@dataclass(frozen=True)
class RequiredVariable:
    name: str
    kind: str
    locations: tuple[str, ...]


@dataclass(frozen=True)
class ScriptReview:
    required: bool
    fingerprint: str | None
    approval_state: str
    hard_rejections: tuple[str, ...]


@dataclass(frozen=True)
class Readiness:
    """ارزیابیِ قطعیِ یک نسخه‌ی تغییرناپذیر. ready فقط برای READY درست است و یعنی
    «مطابقِ قراردادِ M3» — متغیرهای EXTERNAL هنوز باید در مرحله‌ی اجرا تأمین شوند."""

    version_id: int | None
    status: str
    host_variable: str | None
    findings: tuple[Finding, ...]
    required_variables: tuple[RequiredVariable, ...]
    provided_variables: tuple[str, ...]
    script_review: ScriptReview

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY
