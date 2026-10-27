"""
auth_intent.py — قصدِ احراز هویتِ هر تست‌کیس (مستقل از قراردادِ API)

این ماژول یک قراردادِ کوچک و مشترک است بینِ:

  * قدم اول، که قصدِ هر تست‌کیس را در فیلدِ ساختاریافته‌ی ``auth_intent``
    ثبت می‌کند؛
  * قدم چهارم، که همین قصد را به هدرِ واقعیِ درخواستِ Postman تبدیل می‌کند.

تفکیکِ مهم:

  * ``auth_intent`` رفتارِ *تست‌کیس* را می‌گوید: با اعتبارنامه‌ی معتبر، بدونِ
    اعتبارنامه، یا با اعتبارنامه‌ی نامعتبر/منقضی.
  * قراردادِ احراز هویتِ API (نامِ هدر، نامِ کوکی و نامِ متغیر) از سندِ
    Swagger در قدم دوم می‌آید و اینجا بازسازی نمی‌شود.

هیچ توکن یا اعتبارنامه‌ی واقعی در این ماژول ساخته نمی‌شود؛ فقط نامِ قصدها و
نرمال‌سازیِ مقدارِ ورودی وجود دارد.
"""

from __future__ import annotations

import re

# قصدها
AUTH_INTENT_INHERIT = "inherit"
AUTH_INTENT_AUTHENTICATED = "authenticated"
AUTH_INTENT_UNAUTHENTICATED = "unauthenticated"
AUTH_INTENT_INVALID = "invalid_credentials"

AUTH_INTENTS = (
    AUTH_INTENT_INHERIT,
    AUTH_INTENT_AUTHENTICATED,
    AUTH_INTENT_UNAUTHENTICATED,
    AUTH_INTENT_INVALID,
)

# وقتی فیلد غایب یا خالی باشد، قراردادِ API تصمیم می‌گیرد.
DEFAULT_AUTH_INTENT = AUTH_INTENT_INHERIT

# مترادف‌هایی که مدل ممکن است تولید کند. مقدارِ نهایی همیشه یکی از
# AUTH_INTENTS است؛ ورودیِ ناشناخته خطاست و بی‌صدا نادیده گرفته نمی‌شود.
_AUTH_INTENT_ALIASES = {
    "inherit": AUTH_INTENT_INHERIT,
    "default": AUTH_INTENT_INHERIT,
    "unspecified": AUTH_INTENT_INHERIT,
    "not_required": AUTH_INTENT_INHERIT,
    "public": AUTH_INTENT_INHERIT,
    "authenticated": AUTH_INTENT_AUTHENTICATED,
    "authorized": AUTH_INTENT_AUTHENTICATED,
    "valid": AUTH_INTENT_AUTHENTICATED,
    "valid_credentials": AUTH_INTENT_AUTHENTICATED,
    "valid_token": AUTH_INTENT_AUTHENTICATED,
    "with_credentials": AUTH_INTENT_AUTHENTICATED,
    "unauthenticated": AUTH_INTENT_UNAUTHENTICATED,
    "anonymous": AUTH_INTENT_UNAUTHENTICATED,
    "missing": AUTH_INTENT_UNAUTHENTICATED,
    "missing_authentication": AUTH_INTENT_UNAUTHENTICATED,
    "missing_credentials": AUTH_INTENT_UNAUTHENTICATED,
    "no_auth": AUTH_INTENT_UNAUTHENTICATED,
    "none": AUTH_INTENT_UNAUTHENTICATED,
    "without_credentials": AUTH_INTENT_UNAUTHENTICATED,
    "invalid": AUTH_INTENT_INVALID,
    "invalid_credentials": AUTH_INTENT_INVALID,
    "invalid_token": AUTH_INTENT_INVALID,
    "expired": AUTH_INTENT_INVALID,
    "expired_credentials": AUTH_INTENT_INVALID,
    "expired_token": AUTH_INTENT_INVALID,
}

_NORMALIZE_RE = re.compile(r"[\s\-]+")


class AuthIntentError(ValueError):
    """مقدارِ ``auth_intent`` با قرارداد نمی‌خواند."""


def normalize_auth_intent(value: object) -> str | None:
    """مقدارِ خام را به یکی از قصدهای مجاز تبدیل می‌کند.

    ``None`` یا رشته‌ی خالی یعنی «ثبت نشده» و ``None`` برمی‌گرداند. هر مقدارِ
    غیرِخالیِ ناشناخته خطای صریح می‌دهد تا یک قصدِ نادرست بی‌صدا به رفتارِ
    دیگری تبدیل نشود.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuthIntentError(
            f"auth_intent must be a string, got {type(value).__name__}."
        )

    text = value.strip().lower()
    if not text:
        return None

    normalized = _NORMALIZE_RE.sub("_", text)
    intent = _AUTH_INTENT_ALIASES.get(normalized)
    if intent is None:
        allowed = ", ".join(repr(item) for item in AUTH_INTENTS)
        raise AuthIntentError(
            f"auth_intent must be one of {allowed}, got {value!r}."
        )
    return intent


__all__ = [
    "AUTH_INTENT_AUTHENTICATED",
    "AUTH_INTENT_INHERIT",
    "AUTH_INTENT_INVALID",
    "AUTH_INTENT_UNAUTHENTICATED",
    "AUTH_INTENTS",
    "AuthIntentError",
    "DEFAULT_AUTH_INTENT",
    "normalize_auth_intent",
]
