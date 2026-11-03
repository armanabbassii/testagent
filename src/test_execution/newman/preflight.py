"""
preflight.py — بررسیِ فقط‌خواندنیِ ورودی‌ها پیش از اجرای Newman

  * کالکشن، environment و globals باید وجود داشته باشند، JSONِ معتبر باشند و
    شکلِ Postmanِ درست را داشته باشند؛
  * میزبانِ هر درخواست فقط {{base_url}} یا {{base_url_vdi}} است (قراردادِ
    host_selection)؛ کالکشن هیچ‌کدام را تعریف نمی‌کند؛
  * مقدارِ مؤثرِ هر متغیرِ میزبانِ استفاده‌شده — با ترتیبِ Postman (globals <
    collection < environment)، فقط ورودی‌های enabled، و مقدارِ خالیِ سطحِ بالاتر
    هم برنده — نباید خالی باشد.

فایل‌ها فقط خوانده می‌شوند؛ هیچ مقدارِ متغیری در نتیجه نمی‌آید.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.agents.test_case_generator.host_selection import HOST_VARIABLES, host_reference
from src.test_execution.newman.models import (
    ERROR_HOST_VARIABLE_EMPTY,
    ERROR_HOST_VARIABLE_IN_COLLECTION,
    ERROR_INVALID_COLLECTION,
    ERROR_INVALID_ENVIRONMENT,
    ERROR_INVALID_GLOBALS,
    HostVariableCheck,
)


@dataclass
class Preflight:
    collection: dict | None = None
    environment: dict | None = None
    globals: dict | None = None
    hashes: dict[str, str] = field(default_factory=dict)
    host_variables: list[HostVariableCheck] = field(default_factory=list)
    request_count: int = 0
    error_kind: str = ""
    error_message: str = ""

    @property
    def ok(self) -> bool:
        return not self.error_kind


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load(path: Path, kind: str, error_kind: str, result: Preflight) -> dict | None:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        result.error_kind, result.error_message = (
            error_kind,
            f"The {kind} file cannot be read: {type(exc).__name__}.",
        )
        return None
    result.hashes[kind] = hashlib.sha256(raw).hexdigest()
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        result.error_kind, result.error_message = error_kind, f"The {kind} file is not valid JSON."
        return None
    if not isinstance(document, dict):
        result.error_kind, result.error_message = (
            error_kind,
            f"The {kind} file is not a JSON object.",
        )
        return None
    return document


def _requests(items: Any) -> list[dict]:
    found: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("request"), dict):
            found.append(item)
        if isinstance(item.get("item"), list):
            found.extend(_requests(item["item"]))
    return found


def _host_of(request: dict) -> str:
    url = request.get("url")
    if isinstance(url, str):
        return url.split("/", 1)[0]
    if isinstance(url, dict):
        host = url.get("host")
        if isinstance(host, list) and host:
            return ".".join(str(part) for part in host)
        if isinstance(host, str):
            return host
        return str(url.get("raw") or "").split("/", 1)[0]
    return ""


def _variables(document: dict | None, key: str) -> dict[str, dict]:
    """فقط ورودی‌های enabled — آخرین تعریف برنده است (رفتارِ Postman)."""
    entries: dict[str, dict] = {}
    for entry in (document or {}).get(key) or []:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("key"), str)
            and entry.get("enabled", True) is not False
        ):
            entries[entry["key"]] = entry
    return entries


def run_preflight(collection_path: Path, environment_path: Path, globals_path: Path) -> Preflight:
    result = Preflight()

    result.collection = _load(collection_path, "collection", ERROR_INVALID_COLLECTION, result)
    if not result.ok:
        return result
    info = result.collection.get("info")
    schema = info.get("schema") if isinstance(info, dict) else ""
    if (
        not isinstance(schema, str)
        or "collection" not in schema
        or not isinstance(result.collection.get("item"), list)
    ):
        result.error_kind = ERROR_INVALID_COLLECTION
        result.error_message = "The collection has no Postman 'info.schema' or no 'item' array."
        return result
    requests = _requests(result.collection["item"])
    result.request_count = len(requests)
    if not requests:
        result.error_kind, result.error_message = (
            ERROR_INVALID_COLLECTION,
            "The collection contains no request.",
        )
        return result

    result.environment = _load(environment_path, "environment", ERROR_INVALID_ENVIRONMENT, result)
    if not result.ok:
        return result
    if result.environment.get(
        "_postman_variable_scope", "environment"
    ) != "environment" or not isinstance(result.environment.get("values"), list):
        result.error_kind = ERROR_INVALID_ENVIRONMENT
        result.error_message = (
            "The environment file is not a Postman environment with a 'values' array."
        )
        return result

    result.globals = _load(globals_path, "globals", ERROR_INVALID_GLOBALS, result)
    if not result.ok:
        return result
    if result.globals.get("_postman_variable_scope", "globals") != "globals" or not isinstance(
        result.globals.get("values"), list
    ):
        result.error_kind = ERROR_INVALID_GLOBALS
        result.error_message = (
            "The globals file is not a Postman globals file with a 'values' array."
        )
        return result

    allowed = {host_reference(variable): variable for variable in HOST_VARIABLES}
    used: list[str] = []
    for request_item in requests:
        host = _host_of(request_item["request"])
        if host not in allowed:
            result.error_kind = ERROR_INVALID_COLLECTION
            result.error_message = (
                f"Request {str(request_item.get('name') or '')!r} does not use {{{{base_url}}}} or "
                "{{base_url_vdi}} as its host; regenerate the collection with the current pipeline."
            )
            return result
        if allowed[host] not in used:
            used.append(allowed[host])

    collection_vars = _variables(result.collection, "variable")
    defined_in_collection = [variable for variable in HOST_VARIABLES if variable in collection_vars]
    if defined_in_collection:
        result.error_kind = ERROR_HOST_VARIABLE_IN_COLLECTION
        result.error_message = (
            "The collection defines "
            + ", ".join(defined_in_collection)
            + "; host values must come only from the Postman environment."
        )
        return result

    scopes = (
        ("environment", _variables(result.environment, "values")),
        ("collection", collection_vars),
        ("globals", _variables(result.globals, "values")),
    )
    for variable in used:
        source, entry = next(
            ((name, found[variable]) for name, found in scopes if variable in found), ("none", None)
        )
        value = entry.get("value") if entry else None
        check = HostVariableCheck(
            variable=variable,
            source=source,
            defined=entry is not None,
            non_empty=isinstance(value, str) and bool(value.strip()),
        )
        result.host_variables.append(check)
        if not check.non_empty and not result.error_kind:
            result.error_kind = ERROR_HOST_VARIABLE_EMPTY
            result.error_message = f"The effective value of {{{{{variable}}}}} is empty" + (
                f" (set by the {source})." if entry else " (it is not defined)."
            )
    return result


__all__ = ["Preflight", "run_preflight", "sha256_of"]
