"""
source_selection.py — انتخابِ خودکارِ منابعِ Swagger برای قدم دوم

    select_sources(task_description, developed_services)  → SourceSelection

قدم دوم فقط snapshotهایی را می‌خواند که این ماژول انتخاب می‌کند. انتخاب قطعی
است (بدونِ LLM و بدونِ شبکه) و فقط از رجیستریِ ``SWAGGER_SOURCES`` و
snapshotهای محلی می‌خواند؛ هیچ شرطِ ویژه‌ی یک منبع (``if admin``) در الگوریتم
نیست، پس منبعِ تازه (مثلاً ACL) فقط یک ردیفِ رجیستری و یک snapshot است.

ترتیبِ شواهد:

  1. URLِ Swagger/OpenAPI (``urls.primaryName=CRM`` یا ``…/api-docs/CRM``) →
     همان منبع. URLِ Swaggerی که به منبعِ ثبت‌شده‌ای نرسد خطاست.
  2. مسیرِ endpoint (URLِ کاملِ endpoint یا مسیرِ خام در متن) → منبعی که
     snapshotش عملیاتِ همان مسیر را دارد؛ اگر هیچ snapshotی آن را نداشت، منبعی
     که پیشوندِ ثبت‌شده‌اش (``/crm``) قطعه‌به‌قطعه با مسیر می‌خواند. مسیرِ
     بی‌پیشوند هرگز خودبه‌خود به Site نسبت داده نمی‌شود.
  3. تحلیلِ معناییِ تسک (source_semantics.py، LLM) — فارسی، انگلیسی یا آمیخته:
     مدل عملیات‌های لازم را به انگلیسی و با منبعشان نام می‌برد و هر ادعا با
     متادیتای واقعیِ همان snapshot تأیید می‌شود؛ فقط منبعی که عملیاتِ
     تأییدشده دارد انتخاب می‌شود. فقط وقتی صدا زده می‌شود که شاهدِ ۱ و ۲ نباشد،
     یا باشد ولی متنِ تسک غیرِلاتین (مثلاً فارسی) باشد.
  3'. اگر تحلیلِ معنایی لازم نبود، در دسترس نبود یا شکست خورد: تطبیقِ قطعیِ
     واژه‌های تسک با متادیتا (مسیر، operationId، tag، summary، description).
     نامِ خودِ منابع («admin») به‌تنهایی شاهد حساب نمی‌شود.
  4. همه‌ی منابعِ انتخاب‌شده باید فعال باشند و snapshot داشته باشند.
  5. شاهدِ ناکافی خطای روشن است — هیچ fallbackی (مثلِ «Admin + Customer») نیست.

انتخابِ دستی (قدم ۲) جایگزین نمی‌شود؛ فقط با URLِ Swaggerِ صریح مقایسه و در
صورتِ تعارض خطا گزارش می‌شود.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from src.agents.test_case_generator import swagger_snapshots as snapshots
from src.agents.test_case_generator.source_semantics import (
    RequiredOperation,
    SemanticAnalysis,
)
from src.agents.test_case_generator.swagger_snapshots import (
    ServiceUrlResolution,
    SwaggerSnapshotError,
    SwaggerSource,
    expand_selection,
    extract_urls,
    is_swagger_url,
    resolve_service_urls,
    snapshot_path,
    source_for,
    source_for_url,
)

MODE_AUTO = "auto"
MODE_MANUAL = "manual"

EVIDENCE_SWAGGER_URL = "swagger_url"
EVIDENCE_ENDPOINT = "endpoint"
EVIDENCE_METADATA = "metadata"
EVIDENCE_SEMANTIC = "semantic"
EVIDENCE_MANUAL = "manual"

EVIDENCE_LABELS: dict[str, str] = {
    EVIDENCE_SWAGGER_URL: "Swagger URL",
    EVIDENCE_ENDPOINT: "endpoint path",
    EVIDENCE_METADATA: "task ↔ Swagger metadata",
    EVIDENCE_SEMANTIC: "task analysis, verified operation",
    EVIDENCE_MANUAL: "manual selection",
}

# منبعی که بدونِ شاهدِ قوی‌تر فقط از متادیتا انتخاب می‌شود باید دست‌کم این
# نسبت از بهترین امتیاز را داشته باشد؛ دو منبعِ تقریباً هم‌امتیاز هر دو
# انتخاب می‌شوند (هر دو عملیاتِ مرتبط دارند)، نه یکی به‌دلخواه.
METADATA_RATIO = 0.8
# اضافه‌کردنِ منبعِ دیگر به منبعِ مشخص‌شده (کارِ چندسرویسی) شاهدِ قوی‌تری
# می‌خواهد: یک عملیات باید دست‌کم این تعداد واژه‌ی تسک را پوشش دهد.
WIDENING_MIN_TERMS = 2
# واژه‌ای که فقط در summary/description یک عملیات آمده ضعیف‌تر از واژه‌ی مسیر،
# operationId یا tag است.
_PROSE_WEIGHT = 0.5

_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")

# وضعیتِ تحلیلِ معنایی در SourceSelection.semantic
SEMANTIC_NOT_NEEDED = ""  # شاهدِ URL/endpoint کافی بود یا حالتِ دستی
SEMANTIC_USED = "used"
SEMANTIC_FAILED = "failed"  # LLM یا پاسخش شکست خورد — هیچ حدسی جایش ننشست
SEMANTIC_NOT_RUN = "not_run"  # لازم بود ولی تحلیل‌گری داده نشد (پیش‌نمایشِ قدم ۲)

# (متنِ تسک، خلاصه‌ی منابع) → SemanticAnalysis — مثلاً SemanticSourceAnalyzer.analyze
SemanticAnalyze = Callable[[str, str], SemanticAnalysis]

# نمونه‌عملیاتِ هر tag در خلاصه‌ی منابعِ پرامپت — نه کلِ سند.
_SUMMARY_EXAMPLES_PER_TAG = 2
_SUMMARY_TEXT_CHARS = 40

# واژه‌هایی که درباره‌ی «کدام سرویس» چیزی نمی‌گویند.
_STOPWORDS = frozenset(
    """
    the and for with from into that this these those then than when where which
    what who how why are was were been being has have had not but all any each
    can could should would will shall must may might also only just more most
    some such very via per use used using new old its our your their there here
    about after before over under between both other same able need needs
    implement implementation implemented add added adding create created update
    updated delete deleted remove removed edit get gets set list lists show
    find search fetch return returns returned make made test tests testing case
    cases scenario scenarios check verify ensure should must when given
    api apis endpoint endpoints service services request requests response
    responses http https url urls path paths method methods controller
    controllers swagger openapi json body param params parameter parameters
    value values field fields data item items info detail details page user
    users id ids
    """.split()
)

_PARAM_RE = re.compile(r"^\{[^{}]*\}$")
_WORD_RE = re.compile(r"[^\W_]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# مسیرِ خام در متنِ آزاد: «GET /admin/voucher/{id}» یا «/serviceSet/12/webServices».
# بخشی از URL یا واژه (and/or) نیست؛ کوئری و نشانه‌گذاریِ پایانی جدا می‌شوند.
_BARE_PATH_RE = re.compile(r"(?<![\w/.:{}~%-])(/[A-Za-z_{][\w{}.~%-]*(?:/[\w{}.~%-]+)*)/?")


# ── مدلِ نتیجه ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Evidence:
    """چرا یک منبع انتخاب شد: کلیدِ منبع، نوعِ شاهد و جزئیاتِ خوانا."""

    source: str
    kind: str
    detail: str


@dataclass(frozen=True)
class EndpointMatch:
    """یک مسیرِ endpoint از ورودی‌ها و منبعی که به آن نسبت داده شد.

    ``owners``     : منبع(هایی) که مسیر به آن نسبت داده شد — خالی یعنی هیچ‌کدام
    ``verified``   : منابعی که snapshotشان عملیاتِ همین مسیر را دارد
    ``operations`` : عملیات‌های منطبق («GET /admin/voucher/{id}»)
    ``prefix``     : منبعی که پیشوندِ ثبت‌شده‌اش با مسیر می‌خواند (یا "")
    """

    reference: str
    path: str
    owners: tuple[str, ...] = ()
    verified: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    prefix: str = ""


@dataclass(frozen=True)
class SourceSelection:
    """منابعِ Swaggerی که قدم ۲ برای یک اجرا می‌خواند و دلیلِ انتخابشان.

    ``keys``        : منابعِ انتخاب‌شده برای همین اجرا (ترتیبِ رجیستری)
    ``mode``        : ``auto`` یا ``manual``
    ``evidence``    : شواهدِ هر منبعِ انتخاب‌شده
    ``endpoints``   : مسیرهای endpoint و منبعِ هرکدام
    ``urls``        : تطبیقِ URLهای ورودی (Swagger / endpoint / حل‌نشده)
    ``searched``    : منابعِ فعالی که متادیتای snapshotشان بررسی شد
    ``unavailable`` : منابعِ فعالی که snapshot ندارند (بررسی نشدند)
    ``warnings``    : نکته‌هایی که اجرا را متوقف نمی‌کنند
    ``error``       : خطای کاربر — اگر پر باشد قدم ۲ اجرا نمی‌شود
    ``semantic``    : وضعیتِ تحلیلِ معنایی (``SEMANTIC_*``)
    """

    keys: tuple[str, ...] = ()
    mode: str = MODE_AUTO
    evidence: tuple[Evidence, ...] = ()
    endpoints: tuple[EndpointMatch, ...] = ()
    urls: ServiceUrlResolution = field(default_factory=ServiceUrlResolution)
    searched: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str = ""
    semantic: str = SEMANTIC_NOT_NEEDED

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.keys)

    def evidence_for(self, key: str) -> tuple[Evidence, ...]:
        return tuple(item for item in self.evidence if item.source == key)

    def summary(self) -> str:
        """«Admin (Swagger URL), CRM (endpoint path) — automatic»."""
        if not self.keys:
            return "no Swagger source selected"
        parts = []
        for key in self.keys:
            kinds = dict.fromkeys(item.kind for item in self.evidence_for(key))
            reason = ", ".join(EVIDENCE_LABELS[kind] for kind in kinds)
            parts.append(f"{_label(key)} ({reason})" if reason else _label(key))
        mode = "automatic" if self.mode == MODE_AUTO else "manual"
        return f"{', '.join(parts)} — {mode}"

    def to_dict(self) -> dict[str, Any]:
        """شکلِ ساختاریافته برای لاگ و اشکال‌زدایی (بدونِ URLهای ورودی)."""
        return {
            "mode": self.mode,
            "selected": list(self.keys),
            "evidence": {
                key: sorted({item.kind for item in self.evidence_for(key)})
                for key in self.keys
            },
            "endpoints": [
                {"path": match.path, "owners": list(match.owners)}
                for match in self.endpoints
            ],
            "searched": list(self.searched),
            "unavailable": list(self.unavailable),
            "warnings": len(self.warnings),
            "error": bool(self.error),
            "semantic": self.semantic,
        }


# ── مسیرها ───────────────────────────────────────────────────────────────────

def _segments(path: str) -> list[str]:
    return [segment for segment in (path or "").split("/") if segment]


def normalize_path(reference: str) -> str:
    """مسیرِ یک URL یا مسیرِ خام: بدونِ میزبان، کوئری، فرگمنت و اسلشِ اضافه."""
    text = (reference or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
        text = text[text.find("/"):] if "/" in text else ""
    text = re.split(r"[?#]", text, maxsplit=1)[0]
    return "/" + "/".join(_segments(text))


def endpoint_references(text: str) -> list[tuple[str, str]]:
    """(متنِ اصلی، مسیرِ نرمال‌شده) برای هر ارجاع به endpoint در متن.

    URLهای کاملی که سندِ Swagger نیستند، و مسیرهای خامِ داخلِ متن. URLهای
    Swagger و مسیرهای تکراری کنار گذاشته می‌شوند.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(reference: str) -> None:
        path = normalize_path(reference)
        if path != "/" and path.lower() not in seen:
            seen.add(path.lower())
            found.append((reference, path))

    remainder = text or ""
    for url in extract_urls(remainder):
        remainder = remainder.replace(url, " ")
        if source_for_url(url) is None and not is_swagger_url(url):
            add(url)
    for match in _BARE_PATH_RE.finditer(remainder):
        add(match.group(1).rstrip(".,;:!"))
    return found


def _is_param(segment: str) -> bool:
    return bool(_PARAM_RE.match(segment))


def _starts_with(segments: list[str], prefix: list[str]) -> bool:
    return len(segments) >= len(prefix) and [
        part.lower() for part in segments[: len(prefix)]
    ] == [part.lower() for part in prefix]


def _relative_paths(segments: list[str], base_paths: tuple[str, ...]) -> list[list[str]]:
    """مسیر و همان مسیر بدونِ پیشوندِ gateway (``/api/admin/x`` → ``/admin/x``)."""
    candidates = [segments]
    for base in base_paths:
        prefix = _segments(base)
        if prefix and len(segments) > len(prefix) and _starts_with(segments, prefix):
            candidates.append(segments[len(prefix):])
    return candidates


def _prefix_owner(source: SwaggerSource, candidates: list[list[str]]) -> str:
    """پیشوندِ ثبت‌شده‌ای از منبع که قطعه‌به‌قطعه با مسیر می‌خواند — یا ""."""
    for prefix in source.path_prefixes:
        parts = _segments(prefix)
        if parts and any(_starts_with(segments, parts) for segments in candidates):
            return prefix
    return ""


def _template_matches(template: tuple[str, ...], segments: list[str]) -> bool:
    """مسیرِ واقعی (``/admin/voucher/123``) با الگوی سند (``/admin/voucher/{id}``)."""
    if len(template) != len(segments):
        return False
    for expected, actual in zip(template, segments):
        if _is_param(expected):
            continue
        if _is_param(actual) or expected != actual.lower():
            return False
    return True


# ── متادیتای snapshot ────────────────────────────────────────────────────────

def _stem(term: str) -> str:
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def terms(text: str) -> set[str]:
    """واژه‌های معنادارِ یک متن: camelCase و kebab جدا، حروفِ کوچک، ریشه‌ی ساده."""
    found: set[str] = set()
    for word in _WORD_RE.findall(text or ""):
        for part in _CAMEL_RE.split(word):
            term = _stem(part.lower())
            if len(term) >= 3 and not term.isdigit() and term not in _STOPWORDS:
                found.add(term)
    return found


@dataclass(frozen=True)
class _Operation:
    """یک عملیات در کاتالوگِ انتخاب: ``structural`` واژه‌های مسیر، operationId و
    tag است و ``terms`` همه‌ی واژه‌ها (به‌علاوه‌ی summary و description)."""

    label: str
    segments: tuple[str, ...]
    terms: frozenset[str]
    structural: frozenset[str]
    tag: str = ""
    summary: str = ""


@dataclass(frozen=True)
class _Catalog:
    key: str
    base_paths: tuple[str, ...]
    operations: tuple[_Operation, ...]
    terms: frozenset[str]


# (مسیر) → (mtime، اندازه، کاتالوگ). snapshot هر بار که روی دیسک عوض شود
# دوباره خوانده می‌شود؛ این فقط از parseِ تکراریِ یک فایلِ بزرگ در هر rerun
# جلوگیری می‌کند. خودِ قدم ۲ همچنان فایل را تازه می‌خواند.
_CATALOGS: dict[str, tuple[int, int, _Catalog]] = {}


def _catalog_from_spec(key: str, spec: dict) -> _Catalog:
    base_paths = []
    for server in spec.get("servers") or []:
        if isinstance(server, dict) and server.get("url"):
            base = normalize_path(str(server["url"]))
            if base != "/":
                base_paths.append(base)
    operations = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        literal = " ".join(part for part in _segments(path) if not _is_param(part))
        for method in _HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            structural = terms(
                " ".join(
                    [
                        literal,
                        str(operation.get("operationId") or ""),
                        " ".join(str(tag) for tag in operation.get("tags") or []),
                    ]
                )
            )
            prose = terms(
                f"{operation.get('summary') or ''} {operation.get('description') or ''}"
            )
            operations.append(
                _Operation(
                    label=f"{method.upper()} {path}",
                    segments=tuple(part.lower() for part in _segments(path)),
                    terms=frozenset(structural | prose),
                    structural=frozenset(structural),
                    tag=str((operation.get("tags") or [""])[0]),
                    summary=str(operation.get("summary") or ""),
                )
            )
    vocabulary = frozenset().union(*(operation.terms for operation in operations))
    return _Catalog(key, tuple(base_paths), tuple(operations), vocabulary)


def _load_catalog(
    source: SwaggerSource, directory: Path | str | None
) -> tuple[_Catalog | None, str]:
    """(کاتالوگ، مشکل) — snapshotِ ناموجود (None, "") و خراب (None, پیام) است."""
    path = snapshot_path(source.key, directory=directory)
    try:
        stat = path.stat()
    except OSError:
        return None, ""
    cached = _CATALOGS.get(str(path))
    if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
        return cached[2], ""
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        return None, f"The {source.label} snapshot {path} could not be read: {exc}"
    catalog = _catalog_from_spec(source.key, spec)
    _CATALOGS[str(path)] = (stat.st_mtime_ns, stat.st_size, catalog)
    return catalog, ""


def _find_operations(catalog: _Catalog, source: SwaggerSource, segments: list[str]) -> list[str]:
    candidates = _relative_paths(segments, tuple(source.base_paths) + catalog.base_paths)
    return [
        operation.label
        for operation in catalog.operations
        if any(_template_matches(operation.segments, path) for path in candidates)
    ]


@dataclass(frozen=True)
class _Score:
    score: float
    matched: tuple[str, ...]
    operation: str


def _metadata_scores(
    task_terms: set[str], catalogs: dict[str, _Catalog]
) -> dict[str, _Score]:
    """بهترین عملیاتِ هر منبع برای واژه‌های تسک.

    وزنِ هر واژه IDF است (واژه‌ی نادر در کلِ کاتالوگ مهم‌تر)، و واژه‌ای که فقط
    در summary/description آمده نصفِ واژه‌ی مسیر/operationId/tag حساب می‌شود.
    """
    operations = [op for catalog in catalogs.values() for op in catalog.operations]
    if not task_terms or not operations:
        return {}
    weights = {}
    for term in task_terms:
        frequency = sum(1 for op in operations if term in op.terms)
        if frequency:
            weights[term] = math.log(1 + len(operations) / frequency)

    scores: dict[str, _Score] = {}
    for key, catalog in catalogs.items():
        best = _Score(0.0, (), "")
        for operation in catalog.operations:
            matched = tuple(sorted(term for term in weights if term in operation.terms))
            score = sum(
                weights[term] * (1.0 if term in operation.structural else _PROSE_WEIGHT)
                for term in matched
            )
            if score > best.score:
                best = _Score(score, matched, operation.label)
        if best.matched:
            scores[key] = best
    return scores


# ── انتخاب ───────────────────────────────────────────────────────────────────

def _label(key: str) -> str:
    try:
        return source_for(key).label
    except SwaggerSnapshotError:
        return key


def _labels(keys) -> str:
    return ", ".join(_label(key) for key in keys) or "none"


def _ordered(keys, registry: tuple[SwaggerSource, ...]) -> tuple[str, ...]:
    wanted = set(keys)
    return tuple(source.key for source in registry if source.key in wanted)


def _identity_terms(source: SwaggerSource) -> set[str]:
    """نام‌های یک منبع («admin»، «crm») — به‌تنهایی دلیلِ انتخاب نیستند."""
    return terms(" ".join([*source.identifiers(), *source.path_prefixes]))


def _match_endpoint(
    reference: str,
    path: str,
    registry: tuple[SwaggerSource, ...],
    catalogs: dict[str, _Catalog],
    url_keys: list[str],
) -> tuple[EndpointMatch, str, str]:
    """(تطبیق، خطا، هشدار) برای یک مسیرِ endpoint."""
    segments = _segments(path)
    prefix_owners = [
        source.key
        for source in registry
        if _prefix_owner(source, _relative_paths(segments, source.base_paths))
    ]
    prefix = prefix_owners[0] if len(prefix_owners) == 1 else ""

    verified: dict[str, list[str]] = {}
    for source in registry:
        catalog = catalogs.get(source.key)
        if catalog is not None:
            operations = _find_operations(catalog, source, segments)
            if operations:
                verified[source.key] = operations

    def result(owners, error: str = "", warning: str = "") -> tuple[EndpointMatch, str, str]:
        operations = tuple(op for key in owners for op in verified.get(key, []))
        return (
            EndpointMatch(reference, path, tuple(owners), tuple(verified), operations, prefix),
            error,
            warning,
        )

    if len(verified) > 1:
        preferred = [key for key in verified if key in url_keys] or [
            key for key in verified if key in prefix_owners
        ]
        if len(preferred) == 1:
            return result(preferred)
        found = "; ".join(f"{_label(key)}: {ops[0]}" for key, ops in verified.items())
        return result(
            [],
            f"The endpoint {path} matches operations in more than one Swagger source "
            f"({found}). Add the Swagger URL of the intended source "
            "(…?urls.primaryName=<group>) to Developed Services, or choose the source "
            "manually in Step 2.",
        )
    if verified:
        owner = next(iter(verified))
        warning = ""
        if prefix and prefix != owner:
            warning = (
                f"The endpoint {path} starts with the {_label(prefix)} prefix but was "
                f"found only in the {_label(owner)} snapshot; {_label(owner)} was used."
            )
        return result([owner], warning=warning)

    if len(prefix_owners) > 1:
        return result(
            [],
            f"The endpoint {path} matches the path prefix of several registered "
            f"sources ({_labels(prefix_owners)}). Fix the overlapping path_prefixes in "
            "SWAGGER_SOURCES, or choose the source manually in Step 2.",
        )
    if prefix:
        source = source_for(prefix)
        if not source.enabled:
            return result(
                [],
                f"The endpoint {path} belongs to the {source.label} source, which is "
                "registered but disabled (enabled=False in SWAGGER_SOURCES). Enable it "
                "or remove the endpoint.",
            )
        warning = ""
        if prefix in catalogs:
            warning = (
                f"The endpoint {path} was assigned to {source.label} by its path prefix, "
                f"but no matching operation is in the {source.label} snapshot. The "
                f"snapshot may be outdated — use 'Update {source.label}' in Step 2."
            )
        return result([prefix], warning=warning)

    searched = _labels(catalogs)
    return result(
        [],
        warning=(
            f"The endpoint {path} was not found in any searched snapshot "
            f"({searched}) and has no registered path prefix, so it was not "
            "assigned to a source."
        ),
    )


def _missing_snapshot_error(
    missing: list[str], evidence: list[Evidence], directory: Path | str | None
) -> str:
    lines = []
    for key in missing:
        source = source_for(key)
        reasons = "; ".join(
            f"{EVIDENCE_LABELS[item.kind]} {item.detail}"
            for item in evidence
            if item.source == key
        )
        lines.append(
            f"  - {source.label}: {snapshot_path(key, directory=directory)}"
            + (f" (needed because of {reasons})" if reasons else "")
        )
    return (
        "This task needs Swagger sources whose local snapshot is not available:\n"
        + "\n".join(lines)
        + "\n\nUse the 'Update <source>' button in Step 2 to fetch each snapshot, or "
        "save the OpenAPI JSON at that path. Step 2 was not run, so the mapping is "
        "not made against an incomplete catalogue."
    )


def _ambiguity_error(
    endpoints: list[EndpointMatch],
    searched: list[str],
    unavailable: list[str],
    notes: list[str] | None = None,
) -> str:
    details = list(notes or [])
    if endpoints:
        details.append(
            "Endpoint paths without a known source: "
            + ", ".join(match.path for match in endpoints if not match.owners)
        )
    details.append(f"Snapshots searched: {_labels(searched)}.")
    if unavailable:
        details.append(f"Not searched (no local snapshot): {_labels(unavailable)}.")
    return (
        "The Swagger source(s) for this task could not be determined automatically: "
        "there is no Swagger URL, no endpoint path with a known source, and the task "
        "description does not match the Swagger metadata of any searched source.\n\n"
        + "\n".join(details)
        + "\n\nAdd an example endpoint (e.g. https://…/api/admin/voucher/12 or "
        "GET /crm/digitalService/requests) or a Swagger URL (…?urls.primaryName=<group>) "
        "to Developed Services, fetch the missing snapshots in Step 2, or choose the "
        "sources manually in Step 2. Step 2 was not run."
    )


# ── تحلیلِ معنایی: خلاصه‌ی منابع و تأییدِ ادعاها ─────────────────────────────

def _has_non_latin_text(text: str) -> bool:
    """فارسی یا هر خطِ غیرِلاتینِ دیگر — تطبیقِ واژه‌ایِ انگلیسی آن را نمی‌فهمد."""
    return any(char.isalpha() and not char.isascii() for char in text or "")


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _SUMMARY_TEXT_CHARS else text[: _SUMMARY_TEXT_CHARS - 1] + "…"


def catalog_summary(
    registry: tuple[SwaggerSource, ...],
    catalogs: dict[str, _Catalog],
    unavailable: list[str],
) -> str:
    """خلاصه‌ی فشرده‌ی منابعِ فعال برای پرامپت: هر tag با تعداد و چند نمونه‌عملیات.

    کلِ سندِ Swagger فرستاده نمی‌شود؛ فقط شناسه، پیشوند، tagها و نمونه‌ی
    «METHOD /path — summary». منبعِ بدونِ snapshot فقط با نام می‌آید.
    """
    blocks = []
    for source in registry:
        if not source.enabled:
            continue
        prefixes = ", ".join(source.path_prefixes) or "none"
        head = f"### source_id: {source.key} — {source.label} (path prefixes: {prefixes})"
        catalog = catalogs.get(source.key)
        if catalog is None:
            reason = "snapshot not available" if source.key in unavailable else "not searched"
            blocks.append(f"{head}\n({reason} — no operation list)")
            continue
        by_tag: dict[str, list[_Operation]] = {}
        for operation in catalog.operations:
            by_tag.setdefault(operation.tag or "untagged", []).append(operation)
        lines = [head]
        for tag, operations in by_tag.items():
            examples = "; ".join(
                f"{op.label}" + (f" — {_clip(op.summary)}" if op.summary else "")
                for op in operations[:_SUMMARY_EXAMPLES_PER_TAG]
            )
            lines.append(f"- {tag} ({len(operations)} operations): {examples}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _compact(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def _keyword_matches(keyword: str, operation: _Operation) -> bool:
    """کلیدواژه با قطعه‌ی کاملِ مسیر (``serviceSet``) یا واژه‌های ساختاری می‌خواند."""
    found = terms(keyword)
    return _compact(keyword) in operation.segments or bool(found and found <= operation.structural)


def _usable_keywords(keywords: tuple[str, ...], identity: set[str]) -> list[str]:
    """کلیدواژه‌هایی که چیزی درباره‌ی عملیات می‌گویند — نه نامِ منبع، نه واژه‌ی عمومی."""
    usable = []
    for keyword in keywords:
        compact = _compact(keyword)
        if len(compact) < 3 or compact in identity:
            continue
        compound = bool(re.search(r"[a-z][A-Z]|[-_]", keyword))
        if (terms(keyword) - identity) or compound:
            usable.append(keyword)
    return list(dict.fromkeys(usable))


def _verify_operation(
    claim: RequiredOperation,
    source: SwaggerSource,
    catalog: _Catalog,
    identity: set[str],
) -> str:
    """عملیاتِ واقعیِ snapshot که این ادعا را تأیید می‌کند — یا "".

    ``operation`` («GET /admin/voucher/{id}») باید عیناً در همان snapshot باشد؛
    مسیرِ ساختگی رد می‌شود و با کلیدواژه نجات داده نمی‌شود. بدونِ آن،
    دست‌کم min(2، تعدادِ کلیدواژه‌های مفید) کلیدواژه باید در یک عملیات بخوانند.
    """
    if claim.operation:
        method, _, path = claim.operation.strip().partition(" ")
        segments = _segments(path.strip())
        candidates = _relative_paths(segments, tuple(source.base_paths) + catalog.base_paths)
        for operation in catalog.operations:
            if operation.label.split(" ", 1)[0] != method.upper():
                continue
            if any(_template_matches(operation.segments, path) for path in candidates):
                return operation.label
        return ""

    keywords = _usable_keywords(claim.keywords, identity)
    if not keywords:
        keywords = _usable_keywords(tuple(_WORD_RE.findall(claim.description)), identity)
    if not keywords:
        return ""
    needed = min(2, len(keywords))
    best, best_count = "", 0
    for operation in catalog.operations:
        count = sum(1 for keyword in keywords if _keyword_matches(keyword, operation))
        if count > best_count:
            best, best_count = operation.label, count
    return best if best_count >= needed else ""


def _apply_semantics(
    analysis: SemanticAnalysis,
    registry: tuple[SwaggerSource, ...],
    catalogs: dict[str, _Catalog],
    unavailable: list[str],
    identity: set[str],
) -> tuple[list[Evidence], list[str], list[str]]:
    """(شواهدِ تأییدشده، هشدارها، منابعِ لازمِ بدونِ snapshot).

    هر شناسه‌ی منبع با رجیستری و هر عملیات با snapshotِ همان منبع سنجیده
    می‌شود. منبعِ «مرتبط» بدونِ عملیاتِ تأییدشده انتخاب نمی‌شود و confidenceِ
    مدل در تصمیم اثری ندارد.
    """
    names = {name: source for source in registry for name in source.identifiers()}
    evidence: list[Evidence] = []
    warnings: list[str] = []
    missing: list[str] = []
    for claim in analysis.required_operations:
        source = names.get(claim.source_id.strip().lower())
        what = claim.operation or claim.description or ", ".join(claim.keywords)
        if source is None:
            warnings.append(
                f"Task analysis named an unknown Swagger source '{claim.source_id}' "
                f"({what}); it was ignored."
            )
            continue
        if not source.enabled:
            warnings.append(
                f"Task analysis named the disabled source {source.label} ({what}); "
                "it was ignored."
            )
            continue
        catalog = catalogs.get(source.key)
        if catalog is None:
            if source.key in unavailable and source.key not in missing:
                missing.append(source.key)
            continue
        verified = _verify_operation(claim, source, catalog, identity)
        if not verified:
            warnings.append(
                f"Task analysis named an operation in {source.label} that is not in its "
                f"Swagger snapshot ({what}); it was ignored."
            )
            continue
        evidence.append(
            Evidence(source.key, EVIDENCE_SEMANTIC, f"{claim.description or what} → {verified}")
        )

    chosen = {item.source for item in evidence}
    related = []
    for item in analysis.relevant_sources:
        source = names.get(item.source_id.strip().lower())
        if source is not None and source.key not in chosen:
            related.append(source.label)
    if related:
        warnings.append(
            "Related but not selected (no verified operation is needed from them): "
            + ", ".join(dict.fromkeys(related))
            + "."
        )
    return evidence, warnings, missing


def select_sources(
    task_description: str,
    developed_services: str,
    *,
    manual: str = "",
    directory: Path | str | None = None,
    analyze: SemanticAnalyze | None = None,
) -> SourceSelection:
    """منابعِ Swaggerِ قدم ۲ را از ورودی‌های قدم ۱ تعیین می‌کند (بدونِ شبکه).

    ``manual`` انتخابِ دستیِ قدم ۲ است (``admin,crm``؛ خالی یا ``auto`` یعنی
    خودکار). ``directory`` پوشه‌ی snapshotهاست (پیش‌فرض: ``SNAPSHOT_DIR``).
    ``analyze`` تحلیل‌گرِ معنایی (LLM) است؛ بدونِ آن انتخاب کاملاً قطعی است.
    هیچ استثنایی بیرون نمی‌آید: هر مشکل در ``error`` برمی‌گردد.
    """
    registry = snapshots.SWAGGER_SOURCES
    text = f"{developed_services or ''}\n{task_description or ''}"
    urls = resolve_service_urls(text)

    try:
        manual_keys = expand_selection(manual)
    except SwaggerSnapshotError as exc:
        return SourceSelection(
            mode=MODE_MANUAL, urls=urls, error=f"{exc} Choose the sources again in Step 2."
        )
    mode = MODE_MANUAL if manual_keys else MODE_AUTO

    if urls.unresolved:
        return SourceSelection(mode=mode, urls=urls, error=urls.error())
    disabled = [(url, key) for url, key in urls.matched if not source_for(key).enabled]
    if disabled:
        listed = "\n".join(f"  - {url} → {_label(key)}" for url, key in disabled)
        return SourceSelection(
            mode=mode,
            urls=urls,
            error=(
                "These Swagger URLs point to a registered source that is disabled "
                f"(enabled=False in SWAGGER_SOURCES):\n{listed}\n\nEnable the source "
                "or remove the URL. Step 2 was not run."
            ),
        )

    warnings: list[str] = []
    catalogs: dict[str, _Catalog] = {}
    unavailable: list[str] = []
    for source in registry:
        if not source.enabled:
            continue
        catalog, problem = _load_catalog(source, directory)
        if catalog is None:
            unavailable.append(source.key)
            if problem:
                warnings.append(problem)
        else:
            catalogs[source.key] = catalog

    url_keys = urls.keys
    evidence = [Evidence(key, EVIDENCE_SWAGGER_URL, url) for url, key in urls.matched]
    endpoints: list[EndpointMatch] = []
    errors: list[str] = []
    for reference, path in endpoint_references(text):
        match, error, warning = _match_endpoint(reference, path, registry, catalogs, url_keys)
        endpoints.append(match)
        if error:
            errors.append(error)
        if warning:
            warnings.append(warning)
        for owner in match.owners:
            found = f" → {match.operations[0]}" if match.operations else " (path prefix)"
            evidence.append(Evidence(owner, EVIDENCE_ENDPOINT, f"{path}{found}"))

    result = SourceSelection(
        mode=mode,
        urls=urls,
        endpoints=tuple(endpoints),
        searched=_ordered(catalogs, registry),
        unavailable=_ordered(unavailable, registry),
    )

    if mode == MODE_MANUAL:
        return _manual(result, manual_keys, url_keys, warnings, directory)

    if errors:
        return _with(result, evidence=evidence, warnings=warnings, error="\n\n".join(errors))

    anchored = list(dict.fromkeys(url_keys + [o for m in endpoints for o in m.owners]))
    # نامِ منبع («admin») واژه‌ی جست‌وجو نیست؛ فقط وقتی متادیتای همان منبع هم
    # با تسک بخواند، دلیلی برای افزودنِ آن است (پایین).
    mentioned = terms(task_description)
    identities = {source.key: _identity_terms(source) for source in registry}
    identity = set().union(*identities.values())
    notes: list[str] = []

    # ── تحلیلِ معنایی: بدونِ شاهدِ صریح، یا وقتی متنِ تسک انگلیسی نیست ──
    semantic = SEMANTIC_NOT_NEEDED
    semantic_missing: list[str] = []
    chosen: list[str] = []
    if (task_description or "").strip() and (
        not anchored or _has_non_latin_text(task_description)
    ):
        if analyze is None:
            semantic = SEMANTIC_NOT_RUN
        else:
            try:
                analysis = analyze(
                    task_description, catalog_summary(registry, catalogs, unavailable)
                )
            except Exception as exc:  # noqa: BLE001 — شکستِ LLM حدس جایگزینش نمی‌کند
                semantic = SEMANTIC_FAILED
                failure = f"Task analysis failed: {type(exc).__name__}: {exc}"
                warnings.append(failure)
                notes.append(failure)
            else:
                semantic = SEMANTIC_USED
                found, semantic_warnings, semantic_missing = _apply_semantics(
                    analysis, registry, catalogs, unavailable, identity
                )
                warnings.extend(semantic_warnings)
                evidence.extend(found)
                chosen = [item.source for item in found if item.source not in anchored]
                if analysis.needs_clarification and analysis.clarification_reason:
                    note = f"Task analysis needs clarification: {analysis.clarification_reason}"
                    (warnings if chosen or anchored else notes).append(note)
                if semantic_missing and (chosen or anchored):
                    warnings.append(
                        "Task analysis suggests operations from "
                        f"{_labels(semantic_missing)}, whose snapshot is not available; "
                        "they could not be verified."
                    )

    # ── تطبیقِ قطعیِ واژه‌ها — وقتی تحلیلِ معنایی به کار نرفت ──
    if semantic != SEMANTIC_USED:
        task_terms = mentioned - identity
        scores = _metadata_scores(task_terms, catalogs)
        if not anchored and scores:
            best = max(score.score for score in scores.values())
            chosen = [
                key for key, score in scores.items() if score.score >= METADATA_RATIO * best
            ]

        # کارِ چندسرویسی: منبعی که مفهومی از تسک را دارد که منابعِ انتخاب‌شده
        # ندارند، یا منبعی که تسک نامش را می‌برد و عملیاتِ منطبق با تسک هم دارد.
        covered: set[str] = set()
        for key in anchored + chosen:
            if key in catalogs:
                covered |= catalogs[key].terms
        if all(key in catalogs for key in anchored):
            for key, score in sorted(scores.items(), key=lambda item: -item[1].score):
                if key in anchored or key in chosen:
                    continue
                uncovered = set(score.matched) - covered
                named = bool(identities[key] & mentioned)
                if named or (len(score.matched) >= WIDENING_MIN_TERMS and uncovered):
                    chosen.append(key)
                    covered |= catalogs[key].terms

        for key in chosen:
            score = scores[key]
            named = " (task names this service)" if identities[key] & mentioned else ""
            evidence.append(
                Evidence(
                    key,
                    EVIDENCE_METADATA,
                    f"task terms {', '.join(score.matched)} → {score.operation}{named}",
                )
            )

    result = replace(result, semantic=semantic)
    keys = _ordered(anchored + chosen, registry)
    if not keys:
        if semantic_missing:
            return _with(
                result,
                warnings=warnings,
                error=_missing_snapshot_error(
                    semantic_missing,
                    [Evidence(key, EVIDENCE_SEMANTIC, "(task analysis)") for key in semantic_missing],
                    directory,
                ),
            )
        return _with(
            result,
            warnings=warnings,
            error=_ambiguity_error(endpoints, list(catalogs), unavailable, notes),
        )

    missing = [key for key in keys if key not in catalogs]
    if missing:
        return _with(
            result,
            keys=keys,
            evidence=evidence,
            warnings=warnings,
            error=_missing_snapshot_error(missing, evidence, directory),
        )
    return _with(result, keys=keys, evidence=evidence, warnings=warnings)


def _with(selection: SourceSelection, **changes: Any) -> SourceSelection:
    for name in ("evidence", "warnings", "keys"):
        if name in changes:
            changes[name] = tuple(changes[name])
    return replace(selection, **changes)


def _manual(
    selection: SourceSelection,
    manual_keys: list[str],
    url_keys: list[str],
    warnings: list[str],
    directory: Path | str | None,
) -> SourceSelection:
    """انتخابِ دستی جایگزین نمی‌شود؛ فقط تعارض و پیش‌نیازها بررسی می‌شوند."""
    keys = tuple(manual_keys)
    evidence = [Evidence(key, EVIDENCE_MANUAL, "chosen in Step 2") for key in keys]

    missing_urls = [key for key in url_keys if key not in keys]
    if missing_urls:
        requested = ", ".join(
            f"{url} → {_label(key)}"
            for url, key in selection.urls.matched
            if key in missing_urls
        )
        return _with(
            selection,
            keys=keys,
            evidence=evidence,
            warnings=warnings,
            error=(
                "A Swagger URL in the Step 1 inputs points to a source that the manual "
                f"selection ({_labels(keys)}) does not read: {requested}. Include that "
                "source, switch Step 2 to Automatic, or remove the URL. Step 2 was not run."
            ),
        )

    for match in selection.endpoints:
        outside = [key for key in match.owners if key not in keys]
        if outside:
            warnings.append(
                f"The endpoint {match.path} belongs to {_labels(outside)}, which the "
                "manual selection does not read."
            )

    disabled = [key for key in keys if not source_for(key).enabled]
    if disabled:
        return _with(
            selection,
            keys=keys,
            evidence=evidence,
            warnings=warnings,
            error=(
                f"The manual selection includes disabled sources ({_labels(disabled)}). "
                "Choose enabled sources in Step 2."
            ),
        )
    missing = [key for key in keys if key not in selection.searched]
    if missing:
        return _with(
            selection,
            keys=keys,
            evidence=evidence,
            warnings=warnings,
            error=_missing_snapshot_error(missing, evidence, directory),
        )
    return _with(selection, keys=keys, evidence=evidence, warnings=warnings)


__all__ = [
    "EVIDENCE_ENDPOINT",
    "EVIDENCE_LABELS",
    "EVIDENCE_MANUAL",
    "EVIDENCE_METADATA",
    "EVIDENCE_SEMANTIC",
    "EVIDENCE_SWAGGER_URL",
    "METADATA_RATIO",
    "MODE_AUTO",
    "MODE_MANUAL",
    "SEMANTIC_FAILED",
    "SEMANTIC_NOT_NEEDED",
    "SEMANTIC_NOT_RUN",
    "SEMANTIC_USED",
    "SemanticAnalyze",
    "WIDENING_MIN_TERMS",
    "EndpointMatch",
    "Evidence",
    "catalog_summary",
    "SourceSelection",
    "endpoint_references",
    "normalize_path",
    "select_sources",
    "terms",
]
