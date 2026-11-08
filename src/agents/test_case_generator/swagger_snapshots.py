"""
swagger_snapshots.py — منابعِ محلیِ Swagger برای قدم دوم

قدم دوم در اجرای عادیِ ویزارد **هیچ درخواستی به Swagger UI بیرونی نمی‌زند**:
از snapshotهای محلیِ ``data/swagger/`` می‌خواند. این کار اجرا را تکرارپذیر
می‌کند — همان سندِ ورودی، همان کاتالوگِ کشف‌شده — و قدم را از میزبانی که
ممکن است در دسترس نباشد یا احراز هویت بخواهد جدا می‌کند.

خواندن (``load_snapshot``) هیچ fetch، توکن یا کوکی‌ای ندارد. parse و کشف دوباره
پیاده نشده — همان ``discover_from_spec`` ماژولِ ``api_discovery`` استفاده
می‌شود، پس معنای کشف عوض نمی‌شود.

تازه‌سازی (``refresh_snapshot``) تنها جایی است که این ماژول به شبکه می‌رود — و
فقط وقتی کاربر دکمه‌ی Update همان منبع را بزند. سند پیش از هر نوشتنی کامل
دریافت، بررسی و با همان ``discover_from_spec`` اعتبارسنجی می‌شود و بعد به‌صورتِ
اتمی (فایلِ موقت در همان پوشه + ``os.replace``) جای فایلِ قبلی را می‌گیرد؛ هر
خطایی فایلِ قبلی را دست‌نخورده نگه می‌دارد.

همگام‌سازیِ روزانه (src/swagger_sync) همین مسیر را به کار می‌برد: fetch_and_validate،
decide_update (محافظِ کاهشِ operationها و تشخیصِ «بدونِ تغییر») و write_snapshot زیرِ قفلِ
نوشتنِ مشترک (swagger_locks) — دکمه‌ی Update و job هرگز هم‌زمان فایل نمی‌نویسند. هیچ cacheی در کار نیست: قدم دوم در
هر اجرا فایل را دوباره می‌خواند، پس اجرای بعدیِ نگاشت سندِ تازه را می‌بیند.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from src.agents.test_case_generator.api_discovery import (
    DiscoveredService,
    SwaggerLoadError,
    discover_from_spec,
    swagger_group_name,
)
from src.agents.test_case_generator.swagger_locks import (
    SwaggerLockBusy,
    SwaggerLockUnavailable,
    write_lock,
)
from src.swagger_settings import (
    SWAGGER_API_DOCS_BASE_URL,
    SWAGGER_CONNECT_TIMEOUT_SECONDS,
    SWAGGER_READ_TIMEOUT_SECONDS,
    SwaggerSettingsError,
    max_shrink_ratio,
)

# پوشه‌ی snapshotها در ریشه‌ی پروژه. برای تست قابلِ جایگزینی است.
SNAPSHOT_DIR = Path(__file__).resolve().parents[3] / "data" / "swagger"

# راهنمای کاربر بعد از شکست — بسته به نوعِ خطا
_NETWORK_HINT = "Please check the connection and try again."
_RETRY_HINT = "The existing file was kept. You can try again."

# ── انتخابِ منبع ─────────────────────────────────────────────────────────────

SELECTION_ADMIN = "admin"
SELECTION_CUSTOMER = "customer"
SELECTION_CRM = "crm"
SELECTION_PROVIDER = "provider"
SELECTION_SITE = "site"
# انتخابِ خودکار (source_selection.py) — پیش‌فرضِ Run All و قدم ۲.
SELECTION_AUTO = "auto"
# مقدارِ قدیمیِ رادیوی قدم ۲ (Admin + Customer) — فقط برای خواندنِ وضعیت‌های
# ذخیره‌شده‌ی قبلی پذیرفته می‌شود و دیگر هرگز پیش‌فرض یا fallback نیست.
SELECTION_BOTH = "both"

DEFAULT_SELECTION = SELECTION_AUTO


class SwaggerSnapshotError(RuntimeError):
    """snapshot درخواستی وجود ندارد، خوانده نشد یا سندِ معتبری نیست."""


KIND_CONNECT_TIMEOUT = "connect_timeout"
KIND_READ_TIMEOUT = "read_timeout"
KIND_CONNECTION = "connection"
KIND_REQUEST = "request"
KIND_HTTP_STATUS = "http_status"
KIND_EMPTY = "empty"
KIND_INVALID_JSON = "invalid_json"
KIND_NOT_OPENAPI = "not_openapi"
KIND_UNUSABLE = "unusable"
KIND_NO_OPERATIONS = "no_operations"
KIND_SHRINK = "shrink"
KIND_PERMISSION = "permission"
KIND_WRITE = "write"
KIND_LOCKED = "locked"
KIND_CONFIGURATION = "configuration"


class SwaggerRefreshError(RuntimeError):
    """تازه‌سازیِ یک snapshot شکست خورد — فایلِ قبلی دست‌نخورده مانده است.

    ``str(exc)`` همان پیامِ کاربر است: «Failed to refresh Admin Swagger: …» و
    ``hint`` جمله‌ی راهنمای بعدی را نگه می‌دارد.
    """

    def __init__(
        self,
        label: str,
        reason: str,
        hint: str = _RETRY_HINT,
        *,
        kind: str = "error",
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.label = label
        self.reason = reason
        self.hint = hint
        # kind — نوعِ شکست برای تصمیمِ retry در همگام‌سازی (KIND_*)؛ status — کدِ HTTP
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"Failed to refresh {label} Swagger: {reason}")


@dataclass(frozen=True)
class SwaggerSource:
    """یک منبعِ ثبت‌شده‌ی Swagger — تنها تعریفِ منبع در کلِ پروژه.

    ``key``           : شناسه‌ی پایدار (کلیدِ snapshot و مقدارِ انتخاب) — «admin»
    ``label``         : برچسبِ انسانی — «Admin»
    ``filename``      : نامِ snapshotِ محلی در ``data/swagger/``
    ``remote_url``    : نشانیِ خامِ سندِ OpenAPI — دکمه‌ی Update همین را fetch
                        می‌کند و همین source_url سرویسِ کشف‌شده در قدم دوم است
    ``primary_name``  : نامِ گروه در Swagger UI (``urls.primaryName``) و در
                        ``…/api-docs/<group>``؛ خالی یعنی همان ``label``
    ``path_prefixes`` : پیشوندهای مسیرِ endpoint که مالکیت را نشان می‌دهند
                        (``/admin``). مقایسه قطعه‌به‌قطعه است، نه زیررشته‌ای.
                        خالی یعنی مسیرها پیشوندِ متمایز ندارند و مالکیت فقط با
                        متادیتای snapshot ثابت می‌شود (مثلِ Site).
    ``base_paths``    : پیشوندِ gateway که در URLِ کاملِ endpoint پیش از مسیرِ
                        سند می‌آید (``https://host/api/admin/…`` → ``/admin/…``)
    ``enabled``       : منبعِ غیرفعال ثبت‌شده می‌ماند (URLش شناخته می‌شود) ولی
                        هرگز انتخاب نمی‌شود — ارجاع به آن خطای روشن است.
    """

    key: str
    label: str
    filename: str
    remote_url: str
    primary_name: str = ""
    path_prefixes: tuple[str, ...] = ()
    base_paths: tuple[str, ...] = ("/api",)
    enabled: bool = True

    @property
    def group(self) -> str:
        """نامِ گروهِ Swagger این منبع (``urls.primaryName``)."""
        return self.primary_name or self.label

    def identifiers(self) -> set[str]:
        """هر نامی که این منبع با آن شناخته می‌شود (حروفِ کوچک)."""
        return {self.key.lower(), self.label.lower(), self.group.lower()}


@dataclass(frozen=True)
class RefreshResult:
    """نتیجه‌ی یک تازه‌سازیِ موفق — برای پیامِ تأیید در UI.

    changed=False یعنی سندِ دریافتی با فایلِ موجود یکی بود و چیزی نوشته نشد."""

    key: str
    label: str
    path: Path
    url: str
    operations: int
    size_bytes: int
    changed: bool = True
    previous_operations: int | None = None


@dataclass(frozen=True)
class UpdateDecision:
    """تصمیمِ نوشتن برای یک سندِ معتبر: UPDATE یا UNCHANGED (رد شدن = SwaggerRefreshError)."""

    action: str  # DECISION_UPDATE | DECISION_UNCHANGED
    operations: int
    previous_operations: int | None
    shrink_ratio: float


DECISION_UPDATE = "UPDATE"
DECISION_UNCHANGED = "UNCHANGED"


# همه‌ی گروه‌ها روی همان backendِ podium منتشر می‌شوند؛ نشانیِ خامِ هر گروه
# «<این پایه>/<primaryName>» است (همان قاعده‌ی springdoc که Admin و Customer
# دارند).
# پایه از SWAGGER_API_DOCS_BASE_URL (src/swagger_settings.py)؛ پیش‌فرض همان نشانیِ قبلی است.
_API_DOCS_BASE = SWAGGER_API_DOCS_BASE_URL


def _registered(
    key: str, label: str, prefixes: tuple[str, ...], primary_name: str = ""
) -> SwaggerSource:
    group = primary_name or label
    return SwaggerSource(
        key=key,
        label=label,
        filename=f"{key}.json",
        remote_url=f"{_API_DOCS_BASE}/{group}",
        primary_name=primary_name,
        path_prefixes=prefixes,
    )


# رجیستریِ منابع — تنها جایی که یک منبعِ تازه اضافه می‌شود. منبعِ تازه (مثلاً
# ACL) فقط یک ردیفِ دیگر است؛ انتخابِ خودکار، تطبیقِ URL، دکمه‌ی Update و
# انتخابِ دستیِ قدم ۲ همه از همین فهرست می‌خوانند.
SWAGGER_SOURCES: tuple[SwaggerSource, ...] = (
    _registered(SELECTION_ADMIN, "Admin", ("/admin",)),
    _registered(SELECTION_CUSTOMER, "Customer", ("/customer",)),
    _registered(SELECTION_CRM, "CRM", ("/crm",)),
    _registered(SELECTION_PROVIDER, "Provider", ("/provider",)),
    # مسیرهای Site پیشوندِ متمایز ندارند (/serviceSet، …): مالکیت فقط با
    # متادیتای snapshot ثابت می‌شود، نه با «هر مسیرِ بی‌پیشوند».
    _registered(SELECTION_SITE, "Site", ()),
)


# ── پرسش‌های پایه ────────────────────────────────────────────────────────────


def source_for(key: str) -> SwaggerSource:
    """منبعِ یک کلید را برمی‌گرداند.

    پرتاب می‌کند:
        SwaggerSnapshotError : کلید ناشناخته باشد (کلیدهای مجاز نام برده می‌شوند)
    """
    # رجیستری هر بار خوانده می‌شود (چند ردیف است): ردیفِ تازه بدونِ هیچ
    # نگاشتِ جانبی‌ای شناخته می‌شود.
    wanted = (key or "").strip()
    source = next((source for source in SWAGGER_SOURCES if source.key == wanted), None)
    if source is None:
        valid = ", ".join(repr(source.key) for source in SWAGGER_SOURCES)
        raise SwaggerSnapshotError(f"Unknown Swagger source {key!r}. Valid sources are: {valid}.")
    return source


def enabled_sources(
    sources: tuple[SwaggerSource, ...] | None = None,
) -> tuple[SwaggerSource, ...]:
    """منابعی که اجازه‌ی انتخاب دارند، به ترتیبِ رجیستری."""
    return tuple(source for source in (sources or SWAGGER_SOURCES) if source.enabled)


def selection_label(selection: str) -> str:
    """برچسبِ خوانای یک انتخاب — برای نمایش در UI."""
    if (selection or "").strip().lower() in ("", SELECTION_AUTO):
        return "Automatic"
    return " + ".join(source_for(key).label for key in expand_selection(selection))


def expand_selection(selection: str) -> list[str]:
    """انتخابِ دستی را به فهرستِ مرتب و بی‌تکرارِ کلیدهای snapshot تبدیل می‌کند.

    یک کلید (``admin``)، چند کلید با ویرگول (``admin,crm``) یا مقدارِ قدیمیِ
    ``both`` (Admin + Customer). خروجی به ترتیبِ رجیستری است. ``auto`` یا رشته‌ی
    خالی انتخابِ دستی نیست و فهرستِ خالی برمی‌گرداند.

    پرتاب می‌کند:
        SwaggerSnapshotError : کلیدِ ناشناخته (کلیدهای مجاز نام برده می‌شوند)
    """
    value = (selection or "").strip().lower()
    if value in ("", SELECTION_AUTO):
        return []
    if value == SELECTION_BOTH:
        wanted = {SELECTION_ADMIN, SELECTION_CUSTOMER}
    else:
        wanted = {part.strip() for part in value.split(",") if part.strip()}
    for key in wanted:
        source_for(key)  # ناشناخته‌ها همین‌جا با پیامِ روشن رد می‌شوند
    return [source.key for source in SWAGGER_SOURCES if source.key in wanted]


def selection_for_keys(keys: list[str] | tuple[str, ...]) -> str:
    """عکسِ expand_selection — کلیدها به ترتیبِ رجیستری، جداشده با ویرگول.

    فهرستِ خالی رشته‌ی خالی است (یعنی «انتخابِ دستی نیست»).
    """
    wanted = set(keys)
    return ",".join(source.key for source in SWAGGER_SOURCES if source.key in wanted)


# ── URLهای سرویس در Developed Services ──────────────────────────────────────

# هر http(s) URL در متنِ آزاد؛ نشانه‌گذاریِ پایانِ جمله بعداً حذف می‌شود.
_URL_RE = re.compile(r"https?://[^\s<>\"'`()\[\]]+", re.IGNORECASE)
_URL_TRAILING = ".,;:!?"


def extract_urls(text: str) -> list[str]:
    """همه‌ی http(s) URLهای یک متنِ آزاد، به ترتیب و بدونِ تکرار."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(_URL_TRAILING)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def is_swagger_url(url: str) -> bool:
    """آیا این URL به یک سندِ Swagger/OpenAPI اشاره می‌کند (نه به خودِ یک API)؟

    صفحه‌ی Swagger UI، مسیرِ ``api-docs``، فایلِ ``.json``/``.yaml`` یا کوئریِ
    ``urls.primaryName``. بقیه (مثلاً ``https://host/api/admin/voucher``)
    ارجاع به یک endpoint هستند و منبعِ Swagger حساب نمی‌شوند.
    """
    parsed = urlparse(url or "")
    path = parsed.path.lower()
    if "swagger" in path or "api-docs" in path:
        return True
    if path.endswith((".json", ".yaml", ".yml")):
        return True
    query = {key.lower() for key in parse_qs(parsed.query)}
    return "urls.primaryname" in query


def _normalized(url: str) -> tuple[str, str]:
    """(میزبان، مسیر) بدونِ طرح، کوئری، فرگمنت و اسلشِ پایانی — برای مقایسه."""
    parsed = urlparse(url or "")
    return parsed.netloc.lower(), parsed.path.rstrip("/").lower()


def source_for_url(url: str) -> SwaggerSource | None:
    """منبعِ ثبت‌شده‌ای که این URL به آن اشاره می‌کند — یا None.

    ترتیب: ۱) همان ``remote_url`` ثبت‌شده؛ ۲) نامِ گروهِ Swagger (Swagger UI با
    ``urls.primaryName`` یا ``…/api-docs/<Group>``) که با یکی از نام‌های یک
    منبع (کلید، برچسب، primary name) یکی باشد. میزبان عمداً شرط نیست: همان
    گروه در محیط‌های مختلف (مثلاً sandpod و devpod) همان تعریفِ سرویس است و
    snapshotِ محلی منبعِ حقیقت است. URLی که گروهی نام نمی‌برد فقط با تطبیقِ
    کاملِ نشانی شناخته می‌شود. منبعِ غیرفعال هم برگردانده می‌شود تا فراخوان
    «غیرفعال» را از «ناشناخته» جدا کند. هیچ درخواستی به شبکه زده نمی‌شود.
    """
    target = _normalized(url)
    for source in SWAGGER_SOURCES:
        if _normalized(source.remote_url) == target:
            return source
    group = swagger_group_name(url).strip().lower()
    if not group:
        return None
    for source in SWAGGER_SOURCES:
        if group in source.identifiers():
            return source
    return None


@dataclass(frozen=True)
class ServiceUrlResolution:
    """نتیجه‌ی تطبیقِ URLهای Developed Services با منابعِ Swagger.

    ``matched``    : (URL، کلیدِ snapshot) برای هر URLِ Swagger که منبعش شناخته شد
    ``unresolved`` : URLهای Swagger که به هیچ منبعِ ثبت‌شده‌ای نمی‌رسند — خطا
    ``endpoints``  : URLهای غیرِ Swagger (ارجاع به endpoint) — فقط زمینه‌ی قدم ۱
    """

    matched: tuple[tuple[str, str], ...] = ()
    unresolved: tuple[str, ...] = ()
    endpoints: tuple[str, ...] = ()

    @property
    def keys(self) -> list[str]:
        """کلیدهای snapshot به ترتیبِ رجیستری و بدونِ تکرار."""
        found = {key for _, key in self.matched}
        return [source.key for source in SWAGGER_SOURCES if source.key in found]

    def error(self) -> str:
        """پیامِ خطای کاربر برای URLهای حل‌نشده — یا رشته‌ی خالی."""
        if not self.unresolved:
            return ""
        known = ", ".join(source.group for source in SWAGGER_SOURCES)
        listed = "\n".join(f"  - {url}" for url in self.unresolved)
        return (
            "These Swagger URLs do not match a registered Swagger source:\n"
            f"{listed}\n\nRegistered groups: {known}. Use a Swagger UI URL with "
            "?urls.primaryName=<group>, the raw OpenAPI URL (…/v3/api-docs/<group>), "
            "or remove the URL. A new service must first be added to SWAGGER_SOURCES "
            "(src/agents/test_case_generator/swagger_snapshots.py). Step 2 was not "
            "run so that no unrelated Swagger document is used."
        )


def resolve_service_urls(text: str) -> ServiceUrlResolution:
    """URLهای Developed Services را به کلیدهای snapshot تبدیل می‌کند (بدونِ شبکه)."""
    matched: list[tuple[str, str]] = []
    unresolved: list[str] = []
    endpoints: list[str] = []
    for url in extract_urls(text):
        source = source_for_url(url)
        if source is not None:
            matched.append((url, source.key))
        elif is_swagger_url(url):
            unresolved.append(url)
        else:
            endpoints.append(url)
    return ServiceUrlResolution(tuple(matched), tuple(unresolved), tuple(endpoints))


def snapshot_path(key: str, *, directory: Path | str | None = None) -> Path:
    """مسیرِ فایلِ snapshot یک منبع."""
    base = Path(directory) if directory is not None else SNAPSHOT_DIR
    return base / source_for(key).filename


def snapshot_available(key: str, *, directory: Path | str | None = None) -> bool:
    """آیا snapshotِ محلیِ این منبع روی دیسک هست؟ (محتوا بررسی نمی‌شود)"""
    return snapshot_path(key, directory=directory).is_file()


# ── خواندن ───────────────────────────────────────────────────────────────────


def load_snapshot(key: str, *, directory: Path | str | None = None) -> DiscoveredService:
    """یک snapshot محلی را می‌خواند و به DiscoveredService تبدیل می‌کند.

    ``source_url`` عمداً نشانیِ اصلیِ سند است (نه مسیرِ فایل) تا منشأِ
    snapshot در نتیجه‌ی قدم دوم دیده شود.

    پرتاب می‌کند:
        SwaggerSnapshotError : فایل نباشد، JSON نباشد یا OpenAPI معتبری نباشد
    """
    source = source_for(key)
    path = snapshot_path(key, directory=directory)

    if not path.is_file():
        raise SwaggerSnapshotError(
            f"Swagger snapshot for '{source.label}' was not found at {path}. "
            f"Save the OpenAPI JSON document there — see data/swagger/README.md."
        )

    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SwaggerSnapshotError(f"Swagger snapshot {path} could not be read: {exc}") from exc

    try:
        return discover_from_spec(spec, source_url=source.remote_url, name=source.label)
    except SwaggerLoadError as exc:
        raise SwaggerSnapshotError(
            f"Swagger snapshot {path} is not a usable OpenAPI document: {exc}"
        ) from exc


def load_snapshots(
    keys: list[str], *, directory: Path | str | None = None
) -> tuple[list[DiscoveredService], list[str]]:
    """چند snapshot را می‌خواند و (سرویس‌ها، خطاها) را برمی‌گرداند.

    مثلِ ``discover_services``، یک snapshot خراب کلِ قدم را متوقف نمی‌کند:
    خطا جمع می‌شود و بقیه ادامه پیدا می‌کنند تا نتیجه برای بازبینیِ انسانی
    کامل بماند.
    """
    services: list[DiscoveredService] = []
    errors: list[str] = []
    for key in keys:
        try:
            services.append(load_snapshot(key, directory=directory))
        except SwaggerSnapshotError as exc:
            errors.append(str(exc))
    return services, errors


def snapshot_fingerprint(keys: list[str], *, directory: Path | str | None = None) -> str:
    """اثرانگشتِ محتوای snapshotها — مثلاً «admin=1a2b3c4d5e6f».

    قدم دوم این را به‌عنوانِ ورودیِ خودش ثبت می‌کند تا اگر فایلی بعد از اجرای
    نگاشت (با دکمه‌ی Update یا دستی) عوض شد، نتیجه‌ی قبلی به‌عنوانِ «متعلق به
    ورودیِ قدیمی» گزارش شود. فایلِ ناموجود «missing» است، نه خطا.
    """
    parts: list[str] = []
    for key in keys:
        try:
            digest = hashlib.sha256(snapshot_path(key, directory=directory).read_bytes())
            parts.append(f"{key}={digest.hexdigest()[:12]}")
        except OSError:
            parts.append(f"{key}=missing")
    return ", ".join(parts)


# ── تازه‌سازی ────────────────────────────────────────────────────────────────


def _fetch(source: SwaggerSource, session: Any, timeout: tuple[float, float]) -> tuple[bytes, int]:
    """(بدنه‌ی کامل، کدِ HTTP) ِ پاسخ؛ هر شکستِ شبکه/HTTP یک SwaggerRefreshError است."""
    url = source.remote_url
    try:
        response = session.get(url, timeout=timeout, headers={"Accept": "application/json"})
    # ترتیب مهم است: ConnectTimeout هم زیرکلاسِ Timeout است و هم ConnectionError.
    except requests.exceptions.ConnectTimeout as exc:
        raise SwaggerRefreshError(
            source.label,
            f"Connection timed out after {timeout[0]:g}s while connecting to {url}.",
            _NETWORK_HINT,
            kind=KIND_CONNECT_TIMEOUT,
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise SwaggerRefreshError(
            source.label,
            f"Request timed out — {url} did not respond within {timeout[1]:g}s.",
            _NETWORK_HINT,
            kind=KIND_READ_TIMEOUT,
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise SwaggerRefreshError(
            source.label,
            f"Could not connect to {url} (connection refused or network failure).",
            _NETWORK_HINT,
            kind=KIND_CONNECTION,
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise SwaggerRefreshError(
            source.label,
            f"Request to {url} failed: {exc}",
            _NETWORK_HINT,
            kind=KIND_REQUEST,
        ) from exc

    status = response.status_code
    if not 200 <= status < 300:
        reason = f" {response.reason}" if getattr(response, "reason", "") else ""
        raise SwaggerRefreshError(
            source.label,
            f"The server returned HTTP {status}{reason} for {url}.",
            kind=KIND_HTTP_STATUS,
            status=status,
            retry_after=_retry_after(response),
        )
    return response.content or b"", status


def _retry_after(response: Any) -> float | None:
    """Retry-After ِ عددی (ثانیه) — هر شکلِ دیگر نادیده گرفته می‌شود."""
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _validate(source: SwaggerSource, content: bytes) -> DiscoveredService:
    """محتوا باید JSON و یک سندِ OpenAPI/Swagger با دست‌کم یک عملیات باشد."""
    if not content.strip():
        raise SwaggerRefreshError(
            source.label, "The server returned an empty response.", kind=KIND_EMPTY
        )
    try:
        spec = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SwaggerRefreshError(
            source.label, f"The response is not valid JSON ({exc}).", kind=KIND_INVALID_JSON
        ) from exc

    if not isinstance(spec, dict) or not (spec.get("openapi") or spec.get("swagger")):
        raise SwaggerRefreshError(
            source.label,
            "The response is not a Swagger/OpenAPI document (no 'openapi' or "
            "'swagger' version field).",
            kind=KIND_NOT_OPENAPI,
        )
    try:
        service = discover_from_spec(spec, source_url=source.remote_url, name=source.label)
    except SwaggerLoadError as exc:
        raise SwaggerRefreshError(
            source.label, f"The document is not usable: {exc}", kind=KIND_UNUSABLE
        ) from exc
    if not service.apis:
        raise SwaggerRefreshError(
            source.label, "The document contains no API operation.", kind=KIND_NO_OPERATIONS
        )
    return service


def _replace_atomically(source: SwaggerSource, path: Path, content: bytes) -> None:
    """محتوا را در یک فایلِ موقت در همان پوشه می‌نویسد و بعد جایگزین می‌کند.

    ``os.replace`` روی یک فایل‌سیستم اتمی است؛ پس خواننده یا فایلِ قبلی را
    می‌بیند یا فایلِ کامل جدید را — هرگز فایلِ نیمه‌نوشته. در صورتِ شکست، فایلِ
    موقت پاک می‌شود و فایلِ قبلی دست‌نخورده می‌ماند.
    """
    temp_name = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except OSError as exc:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
        if isinstance(exc, PermissionError):
            reason, kind = f"Permission denied while writing {path}.", KIND_PERMISSION
        else:
            reason, kind = f"Could not write {path}: {exc.strerror or exc}.", KIND_WRITE
        raise SwaggerRefreshError(source.label, reason, kind=kind) from exc


@dataclass(frozen=True)
class FetchedDocument:
    """سندِ دریافت‌شده و معتبر، با کدِ HTTP ِ پاسخ."""

    content: bytes
    service: DiscoveredService
    status: int


def fetch_validated(
    key: str,
    *,
    session: Any = None,
    timeout: tuple[float, float] | None = None,
    url: str | None = None,
) -> FetchedDocument:
    """سندِ یک منبع را می‌گیرد و اعتبارسنجی می‌کند — هیچ فایلی نوشته نمی‌شود.

    url فقط برای بررسیِ HTTPS (همان منبع با نشانیِ دیگر) است؛ پیش‌فرض remote_url ِ ثبت‌شده.
    خطای اعتبارسنجی (پاسخِ 2xx با بدنه‌ی نامعتبر) هم کدِ HTTP را در ``status`` دارد.

    پرتاب می‌کند:
        SwaggerSnapshotError : کلید ناشناخته باشد
        SwaggerRefreshError  : شبکه، HTTP، JSON یا اعتبارسنجی شکست بخورد (با kind)
    """
    source = source_for(key)
    if url is not None and url != source.remote_url:
        source = SwaggerSource(**{**source.__dict__, "remote_url": url})
    timeout = timeout or (SWAGGER_CONNECT_TIMEOUT_SECONDS, SWAGGER_READ_TIMEOUT_SECONDS)
    content, status = _fetch(source, session or requests.Session(), timeout)
    try:
        service = _validate(source, content)
    except SwaggerRefreshError as exc:
        exc.status = status
        raise
    return FetchedDocument(content, service, status)


def fetch_and_validate(
    key: str,
    *,
    session: Any = None,
    timeout: tuple[float, float] | None = None,
    url: str | None = None,
) -> tuple[bytes, DiscoveredService]:
    """همان fetch_validated، بدونِ کدِ HTTP: (بایت‌ها، سرویس)."""
    document = fetch_validated(key, session=session, timeout=timeout, url=url)
    return document.content, document.service


def remove_stale_temp_files(directory: Path | str | None = None) -> list[str]:
    """فایل‌های موقتِ جامانده از جایگزینیِ اتمیِ قطع‌شده (پروسه‌ای که وسطِ نوشتن کشته شد) را
    زیرِ قفلِ نوشتن حذف می‌کند — فقط «.<key>.json.*.tmp» ِ منابعِ ثبت‌شده، نه هیچ فایلِ دیگری.
    هر شکست (قفل، دسترسی) بی‌صدا رد می‌شود: این پاک‌سازی هرگز اجرا را متوقف نمی‌کند."""
    base = Path(directory) if directory is not None else SNAPSHOT_DIR
    removed: list[str] = []
    try:
        with write_lock(base, wait_s=5):
            for source in SWAGGER_SOURCES:
                for path in base.glob(f".{source.filename}.*.tmp"):
                    try:
                        if path.is_file():
                            path.unlink()
                            removed.append(path.name)
                    except OSError:
                        continue
    except (SwaggerLockBusy, SwaggerLockUnavailable, OSError):
        return removed
    return removed


def _existing_operations(source: SwaggerSource, path: Path) -> tuple[bytes | None, int | None]:
    """(بایت‌ها، تعدادِ operationها) ِ فایلِ موجود؛ فایلِ ناموجود/نامعتبر → (بایت یا None، None)."""
    try:
        existing = path.read_bytes()
    except OSError:
        return None, None
    try:
        spec = json.loads(existing)
        service = discover_from_spec(spec, source_url=source.remote_url, name=source.label)
    except (UnicodeDecodeError, json.JSONDecodeError, SwaggerLoadError, TypeError, ValueError):
        return existing, None
    return existing, len(service.apis)


def _same_document(old: bytes | None, new: bytes) -> bool:
    if old is None:
        return False
    if old == new:
        return True
    try:
        return json.loads(old) == json.loads(new)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False


def decide_update(
    key: str,
    content: bytes,
    service: DiscoveredService,
    *,
    directory: Path | str | None = None,
    shrink_ratio: float | None = None,
) -> UpdateDecision:
    """سندِ معتبر جای فایلِ موجود بنشیند یا نه — هیچ فایلی نوشته نمی‌شود.

    * محتوای یکسان (بایت‌به‌بایت یا JSON ِ برابر) → UNCHANGED؛
    * محافظِ کاهش: اگر فایلِ موجود معتبر است و operationهای سندِ تازه کمتر از
      shrink_ratio × operationهای فعلی‌اند → SwaggerRefreshError(kind=shrink)؛
      shrink_ratio ِ None یعنی SWAGGER_SYNC_MAX_SHRINK_RATIO[_<KEY>]؛ 0 یعنی غیرفعال.
    """
    source = source_for(key)
    path = snapshot_path(key, directory=directory)
    if shrink_ratio is None:
        try:
            shrink_ratio = max_shrink_ratio(source.key)
        except SwaggerSettingsError as exc:
            raise SwaggerRefreshError(
                source.label, f"{exc} The existing file was kept.", kind=KIND_CONFIGURATION
            ) from None
    existing, previous = _existing_operations(source, path)
    operations = len(service.apis)
    if _same_document(existing, content):
        return UpdateDecision(DECISION_UNCHANGED, operations, previous, shrink_ratio)
    if previous and shrink_ratio > 0 and operations < shrink_ratio * previous:
        raise SwaggerRefreshError(
            source.label,
            f"The new document has {operations} operations, fewer than "
            f"{shrink_ratio:g} × the {previous} operations of the current file; it looks "
            "incomplete. The existing file was kept (shrink guard).",
            kind=KIND_SHRINK,
        )
    return UpdateDecision(DECISION_UPDATE, operations, previous, shrink_ratio)


def write_snapshot(
    key: str,
    content: bytes,
    service: DiscoveredService,
    *,
    directory: Path | str | None = None,
    shrink_ratio: float | None = None,
    lock_wait_s: float | None = None,
) -> RefreshResult:
    """زیرِ قفلِ نوشتنِ مشترک: decide_update و اگر لازم بود جایگزینیِ اتمیِ همان فایل.

    تصمیم داخلِ قفل گرفته می‌شود تا با فایلی مقایسه شود که واقعاً جایگزین می‌شود. هر
    شکستی (قفل، محافظ، نوشتن) فایلِ قبلی را دست‌نخورده نگه می‌دارد."""
    source = source_for(key)
    path = snapshot_path(key, directory=directory)
    base = directory if directory is not None else SNAPSHOT_DIR
    wait = {} if lock_wait_s is None else {"wait_s": lock_wait_s}
    try:
        with write_lock(base, **wait):
            decision = decide_update(
                key, content, service, directory=directory, shrink_ratio=shrink_ratio
            )
            if decision.action == DECISION_UPDATE:
                _replace_atomically(source, path, content)
    except SwaggerLockBusy:
        raise SwaggerRefreshError(
            source.label,
            "Another Swagger update is writing the snapshots right now (the daily sync or "
            "another Update click).",
            kind=KIND_LOCKED,
        ) from None
    except SwaggerLockUnavailable:
        raise SwaggerRefreshError(
            source.label,
            "The Swagger snapshot lock could not be confirmed; nothing was written.",
            kind=KIND_LOCKED,
        ) from None
    return RefreshResult(
        key=source.key,
        label=source.label,
        path=path,
        url=source.remote_url,
        operations=decision.operations,
        size_bytes=len(content),
        changed=decision.action == DECISION_UPDATE,
        previous_operations=decision.previous_operations,
    )


def refresh_snapshot(
    key: str,
    *,
    session: Any = None,
    timeout: tuple[float, float] | None = None,
    directory: Path | str | None = None,
    shrink_ratio: float | None = None,
) -> RefreshResult:
    """سندِ تازه‌ی یک منبع را می‌گیرد و فقط snapshotِ همان منبع را جایگزین می‌کند.

    هر فراخوانی یک درخواستِ تازه است (هیچ cacheی نیست). ترتیب: دریافتِ کامل →
    بررسیِ کدِ HTTP → parse JSON → اعتبارسنجیِ OpenAPI → (زیرِ قفلِ نوشتن) محافظِ کاهش و
    تشخیصِ «بدونِ تغییر» → نوشتنِ اتمی. اگر هر قدم شکست بخورد، فایلِ قبلی دست‌نخورده
    می‌ماند. تلاشِ دوباره خودکار نیست — کاربر دکمه را دوباره می‌زند.

    پرتاب می‌کند:
        SwaggerSnapshotError : کلید ناشناخته باشد
        SwaggerRefreshError  : شبکه، HTTP، JSON، اعتبارسنجی، محافظ، قفل یا نوشتن شکست بخورد
    """
    content, service = fetch_and_validate(key, session=session, timeout=timeout)
    return write_snapshot(key, content, service, directory=directory, shrink_ratio=shrink_ratio)


__all__ = [
    "DECISION_UNCHANGED",
    "DECISION_UPDATE",
    "DEFAULT_SELECTION",
    "SELECTION_ADMIN",
    "SELECTION_AUTO",
    "SELECTION_BOTH",
    "SELECTION_CRM",
    "SELECTION_CUSTOMER",
    "SELECTION_PROVIDER",
    "SELECTION_SITE",
    "SNAPSHOT_DIR",
    "SWAGGER_SOURCES",
    "RefreshResult",
    "ServiceUrlResolution",
    "SwaggerRefreshError",
    "SwaggerSnapshotError",
    "SwaggerSource",
    "UpdateDecision",
    "decide_update",
    "FetchedDocument",
    "fetch_and_validate",
    "fetch_validated",
    "remove_stale_temp_files",
    "write_snapshot",
    "enabled_sources",
    "expand_selection",
    "extract_urls",
    "is_swagger_url",
    "load_snapshot",
    "load_snapshots",
    "refresh_snapshot",
    "resolve_service_urls",
    "selection_for_keys",
    "selection_label",
    "snapshot_available",
    "snapshot_fingerprint",
    "snapshot_path",
    "source_for",
    "source_for_url",
]
