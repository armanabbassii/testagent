"""
src/testcase_box/payload.py — بررسیِ ساختاریِ payloadِ یک نسخه (تابعِ خالص)

    report = inspect_payload(version.payload)

قراردادِ V1 (محافظه‌کارانه): payload دقیقاً یک itemِ درخواستِ Postman v2.1 است.
  * request شیء است با method، urlِ ساختاریافته (raw + host + path/query/variable)،
    headerِ فهرستی، bodyِ raw/urlencoded/formdata(متنی) و authِ خالی/noauth؛
  * میزبان دقیقاً {{base_url}} یا {{base_url_vdi}} است — VDI هرگز از نامِ میزبان حدس
    زده نمی‌شود؛ scheme، port و fragment گزارش می‌شوند، هرگز حذف یا بازنویسی نمی‌شوند؛
  * url.raw با بخش‌های ساختاریافته (میزبان، مسیر، query) مقایسه می‌شود؛
  * احراز هویت فقط «Authorization: {{token}}» — runtime خودش «Bearer » را اضافه می‌کند.

هیچ چیزی اجرا، خوانده یا نوشته نمی‌شود و payload تغییر نمی‌کند. اسکریپت‌ها در
scripts.py بررسی می‌شوند.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from src.testcase_box.contract import (
    HOST_VARIABLES,
    SUPPORTED_METHODS,
    TOKEN_REFERENCE,
    TOKEN_VARIABLE,
    VARIABLE_RE,
    Finding,
    finding,
    is_secret_key,
    is_sensitive_header,
    safe_name,
)

ITEM_KEYS = frozenset({"name", "id", "description", "request", "event", "response"})
REQUEST_KEYS = frozenset({"method", "url", "header", "body", "auth", "description"})
URL_KEYS = frozenset({"raw", "host", "path", "query", "variable"})
URL_REJECTED_KEYS = {
    "protocol": "URL_PROTOCOL_UNSUPPORTED",
    "port": "URL_PORT_UNSUPPORTED",
    "hash": "URL_HASH_UNSUPPORTED",
}
ENTRY_KEYS = frozenset({"key", "value", "disabled", "type", "description"})
BODY_KEYS = frozenset(
    {"mode", "raw", "urlencoded", "formdata", "file", "graphql", "options", "disabled"}
)
NO_AUTH_TYPES = frozenset({"noauth", "inherit"})

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_BEARER_TOKEN_RE = re.compile(r"bearer\s*" + re.escape(TOKEN_REFERENCE), re.IGNORECASE)
_HOST_REFERENCES = {"{{" + name + "}}": name for name in HOST_VARIABLES}


@dataclass(frozen=True)
class VariableReference:
    name: str
    location: str


@dataclass(frozen=True)
class PayloadReport:
    findings: tuple[Finding, ...]
    host_variable: str | None
    references: tuple[VariableReference, ...]


def _blank(value: Any) -> bool:
    return value is None or value in ("", {}, [])


def _literal_text(value: str) -> str:
    """متنی که پس از حذفِ ارجاع‌های {{...}} باقی می‌ماند."""
    return VARIABLE_RE.sub("", value).strip()


def inspect_payload(payload: Any) -> PayloadReport:
    inspector = _Inspector()
    inspector.run(payload)
    return PayloadReport(
        tuple(inspector.findings), inspector.host_variable, tuple(inspector.references)
    )


class _Inspector:
    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self.references: list[VariableReference] = []
        self.host_variable: str | None = None
        self.auth_value_locations: set[str] = set()

    def add(self, code: str, location: str, **params: object) -> None:
        self.findings.append(finding(code, location, **params))

    def scan(self, value: Any, location: str) -> None:
        """ارجاع‌های {{name}} در یک رشته، و «{{» ِ بسته‌نشده یا خالی. «}}» ِ تنها عمداً
        گزارش نمی‌شود: بدنه‌ی JSON با شیءِ تودرتو طبیعتاً به «}}» ختم می‌شود."""
        if not isinstance(value, str):
            return
        for name in VARIABLE_RE.findall(value):
            if name:
                self.references.append(VariableReference(name, location))
            else:
                self.add("VARIABLE_MALFORMED", location)
        if "{{" in VARIABLE_RE.sub("", value):
            self.add("VARIABLE_MALFORMED", location)

    # ── item و request ──────────────────────────────────────────────────────
    def run(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            self.add("PAYLOAD_NOT_OBJECT", "$")
            return
        if "info" in payload:
            self.add("PAYLOAD_IS_COLLECTION", "$")
            return
        if "item" in payload:
            self.add("PAYLOAD_IS_FOLDER", "$")
            return
        for key in sorted(payload):
            if key not in ITEM_KEYS:
                self.add("FIELD_UNSUPPORTED", f"$.{safe_name(key)}", field=safe_name(key))
        for key in ("name", "id"):
            if payload.get(key) is not None and not isinstance(payload[key], str):
                self.add("FIELD_MALFORMED", f"$.{key}", field=key)
        description = payload.get("description")
        if description is not None and not isinstance(description, str | dict):
            self.add("FIELD_MALFORMED", "$.description", field="description")
        if "request" not in payload:
            self.add("REQUEST_MISSING", "$")
            return
        request = payload["request"]
        if not isinstance(request, dict):
            self.add("REQUEST_NOT_OBJECT", "$.request")
            return
        for key in sorted(request):
            if key not in REQUEST_KEYS:
                location = f"$.request.{safe_name(key)}"
                self.add("FIELD_UNSUPPORTED", location, field=f"request.{safe_name(key)}")
        self._method(request)
        self._url(request)
        headers = self._headers(request.get("header"))
        self._body(request.get("body"))
        auth_type = self._auth_block(request)
        self._authorization(headers, auth_type)
        self._references()

    def _method(self, request: dict) -> None:
        method = request.get("method")
        if method is None or method == "":
            self.add("METHOD_MISSING", "$.request.method")
        elif not isinstance(method, str) or method.strip().upper() not in SUPPORTED_METHODS:
            self.add("METHOD_UNSUPPORTED", "$.request.method")

    # ── URL ─────────────────────────────────────────────────────────────────
    def _url(self, request: dict) -> None:
        location = "$.request.url"
        url = request.get("url")
        if _blank(url):
            self.add("URL_MISSING", location)
            return
        if isinstance(url, str):
            self.add("URL_NOT_STRUCTURED", location)
            return
        if not isinstance(url, dict):
            self.add("URL_MALFORMED", location, field="url")
            return
        for key in sorted(url):
            if key in URL_REJECTED_KEYS:
                if not _blank(url[key]):
                    self.add(URL_REJECTED_KEYS[key], f"{location}.{key}")
            elif key == "auth":
                if not _blank(url[key]):
                    self.add("LITERAL_CREDENTIAL", f"{location}.auth", where="URL credentials")
            elif key not in URL_KEYS:
                field = "url." + safe_name(key)
                self.add("FIELD_UNSUPPORTED", f"{location}.{safe_name(key)}", field=field)

        host = self._url_host(url.get("host"), location)
        segments = self._url_path(url.get("path"), location)
        query = self._url_query(url.get("query"), location)
        path_values = self._url_variables(url.get("variable"), location)

        if host is not None:
            self._classify_host(host, location)
        if segments is not None:
            for segment in segments:
                if segment.startswith(":") and len(segment) > 1:
                    name = segment[1:]
                    if not str(path_values.get(name) or "").strip():
                        self.add(
                            "PATH_VARIABLE_EMPTY", f"{location}.variable", name=safe_name(name)
                        )

        raw = url.get("raw")
        if not isinstance(raw, str) or not raw.strip():
            self.add("URL_RAW_MISSING", f"{location}.raw")
            return
        self.scan(raw, f"{location}.raw")
        raw_host, raw_segments, raw_query = self._parse_raw(raw, f"{location}.raw")
        if host is not None and host != raw_host and (host or raw_host):
            self.add("HOST_CONFLICT", f"{location}.raw")
        if segments is not None and segments != raw_segments:
            self.add("URL_COMPONENTS_CONFLICT", f"{location}.raw", component="path")
        if query is not None and query != raw_query:
            self.add("URL_COMPONENTS_CONFLICT", f"{location}.raw", component="query")

    def _url_host(self, host: Any, location: str) -> str | None:
        if host is None:
            return ""
        if isinstance(host, str):
            parts = [host]
        elif isinstance(host, list) and all(isinstance(part, str) for part in host):
            parts = host
        else:
            self.add("URL_MALFORMED", f"{location}.host", field="host")
            return None
        for index, part in enumerate(parts):
            self.scan(part, f"{location}.host[{index}]")
        return ".".join(parts)

    def _url_path(self, path: Any, location: str) -> list[str] | None:
        if path is None:
            return []
        if isinstance(path, str):
            items = path.split("/")
        elif isinstance(path, list) and all(isinstance(segment, str) for segment in path):
            items = path
        else:
            self.add("URL_MALFORMED", f"{location}.path", field="path")
            return None
        for index, segment in enumerate(items):
            self.scan(segment, f"{location}.path[{index}]")
        return [segment for segment in items if segment != ""]

    def _entries(self, entries: Any, location: str, code: str, field: str):
        """فهرستِ {key, value, disabled?}؛ ورودیِ نامعتبر گزارش و کنار گذاشته می‌شود."""
        if entries is None:
            return [], True
        if not isinstance(entries, list):
            self.add(code, location, field=field)
            return [], False
        valid, complete = [], True
        for index, entry in enumerate(entries):
            path = f"{location}[{index}]"
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("key"), str)
                or not (entry.get("value") is None or isinstance(entry.get("value"), str))
                or not (entry.get("disabled") is None or isinstance(entry.get("disabled"), bool))
                or set(entry) - ENTRY_KEYS
            ):
                self.add(code, path, field=f"{field}[{index}]")
                complete = False
                continue
            valid.append(
                (path, entry["key"], entry.get("value") or "", bool(entry.get("disabled")))
            )
        return valid, complete

    def _url_query(self, query: Any, location: str) -> list[tuple[str, str]] | None:
        entries, complete = self._entries(query, f"{location}.query", "QUERY_MALFORMED", "query")
        for path, key, value, _ in entries:
            self.scan(key, f"{path}.key")
            self.scan(value, f"{path}.value")
            self._credential_like(key, value, f"{path}.value")
        if not complete:
            return None
        return [(key, value) for _, key, value, disabled in entries if not disabled]

    def _url_variables(self, variables: Any, location: str) -> dict[str, str]:
        entries, _ = self._entries(variables, f"{location}.variable", "URL_MALFORMED", "variable")
        values = {}
        for path, key, value, _ in entries:
            self.scan(value, f"{path}.value")
            values[key] = value
        return values

    def _classify_host(self, host: str, location: str) -> None:
        if host == "":
            self.add("HOST_MISSING", f"{location}.host")
        elif host in _HOST_REFERENCES:
            self.host_variable = _HOST_REFERENCES[host]
        elif VARIABLE_RE.fullmatch(host):
            name = VARIABLE_RE.fullmatch(host).group(1)
            self.add("HOST_VARIABLE_UNSUPPORTED", f"{location}.host", name=safe_name(name))
        else:
            self.add("HOST_LITERAL", f"{location}.host")

    def _parse_raw(self, raw: str, location: str):
        """فقط برای مقایسه: (میزبان، بخش‌های مسیر، queryِ فعال). raw تغییر نمی‌کند."""
        text = raw.strip()
        scheme = _SCHEME_RE.match(text)
        if scheme:
            self.add("URL_PROTOCOL_UNSUPPORTED", location)
            text = text[scheme.end() :]
        if "#" in text:
            self.add("URL_HASH_UNSUPPORTED", location)
            text = text.split("#", 1)[0]
        cut = min(
            [index for index in (text.find("/"), text.find("?")) if index >= 0] or [len(text)]
        )
        authority, remainder = text[:cut], text[cut:]
        if "@" in authority:
            self.add("LITERAL_CREDENTIAL", location, where="URL credentials")
            authority = authority.rsplit("@", 1)[1]
        if ":" in VARIABLE_RE.sub("", authority):
            self.add("URL_PORT_UNSUPPORTED", location)
            authority = authority[: authority.rfind(":")]
        path_part, _, query_part = remainder.partition("?")
        segments = [segment for segment in path_part.split("/") if segment != ""]
        query = [
            (key, value)
            for key, _, value in (pair.partition("=") for pair in query_part.split("&") if pair)
        ]
        return authority, segments, query

    # ── هدرها، بدنه و احراز هویت ────────────────────────────────────────────
    def _headers(self, headers: Any) -> list[tuple[str, str, str, bool]]:
        location = "$.request.header"
        entries, _ = self._entries(headers, location, "HEADER_MALFORMED", "header")
        for path, key, value, _ in entries:
            self.scan(key, f"{path}.key")
            self.scan(value, f"{path}.value")
            if key.strip().lower() == "authorization":
                self.auth_value_locations.add(f"{path}.value")
            elif is_sensitive_header(key) and _literal_text(value):
                self.add("LITERAL_CREDENTIAL", f"{path}.value", where=f"header '{safe_name(key)}'")
        counts = Counter(
            key.strip().lower()
            for _, key, _, disabled in entries
            if not disabled and key.strip().lower() != "authorization"
        )
        for name, count in sorted(counts.items()):
            if count > 1:
                self.add("HEADER_DUPLICATE", location, name=safe_name(name), count=count)
        return entries

    def _body(self, body: Any) -> None:
        location = "$.request.body"
        if body is None or body == {}:
            return
        if not isinstance(body, dict):
            self.add("BODY_MALFORMED", location, field="body")
            return
        for key in sorted(body):
            if key not in BODY_KEYS:
                field = "body." + safe_name(key)
                self.add("FIELD_UNSUPPORTED", f"{location}.{safe_name(key)}", field=field)
        mode = body.get("mode")
        if mode == "raw":
            raw = body.get("raw", "")
            if not isinstance(raw, str):
                self.add("BODY_MALFORMED", f"{location}.raw", field="body.raw")
                return
            self.scan(raw, f"{location}.raw")
            self._credential_like_json(raw, f"{location}.raw")
        elif mode in ("urlencoded", "formdata"):
            self._form(body.get(mode), mode, f"{location}.{mode}")
        elif mode is None:
            self.add("BODY_MALFORMED", f"{location}.mode", field="body.mode")
        else:
            self.add("BODY_MODE_UNSUPPORTED", f"{location}.mode", mode=safe_name(mode))

    def _form(self, entries: Any, mode: str, location: str) -> None:
        if entries is None:
            return
        if not isinstance(entries, list):
            self.add("BODY_MALFORMED", location, field=f"body.{mode}")
            return
        for index, entry in enumerate(entries):
            path = f"{location}[{index}]"
            if (
                mode == "formdata"
                and isinstance(entry, dict)
                and (entry.get("type") == "file" or "src" in entry)
            ):
                self.add("BODY_FILE_UPLOAD", path)
                continue
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("key"), str)
                or not (entry.get("value") is None or isinstance(entry.get("value"), str))
                or entry.get("type") not in (None, "text")
                or set(entry) - ENTRY_KEYS
            ):
                self.add("BODY_MALFORMED", path, field=f"body.{mode}[{index}]")
                continue
            value = entry.get("value") or ""
            self.scan(entry["key"], f"{path}.key")
            self.scan(value, f"{path}.value")
            self._credential_like(entry["key"], value, f"{path}.value")

    def _credential_like(self, key: str, value: str, location: str) -> None:
        """نامِ شبیهِ اعتبارنامه با مقدارِ literal در query/بدنه — فقط هشدار (تستِ ورودِ
        منفی و اعتبارسنجی باید ممکن بماند). تشخیص اکتشافی است."""
        if is_secret_key(key) and _literal_text(value):
            self.add("CREDENTIAL_LIKE_VALUE", location, name=safe_name(key))

    def _credential_like_json(self, raw: str, location: str) -> None:
        try:
            document = json.loads(raw)
        except ValueError:
            return  # مثلاً «"id": {{id}}» — JSONِ معتبر نیست ولی مجاز است
        pending = [document]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, str):
                        self._credential_like(key, value, location)
                    else:
                        pending.append(value)
            elif isinstance(node, list):
                pending.extend(node)

    def _auth_block(self, request: dict) -> str | None:
        location = "$.request.auth"
        auth = request.get("auth")
        if auth is None:
            return None
        if not isinstance(auth, dict) or not isinstance(auth.get("type"), str):
            # خودش مسدودکننده است؛ «ابهام» را دوباره گزارش نمی‌کند
            self.add("FIELD_MALFORMED", location, field="request.auth")
            return None
        auth_type = auth["type"]
        if auth_type == "noauth":
            return auth_type
        if auth_type == "inherit":
            self.add("AUTH_INHERIT", location)
            return auth_type
        self.add("AUTH_BLOCK_UNSUPPORTED", location, type=safe_name(auth_type))
        params = auth.get(auth_type)
        if isinstance(params, dict):
            values = [(f"{location}.{auth_type}.{safe_name(k)}", v) for k, v in params.items()]
        elif isinstance(params, list):
            values = [
                (f"{location}.{auth_type}[{index}].value", entry.get("value"))
                for index, entry in enumerate(params)
                if isinstance(entry, dict)
            ]
        else:
            values = []
        for path, value in values:
            if isinstance(value, str):
                self.scan(value, path)
                if _literal_text(value):
                    self.add("LITERAL_CREDENTIAL", path, where=f"auth '{safe_name(auth_type)}'")
        return auth_type

    def _authorization(self, headers, auth_type: str | None) -> None:
        location = "$.request.header"
        authorization = [
            (path, value, disabled)
            for path, key, value, disabled in headers
            if key.strip().lower() == "authorization"
        ]
        enabled = [(path, value) for path, value, disabled in authorization if not disabled]
        for path, value, disabled in authorization:
            if disabled and _literal_text(value) and not _BEARER_TOKEN_RE.fullmatch(value.strip()):
                self.add("LITERAL_CREDENTIAL", f"{path}.value", where="header 'Authorization'")
        real_block = auth_type is not None and auth_type not in NO_AUTH_TYPES
        if len(enabled) > 1 or (enabled and real_block):
            self.add("AUTH_AMBIGUOUS", "$.request")
        if not enabled:
            if not real_block:
                self.add("AUTH_HEADER_MISSING", location)
            return
        for path, value in enabled:
            text = value.strip()
            if text == TOKEN_REFERENCE:
                continue
            if _BEARER_TOKEN_RE.fullmatch(text):
                self.add("AUTH_DUPLICATE_BEARER", f"{path}.value")
            elif not text:
                self.add("AUTH_HEADER_MISSING", f"{path}.value")
            elif VARIABLE_RE.search(text):
                self.add("AUTH_VARIABLE_UNSUPPORTED", f"{path}.value")
            else:
                self.add("LITERAL_CREDENTIAL", f"{path}.value", where="header 'Authorization'")

    # ── ارجاع‌ها ────────────────────────────────────────────────────────────
    def _references(self) -> None:
        for reference in self.references:
            name, location = reference.name, reference.location
            if name.startswith("$"):
                self.add("VARIABLE_DYNAMIC", location, name=safe_name(name))
            elif name == TOKEN_VARIABLE and location not in self.auth_value_locations:
                self.add("TOKEN_OUTSIDE_AUTH", location)
            elif name in HOST_VARIABLES and self.host_variable and name != self.host_variable:
                self.add("HOST_MIXED_REFERENCES", location, name=name)
