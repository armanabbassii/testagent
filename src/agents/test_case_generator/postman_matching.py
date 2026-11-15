"""
postman_matching.py — تطبیقِ محافظه‌کارانه و method-awareِ درخواستِ Postman با عملیاتِ Swagger

    matches = match_postman_to_swagger(evidence, services)

فقط در برابرِ DiscoveredServiceهای منابعِ انتخاب‌شده‌ی قدم ۲ (همان کاتالوگی که
نگاشت و اعتبارسنجی می‌بینند). نتیجه مشورتی است: انتخابِ منبع، نامزدهای نگاشت و
اعتبارسنجیِ نگاشت را عوض نمی‌کند. هیچ شبکه، LLM یا حلِ متغیری در کار نیست.

کلیدِ تطبیق (method، مسیر) است. قواعدِ مسیر (بدونِ بازنویسیِ مسیرهای اصلی):

  * اسلشِ ابتدایی افزوده؛ بخش‌های خالیِ «//» حذف و یادداشت می‌شوند؛
  * اسلشِ انتهایی نادیده گرفته می‌شود مگر همان سرویس هر دو شکل را داشته باشد؛
  * حروف حفظ می‌شوند (بدونِ lowercase)؛ تفاوتِ فقط در حروف = UNMATCHED با یادداشت؛
  * percent-decoding نمی‌شود؛ query در تطبیق نقشی ندارد؛
  * پارامتر فقط بخشِ کامل است: ``{id}`` (Swagger)، ``:id`` و ``{{id}}`` (Postman)؛
    بخشِ نیمه‌متغیر (``v{{n}}``) قابلِ مقایسه نیست → UNRESOLVED؛
  * قاعده‌ی OpenAPI «مسیرِ مشخص پیش از الگو»: بیشترین بخشِ لفظیِ برابر برنده است؛
  * فقط برای میزبانِ لفظی: پایه‌ی مسیرِ اعلام‌شده‌ی همان سرویس (servers) می‌تواند
    از ابتدای مسیر حذف شود (یادداشت + سقفِ قطعیت).

میزبان هرگز دسته را تعیین نمی‌کند؛ فقط یادداشت می‌سازد.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from src.agents.test_case_generator.api_discovery import DiscoveredApi, DiscoveredService
from src.agents.test_case_generator.postman_evidence import (
    HOST_LITERAL,
    HOST_NONE,
    HOST_VARIABLE,
    PostmanEndpointEvidence,
    PostmanEvidence,
)

EXACT_MATCH = "EXACT_MATCH"
PATH_PARAMETER_MATCH = "PATH_PARAMETER_MATCH"
TEMPLATE_MATCH = "TEMPLATE_MATCH"
METHOD_MISMATCH = "METHOD_MISMATCH"
AMBIGUOUS = "AMBIGUOUS"
UNRESOLVED = "UNRESOLVED"
UNMATCHED = "UNMATCHED"

SUCCESSFUL = frozenset({EXACT_MATCH, PATH_PARAMETER_MATCH, TEMPLATE_MATCH})

HIGH = "high"
MEDIUM = "medium"
LOW = "low"
_RANK = {HIGH: 3, MEDIUM: 2, LOW: 1}
_BASE_CERTAINTY = {EXACT_MATCH: HIGH, PATH_PARAMETER_MATCH: MEDIUM, TEMPLATE_MATCH: MEDIUM}

_SWAGGER_PARAM_RE = re.compile(r"^\{[^{}]+\}$")
_COLON_PARAM_RE = re.compile(r"^:[^:/{}]+$")
_POSTMAN_VARIABLE_RE = re.compile(r"^\{\{[^{}]+\}\}$")

# نوعِ بخشِ مسیرِ Postman
_LITERAL = "literal"
_COLON = "colon"
_VARIABLE = "variable"
_PARTIAL = "partial"

# نتیجه‌ی مقایسه‌ی یک مسیر با یک عملیات
_OK = "ok"
_CASE_ONLY = "case_only"
_UNDECIDABLE = "undecidable"
_NO = "no"


# ── مدل ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SwaggerCandidate:
    """یک عملیاتِ Swagger از سرویسِ انتخاب‌شده — مسیر همان مسیرِ اصلیِ سند."""

    service: str
    method: str
    path: str
    operation_id: str = ""

    def label(self) -> str:
        op = f", {self.operation_id}" if self.operation_id else ""
        return f"{self.method} {self.path} (service {self.service}{op})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "method": self.method,
            "path": self.path,
            "operation_id": self.operation_id,
        }


@dataclass(frozen=True)
class PostmanSwaggerMatch:
    """نتیجه‌ی تطبیقِ یک درخواستِ Postman — مسیرها همان مسیرهای اصلی‌اند.

    ``certainty`` فقط برای دسته‌های موفق (EXACT/PATH_PARAMETER/TEMPLATE) پر است.
    ``operations`` نامزدهای همان method؛ ``other_methods`` عملیاتِ مسیرِ سازگار با
    methodِ دیگر (برای METHOD_MISMATCH یا methodِ پیش‌فرض‌شده).
    """

    source_type: str
    source_reference: str
    source_label: str
    collection_name: str
    collection_version: str
    request_index: int
    postman_method: str
    method_defaulted: bool
    postman_path: str
    category: str
    certainty: str | None
    operations: tuple[SwaggerCandidate, ...] = ()
    other_methods: tuple[SwaggerCandidate, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def successful(self) -> bool:
        return self.category in SUCCESSFUL

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
            "postman_method": self.postman_method,
            "method_defaulted": self.method_defaulted,
            "postman_path": self.postman_path,
            "category": self.category,
            "certainty": self.certainty,
            "operations": [item.to_dict() for item in self.operations],
            "other_methods": [item.to_dict() for item in self.other_methods],
            "notes": list(self.notes),
        }


# ── مسیرها ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _PostmanPath:
    segments: tuple[str, ...]
    kinds: tuple[str, ...]
    trailing: bool
    repeated_slashes: bool


def _postman_path(path: str) -> _PostmanPath:
    text = path if path.startswith("/") else "/" + path
    raw = text.split("/")[1:]
    trailing = len(raw) > 1 and raw[-1] == ""
    body = raw[:-1] if trailing else raw
    repeated = any(segment == "" for segment in body) and body != [""]
    segments = tuple(segment for segment in body if segment != "")
    kinds = []
    for segment in segments:
        if _COLON_PARAM_RE.match(segment):
            kinds.append(_COLON)
        elif _POSTMAN_VARIABLE_RE.match(segment):
            kinds.append(_VARIABLE)
        elif "{{" in segment or "}}" in segment:
            kinds.append(_PARTIAL)
        else:
            kinds.append(_LITERAL)
    return _PostmanPath(segments, tuple(kinds), trailing, repeated)


def _swagger_segments(path: str) -> tuple[tuple[str, ...], bool]:
    text = path if path.startswith("/") else "/" + path
    trailing = len(text) > 1 and text.endswith("/")
    return tuple(segment for segment in text.split("/") if segment), trailing


@dataclass(frozen=True)
class _Operation:
    service: str
    api: DiscoveredApi
    segments: tuple[str, ...]
    trailing: bool
    has_twin: bool

    def candidate(self) -> SwaggerCandidate:
        return SwaggerCandidate(
            self.service, self.api.method, self.api.path, self.api.operation_id or ""
        )


@dataclass(frozen=True)
class _Service:
    name: str
    hostname: str
    base_segments: tuple[str, ...]
    operations: tuple[_Operation, ...]


def _service(service: DiscoveredService) -> _Service:
    try:
        parsed = urlsplit(service.base_url or "")
        hostname = (parsed.hostname or "").lower()
        base = tuple(segment for segment in parsed.path.split("/") if segment)
    except ValueError:
        hostname, base = "", ()
    paths = {api.path for api in service.apis}
    operations = []
    for api in service.apis:
        segments, trailing = _swagger_segments(api.path)
        twin = api.path.rstrip("/") if trailing else api.path + "/"
        operations.append(_Operation(service.name, api, segments, trailing, twin in paths))
    return _Service(service.name, hostname, base, tuple(operations))


@dataclass(frozen=True)
class _Compared:
    """مقایسه‌ی یک شکلِ مسیرِ Postman با یک عملیات."""

    operation: _Operation
    status: str
    literal: int = 0
    template: int = 0
    parameter: int = 0
    base_removed: bool = False
    variable_vs_literal: bool = False


def _compare(
    path: _PostmanPath,
    segments: tuple[str, ...],
    kinds: tuple[str, ...],
    op: _Operation,
    base_removed: bool,
) -> _Compared:
    if len(segments) != len(op.segments):
        return _Compared(op, _NO)
    if op.has_twin and path.trailing != op.trailing:
        return _Compared(op, _NO)
    literal = template = parameter = 0
    case_only = undecidable = False
    for actual, kind, expected in zip(segments, kinds, op.segments):
        swagger_param = bool(_SWAGGER_PARAM_RE.match(expected))
        if kind == _PARTIAL:
            undecidable = True
        elif kind == _LITERAL:
            if swagger_param:
                template += 1
            elif actual == expected:
                literal += 1
            elif actual.lower() == expected.lower():
                case_only = True
            else:
                return _Compared(op, _NO)
        elif kind == _COLON:
            if swagger_param:
                parameter += 1
            else:
                return _Compared(op, _NO)
        else:  # _VARIABLE
            if swagger_param:
                parameter += 1
            else:
                undecidable = True
    if undecidable:
        return _Compared(op, _UNDECIDABLE, base_removed=base_removed)
    if case_only:
        return _Compared(op, _CASE_ONLY, base_removed=base_removed)
    return _Compared(op, _OK, literal, template, parameter, base_removed)


def _comparisons(
    item: PostmanEndpointEvidence, path: _PostmanPath, services: list[_Service]
) -> list[_Compared]:
    results: list[_Compared] = []
    for service in services:
        forms = [(path.segments, path.kinds, False)]
        prefix = service.base_segments
        if (
            item.host_kind == HOST_LITERAL
            and prefix
            and len(path.segments) > len(prefix)
            and path.segments[: len(prefix)] == prefix
        ):
            forms.append((path.segments[len(prefix) :], path.kinds[len(prefix) :], True))
        for op in service.operations:
            for segments, kinds, removed in forms:
                compared = _compare(path, segments, kinds, op, removed)
                if compared.status != _NO:
                    results.append(compared)
    return results


# ── تصمیم ────────────────────────────────────────────────────────────────────


def _cap(certainty: str, limit: str) -> str:
    return certainty if _RANK[certainty] <= _RANK[limit] else limit


def _unique(candidates: list[SwaggerCandidate]) -> tuple[SwaggerCandidate, ...]:
    return tuple(dict.fromkeys(candidates))


def _host_notes(
    item: PostmanEndpointEvidence, services: list[_Service], matched: list[_Compared]
) -> list[str]:
    if item.host_kind == HOST_VARIABLE:
        return [
            f"host is the Postman variable {item.host}; its value and any base path it "
            "carries are unknown (not resolved)"
        ]
    if item.host_kind == HOST_NONE:
        return ["no host was supplied; the path was matched as written"]
    notes = []
    try:
        hostname = (urlsplit(f"//{item.host}").hostname or "").lower()
    except ValueError:
        hostname = ""
    by_name = {service.name: service for service in services}
    for name in dict.fromkeys(compared.operation.service for compared in matched):
        swagger_host = by_name[name].hostname
        if hostname and swagger_host and hostname != swagger_host:
            notes.append(
                f"literal host {item.host} differs from the Swagger server host "
                f"{swagger_host} of service {name}"
            )
    return notes


def _match_one(item: PostmanEndpointEvidence, services: list[_Service]) -> PostmanSwaggerMatch:
    path = _postman_path(item.path)
    notes: list[str] = []
    if item.method_defaulted:
        notes.append("method not specified in the collection; Postman's default GET was assumed")
    if path.repeated_slashes:
        notes.append("empty path segments from repeated slashes were ignored")

    def result(
        category: str, operations=(), others=(), certainty=None, extra=()
    ) -> PostmanSwaggerMatch:
        return PostmanSwaggerMatch(
            source_type=item.source_type,
            source_reference=item.source_reference,
            source_label=item.source_label,
            collection_name=item.collection_name,
            collection_version=item.collection_version,
            request_index=item.request_index,
            postman_method=item.method,
            method_defaulted=item.method_defaulted,
            postman_path=item.path,
            category=category,
            certainty=certainty,
            operations=_unique(list(operations)),
            other_methods=_unique(list(others)),
            notes=tuple(dict.fromkeys(notes + list(extra))),
        )

    if _PARTIAL in path.kinds:
        return result(
            UNRESOLVED,
            extra=[
                "a path segment mixes text and a Postman variable; it cannot be compared safely"
            ],
        )

    compared = _comparisons(item, path, services)
    compatible = [c for c in compared if c.status == _OK]

    if compatible:
        # OpenAPI: مسیرِ مشخص پیش از الگو — بیشترین بخشِ لفظیِ برابر.
        best = max(c.literal for c in compatible)
        surviving = [c for c in compatible if c.literal == best]
        same = [c for c in surviving if c.operation.api.method == item.method]
        others = [
            c.operation.candidate() for c in surviving if c.operation.api.method != item.method
        ]
        extra = _host_notes(item, services, surviving)
        if any(c.base_removed for c in surviving):
            extra.append(
                "the Swagger server base path of the matched service was removed from the literal-host path"
            )
        if not same:
            return result(METHOD_MISMATCH, others=others, extra=extra + ["not a successful match"])
        candidates = _unique([c.operation.candidate() for c in same])
        if len(candidates) > 1:
            return result(
                AMBIGUOUS,
                operations=candidates,
                others=others if item.method_defaulted else (),
                extra=extra + ["more than one operation matches; none was chosen"],
            )
        chosen = same[0]
        if chosen.template:
            category = TEMPLATE_MATCH
        elif chosen.parameter:
            category = PATH_PARAMETER_MATCH
        else:
            category = EXACT_MATCH
        certainty = _BASE_CERTAINTY[category]
        if item.method_defaulted or item.host_kind == HOST_VARIABLE:
            certainty = _cap(certainty, LOW)
        if chosen.base_removed:
            certainty = _cap(certainty, MEDIUM)
        if path.trailing != chosen.operation.trailing:
            extra.append(
                "a trailing-slash difference was ignored (the service defines only one form)"
            )
        # متغیری که ممکن است با مقدارش به مسیرِ مشخصِ دیگری برسد.
        alternatives = _unique(
            [
                c.operation.candidate()
                for c in compared
                if c.status == _UNDECIDABLE and c.operation.api.method == item.method
            ]
        )
        if alternatives:
            certainty = _cap(certainty, LOW)
            extra.append(
                "depending on the variable's value, the request could also reach: "
                + "; ".join(candidate.label() for candidate in alternatives)
            )
        return result(
            category,
            operations=candidates,
            others=others if item.method_defaulted else (),
            certainty=certainty,
            extra=extra,
        )

    undecidable = [c for c in compared if c.status == _UNDECIDABLE]
    if undecidable:
        return result(
            UNRESOLVED,
            extra=[
                "a Postman variable stands where the Swagger path has a fixed segment; "
                "its value is unknown, so the comparison cannot be made"
            ],
        )
    case_only = [c for c in compared if c.status == _CASE_ONLY]
    extra = [
        "no compatible operation in the selected Swagger sources; it may exist in an unselected source"
    ]
    if case_only:
        extra.append(
            "differs only in letter case from: "
            + "; ".join(c.operation.candidate().label() for c in case_only)
        )
    return result(UNMATCHED, extra=extra)


def match_postman_to_swagger(
    evidence: PostmanEvidence | None,
    services: list[DiscoveredService],
) -> tuple[PostmanSwaggerMatch, ...]:
    """یک نتیجه برای هر درخواستِ Postman، به همان ترتیب — بدونِ شاهد، تهی."""
    if evidence is None or not evidence.requests:
        return ()
    prepared = [_service(service) for service in services]
    return tuple(_match_one(item, prepared) for item in evidence.requests)


__all__ = [
    "AMBIGUOUS",
    "EXACT_MATCH",
    "HIGH",
    "LOW",
    "MEDIUM",
    "METHOD_MISMATCH",
    "PATH_PARAMETER_MATCH",
    "SUCCESSFUL",
    "TEMPLATE_MATCH",
    "UNMATCHED",
    "UNRESOLVED",
    "PostmanSwaggerMatch",
    "SwaggerCandidate",
    "match_postman_to_swagger",
]
