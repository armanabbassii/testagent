"""
src/auth/token_provider.py — گرفتنِ توکنِ دسترسیِ Podium از refresh token

    provider = TokenProvider()
    access = provider.fetch()          # AccessToken — فقط در حافظه
    access.value                       # توکنِ خام؛ «Bearer » فقط در مرزِ Newman اضافه می‌شود

یک درخواست: POST PODIUM_TOKEN_REFRESH_URL با بدنه‌ی JSONِ {"refreshToken": ...}.
بدونِ cookie (هر بار یک session تازه و خالی)، بدونِ دنبال‌کردنِ redirect، با
timeoutِ اتصال/خواندن و تأییدِ گواهیِ TLS (هرگز خاموش نمی‌شود). هیچ تلاشِ دوباره‌ای نیست.

انتقال: https:// همیشه. http:// فقط با PODIUM_TOKEN_ALLOW_INSECURE_HTTP=true — یک
استثنای صریح برای محیطِ Devِ ایزوله‌ی تأییدشده؛ HTTP هیچ رمزگذاری‌ای ندارد و refresh
token و توکنِ دسترسی آشکار جابه‌جا می‌شوند. فقط "true" فعالش می‌کند؛ نبود/خالی/false/0/no
یعنی خاموش و هر مقدارِ دیگر خطای پیکربندی است. این تنظیم فقط مالِ همین provider است.

پاسخ باید HTTP 200 با JSONِ {"result": {"token": "<رشته‌ی ناتهی>"}} باشد.
result.refreshToken خوانده نمی‌شود: refresh tokenِ پیکربندی‌شده ثابت است.

امنیت: پیامِ هر خطا ثابت است — هرگز توکن، بدنه‌ی درخواست/پاسخ یا متنِ استثنای
requests (که ممکن است درخواست را با خودش داشته باشد) در آن نیست؛ زنجیره‌ی استثنا
با «from None» بریده می‌شود. repr هیچ رازی را نشان نمی‌دهد.

انقضا: AccessToken زمانِ گرفتن را دارد (obtained_at) تا تازه‌سازیِ پیش از انقضا بعداً
همین‌جا اضافه شود؛ فعلاً هر نشستِ Batch یک توکن می‌گیرد و پس از 401 تازه نمی‌کند.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

# مقدارهای PODIUM_TOKEN_ALLOW_INSECURE_HTTP (بعد از strip و حروفِ کوچک)
_HTTP_OPT_IN = "true"
_HTTP_OPT_OUT = ("", "false", "0", "no")

# kind — نوعِ شکست؛ پیام برای کاربر است و kind برای تصمیم/تست.
KIND_CONFIGURATION = "configuration"
KIND_INSECURE_TRANSPORT = "insecure_transport"
KIND_TIMEOUT = "timeout"
KIND_CONNECTION = "connection"
KIND_TLS = "tls"
KIND_REJECTED = "rejected"  # 401 / 403
KIND_SERVER = "server"  # 5xx
KIND_HTTP_STATUS = "http_status"  # هر وضعیتِ دیگرِ غیرِ 200 (از جمله redirect)
KIND_MALFORMED = "malformed_response"


class AuthenticationError(Exception):
    """شکستِ گرفتنِ توکن — پیامِ ثابت و بی‌راز."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class TokenSettings:
    refresh_url: str
    refresh_token: str = field(default="", repr=False)
    ca_bundle: str = ""
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    # مقدارِ خامِ PODIUM_TOKEN_ALLOW_INSECURE_HTTP — در validate سخت‌گیرانه خوانده می‌شود
    allow_insecure_http: str = ""

    @property
    def verify(self) -> str | bool:
        """مقدارِ verify برای requests — فایلِ CA یا True؛ هرگز False."""
        return self.ca_bundle or True

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)

    def validate(self) -> TokenSettings:
        """همه‌ی کمبودها با هم؛ HTTPِ بدونِ opt-in جدا گزارش می‌شود (KIND_INSECURE_TRANSPORT)."""
        problems: list[str] = []
        opt_in = self.allow_insecure_http.strip().lower()
        if opt_in != _HTTP_OPT_IN and opt_in not in _HTTP_OPT_OUT:
            # مقدار در پیام نمی‌آید — شاید به‌اشتباه رازی آن‌جا نوشته شده باشد
            raise AuthenticationError(
                KIND_CONFIGURATION,
                "PODIUM_TOKEN_ALLOW_INSECURE_HTTP must be true or false. No request was sent.",
            )
        parts = urlsplit(self.refresh_url)
        if not self.refresh_url:
            problems.append("PODIUM_TOKEN_REFRESH_URL is not set")
        elif parts.scheme == "http" and opt_in != _HTTP_OPT_IN:
            raise AuthenticationError(
                KIND_INSECURE_TRANSPORT,
                "PODIUM_TOKEN_REFRESH_URL uses http://; the refresh token is sent only over "
                "https:// unless PODIUM_TOKEN_ALLOW_INSECURE_HTTP=true. No request was sent.",
            )
        elif parts.scheme not in ("https", "http") or not parts.hostname:
            problems.append("PODIUM_TOKEN_REFRESH_URL must be an https:// URL")
        elif parts.username or parts.password:
            problems.append("PODIUM_TOKEN_REFRESH_URL must not contain credentials")
        if not self.refresh_token:
            problems.append("PODIUM_REFRESH_TOKEN is not set")
        if self.ca_bundle and not Path(self.ca_bundle).is_file():
            problems.append("PODIUM_TOKEN_CA_BUNDLE points to a missing file")
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            problems.append("the PODIUM_TOKEN_*_TIMEOUT_SECONDS values must be > 0")
        if problems:
            raise AuthenticationError(
                KIND_CONFIGURATION,
                "The Podium access token is not configured: "
                + "; ".join(problems)
                + ". Set these in .env (see docs/architecture/batch-testing.md).",
            )
        return self

    @classmethod
    def from_config(cls) -> TokenSettings:
        """مقدارهای src/config.py (که .env را بدونِ بازنویسیِ محیطِ موجود بار می‌کند)."""
        from src import config

        return cls(
            refresh_url=config.PODIUM_TOKEN_REFRESH_URL.strip(),
            refresh_token=config.PODIUM_REFRESH_TOKEN.strip(),
            ca_bundle=config.PODIUM_TOKEN_CA_BUNDLE.strip(),
            connect_timeout=config.PODIUM_TOKEN_CONNECT_TIMEOUT_SECONDS,
            read_timeout=config.PODIUM_TOKEN_READ_TIMEOUT_SECONDS,
            allow_insecure_http=config.PODIUM_TOKEN_ALLOW_INSECURE_HTTP,
        )


@dataclass(frozen=True)
class AccessToken:
    """توکنِ خام (بدونِ «Bearer ») — فقط در حافظه."""

    value: str = field(repr=False)
    obtained_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class TokenProvider:
    """منبعِ مشترکِ توکن برای هر اجرای Newman. وابستگی‌ها برای تست قابلِ جایگزینی‌اند."""

    def __init__(
        self,
        settings_factory: Callable[[], TokenSettings] | None = None,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ) -> None:
        # None = TokenSettings.from_config در لحظه‌ی fetch (نه هنگامِ import) تا تنظیماتِ
        # .env هر بار تازه خوانده شود و در تست قابلِ patch باشد
        self._settings_factory = settings_factory
        self._session_factory = session_factory

    def fetch(self) -> AccessToken:
        """یک توکنِ تازه؛ هر شکست AuthenticationError با پیامِ ثابت است."""
        factory = self._settings_factory or TokenSettings.from_config
        settings = factory().validate()
        try:
            with self._session_factory() as session:
                session.cookies.clear()  # هرگز cookie فرستاده نمی‌شود
                response = session.post(
                    settings.refresh_url,
                    json={"refreshToken": settings.refresh_token},
                    headers={"Accept": "application/json"},
                    timeout=settings.timeout,
                    verify=settings.verify,
                    allow_redirects=False,
                )
                try:
                    return AccessToken(_token_of(response))
                finally:
                    response.close()
        except AuthenticationError:
            raise
        except requests.exceptions.Timeout:
            raise AuthenticationError(
                KIND_TIMEOUT, "The token refresh request timed out."
            ) from None
        except requests.exceptions.SSLError:
            raise AuthenticationError(
                KIND_TLS, "The TLS certificate of the token endpoint could not be verified."
            ) from None
        except requests.exceptions.ConnectionError:
            raise AuthenticationError(
                KIND_CONNECTION, "The token endpoint could not be reached."
            ) from None
        except Exception as exc:  # noqa: BLE001 — متنِ استثنا ممکن است راز داشته باشد
            raise AuthenticationError(
                KIND_CONNECTION, f"The token refresh request failed ({type(exc).__name__})."
            ) from None


def _token_of(response: Any) -> str:
    status = response.status_code
    if status in (401, 403):
        raise AuthenticationError(
            KIND_REJECTED, f"The token endpoint rejected the refresh token (HTTP {status})."
        )
    if 500 <= status <= 599:
        raise AuthenticationError(KIND_SERVER, f"The token endpoint failed (HTTP {status}).")
    if status != 200:
        raise AuthenticationError(
            KIND_HTTP_STATUS, f"The token endpoint returned an unexpected HTTP {status}."
        )
    try:
        body = response.json()
    except ValueError:
        raise AuthenticationError(
            KIND_MALFORMED, "The token endpoint did not return valid JSON."
        ) from None
    result = body.get("result") if isinstance(body, dict) else None
    token = result.get("token") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise AuthenticationError(
            KIND_MALFORMED, "The token endpoint response has no non-empty result.token string."
        )
    if token != token.strip() or any(ch.isspace() for ch in token):
        # «Bearer » فقط یک بار و فقط در مرزِ Newman اضافه می‌شود
        raise AuthenticationError(
            KIND_MALFORMED, "The token endpoint returned a token that contains whitespace."
        )
    return token


__all__ = [
    "KIND_CONFIGURATION",
    "KIND_CONNECTION",
    "KIND_HTTP_STATUS",
    "KIND_INSECURE_TRANSPORT",
    "KIND_MALFORMED",
    "KIND_REJECTED",
    "KIND_SERVER",
    "KIND_TIMEOUT",
    "KIND_TLS",
    "AccessToken",
    "AuthenticationError",
    "TokenProvider",
    "TokenSettings",
]
