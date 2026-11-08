"""
src/testcase_box/library.py — مدلِ نمایشیِ فقط‌خواندنیِ کتابخانه برای UI (M6.1)

    rows = library_rows(store)
    shown = filter_rows(rows, text="DEMO-1", statuses={"ACTIVE"}, readiness={"READY"})
    detail = case_detail(store, rows[0].case_id)

فقط از APIهای موجودِ BoxStore و readiness می‌خواند و هرگز چیزی نمی‌نویسد. منطقِ کسب‌وکار
این‌جا تکرار نمی‌شود: readiness همان assess ِ M3 است (READY، NEEDS_SCRIPT_REVIEW،
NOT_EXECUTABLE)؛ پیکربندی بدونِ پروفایلِ اجرا ارزیابی نمی‌شود (NEEDS_CONFIGURATION ساخته
نمی‌شود).

پوشاندن فقط برای نمایش است — payload و فراداده‌ی ذخیره‌شده تغییر نمی‌کنند:
  * مقدارِ literal ِ هدرهای حساس (Authorization، Cookie، X-Api-Key، …) و query/فرم/متغیرِ
    مسیر/کلیدِ JSONِ بدنه با نامِ شبیهِ اعتبارنامه؛ userinfo و پارامترِ حساس در url.raw؛
    مقدارهای url.auth و request.auth؛
  * ارجاعِ متغیر مثلِ {{token}} و «Bearer {{token}}» دیده می‌مانند (مقدار نیستند)؛
  * متنِ اسکریپت هرگز نمایش داده نمی‌شود — فقط listen، تعدادِ خط و اثرانگشتِ اسکریپت؛
  * نمونه‌پاسخ‌های ذخیره‌شده (response) فقط شمرده می‌شوند.
تشخیص مثلِ M3 اکتشافی است (نام و شکل)، نه مرزِ امنیتیِ کامل.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.testcase_box.contract import (
    VARIABLE_RE,
    Readiness,
    is_secret_key,
    is_sensitive_header,
)
from src.testcase_box.models import (
    CASE_ARCHIVED,
    SOURCE_BATCH,
    BoxCase,
    CaseVersion,
    case_key,
    from_json,
    to_json,
)
from src.testcase_box.readiness import assess
from src.testcase_box.scripts import script_texts
from src.testcase_box.storage import BoxStore

MASK = "[masked]"
SOURCE_NONE = "NONE"  # caseی که هیچ منشأیی ثبت نکرده است
CONFIGURATION_NOTE = "Configuration: not evaluated. Requires an execution profile."
_AUTH_SCHEMES = frozenset({"bearer", "basic", "token"})
_JSON_PAIR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"(\s*:\s*)"((?:[^"\\]|\\.)*)"')


# ── ردیف‌های کتابخانه ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class LibraryRow:
    case_id: int
    key: str
    title: str
    status: str
    source_types: tuple[str, ...]  # مرتب؛ (SOURCE_NONE,) اگر منشأیی نیست
    version_id: int
    version_number: int
    version_count: int
    created_at: str
    updated_at: str
    readiness: str
    approval_state: str
    issue_keys: tuple[str, ...]
    batch_case_ids: tuple[str, ...]
    source_references: tuple[str, ...]
    service: str  # «base_path · operation_id» از منشأِ Batch؛ نامِ سرویس ثبت نمی‌شود

    @property
    def archived(self) -> bool:
        return self.status == CASE_ARCHIVED

    @property
    def source_label(self) -> str:
        return " + ".join(self.source_types)

    def matches(self, needle: str) -> bool:
        text = needle.strip().casefold()
        if not text:
            return True
        haystack = (
            self.key,
            self.title,
            *self.issue_keys,
            *self.batch_case_ids,
            *self.source_references,
        )
        return any(text in value.casefold() for value in haystack)


def library_rows(store: BoxStore) -> tuple[LibraryRow, ...]:
    """همه‌ی caseها (فعال و بایگانی‌شده)، تازه‌ترین تغییر اول. فقط خواندن."""
    rows = [_row(store, case) for case in store.list_cases()]
    return tuple(sorted(rows, key=lambda row: (row.updated_at, row.case_id), reverse=True))


def _row(store: BoxStore, case: BoxCase) -> LibraryRow:
    version = store.get_version(case.current_version_id)
    approval = store.script_approval_state(version.id)
    sources = store.sources(case.id)
    batch = [source for source in sources if source.source_type == SOURCE_BATCH]
    services = sorted(
        {
            " · ".join(
                str(part)
                for part in (source.metadata.get("base_path"), source.metadata.get("operation_id"))
                if part
            )
            for source in batch
        }
        - {""}
    )
    return LibraryRow(
        case_id=case.id,
        key=case_key(case.id),
        title=case.title,
        status=case.status,
        source_types=tuple(sorted({s.source_type for s in sources})) or (SOURCE_NONE,),
        version_id=version.id,
        version_number=version.version_number,
        version_count=len(store.versions(case.id)),
        created_at=case.created_at,
        updated_at=case.updated_at,
        readiness=assess(version, approval).status,
        approval_state=approval,
        issue_keys=_unique(source.metadata.get("issue_key") for source in batch),
        batch_case_ids=_unique(source.source_reference.rsplit("/", 1)[-1] for source in batch),
        source_references=_unique(source.source_reference for source in sources),
        service=", ".join(services),
    )


def _unique(values) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if isinstance(value, str) and value}))


def filter_rows(
    rows,
    *,
    text: str = "",
    statuses=(),
    sources=(),
    readiness=(),
) -> tuple[LibraryRow, ...]:
    """مجموعه‌ی خالی یعنی «بدونِ فیلتر». منبع: هر نوعِ منشأِ case که در sources باشد."""
    statuses, sources, readiness = set(statuses), set(sources), set(readiness)
    return tuple(
        row
        for row in rows
        if row.matches(text)
        and (not statuses or row.status in statuses)
        and (not sources or sources & set(row.source_types))
        and (not readiness or row.readiness in readiness)
    )


# ── جزئیاتِ case ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ScriptSummary:
    location: str
    listen: str
    lines: int


@dataclass(frozen=True)
class VersionView:
    id: int
    number: int
    created_at: str
    created_by: str | None
    fingerprint: str
    script_fingerprint: str | None
    current: bool


@dataclass(frozen=True)
class SourceView:
    id: int
    source_type: str
    source_reference: str
    version_number: int | None
    created_at: str
    metadata: dict  # پوشانده‌شده برای نمایش


@dataclass(frozen=True)
class CaseDetail:
    case: BoxCase
    key: str
    version: VersionView  # نسخه‌ی در حالِ نمایش (پیش‌فرض: جاری)
    versions: tuple[VersionView, ...]  # به ترتیبِ شماره
    payload: dict  # پوشانده‌شده برای نمایش
    masked_values: int
    scripts: tuple[ScriptSummary, ...]
    readiness: Readiness
    approval_state: str
    sources: tuple[SourceView, ...]
    configuration_note: str = CONFIGURATION_NOTE


def case_detail(store: BoxStore, case_id: int, version_id: int | None = None) -> CaseDetail:
    """جزئیاتِ یک case و یکی از نسخه‌هایش (پیش‌فرض: جاری). نسخه باید مالِ همین case باشد."""
    case = store.get_case(case_id)
    versions = store.versions(case_id)
    views = tuple(_version_view(v, case.current_version_id) for v in versions)
    chosen_id = case.current_version_id if version_id is None else version_id
    chosen = next((v for v in versions if v.id == chosen_id), None)
    if chosen is None:
        raise ValueError("The version does not belong to this test case.")
    approval = store.script_approval_state(chosen.id)
    payload, masked = masked_payload(chosen.payload)
    numbers = {v.id: v.version_number for v in versions}
    return CaseDetail(
        case=case,
        key=case_key(case.id),
        version=_version_view(chosen, case.current_version_id),
        versions=views,
        payload=payload,
        masked_values=masked,
        scripts=script_summary(chosen.payload),
        readiness=assess(chosen, approval),
        approval_state=approval,
        sources=tuple(
            SourceView(
                id=source.id,
                source_type=source.source_type,
                source_reference=source.source_reference,
                version_number=numbers.get(source.case_version_id),
                created_at=source.created_at,
                metadata=masked_metadata(source.metadata),
            )
            for source in store.sources(case_id)
        ),
    )


def _version_view(version: CaseVersion, current_id: int) -> VersionView:
    return VersionView(
        id=version.id,
        number=version.version_number,
        created_at=version.created_at,
        created_by=version.created_by,
        fingerprint=version.fingerprint,
        script_fingerprint=version.script_fingerprint,
        current=version.id == current_id,
    )


def script_summary(payload: Any) -> tuple[ScriptSummary, ...]:
    """فقط مکان، listen و تعدادِ خط — متنِ اسکریپت بیرون نمی‌آید."""
    return tuple(
        ScriptSummary(script.location, script.listen, len(script.text.splitlines()))
        for script in script_texts(payload)
    )


# ── پوشاندنِ نمایشی ─────────────────────────────────────────────────────────
def _literal(value: str) -> str:
    return VARIABLE_RE.sub("", value).strip()


def _secret_literal(value: Any) -> bool:
    """مقدارِ literal (نه فقط ارجاعِ متغیر یا نامِ طرحِ احراز هویت)."""
    if not isinstance(value, str):
        return value is not None and value != ""
    literal = _literal(value)
    return bool(literal) and literal.casefold() not in _AUTH_SCHEMES


class _Masker:
    def __init__(self) -> None:
        self.count = 0

    def mask(self) -> str:
        self.count += 1
        return MASK

    def entries(self, entries: Any, *, header: bool = False) -> None:
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            sensitive = is_sensitive_header(key) if header else is_secret_key(key)
            if sensitive and _secret_literal(entry.get("value")):
                entry["value"] = self.mask()
            if entry.get("src") not in (None, ""):
                entry["src"] = self.mask()

    def raw_url(self, raw: str) -> str:
        head, separator, query = raw.partition("?")
        scheme = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", head)
        prefix = scheme.group(0) if scheme else ""
        rest = head[len(prefix) :]
        cut = rest.find("/")
        authority = rest if cut < 0 else rest[:cut]
        if "@" in authority:
            rest = self.mask() + "@" + rest.split("@", 1)[1]
        if not separator:
            return prefix + rest
        pairs = []
        for pair in query.split("&"):
            key, equals, value = pair.partition("=")
            if equals and is_secret_key(key) and _secret_literal(value):
                value = self.mask()
            pairs.append(f"{key}{equals}{value}")
        return prefix + rest + "?" + "&".join(pairs)

    def raw_body(self, raw: str) -> str:
        def replace(match: re.Match) -> str:
            key, colon, value = match.groups()
            if is_secret_key(key) and _secret_literal(value):
                return f'"{key}"{colon}"{self.mask()}"'
            return match.group(0)

        return _JSON_PAIR_RE.sub(replace, raw)

    def auth_block(self, auth: Any) -> None:
        if not isinstance(auth, dict):
            return
        for name, params in auth.items():
            if name == "type":
                continue
            if isinstance(params, dict):
                for key, value in params.items():
                    if _secret_literal(value):
                        params[key] = self.mask()
            elif isinstance(params, list):
                for entry in params:
                    if isinstance(entry, dict) and _secret_literal(entry.get("value")):
                        entry["value"] = self.mask()


def masked_payload(payload: Any) -> tuple[Any, int]:
    """نسخه‌ای مستقل برای نمایش و تعدادِ مقدارهای پوشانده. payload ِ ورودی تغییر نمی‌کند."""
    if not isinstance(payload, dict):
        return payload, 0
    item = from_json(to_json(payload))
    masker = _Masker()
    events = item.get("event")
    if isinstance(events, dict):
        events = [events]
    if isinstance(events, list):
        item["event"] = [
            {"listen": event.get("listen"), "script": "[script text hidden]"}
            if isinstance(event, dict)
            else "[script entry hidden]"
            for event in events
        ]
    if "response" in item:
        count = len(item["response"]) if isinstance(item["response"], list) else 1
        item["response"] = f"[{count} saved response example(s) hidden]"
    request = item.get("request")
    if isinstance(request, dict):
        masker.entries(request.get("header"), header=True)
        masker.auth_block(request.get("auth"))
        url = request.get("url")
        if isinstance(url, dict):
            if isinstance(url.get("raw"), str):
                url["raw"] = masker.raw_url(url["raw"])
            masker.entries(url.get("query"))
            masker.entries(url.get("variable"))
            if url.get("auth") not in (None, "", {}, []):
                url["auth"] = masker.mask()
        elif isinstance(url, str):
            request["url"] = masker.raw_url(url)
        body = request.get("body")
        if isinstance(body, dict):
            if isinstance(body.get("raw"), str):
                body["raw"] = masker.raw_body(body["raw"])
            masker.entries(body.get("urlencoded"))
            masker.entries(body.get("formdata"))
    return item, masker.count


def masked_metadata(metadata: Any) -> Any:
    """فراداده‌ی منشأ: مقدارِ literal ِ هر کلیدِ شبیهِ اعتبارنامه پوشانده می‌شود."""
    if isinstance(metadata, dict):
        return {
            key: MASK
            if is_secret_key(key) and isinstance(value, str) and _secret_literal(value)
            else masked_metadata(value)
            for key, value in metadata.items()
        }
    if isinstance(metadata, list):
        return [masked_metadata(value) for value in metadata]
    return metadata
