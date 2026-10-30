"""
tests/unit/task_sources/fake_jira.py — Jira Server + ADFSِ ساختگی برای تست‌ها

همان جریانی را بازسازی می‌کند که روی Jira واقعی آزموده شد (login.jsp → فرمِ
SAMLRequest → صفحه‌ی Sign Inِ ADFS → فرمِ SAMLResponse → ACSِ Jira → کوکیِ
نشست)، ولی با میزبان‌ها، کاربر و داده‌های کاملاً ساختگی. هیچ شبکه‌ای لازم نیست:
FakeSession همان requests.Session است که فقط request را به این سرور می‌فرستد.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import requests

from src.task_sources.jira.settings import JiraSettings

JIRA = "https://jira.test"
SSO = "https://sso.test/adfs/ls/"
USER = "test.user"
PASSWORD = "not-a-real-password"
TOKEN = "not-a-real-token"
ASSERTION = "synthetic-saml-response"


def adfs_settings(**overrides: Any) -> JiraSettings:
    values = {
        "base_url": JIRA,
        "auth_method": "adfs",
        "sso_url": SSO,
        "username": USER,
        "password": PASSWORD,
    }
    values.update(overrides)
    return JiraSettings(**values)


def pat_settings(**overrides: Any) -> JiraSettings:
    values = {"base_url": JIRA, "auth_method": "pat", "token": TOKEN}
    values.update(overrides)
    return JiraSettings(**values)


def make_response(
    url: str,
    status: int = 200,
    *,
    text: str = "",
    body: Any = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    response = requests.Response()
    response.url = url
    response.status_code = status
    if body is not None:
        content = json.dumps(body).encode("utf-8")
        response.headers["Content-Type"] = "application/json;charset=UTF-8"
    elif content is None:
        content = text.encode("utf-8")
        response.headers["Content-Type"] = "text/html; charset=utf-8"
    response._content = content
    response._content_consumed = True
    response.encoding = "utf-8"
    response.headers.update(headers or {})
    return response


def _form(action: str, fields: dict[str, str], *, form_id: str = "") -> str:
    inputs = "".join(
        f'<input type="hidden" name="{name}" value="{value}" />' for name, value in fields.items()
    )
    return (
        f'<form method="post" id="{form_id}" action="{action}">{inputs}'
        '<input type="submit" value="Continue" /></form>'
    )


def login_page(error: str = "", auth_method: str = "FormsAuthentication", action: str = "") -> str:
    error_html = f'<span id="errorText" class="fieldMargin error">{error}</span>' if error else ""
    return (
        "<html><head><title>Sign In</title></head><body>"
        f'<form method="post" id="loginForm" action="{action or "/adfs/ls/?client-request-id=1"}">'
        '<input id="userNameInput" name="UserName" type="email" value="" />'
        '<input id="passwordInput" name="Password" type="password" />'
        '<input id="kmsiInput" type="checkbox" name="Kmsi" value="true" />'
        f'<input id="optionForms" type="hidden" name="AuthMethod" value="{auth_method}" />'
        f"</form>{error_html}"
        '<form method="post" id="options" action="https://sso.test:443/adfs/ls/?x=1">'
        '<input type="hidden" name="AuthMethod" id="optionSelection" />'
        "</form></body></html>"
    )


@dataclass
class Call:
    method: str
    url: str
    data: dict[str, str]
    headers: dict[str, str]
    verify: Any
    timeout: Any


@dataclass
class FakeJira:
    """وضعیتِ سرور و همه‌ی درخواست‌های دریافت‌شده."""

    # رفتارِ ADFS بعد از ارسالِ رمز: ok | wrong | mfa | unknown
    adfs_mode: str = "ok"
    # مقصدِ فرم‌ها — برای تستِ «رمز به میزبانِ دیگری نرود».
    saml_request_action: str = SSO
    login_action: str = ""
    acs_action: str = f"{JIRA}/plugins/servlet/samlsso"
    login_auth_method: str = "FormsAuthentication"
    login_starts_saml: bool = True
    # بعد از ACS نشست ساخته شود یا نه (Jira assertion را نپذیرد).
    acs_accepts: bool = True
    # 401 برای هر درخواستِ REST، حتی بعد از ورود.
    reject_rest: bool = False
    # REST به‌جای 401 به صفحه‌ی ورود redirect کند.
    redirect_when_anonymous: bool = False
    valid_session: str = "session-1"
    issues: dict[str, Any] = field(default_factory=dict)
    issue_status: dict[str, int] = field(default_factory=dict)
    comments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # تعدادی که سرور در total اعلام می‌کند ولی برنمی‌گرداند (صفحه‌بندیِ ناقص).
    missing_comments: int = 0
    files: dict[str, bytes] = field(default_factory=dict)
    file_status: dict[str, int] = field(default_factory=dict)
    file_headers: dict[str, dict[str, str]] = field(default_factory=dict)
    errors: dict[str, BaseException] = field(default_factory=dict)
    retry_after: str = ""
    calls: list[Call] = field(default_factory=list)
    sign_ins: int = 0

    def session(self) -> FakeSession:
        return FakeSession(self)

    def expire_sessions(self) -> None:
        """همه‌ی نشست‌های فعلی منقضی می‌شوند (ورودِ بعدی نشستِ تازه می‌گیرد)."""
        number = int(self.valid_session.split("-")[1]) + 1
        self.valid_session = f"session-{number}"

    def calls_to(self, host: str) -> list[Call]:
        return [call for call in self.calls if urlsplit(call.url).hostname == host]

    # ── مسیریابی ────────────────────────────────────────────────────────────
    def handle(self, session: FakeSession, method: str, url: str, **kwargs: Any) -> requests.Response:
        data = dict(kwargs.get("data") or {})
        headers = {**session.headers, **(kwargs.get("headers") or {})}
        self.calls.append(Call(method, url, data, headers, kwargs.get("verify"), kwargs.get("timeout")))
        parts = urlsplit(url)
        path = parts.path
        for marker, error in self.errors.items():
            if marker in url:
                raise error
        if parts.hostname == "sso.test":
            return self._adfs(url, data)
        if path == "/login.jsp":
            if not self.login_starts_saml:
                return make_response(url, text="<html><body>Jira login</body></html>")
            return make_response(
                f"{JIRA}/plugins/servlet/samlsso",
                text=_form(self.saml_request_action, {"SAMLRequest": "req", "RelayState": "rs"}),
            )
        if path == "/plugins/servlet/samlsso" and method == "POST":
            if data.get("SAMLResponse") == ASSERTION and self.acs_accepts:
                session.cookies.set("JSESSIONID", self.valid_session, domain="jira.test")
                self.sign_ins += 1
            return make_response(f"{JIRA}/secure/Dashboard.jspa", text="<html>dashboard</html>")
        if not self._authorized(session, headers):
            if self.redirect_when_anonymous:
                return make_response(
                    url, 302, headers={"Location": f"{JIRA}/login.jsp?os_destination=x"}
                )
            return make_response(url, 401, body={"errorMessages": ["Login Required"]})
        return self._rest(url, path, dict(parse_qsl(parts.query)) | (kwargs.get("params") or {}))

    def _authorized(self, session: FakeSession, headers: dict[str, str]) -> bool:
        if self.reject_rest:
            return False
        if headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        return session.cookies.get("JSESSIONID", domain="jira.test") == self.valid_session

    def _adfs(self, url: str, data: dict[str, str]) -> requests.Response:
        if "SAMLRequest" in data:
            return make_response(
                url, text=login_page(auth_method=self.login_auth_method, action=self.login_action)
            )
        if "Password" not in data:
            return make_response(url, text="<html><title>Error</title></html>")
        if self.adfs_mode == "mfa":
            return make_response(
                url,
                text="<html>Enter the verification code from your authenticator app."
                + _form("/adfs/ls/", {"AuthMethod": "AzureMfaAuthentication", "Context": "c"})
                + "</html>",
            )
        if self.adfs_mode == "unknown":
            return make_response(url, text="<html><body>Maintenance</body></html>")
        if self.adfs_mode == "wrong" or data != {
            "UserName": USER,
            "Password": PASSWORD,
            "AuthMethod": "FormsAuthentication",
        }:
            return make_response(url, text=login_page(error="Incorrect user ID or password."))
        return make_response(
            url, text=_form(self.acs_action, {"SAMLResponse": ASSERTION, "RelayState": "rs"})
        )

    def _rest(self, url: str, path: str, params: dict[str, Any]) -> requests.Response:
        if path == "/rest/api/2/myself":
            return make_response(url, body={"name": USER, "displayName": "Test User"})
        if path.startswith("/rest/api/2/attachment/"):
            return make_response(url, body={"id": path.rsplit("/", 1)[1], "filename": "x.txt"})
        if path.startswith("/rest/api/2/issue/") and path.endswith("/comment"):
            key = path.split("/")[5]
            items = self.comments.get(key, [])
            start, size = int(params.get("startAt", 0)), int(params.get("maxResults", 50))
            total = len(items) + self.missing_comments
            return make_response(
                url,
                body={
                    "startAt": start,
                    "maxResults": size,
                    "total": total,
                    "comments": items[start : start + size],
                },
            )
        if path.startswith("/rest/api/2/issue/"):
            key = path.rsplit("/", 1)[1]
            status = self.issue_status.get(key, 200 if key in self.issues else 404)
            if status != 200:
                messages = {403: "You do not have permission", 404: "Issue Does Not Exist"}
                headers = {"Retry-After": self.retry_after} if self.retry_after else {}
                return make_response(
                    url,
                    status,
                    body={"errorMessages": [messages.get(status, "failure")]},
                    headers=headers,
                )
            return make_response(url, body=self.issues[key])
        if path.startswith("/secure/attachment/"):
            name = path.rsplit("/", 1)[1]
            status = self.file_status.get(name, 200 if name in self.files else 404)
            if status != 200:
                return make_response(url, status, body={"errorMessages": ["no"]})
            return make_response(
                url,
                content=self.files[name],
                headers={"Content-Type": "application/octet-stream", **self.file_headers.get(name, {})},
            )
        if path == "/rest/api/2/text":
            return make_response(url, text="<html>not json</html>")
        return make_response(url, 404, body={"errorMessages": ["unknown path"]})


class FakeSession(requests.Session):
    def __init__(self, server: FakeJira) -> None:
        super().__init__()
        self.server = server

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        return self.server.handle(self, method, url, **kwargs)


def issue_body(
    key: str = "DEMO-7",
    *,
    summary: str = "Add an endpoint for voucher types",
    description: str = "Line one\r\nLine two",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "issuetype": {"name": "Story"},
            "status": {"name": "In Progress"},
            "attachment": attachments or [],
        },
    }


def attachment_meta(name: str, size: int, mime: str = "text/plain", number: int = 1) -> dict[str, Any]:
    return {
        "id": str(number),
        "filename": name,
        "mimeType": mime,
        "size": size,
        "content": f"{JIRA}/secure/attachment/{number}/{name}",
    }


def comment(number: int, body: str = "", author: str = "Reviewer", updated: str = "") -> dict[str, Any]:
    created = f"2026-01-{number:02d}T10:00:00.000+0330"
    return {
        "id": str(number),
        "author": {"name": f"user{number}", "displayName": author},
        "body": body or f"comment {number}",
        "created": created,
        "updated": updated or created,
    }
