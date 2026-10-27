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
خطایی فایلِ قبلی را دست‌نخورده نگه می‌دارد. هیچ cacheی در کار نیست: قدم دوم در
هر اجرا فایل را دوباره می‌خواند، پس اجرای بعدیِ نگاشت سندِ تازه را می‌بیند.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from src.agents.test_case_generator.api_discovery import (
    DiscoveredService,
    SwaggerLoadError,
    discover_from_spec,
)
from src.config import SWAGGER_CONNECT_TIMEOUT_SECONDS, SWAGGER_READ_TIMEOUT_SECONDS

# پوشه‌ی snapshotها در ریشه‌ی پروژه. برای تست قابلِ جایگزینی است.
SNAPSHOT_DIR = Path(__file__).resolve().parents[3] / "data" / "swagger"

# راهنمای کاربر بعد از شکست — بسته به نوعِ خطا
_NETWORK_HINT = "Please check the connection and try again."
_RETRY_HINT = "The existing file was kept. You can try again."

# ── انتخابِ منبع ─────────────────────────────────────────────────────────────

SELECTION_ADMIN = "admin"
SELECTION_CUSTOMER = "customer"
SELECTION_BOTH = "both"

# ترتیبِ نمایش در UI. پیش‌فرض همان رفتارِ قبلی است (هر دو سرویس).
SELECTIONS: tuple[str, ...] = (SELECTION_ADMIN, SELECTION_CUSTOMER, SELECTION_BOTH)
DEFAULT_SELECTION = SELECTION_BOTH


class SwaggerSnapshotError(RuntimeError):
    """snapshot درخواستی وجود ندارد، خوانده نشد یا سندِ معتبری نیست."""


class SwaggerRefreshError(RuntimeError):
    """تازه‌سازیِ یک snapshot شکست خورد — فایلِ قبلی دست‌نخورده مانده است.

    ``str(exc)`` همان پیامِ کاربر است: «Failed to refresh Admin Swagger: …» و
    ``hint`` جمله‌ی راهنمای بعدی را نگه می‌دارد.
    """

    def __init__(self, label: str, reason: str, hint: str = _RETRY_HINT) -> None:
        self.label = label
        self.reason = reason
        self.hint = hint
        super().__init__(f"Failed to refresh {label} Swagger: {reason}")


@dataclass(frozen=True)
class SwaggerSource:
    """یک منبعِ Swagger: کلیدِ snapshot، برچسبِ انسانی و نشانیِ اصلیِ سند."""

    key: str
    label: str
    filename: str
    # نشانیِ خامِ سندِ OpenAPI — دکمه‌ی Update همین را fetch می‌کند و همین به‌عنوانِ
    # source_url سرویسِ کشف‌شده در نتیجه‌ی قدم دوم دیده می‌شود.
    remote_url: str


@dataclass(frozen=True)
class RefreshResult:
    """نتیجه‌ی یک تازه‌سازیِ موفق — برای پیامِ تأیید در UI."""

    key: str
    label: str
    path: Path
    url: str
    operations: int
    size_bytes: int


# رجیستریِ منابع — تنها جایی که یک منبعِ تازه اضافه می‌شود.
SWAGGER_SOURCES: tuple[SwaggerSource, ...] = (
    SwaggerSource(
        key=SELECTION_ADMIN,
        label="Admin",
        filename="admin.json",
        remote_url="http://podium-back.devpod.ir/api/v3/api-docs/Admin",
    ),
    SwaggerSource(
        key=SELECTION_CUSTOMER,
        label="Customer",
        filename="customer.json",
        remote_url="http://podium-back.devpod.ir/api/v3/api-docs/Customer",
    ),
)

_BY_KEY: dict[str, SwaggerSource] = {source.key: source for source in SWAGGER_SOURCES}


# ── پرسش‌های پایه ────────────────────────────────────────────────────────────

def source_for(key: str) -> SwaggerSource:
    """منبعِ یک کلید را برمی‌گرداند.

    پرتاب می‌کند:
        SwaggerSnapshotError : کلید ناشناخته باشد (کلیدهای مجاز نام برده می‌شوند)
    """
    source = _BY_KEY.get((key or "").strip())
    if source is None:
        valid = ", ".join(repr(source.key) for source in SWAGGER_SOURCES)
        raise SwaggerSnapshotError(
            f"Unknown Swagger source {key!r}. Valid sources are: {valid}."
        )
    return source


def selection_label(selection: str) -> str:
    """برچسبِ خوانای یک انتخاب — برای نمایش در UI."""
    keys = expand_selection(selection)
    return " + ".join(source_for(key).label for key in keys)


def expand_selection(selection: str) -> list[str]:
    """انتخابِ کاربر را به فهرستِ مرتب و بی‌تکرارِ کلیدهای snapshot تبدیل می‌کند.

    ``admin`` → ``["admin"]``، ``customer`` → ``["customer"]`` و
    ``both`` → ``["admin", "customer"]``.

    پرتاب می‌کند:
        SwaggerSnapshotError : انتخاب ناشناخته باشد
    """
    value = (selection or "").strip().lower()
    if value == SELECTION_BOTH:
        return [source.key for source in SWAGGER_SOURCES]
    source_for(value)  # ناشناخته‌ها همین‌جا با پیامِ روشن رد می‌شوند
    return [value]


def snapshot_path(key: str, *, directory: Path | str | None = None) -> Path:
    """مسیرِ فایلِ snapshot یک منبع."""
    base = Path(directory) if directory is not None else SNAPSHOT_DIR
    return base / source_for(key).filename


# ── خواندن ───────────────────────────────────────────────────────────────────

def load_snapshot(
    key: str, *, directory: Path | str | None = None
) -> DiscoveredService:
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
        raise SwaggerSnapshotError(
            f"Swagger snapshot {path} could not be read: {exc}"
        ) from exc

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


def snapshot_fingerprint(
    keys: list[str], *, directory: Path | str | None = None
) -> str:
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

def _fetch(source: SwaggerSource, session: Any, timeout: tuple[float, float]) -> bytes:
    """بدنه‌ی کاملِ پاسخ را می‌گیرد؛ هر شکستِ شبکه/HTTP یک SwaggerRefreshError است."""
    url = source.remote_url
    try:
        response = session.get(
            url, timeout=timeout, headers={"Accept": "application/json"}
        )
    # ترتیب مهم است: ConnectTimeout هم زیرکلاسِ Timeout است و هم ConnectionError.
    except requests.exceptions.ConnectTimeout as exc:
        raise SwaggerRefreshError(
            source.label,
            f"Connection timed out after {timeout[0]:g}s while connecting to {url}.",
            _NETWORK_HINT,
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise SwaggerRefreshError(
            source.label,
            f"Request timed out — {url} did not respond within {timeout[1]:g}s.",
            _NETWORK_HINT,
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise SwaggerRefreshError(
            source.label,
            f"Could not connect to {url} (connection refused or network failure).",
            _NETWORK_HINT,
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise SwaggerRefreshError(
            source.label, f"Request to {url} failed: {exc}", _NETWORK_HINT
        ) from exc

    status = response.status_code
    if not 200 <= status < 300:
        reason = f" {response.reason}" if getattr(response, "reason", "") else ""
        raise SwaggerRefreshError(
            source.label, f"The server returned HTTP {status}{reason} for {url}."
        )
    return response.content or b""


def _validate(source: SwaggerSource, content: bytes) -> DiscoveredService:
    """محتوا باید JSON و یک سندِ OpenAPI/Swagger با دست‌کم یک عملیات باشد."""
    if not content.strip():
        raise SwaggerRefreshError(source.label, "The server returned an empty response.")
    try:
        spec = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SwaggerRefreshError(
            source.label, f"The response is not valid JSON ({exc})."
        ) from exc

    if not isinstance(spec, dict) or not (spec.get("openapi") or spec.get("swagger")):
        raise SwaggerRefreshError(
            source.label,
            "The response is not a Swagger/OpenAPI document (no 'openapi' or "
            "'swagger' version field).",
        )
    try:
        service = discover_from_spec(spec, source_url=source.remote_url, name=source.label)
    except SwaggerLoadError as exc:
        raise SwaggerRefreshError(source.label, f"The document is not usable: {exc}") from exc
    if not service.apis:
        raise SwaggerRefreshError(
            source.label, "The document contains no API operation."
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
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except OSError as exc:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
        if isinstance(exc, PermissionError):
            reason = f"Permission denied while writing {path}."
        else:
            reason = f"Could not write {path}: {exc.strerror or exc}."
        raise SwaggerRefreshError(source.label, reason) from exc


def refresh_snapshot(
    key: str,
    *,
    session: Any = None,
    timeout: tuple[float, float] | None = None,
    directory: Path | str | None = None,
) -> RefreshResult:
    """سندِ تازه‌ی یک منبع را می‌گیرد و فقط snapshotِ همان منبع را جایگزین می‌کند.

    هر فراخوانی یک درخواستِ تازه است (هیچ cacheی نیست). ترتیب: دریافتِ کامل →
    بررسیِ کدِ HTTP → parse JSON → اعتبارسنجیِ OpenAPI → نوشتنِ اتمی. اگر هر
    قدم شکست بخورد، فایلِ قبلی دست‌نخورده می‌ماند. تلاشِ دوباره خودکار نیست —
    کاربر دکمه را دوباره می‌زند.

    پرتاب می‌کند:
        SwaggerSnapshotError : کلید ناشناخته باشد
        SwaggerRefreshError  : شبکه، HTTP، JSON، اعتبارسنجی یا نوشتن شکست بخورد
    """
    source = source_for(key)
    path = snapshot_path(key, directory=directory)
    timeout = timeout or (SWAGGER_CONNECT_TIMEOUT_SECONDS, SWAGGER_READ_TIMEOUT_SECONDS)

    content = _fetch(source, session or requests.Session(), timeout)
    service = _validate(source, content)
    _replace_atomically(source, path, content)

    return RefreshResult(
        key=source.key,
        label=source.label,
        path=path,
        url=source.remote_url,
        operations=len(service.apis),
        size_bytes=len(content),
    )


__all__ = [
    "DEFAULT_SELECTION",
    "SELECTIONS",
    "SELECTION_ADMIN",
    "SELECTION_BOTH",
    "SELECTION_CUSTOMER",
    "SNAPSHOT_DIR",
    "SWAGGER_SOURCES",
    "RefreshResult",
    "SwaggerRefreshError",
    "SwaggerSnapshotError",
    "SwaggerSource",
    "expand_selection",
    "load_snapshot",
    "load_snapshots",
    "refresh_snapshot",
    "selection_label",
    "snapshot_fingerprint",
    "snapshot_path",
    "source_for",
]
