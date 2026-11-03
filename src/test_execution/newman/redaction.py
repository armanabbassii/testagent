"""
redaction.py — حذفِ مقادیرِ محرمانه پیش از ذخیره‌ی هر خروجی

دو لایه، هر دو لازم:

  1. ساختاری: هدرهای حساس (Authorization، Cookie، Set-Cookie، …) و بدنه‌ی
     درخواست/پاسخ از گزارشِ JSON حذف و مقدارِ متغیرهای environment/globals
     پوشانده می‌شود؛ مقدارِ query parameterهای حساس در URLها «***» می‌شود.
  2. مقدار-محور: هر مقدارِ محرمانه‌ی شناخته‌شده (متغیرهای حساس، هدرهای حساسِ
     دیده‌شده، کوکی‌ها، query parameterهای حساس) در هر جای متن — به شکلِ خام،
     URL-encoded، HTML-escaped یا JSON-escaped — با «***» جایگزین می‌شود. در گزارشِ
     JSON این کار روی تک‌تکِ رشته‌ها (مقدار و کلید) انجام می‌شود، نه روی متنِ
     سریال‌شده؛ پس boolean، عدد و null دست نمی‌خورند و JSON همیشه معتبر می‌ماند.

هدرهای استانداردِ CORS (CORS_HEADERS) با وجودِ نامشان حساس نیستند.

مقدارِ کوتاه (کمتر از MIN_SECRET_LENGTH نویسه، مثلاً «7» یا «ab») را نمی‌توان
بی‌خطر در متنِ آزاد جایگزین کرد، چون همان نویسه‌ها جای دیگری هم معنا دارند. برای
آن‌ها (و برای هر مقدارِ دیگری) پوشاندنِ «بافت‌محور» در متنِ آزاد انجام می‌شود:
مقدارِ بعد از هر کلیدِ حساس («token=…»، «Authorization: …»، «"password": "…"»)
و مقدارِ query parameterهای حساسِ هر URL. بعد verify_clean مقدارهای کوتاه را به
شکلِ «توکنِ کامل» (بدونِ حرف/رقمِ چسبیده) جست‌وجو می‌کند — اگر یکی مانده باشد،
موتور آن خروجی را منتشر نمی‌کند (ERROR). در JSON فقط مقدارهای رشته‌ای بررسی
می‌شوند (کلیدها و عددها شاهد نیستند).

پیام‌ها هرگز خودِ مقدار را ندارند — فقط تعداد.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

MASK = "***"

# نامِ کلید/هدر/پارامتری که مقدارش محرمانه حساب می‌شود.
SECRET_KEY_RE = re.compile(
    r"token|passw|pwd|secret|authori[sz]ation|cookie|api[-_]?key|session|jwt|bearer|"
    r"credential|private[-_]?key|access[-_]?key|otp",
    re.IGNORECASE,
)
SENSITIVE_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token"}
)
# هدرهای استانداردِ CORS هیچ اعتبارنامه‌ای ندارند («Access-Control-Allow-Credentials: true»
# فقط یک پرچم است) ولی نامشان با SECRET_KEY_RE می‌خواند — فقط همین نام‌های دقیق مستثنا‌اند.
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
# مقدارهای کوتاه‌تر جمع‌آوری و بررسی می‌شوند، ولی در متنِ آزاد فقط بافت‌محور
# پوشانده می‌شوند (جایگزینیِ «1» در کلِ متن، متن را خراب می‌کرد).
MIN_SECRET_LENGTH = 4

_KEY = r"[A-Za-z0-9_.\-\[\]]+"
_VALUE_STOP = r"\s\"'&;,<>)\]}"
# شکل‌های HTML-escapedِ «=»، «&» و نقل‌قول — Handlebarsِ htmlextra «=» را «&#x3D;» می‌نویسد.
_EQ = r"(?:=|&#x3[dD];|&#61;|&equals;)"
_AMP = r"(?:&amp;|&#x26;|&#38;|&)"
_QUOTE = r"(?:&quot;|&#x27;|&#39;|&#x22;|&#34;|[\"'])"
# «token=abc»، «password: abc»، «"apiKey": "abc"»، «'secret'='abc'»
_PAIR_RE = re.compile(rf"({_QUOTE}?)({_KEY})\1(\s*(?::|{_EQ})\s*)({_QUOTE}?)([^{_VALUE_STOP}]+)")
# «Authorization: Bearer abc» — طرحِ احراز هویت و خودِ مقدار
_AUTH_RE = re.compile(
    rf"(?i)((?:proxy-)?authori[sz]ation{_QUOTE}?\s*(?::|{_EQ})\s*{_QUOTE}?)((?:bearer|basic|token)\s+)?"
    rf"([^{_VALUE_STOP}]+)"
)
_COOKIE_ATTRIBUTES = frozenset(
    {
        "path",
        "domain",
        "expires",
        "max-age",
        "samesite",
        "secure",
        "httponly",
        "priority",
        "partitioned",
    }
)
_AUTH_KEY_RE = re.compile(r"(?i)(?:proxy-)?authori[sz]ation")
# «?token=abc&page=1» در هر URLِ داخلِ متن (از جمله &amp;ِ HTML)
_QUERY_RE = re.compile(rf"([?;]|{_AMP})({_KEY})({_EQ})([^&#{_VALUE_STOP}]*)")


class SanitizationError(RuntimeError):
    """پاک‌سازی کامل نشد — پیام هیچ مقدارِ محرمانه‌ای ندارد."""


def is_secret_key(name: object) -> bool:
    return isinstance(name, str) and bool(SECRET_KEY_RE.search(name))


def is_sensitive_header(name: object) -> bool:
    if not isinstance(name, str):
        return False
    lowered = name.strip().lower()
    if lowered in CORS_HEADERS:
        return False
    return lowered in SENSITIVE_HEADERS or is_secret_key(name)


# ── جمع‌آوریِ مقادیرِ محرمانه ────────────────────────────────────────────────


def _add(secrets: set[str], value: object) -> None:
    if not isinstance(value, str):
        return
    value = value.strip()
    if not value or "{{" in value:
        return
    secrets.add(value)
    # «Bearer abc» → خودِ توکن هم؛ «SESSION=xyz; Path=/» → مقدارِ کوکی هم.
    scheme, _, rest = value.partition(" ")
    if rest and scheme.lower() in ("bearer", "basic", "token"):
        _add(secrets, rest)
    for part in value.split(";"):
        name, eq, cookie_value = part.partition("=")
        # ویژگی‌های کوکی (Path=/، Domain=…) مقدارِ محرمانه نیستند.
        if eq and cookie_value.strip() and name.strip().lower() not in _COOKIE_ATTRIBUTES:
            secrets.add(cookie_value.strip())


def variable_secrets(document: Any) -> set[str]:
    """مقدارِ متغیرهای حساسِ یک environment / globals / collection."""
    secrets: set[str] = set()
    if not isinstance(document, dict):
        return secrets
    for key in ("values", "variable"):
        for entry in document.get(key) or []:
            if isinstance(entry, dict) and is_secret_key(entry.get("key")):
                _add(secrets, entry.get("value"))
    return secrets


def url_secrets(url: str) -> set[str]:
    secrets: set[str] = set()
    try:
        parts = urlsplit(url)
    except ValueError:
        return secrets
    if parts.password:
        _add(secrets, parts.password)
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if is_secret_key(key):
            _add(secrets, value)
    return secrets


def _header_list(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def summary_secrets(summary: Any) -> set[str]:
    """مقادیرِ محرمانه‌ای که در گزارشِ خامِ Newman دیده می‌شوند."""
    secrets: set[str] = set()
    if not isinstance(summary, dict):
        return secrets
    for key in ("environment", "globals", "collection"):
        secrets |= variable_secrets(summary.get(key))
    run = summary.get("run") if isinstance(summary.get("run"), dict) else {}
    for execution in run.get("executions") or []:
        if not isinstance(execution, dict):
            continue
        for part in ("request", "response"):
            section = execution.get(part) if isinstance(execution.get(part), dict) else {}
            for header in _header_list(section.get("header")):
                if is_sensitive_header(header.get("key")):
                    _add(secrets, header.get("value"))
        request = execution.get("request") if isinstance(execution.get("request"), dict) else {}
        for item in _query_list(request.get("url")):
            if is_secret_key(item.get("key")):
                _add(secrets, item.get("value"))
    return secrets


def _query_list(url: Any) -> list[dict]:
    if isinstance(url, dict):
        return [item for item in url.get("query") or [] if isinstance(item, dict)]
    return []


# ── جایگزینی و بررسی ─────────────────────────────────────────────────────────


def _forms(secret: str) -> set[str]:
    forms = {
        secret,
        quote(secret, safe=""),
        quote(secret, safe="/:"),
        html.escape(secret, quote=True),
        html.escape(secret, quote=False),
        json.dumps(secret)[1:-1],
        _handlebars_escape(secret),
    }
    return {form for form in forms if form}


_HANDLEBARS = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#x27;",
    "`": "&#x60;",
    "=": "&#x3D;",
}


def _handlebars_escape(text: str) -> str:
    """همان escapeِ قالبِ htmlextra (Handlebars) — «=» و «`» هم escape می‌شوند."""
    return "".join(_HANDLEBARS.get(char, char) for char in text)


def _long(secrets: set[str]) -> set[str]:
    return {secret for secret in secrets if len(secret) >= MIN_SECRET_LENGTH}


def _short(secrets: set[str]) -> set[str]:
    return {secret for secret in secrets if len(secret) < MIN_SECRET_LENGTH}


def context_scrub(text: str) -> str:
    """مقدارِ بعد از هر کلید/هدر/query parameterِ حساس → «***» (طول مهم نیست)."""
    text = _AUTH_RE.sub(lambda m: f"{m.group(1)}{m.group(2) or ''}{MASK}", text)

    def pair(match: re.Match) -> str:
        key = match.group(2)
        if not is_secret_key(key) or match.group(5) == MASK or _AUTH_KEY_RE.fullmatch(key):
            return match.group(0)
        quote_mark = match.group(1)
        return f"{quote_mark}{match.group(2)}{quote_mark}{match.group(3)}{match.group(4)}{MASK}"

    def query(match: re.Match) -> str:
        if not is_secret_key(match.group(2)) or not match.group(4):
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}{match.group(3)}{MASK}"

    return _PAIR_RE.sub(pair, _QUERY_RE.sub(query, text))


def scrub_text(text: str, secrets: set[str], *, context: bool = True) -> str:
    """پوشاندنِ بافت‌محور (برای متنِ آزاد) و بعد هر شکلِ هر مقدارِ بلند.

    مقدارهای کوتاه این‌جا جایگزین نمی‌شوند؛ اگر بیرون از بافتِ حساس مانده
    باشند، verify_clean خروجی را رد می‌کند.
    """
    if context:
        text = context_scrub(text)
    forms = sorted(
        {form for secret in _long(secrets) for form in _forms(secret)}, key=len, reverse=True
    )
    for form in forms:
        text = text.replace(form, MASK)
    return text


def _token_pattern(form: str) -> re.Pattern:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])")


def leaked_count(text: str, secrets: set[str]) -> int:
    """چند مقدارِ محرمانه هنوز در متن هست — فقط تعداد، هرگز خودِ مقدار.

    مقدارِ بلند هر جا باشد شمرده می‌شود؛ مقدارِ کوتاه فقط وقتی یک توکنِ کامل است
    («ab» در «about» نشت نیست، در «key ab» هست).
    """
    leaked = sum(1 for secret in _long(secrets) if any(form in text for form in _forms(secret)))
    leaked += sum(
        1
        for secret in _short(secrets)
        if any(_token_pattern(form).search(text) for form in _forms(secret))
    )
    return leaked


def verify_clean(text: str, secrets: set[str], what: str) -> None:
    leaked = leaked_count(text, secrets)
    if leaked:
        raise SanitizationError(f"{leaked} secret value(s) are still present in {what}.")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def verify_clean_json(document: Any, secrets: set[str], what: str) -> None:
    """فقط مقدارهای رشته‌ای بررسی می‌شوند — عدد (مثلاً status=200) شاهدِ نشت نیست."""
    verify_clean("\n".join(_strings(document)), secrets, what)


def redact_url(url: str, secrets: set[str]) -> str:
    """query parameterهای حساس و user:password@ → «***»، بعد مقدارهای شناخته‌شده."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return scrub_text(url, secrets)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{MASK}@{netloc.rsplit('@', 1)[1]}"
    query = "&".join(
        f"{key}={MASK if is_secret_key(key) and value else value}"
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    )
    rebuilt = urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    return scrub_text(rebuilt, secrets)


# ── پاک‌سازیِ ساختاریِ گزارشِ JSONِ Newman ──────────────────────────────────


def _mask_variables(document: Any) -> Any:
    """مقدارِ همه‌ی متغیرهای environment/globals پوشانده می‌شود؛ فقط کلیدها می‌مانند."""
    if not isinstance(document, dict):
        return document
    cleaned = dict(document)
    for key in ("values", "variable"):
        if isinstance(cleaned.get(key), list):
            cleaned[key] = [
                {
                    **entry,
                    "value": MASK if entry.get("value") not in (None, "") else entry.get("value"),
                }
                if isinstance(entry, dict)
                else entry
                for entry in cleaned[key]
            ]
    return cleaned


def _mask_secret_variables(document: Any) -> Any:
    """در کالکشن فقط متغیرهای حساس پوشانده می‌شوند (بقیه بخشی از تعریفِ تست‌اند)."""
    if not isinstance(document, dict):
        return document
    cleaned = dict(document)
    if isinstance(cleaned.get("variable"), list):
        cleaned["variable"] = [
            {**entry, "value": MASK}
            if isinstance(entry, dict) and is_secret_key(entry.get("key")) and entry.get("value")
            else entry
            for entry in cleaned["variable"]
        ]
    return cleaned


def _clean_headers(headers: Any) -> Any:
    if not isinstance(headers, list):
        return headers
    return [
        {**header, "value": MASK}
        if isinstance(header, dict) and is_sensitive_header(header.get("key"))
        else header
        for header in headers
    ]


def _clean_url(url: Any) -> Any:
    if not isinstance(url, dict):
        return url
    cleaned = dict(url)
    if isinstance(cleaned.get("query"), list):
        cleaned["query"] = [
            {**item, "value": MASK}
            if isinstance(item, dict) and is_secret_key(item.get("key")) and item.get("value")
            else item
            for item in cleaned["query"]
        ]
    if cleaned.get("auth"):
        cleaned["auth"] = MASK
    return cleaned


def _clean_execution(execution: Any) -> Any:
    if not isinstance(execution, dict):
        return execution
    cleaned = dict(execution)
    request = cleaned.get("request")
    if isinstance(request, dict):
        request = dict(request)
        request["header"] = _clean_headers(request.get("header"))
        request["url"] = _clean_url(request.get("url"))
        if "body" in request:
            request["body"] = {"redacted": True}
        if request.get("auth"):
            request["auth"] = MASK
        cleaned["request"] = request
    response = cleaned.get("response")
    if isinstance(response, dict):
        response = dict(response)
        response["header"] = _clean_headers(response.get("header"))
        for key in ("stream", "body"):
            if key in response:
                response[key] = {"redacted": True}
        if isinstance(response.get("cookie"), list):
            response["cookie"] = [{"redacted": True} for _ in response["cookie"]]
        cleaned["response"] = response
    return cleaned


def sanitize_summary(summary: dict, secrets: set[str]) -> dict:
    """نسخه‌ی پاک‌شده‌ی گزارشِ خامِ Newman — ورودی دست نمی‌خورد."""
    cleaned = dict(summary)
    cleaned["environment"] = _mask_variables(summary.get("environment"))
    cleaned["globals"] = _mask_variables(summary.get("globals"))
    cleaned["collection"] = _mask_secret_variables(summary.get("collection"))
    run = summary.get("run")
    if isinstance(run, dict):
        run = dict(run)
        run["executions"] = [_clean_execution(item) for item in run.get("executions") or []]
        cleaned["run"] = run
    # لایه‌ی دوم: هر مقدارِ شناخته‌شده در هر رشته‌ی دیگر (پیامِ خطا، …) — روی خودِ
    # رشته‌ها، نه روی JSONِ سریال‌شده، تا true/عدد/null هرگز «***» نشوند.
    # بافت‌محور نه: متنِ اسکریپت‌های کالکشن دست نمی‌خورد.
    return _scrub_strings(cleaned, secrets)


def _scrub_strings(value: Any, secrets: set[str]) -> Any:
    """هر رشته (مقدار یا کلید) پوشانده می‌شود؛ boolean، عدد و null دست نمی‌خورند."""
    if isinstance(value, str):
        return scrub_text(value, secrets, context=False)
    if isinstance(value, list):
        return [_scrub_strings(item, secrets) for item in value]
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            new_key = scrub_text(key, secrets, context=False) if isinstance(key, str) else key
            if new_key in cleaned:
                # دو کلید پس از پوشاندن یکی شدند — ادغام یعنی از دست رفتنِ داده؛ رد.
                raise SanitizationError("two report keys became identical after redaction.")
            cleaned[new_key] = _scrub_strings(item, secrets)
        return cleaned
    return value


__all__ = [
    "CORS_HEADERS",
    "MASK",
    "MIN_SECRET_LENGTH",
    "SanitizationError",
    "context_scrub",
    "is_secret_key",
    "is_sensitive_header",
    "leaked_count",
    "redact_url",
    "sanitize_summary",
    "scrub_text",
    "summary_secrets",
    "url_secrets",
    "variable_secrets",
    "verify_clean",
    "verify_clean_json",
]
