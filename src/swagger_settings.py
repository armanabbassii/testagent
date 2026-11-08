"""
swagger_settings.py — تنظیماتِ Swagger (snapshot، دکمه‌ی Update و همگام‌سازیِ روزانه)

مستقل از LLM: فقط .env را با src/env_loader می‌خواند (import ِ پکیجِ src همین کار را کرده است)
و هرگز src/config.py یا متغیرهای الزامیِ LLM/Embedding را لازم ندارد.

    SWAGGER_CONNECT_TIMEOUT_SECONDS / SWAGGER_READ_TIMEOUT_SECONDS   همان نام‌ها و پیش‌فرض‌های قبلی
    SWAGGER_API_DOCS_BASE_URL       پایه‌ی «<base>/<group>» ِ منابعِ ثبت‌شده
    SWAGGER_SYNC_*                  تنظیماتِ همگام‌سازیِ روزانه (load_sync_settings)
                                    MAX_ATTEMPTS و MAX_RUNTIME_MINUTES: 0 (پیش‌فرض) = نامحدود
    SWAGGER_SYNC_MAX_SHRINK_RATIO[_<KEY>]   محافظِ کاهشِ operationها (max_shrink_ratio)

خطای پیکربندی (SwaggerSettingsError) فقط نامِ متغیر را می‌گوید؛ مقدارِ نامعتبر در پیام نمی‌آید
(شاید به‌اشتباه رازی آن‌جا نوشته شده باشد).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from src.env_loader import load_env

load_env()  # اگر این ماژول مستقیم (بیرون از پکیجِ src) بار شود — بی‌اثر در غیرِ این صورت

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API_DOCS_BASE_URL = "http://podium-back.devpod.ir/api/v3/api-docs"
DEFAULT_SHRINK_RATIO = 0.5
# retry ِ نامحدود هرگز نباید سرور را پشتِ‌سرِ‌هم بکوبد: کفِ فاصله‌ها ۱ ثانیه است
MIN_RETRY_INTERVAL_S = 1.0


class SwaggerSettingsError(ValueError):
    """تنظیمِ نامعتبر — پیام فقط نامِ متغیر را دارد."""


def _raw(name: str) -> str | None:
    value = os.getenv(name)
    return None if value is None or not value.strip() else value.strip()


def _float(name: str, default: float, *, minimum: float, maximum: float | None = None) -> float:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SwaggerSettingsError(f"{name} must be a number.") from None
    if value != value or value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum:g} and {maximum:g}" if maximum is not None else f">= {minimum:g}"
        raise SwaggerSettingsError(f"{name} must be {bound}.")
    return value


def _int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = _raw(name)
    if raw is None:
        return default
    if not re.fullmatch(r"[+-]?\d+", raw):
        raise SwaggerSettingsError(f"{name} must be an integer.")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise SwaggerSettingsError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _raw(name)
    if raw is None:
        return default
    value = raw.lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    raise SwaggerSettingsError(f"{name} must be true or false.")


def validate_base_url(value: str, name: str = "SWAGGER_API_DOCS_BASE_URL") -> str:
    """http(s) با میزبان؛ بی‌credential، بی‌query و بی‌fragment؛ اسلشِ پایانی حذف می‌شود."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SwaggerSettingsError(f"{name} must be an http:// or https:// URL with a host.")
    if parts.username or parts.password:
        raise SwaggerSettingsError(f"{name} must not contain credentials.")
    if parts.query or parts.fragment:
        raise SwaggerSettingsError(f"{name} must not contain a query or fragment.")
    return value.rstrip("/")


# ── مقدارهای importزمان (همان رفتارِ قبلیِ src/config.py برای timeoutها) ─────────────
# timeoutِ دریافتِ سندِ Swagger. اتصال کوتاه است تا میزبانِ ازدسترس‌خارج زود گزارش شود؛
# خواندن بلندتر است: سرور سند را در اولین درخواستِ بعد از راه‌اندازی می‌سازد و در آزمونِ
# واقعی بیش از ۶۰ ثانیه طول کشید؛ بعد از آن زیرِ یک ثانیه است.
SWAGGER_CONNECT_TIMEOUT_SECONDS: float = float(os.getenv("SWAGGER_CONNECT_TIMEOUT_SECONDS", "10"))
SWAGGER_READ_TIMEOUT_SECONDS: float = float(os.getenv("SWAGGER_READ_TIMEOUT_SECONDS", "120"))
SWAGGER_API_DOCS_BASE_URL: str = validate_base_url(
    _raw("SWAGGER_API_DOCS_BASE_URL") or DEFAULT_API_DOCS_BASE_URL
)


def max_shrink_ratio(key: str) -> float:
    """آستانه‌ی محافظِ کاهش برای یک منبع: SWAGGER_SYNC_MAX_SHRINK_RATIO_<KEY>، وگرنه
    SWAGGER_SYNC_MAX_SHRINK_RATIO، وگرنه 0.5. بازه‌ی 0..1؛ 0 یعنی غیرفعالِ صریح.
    هر بار از محیط خوانده می‌شود (دکمه‌ی Update و job هر دو)."""
    default = _float("SWAGGER_SYNC_MAX_SHRINK_RATIO", DEFAULT_SHRINK_RATIO, minimum=0, maximum=1)
    specific = f"SWAGGER_SYNC_MAX_SHRINK_RATIO_{re.sub(r'[^A-Za-z0-9]', '_', key).upper()}"
    return _float(specific, default, minimum=0, maximum=1)


@dataclass(frozen=True)
class SyncSettings:
    enabled: bool
    daily_time: str  # HH:MM — فقط install-task آن را به Task Scheduler می‌دهد
    sources: tuple[str, ...]  # خالی = همه‌ی منابعِ فعال
    max_attempts: int  # 0 = نامحدود (retry تا موفقیت)
    retry_interval_s: float
    backoff_multiplier: float
    max_retry_interval_s: float
    max_runtime_s: float  # 0 = نامحدود؛ همچنین سقفِ ExecutionTimeLimit ِ Task
    log_dir: Path
    log_backups: int
    connect_timeout_s: float
    read_timeout_s: float

    @property
    def unlimited_attempts(self) -> bool:
        return self.max_attempts == 0

    @property
    def unlimited_runtime(self) -> bool:
        return self.max_runtime_s == 0

    def retry_delay(self, attempt: int) -> float:
        """فاصله پس از تلاشِ شماره‌ی attempt (۱ = پس از تلاشِ اول)، با سقفِ max_retry_interval.

        برای retry ِ نامحدود attempt بی‌کران است؛ توان هرگز overflow نمی‌کند."""
        cap = self.max_retry_interval_s
        try:
            delay = self.retry_interval_s * (self.backoff_multiplier ** max(0, attempt - 1))
        except OverflowError:
            return cap
        return min(delay, cap)


def _daily_time(name: str, default: str) -> str:
    raw = _raw(name) or default
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise SwaggerSettingsError(f"{name} must be a local time in HH:MM (24-hour) format.")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _log_dir(name: str, default: str) -> Path:
    path = Path(_raw(name) or default)
    return path if path.is_absolute() else REPO_ROOT / path


def load_sync_settings() -> SyncSettings:
    """همه‌ی SWAGGER_SYNC_* را سخت‌گیرانه می‌خواند؛ هر مقدارِ نامعتبر SwaggerSettingsError."""
    sources = tuple(
        part.strip().lower()
        for part in (_raw("SWAGGER_SYNC_SOURCES") or "").split(",")
        if part.strip()
    )
    settings = SyncSettings(
        enabled=_bool("SWAGGER_SYNC_ENABLED", True),
        daily_time=_daily_time("SWAGGER_SYNC_DAILY_TIME", "03:00"),
        sources=sources,
        max_attempts=_int("SWAGGER_SYNC_MAX_ATTEMPTS", 0, minimum=0, maximum=1_000_000),
        retry_interval_s=_float(
            "SWAGGER_SYNC_RETRY_INTERVAL_SECONDS", 30, minimum=MIN_RETRY_INTERVAL_S, maximum=3600
        ),
        backoff_multiplier=_float("SWAGGER_SYNC_BACKOFF_MULTIPLIER", 2, minimum=1, maximum=10),
        max_retry_interval_s=_float(
            "SWAGGER_SYNC_MAX_RETRY_INTERVAL_SECONDS",
            300,
            minimum=MIN_RETRY_INTERVAL_S,
            maximum=3600,
        ),
        max_runtime_s=60
        * _float("SWAGGER_SYNC_MAX_RUNTIME_MINUTES", 0, minimum=0, maximum=7 * 24 * 60),
        log_dir=_log_dir("SWAGGER_SYNC_LOG_DIR", "logs/swagger_sync"),
        log_backups=_int("SWAGGER_SYNC_LOG_BACKUPS", 14, minimum=0, maximum=365),
        connect_timeout_s=_float(
            "SWAGGER_CONNECT_TIMEOUT_SECONDS", 10, minimum=0.001, maximum=3600
        ),
        read_timeout_s=_float("SWAGGER_READ_TIMEOUT_SECONDS", 120, minimum=0.001, maximum=3600),
    )
    for key in settings.sources:
        if not re.fullmatch(r"[a-z0-9_-]+", key):
            raise SwaggerSettingsError("SWAGGER_SYNC_SOURCES must be comma-separated source keys.")
    return settings


__all__ = [
    "DEFAULT_API_DOCS_BASE_URL",
    "DEFAULT_SHRINK_RATIO",
    "SWAGGER_API_DOCS_BASE_URL",
    "SWAGGER_CONNECT_TIMEOUT_SECONDS",
    "SWAGGER_READ_TIMEOUT_SECONDS",
    "SwaggerSettingsError",
    "SyncSettings",
    "load_sync_settings",
    "max_shrink_ratio",
    "validate_base_url",
]
