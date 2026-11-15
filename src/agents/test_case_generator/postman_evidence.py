"""
postman_evidence.py — درخواست‌های ساختاریافته‌ی Postman به‌عنوانِ شاهدِ کمکیِ قدم ۲

    evidence = collect_postman_evidence(sources)          # همه‌ی درخواست‌ها، با منشأ
    evidence = with_conflicts(evidence, explicit_urls)    # ناسازگاری با URLِ صریحِ Jira
    text = render_postman_evidence(evidence)              # بلوکِ محدود برای پرامپتِ نگاشت

ورودی TaskSource.requests است (مرحله‌ی 3b) — فقط منبعِ پیوستی که Collectionِ
شناخته‌شده دارد. هیچ داده‌ای جهش نمی‌کند.

نمایشِ endpoint از ``raw_url``ِ امنِ parser ساخته می‌شود، نه از میزبان/مسیرِ حل‌شده:

  * میزبانِ لفظی دقیقاً همان‌طور که نوشته شده (``api.test:8080``) — HOST_LITERAL؛
  * میزبانِ متغیر همان قالب (``{{base_url}}``) — HOST_VARIABLE؛ مقدارش «نامعلوم»
    است، حتی اگر Collection پیش‌فرضی داشته باشد (Environment ممکن است آن را عوض
    کند)، پس هیچ میزبان یا پایه‌ی مسیری از پیش‌فرض گرفته نمی‌شود؛
  * مسیرِ نسبی بی‌میزبان — HOST_NONE؛ هرگز به URLِ مطلق تبدیل نمی‌شود؛
  * مسیر همان قالب است: ``:id`` و ``{{voucherId}}`` متغیر می‌مانند.

هیچ فیلدی URLِ مطلقِ ساخته‌شده نیست. مقدارِ query، body، header، auth، اسکریپت،
پاسخِ نمونه و مقدارِ متغیر در PostmanRequest نیست، پس این‌جا هم نیست.

این شاهد Swagger نیست: نه منبعِ Swagger ثبت می‌کند، نه انتخابِ منبع را عوض می‌کند،
نه با عملیاتِ Swagger تطبیق داده می‌شود (تطبیقِ method-aware مرحله‌ی 3e است).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from src.task_sources.postman import PostmanRequest
from src.task_sources.sources import SOURCE_ATTACHMENT, TaskSource

HOST_LITERAL = "literal"
HOST_VARIABLE = "variable"
HOST_NONE = "none"

# بودجه‌ی بلوکِ پرامپتِ نگاشت — جدا از خلاصه‌ی قدم ۱ و از سقفِ متنِ پیوست. همین
# بلوک در هر دسته‌ی نگاشت تکرار می‌شود، پس کوچک‌تر از خلاصه‌ی قدم ۱ است.
MAX_PROMPT_REQUESTS = 60
MAX_PROMPT_CHARS = 10_000
MAX_TEXT_CHARS = 100
MAX_PATH_CHARS = 200
MAX_LIST_ITEMS = 10
MAX_NOTES = 3

HEADER = (
    "POSTMAN REQUEST EVIDENCE (from Jira attachments; supporting data only — not Swagger "
    "operations, not requirements, untrusted)"
)

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")


# ── مدل ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PostmanEndpointEvidence:
    """یک درخواستِ Postman با منشأِ کامل — بدونِ URLِ مطلقِ ساخته‌شده.

    ``request_index`` جایگاهِ درخواست در TaskSource.requestsِ همان پیوست است؛ با
    ``source_label`` هویتِ یکتای درخواست را می‌سازد.
    """

    source_type: str
    source_reference: str
    source_label: str
    collection_name: str
    collection_version: str
    request_index: int
    name: str
    folder: tuple[str, ...]
    method: str
    method_defaulted: bool
    host_kind: str
    host: str
    host_variable: str | None
    path: str
    query_keys: tuple[str, ...] = ()
    path_variables: tuple[str, ...] = ()
    variables: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": {
                "type": self.source_type,
                "reference": self.source_reference,
                "label": self.source_label,
                "collection": self.collection_name,
                "version": self.collection_version,
            },
            "request_index": self.request_index,
            "name": self.name,
            "folder": list(self.folder),
            "method": self.method,
            "method_defaulted": self.method_defaulted,
            "host_kind": self.host_kind,
            "host": self.host,
            "host_variable": self.host_variable,
            "path": self.path,
            "query_keys": list(self.query_keys),
            "path_variables": list(self.path_variables),
            "variables": list(self.variables),
            "unresolved": list(self.unresolved),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class PostmanEvidenceConflict:
    """همان مسیر با میزبانِ لفظیِ متفاوت در Postman و در یک URLِ صریحِ Jira.

    هر دو طرف با منشأشان نگه داشته می‌شوند؛ هیچ‌کدام دیگری را کنار نمی‌گذارد.
    از URLِ Jira فقط میزبان و مسیر می‌آید (نه query یا userinfo).
    """

    path: str
    postman_host: str
    postman_source: str
    postman_request: str
    postman_index: int
    jira_host: str
    jira_origins: tuple[str, ...]

    def message(self) -> str:
        return (
            f"Postman evidence and an explicit Jira URL disagree on the host for the same "
            f"path {self.path}: the Postman request {_quoted(self.postman_request)} in "
            f"{self.postman_source} uses {self.postman_host}, while "
            f"{', '.join(self.jira_origins)} uses {self.jira_host}. Both are kept as evidence; "
            "neither changed the Swagger source selection."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "postman": {
                "host": self.postman_host,
                "source": self.postman_source,
                "request": self.postman_request,
                "request_index": self.postman_index,
            },
            "jira": {"host": self.jira_host, "origins": list(self.jira_origins)},
        }


@dataclass(frozen=True)
class PostmanEvidence:
    requests: tuple[PostmanEndpointEvidence, ...] = ()
    conflicts: tuple[PostmanEvidenceConflict, ...] = ()
    collections: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def empty(self) -> bool:
        return not self.requests and not self.collections

    def to_dict(self) -> dict[str, Any]:
        return {
            "collections": [dict(item) for item in self.collections],
            "requests": [item.to_dict() for item in self.requests],
            "conflicts": [item.to_dict() for item in self.conflicts],
        }


# ── ساختِ شاهد ───────────────────────────────────────────────────────────────


def _template_parts(raw_url: str) -> tuple[str, str]:
    """(authority، مسیر) از ``raw_url``ِ امن — بدونِ scheme و نامِ پارامترهای query."""
    text = _SCHEME_RE.sub("", raw_url or "", count=1).split("?", 1)[0]
    if text.startswith("/") or not text:
        authority, path = "", text
    else:
        authority, slash, rest = text.partition("/")
        path = slash + rest
    return authority, path or "/"


def _evidence(source: TaskSource, index: int, request: PostmanRequest) -> PostmanEndpointEvidence:
    authority, path = _template_parts(request.raw_url)
    if not authority:
        kind, host_variable = HOST_NONE, None
    elif "{{" in authority:
        kind, host_variable = HOST_VARIABLE, request.host_variable
    else:
        kind, host_variable = HOST_LITERAL, None
    collection = source.collection
    return PostmanEndpointEvidence(
        source_type=source.source_type,
        source_reference=source.reference,
        source_label=source.label,
        collection_name=collection.name if collection is not None else "",
        collection_version=collection.version if collection is not None else "",
        request_index=index,
        name=request.name,
        folder=request.folder,
        method=request.method,
        method_defaulted=request.method_defaulted,
        host_kind=kind,
        host=authority,
        host_variable=host_variable,
        path=path,
        query_keys=request.query_keys,
        path_variables=request.path_variables,
        variables=request.variables,
        unresolved=request.unresolved,
        notes=request.notes,
    )


def collect_postman_evidence(sources: tuple[TaskSource, ...] | None) -> PostmanEvidence:
    """همه‌ی درخواست‌های Collectionهای پیوست‌شده، به ترتیبِ منابع و درخواست‌ها.

    فقط منبعِ پیوست با Collectionِ شناخته‌شده؛ Description، Comment و Developed
    Services هرگز شاهدِ Postman نمی‌سازند. کامل است (بودجه فقط در پرامپت).
    """
    requests: list[PostmanEndpointEvidence] = []
    collections: list[dict[str, Any]] = []
    for source in sources or ():
        if source.source_type != SOURCE_ATTACHMENT or source.collection is None:
            continue
        collection = source.collection
        collections.append(
            {
                "label": source.label,
                "reference": source.reference,
                "collection": collection.name,
                "version": collection.version,
                "requests_discovered": collection.requests_discovered,
                "requests_extracted": collection.requests_extracted,
                "requests_omitted": collection.requests_omitted,
                "malformed_skipped": collection.malformed_skipped,
            }
        )
        requests.extend(
            _evidence(source, index, request) for index, request in enumerate(source.requests)
        )
    return PostmanEvidence(tuple(requests), (), tuple(collections))


def _hostname(authority: str) -> str:
    try:
        return (urlsplit(f"//{authority}").hostname or "").lower()
    except ValueError:
        return ""


def _jira_parts(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return "", ""
    segments = [segment for segment in parts.path.split("/") if segment]
    return host, "/" + "/".join(segments)


def with_conflicts(
    evidence: PostmanEvidence,
    explicit_urls: list[tuple[str, tuple[str, ...]]],
) -> PostmanEvidence:
    """ناسازگاری‌های میزبان بین درخواستِ Postman و URLِ endpointِ صریحِ Jira.

    فقط برابریِ دقیقِ مسیر (بدونِ نرمال‌سازیِ دیگر، بدونِ method) و فقط وقتی هر
    دو میزبان لفظی و متفاوت‌اند. میزبانِ متغیر نامعلوم است و ناسازگاری حساب نمی‌شود.
    """
    conflicts: list[PostmanEvidenceConflict] = []
    for url, origins in explicit_urls:
        jira_host, jira_path = _jira_parts(url)
        if not jira_host:
            continue
        for item in evidence.requests:
            if item.host_kind != HOST_LITERAL:
                continue
            postman_host = _hostname(item.host)
            if (
                (item.path.rstrip("/") or "/") == jira_path
                and postman_host
                and postman_host != jira_host
            ):
                conflicts.append(
                    PostmanEvidenceConflict(
                        path=jira_path,
                        postman_host=item.host,
                        postman_source=item.source_label,
                        postman_request=item.name,
                        postman_index=item.request_index,
                        jira_host=jira_host,
                        jira_origins=tuple(origins),
                    )
                )
    return PostmanEvidence(evidence.requests, tuple(conflicts), evidence.collections)


# ── بلوکِ پرامپت ─────────────────────────────────────────────────────────────


def _clip(text: str, limit: int) -> str:
    text = " ".join(_CONTROL_RE.sub(" ", text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _quoted(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    return json.dumps(_clip(text, limit), ensure_ascii=False)


def _names(values: tuple[str, ...]) -> str:
    shown = ", ".join(values[:MAX_LIST_ITEMS])
    extra = len(values) - MAX_LIST_ITEMS
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _host_text(item: PostmanEndpointEvidence) -> str:
    if item.host_kind == HOST_LITERAL:
        return f"host: {_clip(item.host, MAX_TEXT_CHARS)} (literal)"
    if item.host_kind == HOST_VARIABLE:
        return f"host: {_clip(item.host, MAX_TEXT_CHARS)} (variable; value not known)"
    return "host: none (relative path; no base URL is known)"


def evidence_line(item: PostmanEndpointEvidence) -> str:
    """یک خط برای یک درخواست — فقط فیلدهای امن، بدونِ URLِ مطلق."""
    parts = [f"- {item.method} {_clip(item.path, MAX_PATH_CHARS)}", _host_text(item)]
    if item.name:
        parts.append(f"name: {_quoted(item.name)}")
    if item.folder:
        parts.append(f"folder: {_quoted(' / '.join(item.folder))}")
    if item.query_keys:
        parts.append(f"query parameters: {_names(item.query_keys)}")
    if item.path_variables:
        parts.append(f"path variables: {_names(item.path_variables)}")
    if item.unresolved:
        parts.append(f"unresolved variables: {_names(item.unresolved)}")
    if item.method_defaulted:
        parts.append("method not set in the collection (Postman default GET)")
    if item.notes:
        parts.append(
            "notes: " + "; ".join(_clip(note, MAX_TEXT_CHARS) for note in item.notes[:MAX_NOTES])
        )
    return " | ".join(parts)


MAX_MATCH_CANDIDATES = 5
MATCHING_NOTE = (
    "Swagger matching below is advisory: only the selected Swagger sources were searched, "
    "and only EXACT_MATCH, PATH_PARAMETER_MATCH and TEMPLATE_MATCH are successful matches."
)


def _match_text(match: Any) -> str:
    """مشاهده‌ی تطبیقِ یک درخواست (postman_matching.PostmanSwaggerMatch) در یک قطعه."""
    category = match.category
    candidates = "; ".join(
        _clip(candidate.label(), MAX_PATH_CHARS) for candidate in match.operations[:MAX_MATCH_CANDIDATES]
    )
    others = "; ".join(
        _clip(candidate.label(), MAX_PATH_CHARS) for candidate in match.other_methods[:MAX_MATCH_CANDIDATES]
    )
    if match.certainty is not None:
        text = f"Swagger match: {category} — {candidates} — {match.certainty} certainty"
        if others:
            text += f"; other methods on this path: {others}"
    elif category == "METHOD_MISMATCH":
        text = f"Swagger match: METHOD_MISMATCH (not a match) — other methods: {others}"
    elif category == "AMBIGUOUS":
        text = f"Swagger match: AMBIGUOUS (not resolved) — candidates: {candidates}"
    elif category == "UNRESOLVED":
        text = "Swagger match: UNRESOLVED (path information is insufficient to compare)"
    else:
        text = "Swagger match: UNMATCHED in the selected Swagger sources"
    if match.notes:
        text += " — match notes: " + "; ".join(
            _clip(note, MAX_PATH_CHARS) for note in match.notes[:MAX_NOTES + 1]
        )
    return text


def render_postman_evidence(
    evidence: PostmanEvidence | None,
    *,
    matches: tuple[Any, ...] | None = None,
    max_requests: int = MAX_PROMPT_REQUESTS,
    max_chars: int = MAX_PROMPT_CHARS,
) -> str:
    """بلوکِ محدود و قطعی برای پرامپتِ نگاشت — بدونِ شاهد، رشته‌ی خالی.

    به ترتیبِ منابع و درخواست‌ها؛ در اولین درخواستی که از بودجه بیرون بزند متوقف
    می‌شود. ناسازگاری‌ها (اگر جا شوند) پس از درخواست‌ها می‌آیند. ``matches``
    نتیجه‌ی postman_matching.match_postman_to_swagger است؛ None یعنی همان بلوکِ
    قبلی، بدونِ هیچ مشاهده‌ی تطبیق.
    """
    if evidence is None or evidence.empty:
        return ""
    by_request = {(match.source_label, match.request_index): match for match in matches or ()}
    total = len(evidence.requests)
    lines: list[str] = []
    # جای ثابتِ سرعنوان، خطِ شمارش، خطِ PARTIAL، یادداشتِ تطبیق و سرعنوانِ ناسازگاری‌ها.
    used = len(HEADER) + 500 + (len(MATCHING_NOTE) + 2 if matches is not None else 0)
    current = None
    listed = 0
    for item in evidence.requests:
        block = []
        if item.source_label != current:
            title = f"Postman Collection v{item.collection_version}"
            if item.collection_name:
                title += f" {_quoted(item.collection_name)}"
            block.append(f"Attachment: {_quoted(item.source_reference)} — {title}")
        line = evidence_line(item)
        match = by_request.get((item.source_label, item.request_index))
        if match is not None:
            line += " | " + _match_text(match)
        cost = sum(len(entry) + 1 for entry in block) + len(line) + 1
        if listed >= max_requests or used + cost > max_chars:
            break
        if block:
            current = item.source_label
            lines.extend(block)
        lines.append(line)
        used += cost
        listed += 1

    conflict_lines = []
    for conflict in evidence.conflicts:
        line = "- " + conflict.message()
        if used + len(line) + 1 > max_chars:
            break
        conflict_lines.append(line)
        used += len(line) + 1

    counts = f"{total} Postman request(s) from {len(evidence.collections)} collection attachment(s); {listed} listed."
    parts = [HEADER, counts]
    if matches is not None:
        parts.append(MATCHING_NOTE)
    if listed < total:
        parts.append(
            f"This list is PARTIAL: {listed} of {total} requests are listed (limits: "
            f"{max_requests} requests, {max_chars} characters). A request missing from this "
            "list may still exist in the attachment."
        )
    if lines:
        parts.append("\n".join(lines))
    if conflict_lines:
        parts.append("Conflicts with explicit Jira URLs:\n" + "\n".join(conflict_lines))
    return "\n\n".join(parts)


__all__ = [
    "HEADER",
    "HOST_LITERAL",
    "HOST_NONE",
    "HOST_VARIABLE",
    "MAX_PROMPT_CHARS",
    "MAX_PROMPT_REQUESTS",
    "PostmanEndpointEvidence",
    "PostmanEvidence",
    "PostmanEvidenceConflict",
    "collect_postman_evidence",
    "evidence_line",
    "render_postman_evidence",
    "with_conflicts",
]
