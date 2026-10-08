"""
swagger_snapshots.py — منابعِ محلیِ Swagger برای قدم دوم

قدم دوم در اجرای عادیِ ویزارد **هیچ درخواستی به Swagger UI بیرونی نمی‌زند**:
از snapshotهای محلیِ ``data/swagger/`` می‌خواند. این کار اجرا را تکرارپذیر
می‌کند — همان سندِ ورودی، همان کاتالوگِ کشف‌شده — و قدم را از میزبانی که
ممکن است در دسترس نباشد یا احراز هویت بخواهد جدا می‌کند.

این ماژول فقط *می‌خواند*: هیچ fetch، هیچ توکن و هیچ کوکی‌ای اینجا نیست.
parse و کشف دوباره پیاده نشده — همان ``discover_from_spec`` ماژولِ
``api_discovery`` استفاده می‌شود، پس معنای کشف عوض نمی‌شود.

کدِ کشفِ از راه دور (``api_discovery.load_service`` / ``discover_services``)
دست‌نخورده باقی مانده است: برای «تازه‌سازیِ snapshot» در آینده لازم می‌شود.

تازه‌سازی عمداً پیاده نشده: هر منبع ``remote_url`` خود را به‌عنوان فراداده
نگه می‌دارد و همین رجیستری جایی است که یک مکانیزمِ refresh در آینده به آن
نوشته می‌شود — ولی هیچ درخواستِ شبکه‌ای در این تغییر انجام نمی‌شود.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.agents.test_case_generator.api_discovery import (
    DiscoveredService,
    SwaggerLoadError,
    discover_from_spec,
)

# پوشه‌ی snapshotها در ریشه‌ی پروژه. برای تست قابلِ جایگزینی است.
SNAPSHOT_DIR = Path(__file__).resolve().parents[3] / "data" / "swagger"

# متنِ ثابتِ دکمه‌های Update در UI — تازه‌سازی هنوز پیاده نشده است.
REFRESH_NOT_IMPLEMENTED = (
    "Refreshing a snapshot is not implemented yet. Update the JSON file in "
    "data/swagger/ by hand for now."
)

# ── انتخابِ منبع ─────────────────────────────────────────────────────────────

SELECTION_ADMIN = "admin"
SELECTION_CUSTOMER = "customer"
SELECTION_BOTH = "both"

# ترتیبِ نمایش در UI. پیش‌فرض همان رفتارِ قبلی است (هر دو سرویس).
SELECTIONS: tuple[str, ...] = (SELECTION_ADMIN, SELECTION_CUSTOMER, SELECTION_BOTH)
DEFAULT_SELECTION = SELECTION_BOTH


class SwaggerSnapshotError(RuntimeError):
    """snapshot درخواستی وجود ندارد، خوانده نشد یا سندِ معتبری نیست."""


@dataclass(frozen=True)
class SwaggerSource:
    """یک منبعِ Swagger: کلیدِ snapshot، برچسبِ انسانی و نشانیِ اصلیِ سند."""

    key: str
    label: str
    filename: str
    # فقط فراداده: هیچ‌جا fetch نمی‌شود. لنگرِ تازه‌سازیِ آینده است.
    remote_url: str


# رجیستریِ منابع — تنها جایی که یک منبعِ تازه اضافه می‌شود.
SWAGGER_SOURCES: tuple[SwaggerSource, ...] = (
    SwaggerSource(
        key=SELECTION_ADMIN,
        label="Admin",
        filename="admin.json",
        remote_url=(
            "https://podium-admin.sandpod.ir/api/swagger-ui/index.html"
            "?urls.primaryName=Admin"
        ),
    ),
    SwaggerSource(
        key=SELECTION_CUSTOMER,
        label="Customer",
        filename="customer.json",
        remote_url=(
            "http://podium-back.devpod.ir/api/swagger-ui/index.html"
            "?urls.primaryName=Customer"
        ),
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


__all__ = [
    "DEFAULT_SELECTION",
    "REFRESH_NOT_IMPLEMENTED",
    "SELECTIONS",
    "SELECTION_ADMIN",
    "SELECTION_BOTH",
    "SELECTION_CUSTOMER",
    "SNAPSHOT_DIR",
    "SWAGGER_SOURCES",
    "SwaggerSnapshotError",
    "SwaggerSource",
    "expand_selection",
    "load_snapshot",
    "load_snapshots",
    "selection_label",
    "snapshot_path",
    "source_for",
]
