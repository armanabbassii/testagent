"""
src/task_sources/jira/client.py — کلاینتِ REST برای Jira Server (API v2)

یک نشست برای همه‌ی درخواست‌ها؛ ورود تنبل است (اولین درخواست) و با
/rest/api/2/myself تأیید می‌شود. نشستِ منقضی (401 یا redirect به صفحه‌ی ورود)
فقط یک بار دوباره ساخته می‌شود؛ اگر باز هم رد شود خطای صریح است — هیچ
حلقه‌ی ورودی وجود ندارد. هر کدِ خطا نوعِ خودش را دارد و هیچ شکستی به نتیجه‌ی
خالی تبدیل نمی‌شود.
"""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import urlsplit

import requests

from src.task_sources.attachments import AttachmentTooLarge
from src.task_sources.jira.auth import authenticator_for
from src.task_sources.jira.errors import (
    JiraAuthenticationError,
    JiraNotFoundError,
    JiraPermissionError,
    JiraRateLimitError,
    JiraResponseError,
    JiraServerError,
)
from src.task_sources.jira.settings import JiraSettings
from src.task_sources.jira.transport import send

API = "/rest/api/2"
ISSUE_FIELDS = "summary,description,attachment,issuetype,status"
COMMENT_PAGE_SIZE = 50
_DOWNLOAD_CHUNK = 64 * 1024
# مسیرهایی که redirect به آن‌ها یعنی «نشست معتبر نیست».
_LOGIN_PATHS = ("/login.jsp", "/plugins/servlet/samlsso")


def _jira_messages(response: requests.Response) -> str:
    """errorMessagesِ پاسخِ JSONِ Jira (کوتاه) — بدونِ متنِ HTML یا هدرها."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    messages = [str(item) for item in body.get("errorMessages") or [] if item]
    messages += [f"{key}: {value}" for key, value in (body.get("errors") or {}).items()]
    return " ".join(messages)[:300]


class JiraClient:
    def __init__(
        self,
        settings: JiraSettings,
        *,
        session: requests.Session | None = None,
        authenticator: Any = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.authenticator = authenticator or authenticator_for(settings)
        self.identity: dict[str, Any] | None = None
        self._lock = threading.RLock()

    @property
    def auth_method_name(self) -> str:
        return self.authenticator.name

    # ── ورود ────────────────────────────────────────────────────────────────
    def authenticate(self) -> dict[str, Any]:
        """ورودِ تازه و تأییدِ هویت؛ خروجی همان پاسخِ /myself است."""
        with self._lock:
            self.identity = None
            self.authenticator.authenticate(self.session)
            response = self._send("GET", f"{API}/myself")
            if response.status_code != 200 or self._login_redirect(response):
                raise JiraAuthenticationError(
                    f"Signing in with {self.auth_method_name} did not give an authenticated "
                    f"Jira session (GET {API}/myself returned HTTP {response.status_code})."
                )
            identity = self._decode(response)
            if not isinstance(identity, dict) or not identity.get("name"):
                raise JiraResponseError(f"GET {API}/myself did not return a Jira user.")
            self.identity = identity
            return identity

    def ensure_authenticated(self) -> dict[str, Any]:
        """هویتِ فعلی — با نشستِ موجود اگر هست، وگرنه یک ورودِ تازه."""
        with self._lock:
            return self.identity if self.identity is not None else self.authenticate()

    # ── درخواست ─────────────────────────────────────────────────────────────
    def _url(self, path_or_url: str) -> str:
        if "://" not in path_or_url:
            return f"{self.settings.base_url}{path_or_url}"
        if (
            urlsplit(path_or_url).scheme != "https"
            or urlsplit(path_or_url).netloc.lower() != urlsplit(self.settings.base_url).netloc.lower()
        ):
            # نشست و توکنِ Jira هرگز به میزبانِ دیگری فرستاده نمی‌شود.
            raise JiraResponseError(
                f"Refusing to send the Jira session to another host: {urlsplit(path_or_url).netloc}"
            )
        return path_or_url

    def _send(self, method: str, path_or_url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("allow_redirects", False)
        return send(self.session, method, self._url(path_or_url), self.settings, **kwargs)

    @staticmethod
    def _login_redirect(response: requests.Response) -> bool:
        if not response.is_redirect:
            return False
        location = urlsplit(response.headers.get("Location", "")).path
        return any(location.endswith(path) or path in location for path in _LOGIN_PATHS)

    def _request(self, method: str, path_or_url: str, **kwargs: Any) -> requests.Response:
        """درخواست با نشستِ فعلی؛ نشستِ منقضی فقط یک بار دوباره ساخته می‌شود."""
        with self._lock:
            self.ensure_authenticated()
            response = self._send(method, path_or_url, **kwargs)
            if response.status_code == 401 or self._login_redirect(response):
                response.close()
                if not self.authenticator.reauthenticates:
                    raise JiraAuthenticationError(
                        f"Jira rejected the {self.auth_method_name} (HTTP 401). Check that "
                        "JIRA_TOKEN is valid and not expired."
                    )
                self.authenticate()
                response = self._send(method, path_or_url, **kwargs)
                if response.status_code == 401 or self._login_redirect(response):
                    response.close()
                    raise JiraAuthenticationError(
                        "The Jira session expired and signing in again did not restore it."
                    )
            self._raise_for_status(response, path_or_url)
            return response

    def _raise_for_status(self, response: requests.Response, what: str) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        path = urlsplit(what).path or what
        detail = _jira_messages(response)
        suffix = f": {detail}" if detail else "."
        response.close()
        if status == 403:
            raise JiraPermissionError(
                f"Signed in, but this Jira account may not access {path} (HTTP 403){suffix}"
            )
        if status == 404:
            raise JiraNotFoundError(
                f"Jira could not find {path} (HTTP 404) — it does not exist or this "
                f"account cannot see it{suffix}"
            )
        if status == 429:
            retry = response.headers.get("Retry-After")
            raise JiraRateLimitError(
                "Jira is rate-limiting requests (HTTP 429)"
                + (f"; retry after {retry} seconds." if retry else ".")
            )
        if status >= 500:
            raise JiraServerError(f"Jira failed with HTTP {status} for {path}{suffix}")
        if 300 <= status < 400:
            raise JiraResponseError(f"Unexpected redirect (HTTP {status}) for {path}.")
        raise JiraResponseError(f"Jira returned HTTP {status} for {path}{suffix}")

    @staticmethod
    def _decode(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise JiraResponseError(
                f"Jira returned a non-JSON response for {urlsplit(response.url).path}."
            ) from exc

    def _json(self, path: str, **params: Any) -> Any:
        return self._decode(self._request("GET", path, params=params or None))

    # ── عملیات ──────────────────────────────────────────────────────────────
    def myself(self) -> dict[str, Any]:
        return self._json(f"{API}/myself")

    def issue(self, key: str, fields: str = ISSUE_FIELDS) -> dict[str, Any]:
        body = self._json(f"{API}/issue/{key}", fields=fields)
        if not isinstance(body, dict) or not isinstance(body.get("fields"), dict):
            raise JiraResponseError(f"Jira returned an issue without fields for {key}.")
        return body

    def comments(self, key: str, page_size: int = COMMENT_PAGE_SIZE) -> list[dict[str, Any]]:
        """همه‌ی کامنت‌ها، قدیمی‌ترین اول — صفحه به صفحه تا total."""
        collected: list[dict[str, Any]] = []
        while True:
            body = self._json(
                f"{API}/issue/{key}/comment",
                startAt=len(collected),
                maxResults=page_size,
                orderBy="created",
            )
            batch = body.get("comments") if isinstance(body, dict) else None
            total = body.get("total") if isinstance(body, dict) else None
            if not isinstance(batch, list) or not isinstance(total, int):
                raise JiraResponseError(f"Jira returned a malformed comment page for {key}.")
            collected.extend(batch)
            if len(collected) >= total:
                return collected
            if not batch:
                raise JiraResponseError(
                    f"Jira reported {total} comments on {key} but returned only "
                    f"{len(collected)}; the comment list is incomplete."
                )

    def attachment(self, attachment_id: str) -> dict[str, Any]:
        return self._json(f"{API}/attachment/{attachment_id}")

    def download(self, url: str, max_bytes: int) -> bytes:
        """محتوای پیوست؛ بیش از max_bytes → AttachmentTooLarge (دانلود همان‌جا قطع می‌شود)."""
        response = self._request("GET", url, stream=True)
        try:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise AttachmentTooLarge
            data = bytearray()
            for chunk in response.iter_content(_DOWNLOAD_CHUNK):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise AttachmentTooLarge
            return bytes(data)
        finally:
            response.close()


__all__ = ["API", "COMMENT_PAGE_SIZE", "ISSUE_FIELDS", "JiraClient"]
