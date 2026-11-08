"""
src/testcase_box/run_views.py — مدلِ نمایشیِ فقط‌خواندنیِ runها برای UI (M7.7)

    summary = run_summary(store, run)          # شمارش‌ها، پیشرفت، unitِ جاری، دلیلِ توقف/بازیابی
    rows = unit_rows(store, run.id)            # یک ردیف برای هر unit با verification
    files = run_artifacts(artifact_root, store, run)   # فقط فایل‌های امن و واقعاً موجود

فقط از storage می‌خواند و هرگز چیزی نمی‌نویسد؛ پیشرفت فقط از وضعیت‌های ثبت‌شده می‌آید (هیچ تخمینِ
زمانی). artifactها فقط با نامِ ثابت از فهرستِ مجاز و فقط داخلِ پوشه‌ی همان run نشان داده می‌شوند —
هیچ مسیری از ورودیِ کاربر ساخته نمی‌شود؛ فایلِ ناموجود «در دسترس نیست» است، نه پیوندِ ساختگی.
توکن (دسترسی یا مالکیت) و مقدارِ متغیر در هیچ رکوردِ این ماژول نیست.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from src.testcase_box.db import NotFoundError
from src.testcase_box.models import (
    RUN_FINAL,
    UNIT_CANCELLED,
    UNIT_ERROR,
    UNIT_FAIL,
    UNIT_NOT_RUN,
    UNIT_PASS,
    UNIT_QUEUED,
    UNIT_RUNNING,
    BoxRun,
    case_key,
)
from src.testcase_box.storage import BoxStore

VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
RESULT_VERIFIED_PASS = "VERIFIED_PASS"
VERIFICATION_TEXT = {
    "VERIFIED": "Verified by assertions.",
    "UNVERIFIED": "Request execution completed, but the response was not verified by assertions.",
    "FAILED": "An assertion failed.",
    "NOT_VERIFIED": "No verification result (the case did not complete normally).",
}
_RUN_DIR_RE = re.compile(r"^run_\d{6}_\d{8}T\d{6}Z_[0-9a-f]{8}$")
_UNIT_DIR_RE = re.compile(r"^unit_\d{3}$")
# نام‌هایی که موتور پس از پاک‌سازی منتشر می‌کند + کالکشنِ همان unit
SAFE_ARTIFACT_NAMES = frozenset(
    {
        "collection.json",
        "result.json",
        "newman-report.json",
        "newman-report.html",
        "stdout.log",
        "stderr.log",
    }
)


@dataclass(frozen=True)
class UnitRow:
    unit_number: int
    case_id: int
    case_key: str
    case_version_id: int
    version_number: int | None
    status: str
    case_status: str
    classification: str
    verification: str
    http_status: tuple[int, ...]

    # یک طبقه‌بندی برای ردیف و برای شمارش‌ها (R1): فقط PASS با verification=VERIFIED تأییدشده
    # است؛ هر PASS ِ دیگری — بی‌assertion یا بی‌فراداده‌ی verification — UNVERIFIED است.
    @property
    def verified_pass(self) -> bool:
        return self.status == UNIT_PASS and self.verification == VERIFIED

    @property
    def unverified_pass(self) -> bool:
        return self.status == UNIT_PASS and self.verification != VERIFIED

    @property
    def result(self) -> str:
        """VERIFIED_PASS | UNVERIFIED | وضعیتِ خودِ unit (FAIL، ERROR، NOT_RUN، …)."""
        if self.verified_pass:
            return RESULT_VERIFIED_PASS
        if self.unverified_pass:
            return UNVERIFIED
        return self.status


@dataclass(frozen=True)
class RunSummary:
    run: BoxRun
    total: int
    done: int
    remaining: int
    counts: dict[str, int]
    verified: int
    unverified: int
    current_unit: int | None
    stop_reason: str
    recovery_reason: str
    retry_of_run_id: int | None
    blocked: int = 0
    classifications: dict[str, int] = field(default_factory=dict)

    @property
    def progress(self) -> float:
        return self.done / self.total if self.total else 1.0

    @property
    def final(self) -> bool:
        return self.run.status in RUN_FINAL


def unit_rows(store: BoxStore, run_id: int) -> list[UnitRow]:
    rows = []
    for unit in store.run_units(run_id):
        cases = store.run_cases(run_id, unit_id=unit.id)
        case = cases[0] if cases else None
        if case is None:
            continue
        try:
            version_number = store.get_version(case.case_version_id).version_number
        except NotFoundError:
            version_number = None
        http = unit.metadata.get("http_status") or []
        rows.append(
            UnitRow(
                unit_number=unit.unit_number,
                case_id=case.case_id,
                case_key=case_key(case.case_id),
                case_version_id=case.case_version_id,
                version_number=version_number,
                status=unit.status,
                case_status=case.status,
                classification=str(unit.metadata.get("classification", "")),
                verification=str(
                    unit.metadata.get("verification")
                    or case.result_metadata.get("verification")
                    or ""
                ),
                http_status=tuple(code for code in http if isinstance(code, int)),
            )
        )
    return rows


def run_summary(store: BoxStore, run: BoxRun) -> RunSummary:
    rows = unit_rows(store, run.id)
    counts = {
        status: sum(1 for r in rows if r.status == status)
        for status in (UNIT_PASS, UNIT_FAIL, UNIT_ERROR, UNIT_NOT_RUN, UNIT_CANCELLED)
    }
    open_rows = [r for r in rows if r.status in (UNIT_QUEUED, UNIT_RUNNING)]
    current = next((r.unit_number for r in rows if r.status == UNIT_RUNNING), None)
    classifications: dict[str, int] = {}
    for row in rows:
        if row.classification:
            classifications[row.classification] = classifications.get(row.classification, 0) + 1
    recovery = (
        run.metadata.get("recovery") if isinstance(run.metadata.get("recovery"), dict) else {}
    )
    return RunSummary(
        run=run,
        total=len(rows),
        done=len(rows) - len(open_rows),
        remaining=len(open_rows),
        counts=counts,
        verified=sum(1 for r in rows if r.verified_pass),
        unverified=sum(1 for r in rows if r.unverified_pass),
        current_unit=current,
        stop_reason=str(run.metadata.get("stop_reason", "")),
        recovery_reason=str(recovery.get("reason", "")),
        retry_of_run_id=run.retry_of_run_id,
        blocked=sum(1 for r in rows if r.case_status == "NOT_EXECUTABLE"),
        classifications=classifications,
    )


@dataclass(frozen=True)
class ArtifactEntry:
    unit_number: int
    name: str
    path: Path | None  # فقط وقتی فایل واقعاً هست و امن است
    reason: str = ""  # چرا در دسترس نیست

    @property
    def available(self) -> bool:
        return self.path is not None


def _safe_file(run_dir: Path, relative: str) -> tuple[Path | None, str]:
    candidate = run_dir / relative
    try:
        resolved = candidate.resolve()
    except OSError:
        return None, "The file cannot be resolved."
    if run_dir not in resolved.parents:
        return None, "The file is outside the run directory."
    if resolved.name not in SAFE_ARTIFACT_NAMES:
        return None, "The file type is not published."
    if candidate.is_symlink() or not resolved.is_file():
        return None, "The file is missing."
    return resolved, ""


def run_artifacts(artifact_root, store: BoxStore, run: BoxRun) -> list[ArtifactEntry]:
    """فایل‌های منتشرشده‌ی هر unit — از روی فراداده‌ی ثبت‌شده، هرگز با حدسِ مسیر."""
    name = run.metadata.get("artifact_dir")
    if not isinstance(name, str) or not _RUN_DIR_RE.match(name):
        return []
    root = Path(artifact_root).resolve()
    run_dir = (root / name).resolve()
    if run_dir.parent != root or not run_dir.is_dir():
        return [ArtifactEntry(0, name, None, "The run directory is missing.")]
    entries: list[ArtifactEntry] = []
    for unit in store.run_units(run.id):
        unit_dir = f"unit_{unit.unit_number:03d}"
        if not _UNIT_DIR_RE.match(unit_dir):
            continue
        relatives = [f"{unit_dir}/collection.json"]
        recorded = unit.metadata.get("artifacts")
        if isinstance(recorded, dict):
            for key in ("result_json", "json_report", "html_report", "stdout_log", "stderr_log"):
                value = recorded.get(key)
                if isinstance(value, str) and value.startswith(unit_dir + "/"):
                    relatives.append(value)
        if unit.status in (UNIT_QUEUED,) or (
            unit.status == UNIT_NOT_RUN and not (run_dir / unit_dir).exists()
        ):
            continue
        for relative in relatives:
            path, reason = _safe_file(run_dir, relative)
            entries.append(ArtifactEntry(unit.unit_number, Path(relative).name, path, reason))
    return entries


__all__ = [
    "RESULT_VERIFIED_PASS",
    "SAFE_ARTIFACT_NAMES",
    "VERIFICATION_TEXT",
    "ArtifactEntry",
    "RunSummary",
    "UnitRow",
    "run_artifacts",
    "run_summary",
    "unit_rows",
]
