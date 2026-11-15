"""
src/task_sources/postman.py — تشخیص و تجزیه‌ی Postman Collection (تابعِ خالص)

    result = parse_postman_collection(text)
    if result.recognised:
        for request in result.collection.requests: ...

فقط Collection v2.0.0 و v2.1.0 شناخته می‌شوند: شیءِ سطحِ بالا، ``info`` شیء،
``info.schema`` روی ``schema.getpostman.com`` با مسیرِ یکی از همان دو نسخه، و
``item`` فهرست. ``_postman_id`` یا وجودِ URLِ HTTP به‌تنهایی هرگز کافی نیست.

خروجی برای تحلیل است، نه برای اجرا: نام، مسیرِ پوشه، method، میزبان، پایه‌ی
مسیر، مسیر، نامِ پارامترهای query و نامِ متغیرها. هیچ مقدارِ query، body،
auth، اسکریپت، پاسخِ نمونه یا مقدارِ متغیرِ محرمانه نگه داشته نمی‌شود.

متغیرها فقط از ``variable[]``ِ خودِ Collection خوانده می‌شوند (آخرین ورودیِ فعال
برنده است) — نه Environment، نه Globals، نه .env. مقدارِ Collection فقط پیش‌فرض
است: در اجرا ممکن است Environment آن را عوض کند.

امنیت (هیچ مقدارِ محرمانه‌ای در هیچ خروجی، یادداشت یا خطایی نمی‌آید):

  * متغیرِ ``type: secret`` یا با نامِ محرمانه‌نما (``is_secret_key``) هرگز
    جایگذاری نمی‌شود — فقط نامش در ``secret_variables`` می‌آید؛
  * مقداری که (مستقیم یا با چند واسطه) به متغیرِ محرمانه برسد «آلوده» است و
    مثلِ خودِ آن رفتار می‌شود؛
  * userinfoِ URL (``user:pass@``)، مقدارِ query و fragment همیشه حذف می‌شوند؛
  * نامِ متغیرها و پارامترها فقط با ``safe_name`` در خروجی می‌آیند؛
  * ``raw_url`` بازسازیِ امن است، نه متنِ اصلی: متغیرها جایگذاری نمی‌شوند.

محدودیت: متنِ لفظیِ مسیر (مثلاً ``/reset/<token>``ِ نوشته‌شده در خودِ URL) قابلِ
تشخیص نیست و همان‌طور که هست می‌آید.

هیچ چیزی اجرا، دانلود، خوانده یا نوشته نمی‌شود؛ ورودی تغییر نمی‌کند.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from src.testcase_box.contract import VARIABLE_RE, is_secret_key, safe_name

POSTMAN_SCHEMA_HOST = "schema.getpostman.com"
SUPPORTED_VERSIONS = ("2.0.0", "2.1.0")

STATUS_COLLECTION = "collection"
STATUS_UNSUPPORTED = "unsupported_postman"
STATUS_NOT_POSTMAN = "not_postman"
STATUS_INVALID_JSON = "invalid_json"

MAX_FOLDER_DEPTH = 20
MAX_REQUESTS = 1000
MAX_VARIABLE_DEPTH = 10

# مقدارِ پیش‌فرضِ Postman وقتی request.method نیست.
DEFAULT_METHOD = "GET"

_SCHEMA_PATH_RE = re.compile(r"^/json/collection/v(\d+\.\d+\.\d+)/collection\.json$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_METHOD_RE = re.compile(r"^[A-Z]{1,20}$")
_WHOLE_VARIABLE_RE = re.compile(r"^\{\{([^{}]*?)}}$")


# ── نتیجه ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PostmanRequest:
    """یک درخواستِ استخراج‌شده — بدونِ هیچ مقدارِ محرمانه، body یا auth.

    ``host``          : میزبانِ امنِ قابلِ‌حل، وگرنه None (مثلاً متغیرِ حل‌نشده)
    ``host_variable`` : نامِ متغیر وقتی کلِ میزبان یک متغیر است (``baseUrl``)
    ``base_path``     : مسیرِ داخلِ مقدارِ آن متغیر (``/api``) یا ""
    ``path``          : مسیرِ خودِ درخواست (``/admin/voucher/:id``)؛ متغیرِ حل‌نشده
                        یا محرمانه به شکلِ ``{{name}}`` می‌ماند
    ``raw_url``       : بازسازیِ امن — بدونِ userinfo، مقدارِ query و fragment، و
                        بدونِ جایگذاریِ متغیر
    """

    name: str
    folder: tuple[str, ...]
    method: str
    method_defaulted: bool
    raw_url: str
    host: str | None
    host_variable: str | None
    base_path: str
    path: str
    query_keys: tuple[str, ...] = ()
    path_variables: tuple[str, ...] = ()
    variables: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    secret_variables: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostmanCollection:
    """Collectionِ شناخته‌شده و شمارش‌های لازم برای یکپارچه‌سازیِ بعدی.

    ``requests_discovered`` = همه‌ی ورودی‌های درخواست در عمقِ مجاز؛
    ``requests_extracted`` + ``requests_omitted`` + درخواست‌های معیوب = همان.
    ``malformed_skipped`` همه‌ی ورودی‌های معیوب است (درخواست یا غیرِ آن).
    """

    version: str
    name: str
    requests: tuple[PostmanRequest, ...]
    requests_discovered: int
    requests_omitted: int
    malformed_skipped: int
    folders_beyond_depth: int
    variables_defined: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def requests_extracted(self) -> int:
        return len(self.requests)

    @property
    def limited(self) -> bool:
        """آیا بخشی از Collection به‌خاطرِ سقف‌ها استخراج نشد؟"""
        return bool(self.requests_omitted or self.folders_beyond_depth)


@dataclass(frozen=True)
class PostmanParseResult:
    """``status`` یکی از ``STATUS_*``؛ ``reason`` توضیحِ امن (بدونِ مقدار)."""

    status: str
    collection: PostmanCollection | None = None
    version: str = ""
    reason: str = ""

    @property
    def recognised(self) -> bool:
        return self.status == STATUS_COLLECTION


# ── متغیرها ──────────────────────────────────────────────────────────────────


@dataclass
class _Resolution:
    text: str
    tainted: bool = False
    unresolved: set[str] = field(default_factory=set)
    secrets: set[str] = field(default_factory=set)
    references: set[str] = field(default_factory=set)
    notes: set[str] = field(default_factory=set)


class _Variables:
    """متغیرهای سطحِ Collection: نام → (مقدار یا None، محرمانه؟)."""

    def __init__(self, entries: Any, notes: list[str]) -> None:
        self._values: dict[str, tuple[str | None, bool]] = {}
        if entries is None:
            return
        if not isinstance(entries, list):
            notes.append("collection variables are malformed and were ignored")
            return
        skipped = 0
        for entry in entries:
            if not isinstance(entry, dict):
                skipped += 1
                continue
            key = entry.get("key", entry.get("id"))
            if not isinstance(key, str) or not key:
                skipped += 1
                continue
            if entry.get("disabled") is True or entry.get("enabled") is False:
                continue
            secret = entry.get("type") == "secret" or is_secret_key(key)
            raw = entry.get("value")
            if raw is None:
                value: str | None = ""
            elif isinstance(raw, bool):
                value = "true" if raw else "false"
            elif isinstance(raw, (str, int, float)):
                value = str(raw)
            else:
                value = None
            # Postman: آخرین ورودیِ فعال برنده است.
            self._values.pop(key, None)
            self._values[key] = (value, secret)
        if skipped:
            notes.append(
                f"{skipped} malformed collection variable entr{'y' if skipped == 1 else 'ies'} skipped"
            )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(safe_name(name) for name in self._values)

    def resolve(self, text: str, _stack: tuple[str, ...] = ()) -> _Resolution:
        """``{{name}}``ها را با مقدارِ Collection جایگذاری می‌کند (فقط غیرِمحرمانه).

        متغیرِ حل‌نشده، پویا (``$guid``)، چرخه‌ای یا محرمانه همان ``{{name}}``
        می‌ماند. اگر چیزی به متغیرِ محرمانه برسد ``tainted`` است و متنِ حاصل
        نباید هرگز در خروجی بیاید.
        """
        result = _Resolution(text)

        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            result.references.add(name)
            if name.startswith("$"):
                result.unresolved.add(name)
                return match.group(0)
            if name in _stack:
                result.unresolved.add(name)
                result.notes.add(f"variable cycle involving {{{{{safe_name(name)}}}}}")
                return match.group(0)
            if name not in self._values:
                result.unresolved.add(name)
                return match.group(0)
            value, secret = self._values[name]
            if secret:
                result.secrets.add(name)
                result.unresolved.add(name)
                result.tainted = True
                return match.group(0)
            if value is None:
                result.unresolved.add(name)
                result.notes.add(f"variable {{{{{safe_name(name)}}}}} has a non-text value")
                return match.group(0)
            if len(_stack) >= MAX_VARIABLE_DEPTH:
                result.unresolved.add(name)
                result.notes.add("variable nesting exceeds the resolution depth limit")
                return match.group(0)
            inner = self.resolve(value, _stack + (name,))
            result.unresolved |= inner.unresolved
            result.secrets |= inner.secrets
            result.references |= inner.references
            result.notes |= inner.notes
            if inner.tainted:
                # افشای غیرمستقیم: متغیری که به محرمانه می‌رسد خودش محرمانه است و
                # حتی بخشِ غیرِمحرمانه‌ی مقدارش هم جایگذاری نمی‌شود.
                result.tainted = True
                result.secrets.add(name)
                result.unresolved.add(name)
                return match.group(0)
            if inner.unresolved:
                # مقدارِ نهایی معلوم نیست؛ خودِ این متغیر هم حل‌نشده است.
                result.unresolved.add(name)
            return inner.text

        result.text = VARIABLE_RE.sub(substitute, text)
        return result


def _safe_template(text: str) -> str:
    """متنِ قالب با نامِ امنِ متغیرها؛ هیچ مقداری جایگذاری نمی‌شود."""
    return VARIABLE_RE.sub(lambda match: "{{" + safe_name(match.group(1)) + "}}", text)


def _names(values: set[str]) -> tuple[str, ...]:
    return tuple(sorted({safe_name(name) for name in values}))


# ── URL ──────────────────────────────────────────────────────────────────────


@dataclass
class _UrlParts:
    """اجزای قالبِ URL (پیش از جایگذاری) — مقدارِ query فقط برای نامِ متغیرها."""

    scheme: str = ""
    authority: str = ""
    segments: list[str] = field(default_factory=list)
    query: list[tuple[str, str]] = field(default_factory=list)
    userinfo: bool = False


def _split_authority(authority: str, parts: _UrlParts) -> None:
    if "@" in authority:
        parts.userinfo = True
        authority = authority.rsplit("@", 1)[1]
    parts.authority = authority


def _parse_raw(raw: str) -> _UrlParts:
    parts = _UrlParts()
    text = raw.strip().split("#", 1)[0]
    text, _, query = text.partition("?")
    scheme = _SCHEME_RE.match(text)
    if scheme:
        parts.scheme = scheme.group(0)[:-3].lower()
        text = text[scheme.end() :]
        authority, _, path = text.partition("/")
    elif text.startswith("/"):
        authority, path = "", text
    else:
        # Postman بخشِ اولِ URLِ بی‌scheme را میزبان می‌داند ({{baseUrl}}، localhost:3000).
        authority, _, path = text.partition("/")
    _split_authority(authority, parts)
    parts.segments = [segment for segment in path.split("/") if segment]
    for pair in query.split("&") if query else []:
        key, _, value = pair.partition("=")
        if key:
            parts.query.append((key, value))
    return parts


def _strings(value: Any) -> list[str] | None:
    """رشته یا فهرستی از رشته/``{value}`` — وگرنه None (معیوب)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, str):
                items.append(item)
            elif isinstance(item, dict) and isinstance(item.get("value"), str):
                items.append(item["value"])
            else:
                return None
        return items
    return None


def _structured(url: dict, notes: list[str]) -> _UrlParts | None:
    """اجزای شیءِ url؛ None یعنی شیء جزئی جز raw ندارد."""
    if not any(key in url for key in ("host", "path")):
        return None
    parts = _UrlParts()
    protocol = url.get("protocol")
    if isinstance(protocol, str):
        parts.scheme = protocol.strip().lower()
    host = url.get("host")
    if host is not None:
        items = _strings(host)
        if items is None:
            notes.append("url.host is malformed and was ignored")
        else:
            _split_authority(".".join(items), parts)
            # شیءِ url درگاه را جدا (url.port) نگه می‌دارد؛ بخشی از میزبانِ نوشته‌شده است.
            port = url.get("port")
            if isinstance(port, (str, int)) and not isinstance(port, bool) and str(port).strip():
                parts.authority = f"{parts.authority}:{str(port).strip()}"
    path = url.get("path")
    if path is not None:
        items = _strings(path)
        if items is None:
            notes.append("url.path is malformed and was ignored")
        else:
            joined = "/".join(items)
            parts.segments = [segment for segment in joined.split("/") if segment]
    if isinstance(url.get("auth"), dict) and url["auth"]:
        parts.userinfo = True
    return parts


def _query_entries(url: dict, notes: list[str]) -> list[tuple[str, str]] | None:
    """ورودی‌های فعالِ url.query؛ None یعنی query تعریف نشده."""
    entries = url.get("query")
    if entries is None:
        return None
    if not isinstance(entries, list):
        notes.append("url.query is malformed and was ignored")
        return []
    query = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            notes.append("a malformed url.query entry was skipped")
            continue
        if entry.get("disabled") is True:
            continue
        value = entry.get("value")
        query.append((entry["key"], value if isinstance(value, str) else ""))
    return query


def _path_variables(url: dict, segments: list[str]) -> tuple[str, ...]:
    names = [segment[1:] for segment in segments if segment.startswith(":") and len(segment) > 1]
    entries = url.get("variable") if isinstance(url.get("variable"), list) else []
    for entry in entries:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("key"), str)
            and entry["key"] not in names
        ):
            names.append(entry["key"])
    return tuple(safe_name(name) for name in names)


@dataclass
class _Host:
    host: str | None = None
    variable: str | None = None
    base_path: str = ""


def _resolve_host(authority: str, variables: _Variables, acc: _Resolution) -> _Host:
    """میزبان و پایه‌ی مسیر از قالبِ authority — فقط اگر کامل و امن حل شود."""
    found = _Host()
    if not authority:
        return found
    whole = _WHOLE_VARIABLE_RE.match(authority)
    if whole:
        found.variable = whole.group(1)
    resolution = variables.resolve(authority)
    acc.references |= resolution.references
    acc.unresolved |= resolution.unresolved
    acc.secrets |= resolution.secrets
    acc.notes |= resolution.notes
    if resolution.tainted:
        acc.notes.add("the host depends on a secret variable and was not resolved")
        return found
    if VARIABLE_RE.search(resolution.text):
        return found
    value = resolution.text.strip()
    try:
        parsed = urlsplit(value if _SCHEME_RE.match(value) else f"http://{value}")
        hostname = parsed.hostname
    except ValueError:
        hostname = None
    if not hostname:
        acc.notes.add("the host could not be parsed")
        return found
    if "@" in parsed.netloc:
        acc.notes.add("credentials in the host value were removed")
    if parsed.query or parsed.fragment:
        acc.notes.add("query or fragment in the host value was ignored")
    found.host = hostname.lower()
    segments = [segment for segment in parsed.path.split("/") if segment]
    found.base_path = "/" + "/".join(segments) if segments else ""
    return found


def _resolve_path(segments: list[str], variables: _Variables, acc: _Resolution) -> str:
    """مسیر با مقدارِ متغیرهای غیرِمحرمانه؛ بخشی که کامل یا امن حل نشود قالب می‌ماند."""
    parts: list[str] = []
    for segment in segments:
        resolution = variables.resolve(segment)
        acc.references |= resolution.references
        acc.unresolved |= resolution.unresolved
        acc.secrets |= resolution.secrets
        acc.notes |= resolution.notes
        if resolution.tainted or VARIABLE_RE.search(resolution.text):
            parts.append(_safe_template(segment))
        else:
            parts.extend(piece for piece in resolution.text.split("/") if piece)
    return "/" + "/".join(parts)


def _differences(chosen: _UrlParts, raw: _UrlParts) -> list[str]:
    differ = []
    if chosen.authority != raw.authority:
        differ.append("host")
    if chosen.segments != raw.segments:
        differ.append("path")
    if [key for key, _ in chosen.query] != [key for key, _ in raw.query]:
        differ.append("query")
    return differ


def _safe_raw(parts: _UrlParts, query_keys: tuple[str, ...]) -> str:
    authority = _safe_template(parts.authority)
    prefix = (
        f"{parts.scheme}://"
        if parts.scheme and authority and not authority.startswith("{{")
        else ""
    )
    path = (
        "/" + "/".join(_safe_template(segment) for segment in parts.segments)
        if parts.segments
        else ""
    )
    query = "?" + "&".join(query_keys) if query_keys else ""
    return f"{prefix}{authority}{path}{query}"


# ── درخواست‌ها ───────────────────────────────────────────────────────────────


def _method(request: dict, notes: list[str]) -> tuple[str, bool]:
    value = request.get("method")
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_METHOD, True
    if isinstance(value, str) and _METHOD_RE.match(value.strip().upper()):
        return value.strip().upper(), False
    notes.append(f"the method is invalid; Postman's default {DEFAULT_METHOD} is assumed")
    return DEFAULT_METHOD, True


def _request(entry: dict, folder: tuple[str, ...], variables: _Variables) -> PostmanRequest | None:
    """یک ورودیِ درخواست؛ None یعنی معیوب (شمرده و کنار گذاشته می‌شود)."""
    notes: list[str] = []
    request = entry.get("request")
    if isinstance(request, str):
        # شکلِ کوتاهِ v2: «request» خودِ URL است و method پیش‌فرض.
        request = {"url": request}
    if not isinstance(request, dict):
        return None
    method, defaulted = _method(request, notes)

    url = request.get("url")
    path_variables: tuple[str, ...] = ()
    if isinstance(url, str):
        if not url.strip():
            return None
        parts = _parse_raw(url)
    elif isinstance(url, dict):
        raw = url.get("raw")
        raw_parts = _parse_raw(raw) if isinstance(raw, str) and raw.strip() else None
        structured = _structured(url, notes)
        query = _query_entries(url, notes)
        if structured is None and raw_parts is None:
            return None
        if structured is None:
            parts = raw_parts
        else:
            parts = structured
            parts.query = query if query is not None else (raw_parts.query if raw_parts else [])
            if not parts.scheme and raw_parts is not None:
                parts.scheme = raw_parts.scheme
            if raw_parts is not None:
                differ = _differences(parts, raw_parts)
                if differ:
                    notes.append(
                        "url.raw disagrees with the structured URL on "
                        f"{', '.join(differ)}; the structured parts were used"
                    )
        if structured is None and query is not None:
            parts.query = query
        path_variables = _path_variables(url, parts.segments)
    else:
        return None

    if parts.userinfo:
        notes.append("credentials in the URL were removed")
    acc = _Resolution("")
    host = _resolve_host(parts.authority, variables, acc)
    path = _resolve_path(parts.segments, variables, acc)
    query_keys = tuple(safe_name(key) for key, _ in parts.query)
    for _, value in parts.query:
        for match in VARIABLE_RE.finditer(value):
            name = match.group(1)
            acc.references.add(name)
            resolution = variables.resolve(match.group(0))
            acc.unresolved |= resolution.unresolved
            acc.secrets |= resolution.secrets
            acc.references |= resolution.references
    if not path_variables:
        path_variables = tuple(
            safe_name(segment[1:])
            for segment in parts.segments
            if segment.startswith(":") and len(segment) > 1
        )

    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        name = ""
        notes.append("the request has no name")
    return PostmanRequest(
        name=name,
        folder=folder,
        method=method,
        method_defaulted=defaulted,
        raw_url=_safe_raw(parts, query_keys),
        host=host.host,
        host_variable=safe_name(host.variable) if host.variable is not None else None,
        base_path=host.base_path,
        path=path,
        query_keys=query_keys,
        path_variables=path_variables,
        variables=_names(acc.references),
        unresolved=_names(acc.unresolved),
        secret_variables=_names(acc.secrets),
        notes=tuple(notes) + tuple(sorted(acc.notes)),
    )


@dataclass
class _Walk:
    variables: _Variables
    requests: list[PostmanRequest] = field(default_factory=list)
    discovered: int = 0
    omitted: int = 0
    malformed: int = 0
    beyond_depth: int = 0

    def items(self, items: Any, folder: tuple[str, ...], depth: int) -> None:
        if not isinstance(items, list):
            self.malformed += 1
            return
        for entry in items:
            if not isinstance(entry, dict):
                self.malformed += 1
            elif "item" in entry:
                if depth >= MAX_FOLDER_DEPTH:
                    self.beyond_depth += 1
                    continue
                name = entry.get("name")
                label = (
                    name.strip() if isinstance(name, str) and name.strip() else "<unnamed folder>"
                )
                self.items(entry["item"], folder + (label,), depth + 1)
            elif "request" in entry:
                self.discovered += 1
                if len(self.requests) >= MAX_REQUESTS:
                    self.omitted += 1
                    continue
                request = _request(entry, folder, self.variables)
                if request is None:
                    self.malformed += 1
                else:
                    self.requests.append(request)
            else:
                self.malformed += 1


# ── تشخیص و تجزیه ────────────────────────────────────────────────────────────


def _looks_like_v1(document: dict) -> bool:
    """قالبِ v1: فهرستِ «requests» با url/method به‌همراهِ «order» یا «folders»."""
    requests = document.get("requests")
    return (
        isinstance(requests, list)
        and bool(requests)
        and all(isinstance(item, dict) and "url" in item and "method" in item for item in requests)
        and (isinstance(document.get("order"), list) or isinstance(document.get("folders"), list))
    )


def detect_postman(document: Any) -> PostmanParseResult:
    """فقط تشخیص (بدونِ استخراج): collection، unsupported_postman یا not_postman."""
    if not isinstance(document, dict):
        return PostmanParseResult(STATUS_NOT_POSTMAN, reason="the JSON document is not an object")
    info = document.get("info")
    schema = info.get("schema") if isinstance(info, dict) else None
    if isinstance(schema, str):
        try:
            parsed = urlsplit(schema.strip())
            host = (parsed.hostname or "").lower()
        except ValueError:
            parsed, host = None, ""
        if host == POSTMAN_SCHEMA_HOST and parsed is not None:
            match = _SCHEMA_PATH_RE.match(parsed.path)
            version = match.group(1) if match else ""
            if version not in SUPPORTED_VERSIONS:
                shown = f"v{version}" if version else "an unknown version"
                return PostmanParseResult(
                    STATUS_UNSUPPORTED,
                    version=version,
                    reason=f"Postman Collection schema {shown} is not supported "
                    f"(supported: {', '.join('v' + v for v in SUPPORTED_VERSIONS)})",
                )
            if not isinstance(document.get("item"), list):
                return PostmanParseResult(
                    STATUS_UNSUPPORTED,
                    version=version,
                    reason="the Postman Collection has no item list",
                )
            return PostmanParseResult(STATUS_COLLECTION, version=version)
    if _looks_like_v1(document):
        return PostmanParseResult(
            STATUS_UNSUPPORTED, version="1", reason="Postman Collection v1 format is not supported"
        )
    return PostmanParseResult(
        STATUS_NOT_POSTMAN, reason="the JSON document is not a Postman Collection"
    )


def parse_postman_document(document: Any) -> PostmanParseResult:
    """یک JSONِ از پیش parse‌شده را تشخیص و (اگر Collection است) استخراج می‌کند."""
    detection = detect_postman(document)
    if not detection.recognised:
        return detection
    notes: list[str] = []
    variables = _Variables(document.get("variable"), notes)
    walk = _Walk(variables)
    walk.items(document["item"], (), 0)
    if walk.omitted:
        notes.append(f"{walk.omitted} request(s) omitted: the limit is {MAX_REQUESTS} requests")
    if walk.beyond_depth:
        notes.append(
            f"{walk.beyond_depth} folder(s) not read: the folder nesting limit is {MAX_FOLDER_DEPTH}"
        )
    if walk.malformed:
        notes.append(
            f"{walk.malformed} malformed entr{'y' if walk.malformed == 1 else 'ies'} skipped"
        )
    name = document["info"].get("name")
    collection = PostmanCollection(
        version=detection.version,
        name=name.strip() if isinstance(name, str) else "",
        requests=tuple(walk.requests),
        requests_discovered=walk.discovered,
        requests_omitted=walk.omitted,
        malformed_skipped=walk.malformed,
        folders_beyond_depth=walk.beyond_depth,
        variables_defined=variables.names,
        notes=tuple(notes),
    )
    return PostmanParseResult(STATUS_COLLECTION, collection=collection, version=detection.version)


def parse_postman_collection(text: str) -> PostmanParseResult:
    """متنِ JSON را تشخیص و (اگر Collection است) استخراج می‌کند — هرگز استثنا نمی‌دهد."""
    try:
        document = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return PostmanParseResult(STATUS_INVALID_JSON, reason="the text is not valid JSON")
    return parse_postman_document(document)


__all__ = [
    "DEFAULT_METHOD",
    "MAX_FOLDER_DEPTH",
    "MAX_REQUESTS",
    "MAX_VARIABLE_DEPTH",
    "POSTMAN_SCHEMA_HOST",
    "STATUS_COLLECTION",
    "STATUS_INVALID_JSON",
    "STATUS_NOT_POSTMAN",
    "STATUS_UNSUPPORTED",
    "SUPPORTED_VERSIONS",
    "PostmanCollection",
    "PostmanParseResult",
    "PostmanRequest",
    "detect_postman",
    "parse_postman_collection",
    "parse_postman_document",
]
