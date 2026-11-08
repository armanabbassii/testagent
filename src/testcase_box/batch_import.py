"""
src/testcase_box/batch_import.py — پیش‌نمایش و واردکردنِ caseهای Batch به کتابخانه (M5)

    attempt = load_attempt(root, ref)
    previews = preview_cases(store, attempt)                       # بی‌نوشتن
    report = import_cases(
        store, root, ref,
        [ImportSelection("TC-001", previews[0].source.item_fingerprint, title=None)],
        expected_snapshot_fingerprint=attempt.snapshot_fingerprint,
    )

import منبع را دوباره از دیسک می‌خواند و همه‌ی بررسی‌ها را تکرار می‌کند؛ اگر
snapshot_fingerprint (هشِ همه‌ی artifactهای خوانده‌شده) با مقدارِ پیش‌نمایش فرق کند کلِ
انتخاب SOURCE_CHANGED رد می‌شود. هر case در تراکنشِ خودش با
BoxStore.create_case_for_source (هویت، بعد اثرانگشت) نوشته می‌شود و نتیجه‌ی هر case
ADDED، ALREADY_PRESENT، REJECTED یا FAILED است.

سیاست (M3/M4 ضعیف نمی‌شوند):
  * خطای شکلِ payload و LITERAL_CREDENTIAL → REJECTED (اعتبارنامه هرگز ذخیره نمی‌شود)؛
  * بقیه — حتی NOT_EXECUTABLE یا NEEDS_SCRIPT_REVIEW — همان‌طور که هست وارد می‌شود و
    وضعیتِ readiness فقط گزارش می‌شود؛ هیچ تعمیر، تأییدِ اسکریپت، حدسِ میزبان، آماده‌سازی
    یا اجرایی در کار نیست. «ready» ِ قدمِ ۵ تأییدِ اسکریپت نیست.

عنوان: عنوانِ اصلیِ case در artifact نیست (Batch فقط هشش را نگه می‌دارد)، پس
source_title همیشه null است. عنوانِ کتابخانه عنوانِ کاربر یا عنوانِ ساخته‌شده‌ی
«<issue> · <case>» است و title_origin (USER/GENERATED) آن را مشخص می‌کند.

وابستگی‌ها: depends_on/consumes/produces فقط در فراداده‌ی منشأ می‌مانند؛ هیچ رابطه‌ای ساخته
نمی‌شود. هر caseِ وابسته DEPENDENCIES_NOT_LINKED می‌گیرد — حتی اگر تولیدکننده در همین
انتخاب باشد — چون caseهای واردشده مدخل‌های مستقل‌اند، نه یک گروهِ مرتبِ قابلِ اجرا؛
متغیرِ تولیدکننده در آماده‌سازیِ M4 فقط از راهِ پروفایل در دسترس است.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.testcase_box.batch_source import AttemptRef, BatchAttempt, SourceCase, load_attempt
from src.testcase_box.contract import Readiness, safe_name
from src.testcase_box.models import (
    IMPORT_COMPLETED,
    IMPORT_FAILED,
    SCRIPTS_NOT_REQUIRED,
    SCRIPTS_PENDING,
    SOURCE_BATCH,
    NewCase,
    NewSource,
    script_fingerprint,
    validate_title,
)
from src.testcase_box.readiness import assess_payload, assess_version
from src.testcase_box.storage import (
    SOURCED_ALREADY_PRESENT,
    SOURCED_CREATED,
    BoxStore,
)

OUTCOME_ADDED = "ADDED"
OUTCOME_ALREADY_PRESENT = "ALREADY_PRESENT"
OUTCOME_REJECTED = "REJECTED"
OUTCOME_FAILED = "FAILED"
OUTCOMES = (OUTCOME_ADDED, OUTCOME_ALREADY_PRESENT, OUTCOME_REJECTED, OUTCOME_FAILED)

TITLE_GENERATED = "GENERATED"
TITLE_USER = "USER"

SOURCE_CHANGED = "SOURCE_CHANGED"
SOURCE_CASE_NOT_FOUND = "SOURCE_CASE_NOT_FOUND"
SELECTION_DUPLICATE = "SELECTION_DUPLICATE"
SELECTION_INVALID = "SELECTION_INVALID"
TITLE_INVALID = "TITLE_INVALID"
PAYLOAD_REJECTED = "SOURCE_PAYLOAD_REJECTED"
DEPENDENCIES_NOT_LINKED = "DEPENDENCIES_NOT_LINKED"

# یافته‌های M3 که case را ردِ واردکردن می‌کنند (خراب بودنِ منبع یا اعتبارنامه‌ی literal)
REJECTING_FINDINGS = frozenset(
    {
        "PAYLOAD_NOT_OBJECT",
        "PAYLOAD_IS_COLLECTION",
        "PAYLOAD_IS_FOLDER",
        "REQUEST_MISSING",
        "REQUEST_NOT_OBJECT",
        "LITERAL_CREDENTIAL",
    }
)


@dataclass(frozen=True)
class DependencyNote:
    """یک تولیدکننده‌ی لازم — فقط برای اطلاع؛ بودنش در انتخاب یا کتابخانه ترتیبی نمی‌سازد."""

    case_id: str
    in_selection: bool
    in_library: bool


@dataclass(frozen=True)
class CasePreview:
    source: SourceCase
    readiness: Readiness
    importable: bool
    rejection_codes: tuple[str, ...]
    warnings: tuple[str, ...]
    dependencies: tuple[DependencyNote, ...]
    existing_case_ids: tuple[int, ...]


@dataclass(frozen=True)
class ImportSelection:
    case_id: str
    expected_item_fingerprint: str
    title: str | None = None


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    source_reference: str | None
    outcome: str
    codes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    box_case_id: int | None = None
    box_version_id: int | None = None
    archived: bool = False
    readiness_status: str | None = None


@dataclass(frozen=True)
class ImportReport:
    job_id: int
    attempt_reference: str
    snapshot_fingerprint: str | None
    outcomes: tuple[CaseOutcome, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {name: sum(o.outcome == name for o in self.outcomes) for name in OUTCOMES}


# ── پیش‌نمایش ───────────────────────────────────────────────────────────────
def _readiness(source: SourceCase) -> Readiness:
    """readiness ِ همان نسخه‌ای که ساخته می‌شود: بی‌اسکریپت NOT_REQUIRED، وگرنه PENDING."""
    fingerprint = script_fingerprint(source.item)
    return assess_payload(
        source.item,
        stored_script_fingerprint=fingerprint,
        approval_state=SCRIPTS_NOT_REQUIRED if fingerprint is None else SCRIPTS_PENDING,
    )


def _rejections(source: SourceCase, readiness: Readiness) -> tuple[str, ...]:
    codes = {problem.code for problem in source.problems}
    blocked = sorted({f.code for f in readiness.findings} & REJECTING_FINDINGS)
    if blocked:
        codes |= {PAYLOAD_REJECTED, *blocked}
    return tuple(sorted(codes))


def _producers(source: SourceCase) -> list[str]:
    metadata = source.metadata
    producers = set(metadata.get("depends_on") or [])
    for entry in metadata.get("consumes") or []:
        producers |= set(entry.get("producers") or [])
    producers.discard(source.case_id)
    return sorted(producers)


def _dependent(source: SourceCase) -> bool:
    return bool(source.metadata.get("depends_on") or source.metadata.get("consumes"))


def _existing(store: BoxStore | None, reference: str | None) -> tuple[int, ...]:
    if store is None or reference is None:
        return ()
    return tuple(sorted({row.case_id for row in store.find_sources(SOURCE_BATCH, reference)}))


def preview_cases(
    store: BoxStore | None, attempt: BatchAttempt, case_ids=None
) -> tuple[CasePreview, ...]:
    """بی‌نوشتن. case_ids=None یعنی همه؛ همین مجموعه «انتخاب» برای DependencyNote است."""
    if isinstance(case_ids, str):
        raise TypeError("case_ids must be a collection of case ids.")
    if not attempt.eligible:
        return ()
    selected = (
        [case for case in attempt.cases if case.case_id in set(case_ids)]
        if case_ids is not None
        else list(attempt.cases)
    )
    chosen = {case.case_id for case in selected}
    previews = []
    for source in selected:
        readiness = _readiness(source)
        rejections = _rejections(source, readiness)
        notes = tuple(
            DependencyNote(
                producer,
                producer in chosen,
                bool(_existing(store, attempt.ref.case_reference(producer))),
            )
            for producer in _producers(source)
        )
        previews.append(
            CasePreview(
                source=source,
                readiness=readiness,
                importable=not rejections,
                rejection_codes=rejections,
                warnings=(DEPENDENCIES_NOT_LINKED,) if _dependent(source) else (),
                dependencies=notes,
                existing_case_ids=_existing(store, source.source_reference),
            )
        )
    return tuple(previews)


# ── واردکردن ────────────────────────────────────────────────────────────────
def import_cases(
    store: BoxStore,
    root: Path | str,
    ref: AttemptRef,
    selections,
    *,
    expected_snapshot_fingerprint: str,
    created_by: str | None = None,
) -> ImportReport:
    if not isinstance(ref, AttemptRef):
        raise TypeError("ref must be an AttemptRef.")
    selections = tuple(selections)
    job = store.create_import_job(
        SOURCE_BATCH,
        ref.reference,
        {"expected_snapshot_fingerprint": str(expected_snapshot_fingerprint)},
    )
    outcomes: list[CaseOutcome] = []
    snapshot = None
    try:
        attempt = load_attempt(root, ref)
        snapshot = attempt.snapshot_fingerprint
        if not attempt.eligible:
            codes = tuple(sorted({problem.code for problem in attempt.problems}))
            outcomes = [_rejected(selection, None, codes) for selection in selections]
        elif snapshot != expected_snapshot_fingerprint:
            outcomes = [_rejected(selection, None, (SOURCE_CHANGED,)) for selection in selections]
        else:
            seen: set[str] = set()
            for selection in selections:
                outcomes.append(_import_one(store, attempt, selection, seen, job.id, created_by))
    except BaseException as exc:
        try:
            store.finish_import_job(job.id, IMPORT_FAILED, _summary(ref, snapshot, outcomes, True))
        except Exception as finish_error:  # noqa: BLE001 — خطای اصلی گم نمی‌شود
            exc.add_note(f"The import job could not be closed ({type(finish_error).__name__}).")
        raise
    store.finish_import_job(job.id, IMPORT_COMPLETED, _summary(ref, snapshot, outcomes, False))
    return ImportReport(job.id, ref.reference, snapshot, tuple(outcomes))


def _rejected(selection: Any, source: SourceCase | None, codes, warnings=()) -> CaseOutcome:
    case_id = selection.case_id if isinstance(selection, ImportSelection) else None
    return CaseOutcome(
        case_id=case_id if isinstance(case_id, str) else "",
        source_reference=source.source_reference if source else None,
        outcome=OUTCOME_REJECTED,
        codes=tuple(codes),
        warnings=tuple(warnings),
    )


def _import_one(
    store: BoxStore,
    attempt: BatchAttempt,
    selection: Any,
    seen: set[str],
    job_id: int,
    created_by: str | None,
) -> CaseOutcome:
    if not isinstance(selection, ImportSelection) or not isinstance(selection.case_id, str):
        return _rejected(selection, None, (SELECTION_INVALID,))
    if selection.case_id in seen:
        return _rejected(selection, None, (SELECTION_DUPLICATE,))
    seen.add(selection.case_id)
    source = attempt.case(selection.case_id)
    if source is None:
        return _rejected(selection, None, (SOURCE_CASE_NOT_FOUND,))
    warnings = (DEPENDENCIES_NOT_LINKED,) if _dependent(source) else ()
    if selection.expected_item_fingerprint != source.item_fingerprint:
        return _rejected(selection, source, (SOURCE_CHANGED,), warnings)
    readiness = _readiness(source)
    rejections = _rejections(source, readiness)
    if rejections:
        return _rejected(selection, source, rejections, warnings)
    title, origin = selection.title, TITLE_USER
    if title is None:
        title, origin = source.default_title, TITLE_GENERATED
    try:
        validate_title(title)
    except ValueError:
        return _rejected(selection, source, (TITLE_INVALID,), warnings)

    metadata = {
        **source.metadata,
        "title_origin": origin,
        "import_job_id": job_id,
        "readiness_status_at_import": readiness.status,
    }
    try:
        result = store.create_case_for_source(
            NewCase(
                title=title,
                payload=source.item,
                sources=(NewSource(SOURCE_BATCH, source.source_reference, metadata),),
                created_by=created_by,
            )
        )
    except Exception as exc:  # noqa: BLE001 — همان case rollback شد؛ بقیه ادامه می‌دهند
        return CaseOutcome(
            source.case_id,
            source.source_reference,
            OUTCOME_FAILED,
            codes=(type(exc).__name__,),
            warnings=warnings,
        )
    if result.outcome == SOURCED_CREATED:
        version_id = result.case.current_version_id
        return CaseOutcome(
            source.case_id,
            source.source_reference,
            OUTCOME_ADDED,
            warnings=warnings,
            box_case_id=result.case.id,
            box_version_id=version_id,
            readiness_status=readiness.status,
        )
    if result.outcome == SOURCED_ALREADY_PRESENT:
        version_id = result.existing_sources[0].case_version_id
        return CaseOutcome(
            source.case_id,
            source.source_reference,
            OUTCOME_ALREADY_PRESENT,
            warnings=warnings,
            box_case_id=result.case.id,
            box_version_id=version_id,
            archived=result.case.archived,
            readiness_status=assess_version(store, version_id).status,
        )
    return _rejected(selection, source, (result.outcome,), warnings)


def _summary(ref: AttemptRef, snapshot, outcomes: list[CaseOutcome], failed: bool) -> dict:
    """فقط کد، شمارش و شناسه — case_id ِ ورودیِ کاربر با safe_name."""
    return {
        "attempt": ref.reference,
        "snapshot_fingerprint": snapshot,
        "interrupted": failed,
        "counts": {name: sum(o.outcome == name for o in outcomes) for name in OUTCOMES},
        "outcomes": [
            {
                "case_id": safe_name(outcome.case_id),
                "outcome": outcome.outcome,
                "codes": list(outcome.codes),
                "warnings": list(outcome.warnings),
                "box_case_id": outcome.box_case_id,
            }
            for outcome in outcomes
        ],
    }
