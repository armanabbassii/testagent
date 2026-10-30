"""
src/task_sources/jira/auth.py — ورود به Jira

دو روش، هر دو روی یک requests.Session (کوکی‌ها و assertion فقط در حافظه):

AdfsSamlAuthenticator (پیش‌فرض، JIRA_AUTH_METHOD=adfs) — همان جریانی که روی
jira.dotin.ir واقعاً آزموده شد (Jira Server 9.12 + ADFS):

  1. GET {base}/login.jsp → افزونه‌ی SAMLِ Jira (/plugins/servlet/samlsso) یک
     فرمِ خودکار با SAMLRequest و RelayState برمی‌گرداند.
  2. همان فرم به ADFS (JIRA_SSO_URL) فرستاده می‌شود → صفحه‌ی Sign In با
     loginForm (UserName، Password، AuthMethod=FormsAuthentication).
  3. نام کاربری و رمز با همان فیلدهای فرم فرستاده می‌شوند (Kmsi نه — نشستِ
     ماندگارِ ADFS خواسته نمی‌شود) → فرمِ خودکار با SAMLResponse.
  4. SAMLResponse به ACSِ Jira فرستاده می‌شود → کوکی‌های نشستِ Jira.

هیچ نشانی یا فیلدی حدس زده نمی‌شود: action و فیلدهای پنهان از خودِ فرم‌ها
خوانده می‌شوند. رمز فقط به میزبانِ JIRA_SSO_URL و فقط روی https فرستاده
می‌شود و SAMLResponse فقط به میزبانِ JIRA_BASE_URL. هر پاسخِ دیگری — رد شدنِ
رمز، MFA یا صفحه‌ی ناشناخته — خطای صریح است و دور زده نمی‌شود.

PatAuthenticator (JIRA_AUTH_METHOD=pat) — Personal Access Token با هدرِ
«Authorization: Bearer». توکن ساخته نمی‌شود؛ فقط همانی که در JIRA_TOKEN است.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests

from src.task_sources.jira.errors import (
    JiraAuthenticationError,
    JiraMfaRequiredError,
)
from src.task_sources.jira.settings import AUTH_PAT, JiraSettings
from src.task_sources.jira.transport import send

_FORMS_AUTH = "FormsAuthentication"
# نشانه‌های گامِ تعاملیِ دیگر در ADFS — فقط برای تشخیص، نه برای ادامه.
_MFA_AUTH_METHODS = ("mfa", "azure", "otp", "certificate", "phone", "sms", "duo")
_MFA_TEXT = (
    "multi-factor",
    "multifactor",
    "verification code",
    "authenticator",
    "one-time",
    "additional authentication",
)
_MFA_FIELDS = ("code", "otp", "token", "pin")


@dataclass
class _Form:
    action: str
    id: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    has_password: bool = False


class _FormParser(HTMLParser):
    """فرم‌ها، فیلدهایشان و متنِ errorTextِ ADFS — بدونِ اجرای هیچ اسکریپتی."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_Form] = []
        self.error_text = ""
        self._in_error = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "form":
            self.forms.append(_Form(action=values.get("action", ""), id=values.get("id", "")))
        elif tag == "input" and self.forms:
            kind = values.get("type", "text").lower()
            name = values.get("name", "")
            if kind == "password":
                self.forms[-1].has_password = True
            if not name or kind in ("submit", "button", "image", "reset"):
                return
            if kind in ("checkbox", "radio") and "checked" not in values:
                return
            self.forms[-1].fields[name] = values.get("value", "")
        if values.get("id") == "errorText":
            self._in_error = 1
        elif self._in_error:
            self._in_error += 1

    def handle_endtag(self, tag: str) -> None:
        if self._in_error:
            self._in_error -= 1

    def handle_data(self, data: str) -> None:
        if self._in_error:
            self.error_text += data


def _parse(response: requests.Response) -> _FormParser:
    parser = _FormParser()
    parser.feed(response.text or "")
    return parser


def _host(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    return (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


def _same_https_host(url: str, expected: str) -> bool:
    return urlsplit(url).scheme == "https" and _host(url) == _host(expected)


class AdfsSamlAuthenticator:
    """ورود با نام کاربری/رمز از راهِ SAML 2.0 و فرمِ ADFS."""

    name = "ADFS (SAML 2.0, forms authentication)"
    # نشستِ منقضی را می‌شود یک بار دوباره ساخت.
    reauthenticates = True

    def __init__(self, settings: JiraSettings) -> None:
        self._settings = settings

    def authenticate(self, session: requests.Session) -> None:
        settings = self._settings
        session.cookies.clear()  # ورودِ تازه — هیچ کوکیِ قدیمی‌ای دوباره خرج نمی‌شود

        response = send(session, "GET", f"{settings.base_url}/login.jsp", settings)
        request_form = self._form_with(_parse(response), "SAMLRequest")
        if request_form is None:
            raise JiraAuthenticationError(
                "Jira did not start a SAML sign-in at /login.jsp. Check that "
                "JIRA_AUTH_METHOD=adfs is right for this Jira."
            )
        target = urljoin(response.url, request_form.action)
        if not _same_https_host(target, settings.sso_url):
            raise JiraAuthenticationError(
                f"Jira sends the SAML sign-in to {urlsplit(target).netloc}, not to "
                f"JIRA_SSO_URL ({urlsplit(settings.sso_url).netloc}); no credentials were sent."
            )
        response = send(session, "POST", target, settings, data=request_form.fields)
        page = _parse(response)

        if self._form_with(page, "SAMLResponse") is None:
            login = self._login_form(page)
            if login is None:
                raise self._unexpected(response, page, stage="the ADFS sign-in page")
            target = urljoin(response.url, login.action)
            if not _same_https_host(target, settings.sso_url):
                raise JiraAuthenticationError(
                    f"The ADFS sign-in form posts to {urlsplit(target).netloc}, not to "
                    "JIRA_SSO_URL; no credentials were sent."
                )
            data = dict(login.fields)
            data.pop("Kmsi", None)
            data.update({"UserName": settings.username, "Password": settings.password})
            response = send(session, "POST", target, settings, data=data)
            page = _parse(response)

        assertion = self._form_with(page, "SAMLResponse")
        if assertion is None:
            if self._login_form(page) is not None:
                reason = html.unescape(page.error_text).strip()[:200]
                raise JiraAuthenticationError(
                    "ADFS rejected the sign-in"
                    + (f": {reason}" if reason else " (username or password not accepted).")
                    + " Check JIRA_USERNAME and JIRA_PASSWORD."
                )
            raise self._unexpected(response, page, stage="the ADFS response to the credentials")
        target = urljoin(response.url, assertion.action)
        if not _same_https_host(target, settings.base_url):
            raise JiraAuthenticationError(
                f"ADFS returns the SAML response to {urlsplit(target).netloc}, not to "
                "JIRA_BASE_URL; it was not forwarded."
            )
        send(session, "POST", target, settings, data=assertion.fields)

    @staticmethod
    def _form_with(page: _FormParser, name: str) -> _Form | None:
        return next((form for form in page.forms if name in form.fields), None)

    @staticmethod
    def _login_form(page: _FormParser) -> _Form | None:
        """فرمِ رمزِ ADFS — فقط اگر خودش forms authentication را اعلام کند."""
        for form in page.forms:
            if form.has_password and "UserName" in form.fields:
                if form.fields.get("AuthMethod", "") != _FORMS_AUTH:
                    raise JiraAuthenticationError(
                        "ADFS does not offer forms authentication (username and "
                        "password) for this sign-in."
                    )
                return form
        return None

    @staticmethod
    def _unexpected(
        response: requests.Response, page: _FormParser, *, stage: str
    ) -> JiraAuthenticationError:
        """MFA یا پاسخِ ناشناخته — هر دو بدونِ ادامه دادن و بدونِ متنِ صفحه/کوکی."""
        methods = [form.fields.get("AuthMethod", "").lower() for form in page.forms]
        field_names = [name.lower() for form in page.forms for name in form.fields]
        text = (response.text or "").lower()
        if (
            any(marker in method for method in methods for marker in _MFA_AUTH_METHODS)
            or any(marker in name for name in field_names for marker in _MFA_FIELDS)
            or any(marker in text for marker in _MFA_TEXT)
        ):
            return JiraMfaRequiredError(
                "ADFS asked for an additional interactive verification step (MFA). "
                "It cannot be completed automatically; use JIRA_AUTH_METHOD=pat with a "
                "personal access token instead."
            )
        return JiraAuthenticationError(
            f"Unexpected response from {stage} (HTTP {response.status_code} at "
            f"{urlsplit(response.url).netloc}{urlsplit(response.url).path}). The sign-in "
            "flow may have changed; no further credentials were sent."
        )


class PatAuthenticator:
    """Personal Access Token — هدرِ Bearer روی همان نشست."""

    name = "personal access token"
    # توکنِ ردشده با تکرار درست نمی‌شود.
    reauthenticates = False

    def __init__(self, settings: JiraSettings) -> None:
        self._token = settings.token

    def authenticate(self, session: requests.Session) -> None:
        session.headers["Authorization"] = f"Bearer {self._token}"


def authenticator_for(settings: JiraSettings) -> AdfsSamlAuthenticator | PatAuthenticator:
    if settings.auth_method == AUTH_PAT:
        return PatAuthenticator(settings)
    return AdfsSamlAuthenticator(settings)


__all__ = ["AdfsSamlAuthenticator", "PatAuthenticator", "authenticator_for"]
