"""
environment_decisions.py — مرحله‌ی A: سنجشِ تصمیم‌های محیطِ استخراج‌شده در قدم ۱

مدل فقط جمله‌های صریحِ «این سرویس در VDI / در محیطِ عادی اجرا یا تست شود» را با
منبعشان گزارش می‌کند (فیلدِ اختیاریِ environment_decisions). این‌جا هر تصمیم
جداگانه سنجیده می‌شود:

  * scope ∈ {task, service}، environment ∈ {vdi, normal}، شاهد اجباری؛
  * شاهد باید عیناً در منبعِ گفته‌شده باشد (Description، یک Comment یا متنِ یک
    پیوست). مقایسه روی شکلِ نرمال‌شده است (v2: NFKC، فاصله‌ها یکی، ي/ك فارسی،
    حذفِ ZWNJ/کشیده و نشانه‌های نامرئیِ جهت، و فاصله/نیم‌فاصله/چسبیده‌ی برابر فقط
    در مرزِ «می/نمی» و «ها/های/تر/ترین» میانِ حروفِ فارسی). حروفِ لاتین به
    بزرگی/کوچکی حساس‌اند و املا نرمال نمی‌شود. شاهدِ ذخیره‌شده همان برشِ دقیقِ متنِ
    منبع است، نه رونوشتِ مدل؛
  * ارجاعِ غایب یا اشتباه: اگر شاهد فقط در یک منبع از همان نوع باشد، با هشدارِ
    REFERENCE_CORRECTED پذیرفته می‌شود؛ در چند منبع → AMBIGUOUS_SOURCE؛ در هیچ‌جا
    → EVIDENCE_NOT_FOUND.

تصمیمِ نامعتبر هرگز کلِ قدم ۱ را نمی‌شکند و دور هم ریخته نمی‌شود: با دلیل و هر
شاهد/منبعی که پیدا شد در rejected_environment_decisions می‌ماند تا مرحله‌ی B
تست‌کیس‌های متأثر را NEEDS_REVIEW کند.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any

from src.agents.test_case_generator.host_selection import (
    ENVIRONMENT_NORMAL,
    ENVIRONMENT_VDI,
    SCOPE_SERVICE,
    SCOPE_TASK,
    UNKNOWN_MARKER,
)
from src.task_sources.sources import (
    SOURCE_COMMENT,
    SOURCE_DESCRIPTION,
    SOURCE_TYPES,
    TaskSource,
)

STATUS_ACCEPTED = "ACCEPTED"
STATUS_REJECTED = "REJECTED"

REJECT_MALFORMED = "MALFORMED"
REJECT_INVALID_SCOPE = "INVALID_SCOPE"
REJECT_INVALID_ENVIRONMENT = "INVALID_ENVIRONMENT"
REJECT_MISSING_SERVICE = "MISSING_SERVICE"
REJECT_MISSING_EVIDENCE = "MISSING_EVIDENCE"
REJECT_UNKNOWN_SOURCE_TYPE = "UNKNOWN_SOURCE_TYPE"
REJECT_EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"
REJECT_AMBIGUOUS_SOURCE = "AMBIGUOUS_SOURCE"

WARNING_REFERENCE_CORRECTED = "REFERENCE_CORRECTED"

NORMALIZATION_VERSION = "v2"
# نسخه‌هایی که در نتیجه‌های ذخیره‌شده دیده می‌شوند؛ v1 دوباره سنجیده نمی‌شود.
NORMALIZATION_VERSIONS = ("v1", "v2")

# فیلدهایی که قدم ۱ اضافه می‌کند — هیچ‌کدام واردِ پرامپتِ قدم ۳ نمی‌شود.
ENVIRONMENT_FIELDS = (
    "environment_decisions",
    "rejected_environment_decisions",
    "environment_sources",
    "environment_warnings",
)

_REPLACEMENTS = {"ي": "ی", "ك": "ک"}
_ZWNJ = "‌"
# کشیده، ZWJ، LRM/RLM و BOM — فقط برای مقایسه حذف می‌شوند. ZWNJ جدا (یک «فاصله‌ی
# درون‌واژه‌ای») رفتار می‌شود؛ بخشِ نرمال‌سازی را ببینید.
_DROPPED = frozenset("ـ‍‎‏﻿")
# مرزهای تکواژیِ فارسی که فاصله، نیم‌فاصله یا چسبیده نوشته می‌شوند (v2).
_JOINING_PREFIXES = frozenset({"می", "نمی"})
_JOINING_SUFFIXES = frozenset(
    {
        "ها",
        "های",
        "هایی",
        "هایم",
        "هایت",
        "هایش",
        "هایمان",
        "هایتان",
        "هایشان",
        "تر",
        "ترین",
    }
)
_VDI_MENTION_RE = re.compile(r"\bvdi\b|وی ?دی ?آی", re.IGNORECASE)
_COMMENT_NUMBER_RE = re.compile(r"^(?:comment\s*)?#?\s*(\d+)$", re.IGNORECASE)


# ── نرمال‌سازی ───────────────────────────────────────────────────────────────


def _persian_letter(char: str) -> bool:
    return char.isalpha() and "؀" <= char <= "ۿ"


def _units(text: str) -> list[tuple[str, int, bool]]:
    """گذرِ ۱: (نویسه، جایگاه، False) یا (""، جایگاهِ شروعِ فاصله، فاصله‌ی معمولی دارد؟).

    هر دنباله‌ی پیوسته‌ی فاصله و ZWNJ یک «فاصله» است؛ فاصله‌ی آغاز و پایان حذف می‌شود.
    """
    units: list[tuple[str, int, bool]] = []
    gap_at, gap_space = -1, False
    for index, char in enumerate(text):
        for part in unicodedata.normalize("NFKC", char):
            part = _REPLACEMENTS.get(part, part)
            if part in _DROPPED:
                continue
            if part.isspace() or part == _ZWNJ:
                if units:
                    gap_at = index if gap_at < 0 else gap_at
                    gap_space = gap_space or part.isspace()
                continue
            if gap_at >= 0:
                units.append(("", gap_at, gap_space))
                gap_at, gap_space = -1, False
            units.append((part, index, False))
    return units


def _word(units: list[tuple[str, int, bool]], start: int, step: int) -> str:
    """واژه‌ی فارسیِ چسبیده به فاصله‌ی units[start - step]، در جهتِ step.

    فاصله‌ی فقط-ZWNJ درونِ واژه حساب می‌شود (همان‌طور که v1 آن را حذف می‌کرد)، پس
    دو متنی که در v1 یکی بودند این‌جا هم واژه‌های یکسان دارند.
    """
    letters: list[str] = []
    index = start
    while 0 <= index < len(units):
        char, _, has_space = units[index]
        if char:
            if not _persian_letter(char):
                break
            letters.append(char)
        elif has_space:
            break
        index += step
    return "".join(letters if step > 0 else reversed(letters))


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """متنِ نرمال‌شده و، برای هر نویسه‌اش، جایگاهِ همان نویسه در متنِ اصلی.

    v2 — گذرِ ۲، تصمیم برای هر فاصله:
      * فقط ZWNJ → حذف (همان v1)؛
      * فاصله‌ی معمولی بینِ دو حرفِ فارسی، وقتی واژه‌ی چپ پیشوندِ «می/نمی» یا واژه‌ی
        راست پسوندِ «ها/های/…/تر/ترین» است → حذف («تست ها» = «تست‌ها» = «تستها»)؛
      * در غیرِ این صورت یک فاصله (همان v1).
    حروف (از جمله حروفِ لاتین با همان بزرگی/کوچکی) و نشانه‌ها دست نمی‌خورند.
    """
    units = _units(text)
    chars: list[str] = []
    positions: list[int] = []
    for index, (char, position, has_space) in enumerate(units):
        if char:
            chars.append(char)
            positions.append(position)
            continue
        if not has_space:
            continue
        left, right = units[index - 1][0], units[index + 1][0]
        if (
            _persian_letter(left)
            and _persian_letter(right)
            and (
                _word(units, index - 1, -1) in _JOINING_PREFIXES
                or _word(units, index + 1, 1) in _JOINING_SUFFIXES
            )
        ):
            continue
        chars.append(" ")
        positions.append(position)
    return "".join(chars), positions


def normalize_for_match(text: str) -> str:
    return _normalize_with_map(text or "")[0]


def _find(evidence: str, source: TaskSource) -> tuple[int, int] | None:
    """برشِ [شروع، پایان) از متنِ اصلیِ منبع که شکلِ نرمالش همان شاهد است."""
    needle = normalize_for_match(evidence)
    if not needle:
        return None
    haystack, positions = _normalize_with_map(source.text)
    found = haystack.find(needle)
    if found < 0:
        return None
    span = positions[found], positions[found + len(needle) - 1] + 1
    # برشِ اصلی باید خودش دقیقاً به همان شاهد نرمال شود؛ وگرنه حدس نمی‌زنیم.
    if normalize_for_match(source.text[span[0] : span[1]]) != needle:
        return None
    return span


# ── پیدا کردنِ منبع ──────────────────────────────────────────────────────────


def _named(candidates: list[TaskSource], source_type: str, reference: str) -> TaskSource | None:
    """منبعی که مدل نام برده — فقط اگر دقیقاً یکی باشد."""
    if source_type == SOURCE_DESCRIPTION:
        return candidates[0] if len(candidates) == 1 else None
    wanted = reference.strip().lower()
    if not wanted:
        return None
    number = _COMMENT_NUMBER_RE.match(wanted)
    matches = [
        source
        for source in candidates
        if wanted in (source.label.lower(), source.reference.lower())
        or (
            source_type == SOURCE_COMMENT
            and number
            and source.label.lower() == f"comment {number.group(1)}"
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _locate(
    evidence: str, source_type: str, reference: str, sources: tuple[TaskSource, ...]
) -> tuple[TaskSource | None, tuple[int, int] | None, str, str]:
    """(منبع، برش، هشدار، کدِ رد) — دقیقاً یکی از «منبع» یا «کدِ رد» پر است."""
    candidates = [source for source in sources if source.source_type == source_type]
    cross_type = not candidates
    if cross_type:
        # متنِ دستی فقط یک description دارد؛ «comment»ِ مدل همان متن است.
        candidates = list(sources)

    named = None if cross_type else _named(candidates, source_type, reference)
    if named is not None:
        span = _find(evidence, named)
        if span is not None:
            return named, span, "", ""

    found = [
        (source, span) for source in candidates if (span := _find(evidence, source)) is not None
    ]
    if len(found) == 1:
        source, span = found[0]
        stated = f"{source_type} {reference!r}".strip() if reference else source_type
        return (
            source,
            span,
            f"{WARNING_REFERENCE_CORRECTED}: the model cited {stated}, but the sentence "
            f"was found only in {source.label}.",
            "",
        )
    if len(found) > 1:
        labels = ", ".join(source.label for source, _ in found)
        return None, None, "", f"{REJECT_AMBIGUOUS_SOURCE}: the sentence appears in {labels}"
    return (
        None,
        None,
        "",
        f"{REJECT_EVIDENCE_NOT_FOUND}: the sentence was not found in any {source_type} source",
    )


# ── سنجشِ هر تصمیم ──────────────────────────────────────────────────────────


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _service(value: object) -> dict[str, str]:
    value = value if isinstance(value, dict) else {}
    cleaned = {}
    for key in ("method", "path", "name"):
        text = _clean(value.get(key))
        cleaned[key] = "" if text.lower() == UNKNOWN_MARKER else text
    cleaned["method"] = cleaned["method"].upper()
    return cleaned


def _hints(value: object, known: set[str], warnings: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        warnings.append("'test_case_ids' is not an array and was ignored.")
        return []
    hints: list[str] = []
    for item in value:
        case_id = _clean(item)
        if case_id in known and case_id not in hints:
            hints.append(case_id)
        elif case_id:
            warnings.append(f"Unknown test case id {case_id!r} in 'test_case_ids' was ignored.")
    return hints


def _judge(entry: Any, sources: tuple[TaskSource, ...], known: set[str]) -> dict[str, Any]:
    """یک تصمیمِ خام → رکوردِ ACCEPTED یا REJECTED (بدونِ شناسه)."""
    warnings: list[str] = []
    if not isinstance(entry, dict):
        return {
            "status": STATUS_REJECTED,
            "scope": "",
            "environment": "",
            "service": _service(None),
            "test_case_ids": [],
            "evidence": None,
            "source": None,
            "warnings": warnings,
            "rejection": {
                "code": REJECT_MALFORMED,
                "message": f"the entry is a {type(entry).__name__}, not an object",
            },
            "raw": copy.deepcopy(entry),
        }

    scope = _clean(entry.get("scope")).lower()
    environment = _clean(entry.get("environment")).lower()
    evidence_text = entry.get("evidence") if isinstance(entry.get("evidence"), str) else ""
    source_type = _clean(entry.get("source_type")).lower()
    reference = _clean(entry.get("source_reference"))
    service = _service(entry.get("service"))
    hints = _hints(entry.get("test_case_ids"), known, warnings)

    problems: list[tuple[str, str]] = []
    if scope not in (SCOPE_TASK, SCOPE_SERVICE):
        problems.append(
            (REJECT_INVALID_SCOPE, f"scope {entry.get('scope')!r} is not 'task' or 'service'")
        )
    if environment not in (ENVIRONMENT_VDI, ENVIRONMENT_NORMAL):
        problems.append(
            (
                REJECT_INVALID_ENVIRONMENT,
                f"environment {entry.get('environment')!r} is not 'vdi' or 'normal'",
            )
        )
    if not evidence_text.strip():
        problems.append((REJECT_MISSING_EVIDENCE, "no evidence sentence was given"))
    if scope == SCOPE_SERVICE and not (service["path"] or service["name"] or hints):
        problems.append((REJECT_MISSING_SERVICE, "a service-level statement names no service"))
    if source_type not in SOURCE_TYPES:
        problems.append(
            (
                REJECT_UNKNOWN_SOURCE_TYPE,
                f"source_type {entry.get('source_type')!r} is not description/comment/attachment",
            )
        )

    evidence = None
    source_record = None
    if evidence_text.strip() and source_type in SOURCE_TYPES:
        source, span, warning, failure = _locate(evidence_text, source_type, reference, sources)
        if source is not None and span is not None:
            original = source.text[span[0] : span[1]]
            evidence = {
                "original": original,
                "as_returned": evidence_text,
                "normalized": normalize_for_match(original),
                "span": [span[0], span[1]],
                "normalization": NORMALIZATION_VERSION,
            }
            source_record = {
                "type": source.source_type,
                "reference": source.reference,
                "label": source.label,
                "sha256": source.sha256,
            }
            if warning:
                warnings.append(warning)
        else:
            code, _, message = failure.partition(": ")
            problems.append((code, message))
    if evidence is None and evidence_text.strip():
        evidence = {
            "original": "",
            "as_returned": evidence_text,
            "normalized": normalize_for_match(evidence_text),
            "span": None,
            "normalization": NORMALIZATION_VERSION,
        }
    if source_record is None and (source_type or reference):
        source_record = {"type": source_type, "reference": reference, "label": "", "sha256": ""}

    record: dict[str, Any] = {
        "status": STATUS_REJECTED if problems else STATUS_ACCEPTED,
        "scope": scope,
        "environment": environment,
        "service": service,
        "test_case_ids": hints,
        "evidence": evidence,
        "source": source_record,
        "warnings": warnings,
    }
    if problems:
        record["rejection"] = {
            "code": problems[0][0],
            "message": "; ".join(text for _, text in problems),
        }
        record["raw"] = copy.deepcopy(entry)
    return record


def extract_environment_decisions(
    raw: Any, sources: tuple[TaskSource, ...], known_case_ids: list[str]
) -> dict[str, Any]:
    """فیلدهای محیطِ نتیجه‌ی قدم ۱ را از خروجیِ خامِ مدل می‌سازد.

    غایب/null/[] یعنی «هیچ تصمیمی» — رفتارِ قبلی و base_url برای همه.
    """
    known = set(known_case_ids)
    entries: list[Any]
    if raw is None:
        entries = []
    elif isinstance(raw, list):
        entries = raw
    else:
        entries = [raw]

    # ترتیبِ منبع: description → کامنت‌ها (قدیمی اول) → پیوست‌ها؛ سپس جای شاهد.
    labels = [source.label for source in sources]
    judged = []
    for index, entry in enumerate(entries):
        record = _judge(entry, sources, known)
        position, start = len(sources), 0
        span = (record["evidence"] or {}).get("span")
        if span and record["source"]["label"] in labels:
            position, start = labels.index(record["source"]["label"]), span[0]
        judged.append(((position, start, index), record))
    judged.sort(key=lambda item: item[0])

    accepted: list[dict] = []
    rejected: list[dict] = []
    for number, (_, record) in enumerate(judged, start=1):
        record = {"id": f"ENV-{number}", **record}
        (accepted if record["status"] == STATUS_ACCEPTED else rejected).append(record)

    warnings = [
        f"{record['id']}: {warning}"
        for record in accepted + rejected
        for warning in record["warnings"]
    ]
    uncovered = _uncovered_vdi_mentions(sources, accepted + rejected)
    if uncovered:
        warnings.append(
            "The task mentions VDI in "
            + ", ".join(uncovered)
            + ", but no environment decision covers that text — check whether a service "
            "must run in VDI. Informational only; hosts were not changed."
        )

    return {
        "environment_decisions": accepted,
        "rejected_environment_decisions": rejected,
        "environment_sources": [source.summary() for source in sources],
        "environment_warnings": warnings,
    }


def _find_all(evidence: str, source: TaskSource) -> list[tuple[int, int]]:
    """همه‌ی برش‌های متنِ اصلیِ منبع که شکلِ نرمالشان همان جمله است."""
    needle = normalize_for_match(evidence)
    if not needle:
        return []
    haystack, positions = _normalize_with_map(source.text)
    spans = []
    found = haystack.find(needle)
    while found >= 0:
        spans.append((positions[found], positions[found + len(needle) - 1] + 1))
        found = haystack.find(needle, found + 1)
    return spans


def _uncovered_vdi_mentions(sources: tuple[TaskSource, ...], records: list[dict]) -> list[str]:
    """منابعی که دست‌کم یک ذکرِ VDIِ بیرون از شاهدِ هر تصمیم (پذیرفته یا ردشده) دارند.

    ذکری که داخلِ جمله‌ی شاهدِ یک تصمیم است (مثلاً «normal (non-VDI)» در یک تصمیمِ
    normal، یا جمله‌ی یک تصمیمِ VDI) پوشش داده شده است. فقط بر اساسِ جایگاه
    است، نه کلیدواژه: هر ذکرِ دیگری در همان منبع همچنان هشدار می‌دهد.
    """
    covered: dict[str, list[tuple[int, int]]] = {}
    for record in records:
        evidence = record.get("evidence") or {}
        label = (record.get("source") or {}).get("label") or ""
        if evidence.get("span") and label:
            covered.setdefault(label, []).append(tuple(evidence["span"]))
        elif evidence.get("as_returned"):
            # شاهدِ پیدانشده در یک منبعِ مشخص (مثلاً AMBIGUOUS_SOURCE): هر جای آن جمله.
            for source in sources:
                covered.setdefault(source.label, []).extend(
                    _find_all(evidence["as_returned"], source)
                )

    uncovered: list[str] = []
    for source in sources:
        normalized, positions = _normalize_with_map(source.text)
        spans = covered.get(source.label, [])
        for match in _VDI_MENTION_RE.finditer(normalized):
            start = positions[match.start()]
            if not any(begin <= start < end for begin, end in spans):
                uncovered.append(source.label)
                break
    return uncovered


def without_environment_fields(step1_result: Any) -> Any:
    """همان نتیجه‌ی قدم ۱ بدونِ فیلدهای محیط — ورودیِ پرامپتِ قدم ۳ عوض نمی‌شود."""
    if not isinstance(step1_result, dict):
        return step1_result
    return {key: value for key, value in step1_result.items() if key not in ENVIRONMENT_FIELDS}


__all__ = [
    "ENVIRONMENT_FIELDS",
    "NORMALIZATION_VERSION",
    "NORMALIZATION_VERSIONS",
    "REJECT_AMBIGUOUS_SOURCE",
    "REJECT_EVIDENCE_NOT_FOUND",
    "REJECT_INVALID_ENVIRONMENT",
    "REJECT_INVALID_SCOPE",
    "REJECT_MALFORMED",
    "REJECT_MISSING_EVIDENCE",
    "REJECT_MISSING_SERVICE",
    "REJECT_UNKNOWN_SOURCE_TYPE",
    "STATUS_ACCEPTED",
    "STATUS_REJECTED",
    "WARNING_REFERENCE_CORRECTED",
    "extract_environment_decisions",
    "normalize_for_match",
    "without_environment_fields",
]
