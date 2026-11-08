"""
src/testcase_box/run_planning.py — برنامه‌ریزیِ قطعیِ یک run پیش از اجرا (M7.4)

    plan = plan_new_run(store, [3, 1, 7], profile=run_profile, variables=variable_profile)
    plan = plan_rerun(store, run_id, profile=run_profile, variables=variable_profile)
    plan.executable, plan.eligible_entries, plan.blocked_entries, plan.fingerprint

فقط خواندن: هیچ چیزی در پایگاه نوشته نمی‌شود، هیچ run ای ساخته یا گرفته نمی‌شود، هیچ اسکریپت،
درخواست، Newman یا Gate ای درگیر نیست. هر caseِ انتخاب‌شده دقیقاً یک ورودیِ برنامه است (D2: یک
case = یک unit) — هیچ انتخابی بی‌صدا حذف نمی‌شود؛ caseِ ناواجد با کدهای مسدودکننده می‌ماند.

ترتیب: run ِ تازه = ترتیبِ انتخاب؛ rerun = ترتیبِ unit و execution_order ِ همان run (R4).

نسخه: run ِ تازه نسخه‌ی جاریِ case را سنجاق می‌کند؛ rerun دقیقاً نسخه‌های سنجاق‌شده‌ی run ِ قبلی
را (هرگز نسخه‌ی جاری را) — حتی اگر case اکنون بایگانی باشد (D6). نسخه‌ی گم‌شده، متعلق به caseِ
دیگر، یا با اثرانگشتی که با payload نمی‌خواند، مسدودکننده است.

سیاستِ اجرای بی‌اسکریپتِ V1 (D1، fail-closed) — مستقل از تأییدِ اسکریپت:
  * اثرانگشتِ اسکریپتِ ذخیره‌شده یا بازمحاسبه‌شده، یا هر eventی که ممکن است اجرا شود →
    EXECUTION_SCRIPTS_PRESENT؛
  * کلیدِ event با هر مقداری جز null — حتی آرایه‌ی خالیِ [] یا entry ِ خالی →
    EXECUTION_EVENTS_PRESENT — Box آن را حذف نمی‌کند (فقط event ِ غایب یا null مجاز است)؛
  * شکلی که نمی‌توان با اطمینان دسته‌بندی کرد یا اثرانگشت‌های ناهم‌خوان →
    EXECUTION_SCRIPT_STRUCTURE_INVALID.
سپس readiness/آماده‌سازیِ M3/M4 (prepare_case) بی‌تغییر؛ فقط PREPARED واجد است و payloadِ
آماده‌شده و اثرانگشتش در برنامه می‌ماند. هشدارها (WARNING/INFO، از جمله D7:
SCRIPT_TOKEN_ACCESS و SCRIPT_MUTATES_REQUEST) جدا از کدهای مسدودکننده نگه داشته می‌شوند و هرگز
caseی را واجد نمی‌کنند.

قطعیت: برای ورودی و وضعیتِ پایگاهِ یکسان، to_dict() و fingerprint یکسان‌اند (هیچ زمان یا
مقدارِ تصادفی‌ای در برنامه نیست).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from src.testcase_box.contract import (
    SCRIPT_EVENTS,
    SEVERITY_CONFIGURATION,
    SEVERITY_ERROR,
    SEVERITY_REVIEW,
    Finding,
    finding,
    ordered,
)
from src.testcase_box.db import NotFoundError
from src.testcase_box.models import (
    CASE_ARCHIVED,
    RUN_FINAL,
    BoxCase,
    CaseVersion,
    case_key,
    payload_scripts,
    script_fingerprint,
    sha256_json,
)
from src.testcase_box.preparation import prepare_case
from src.testcase_box.profiles import ExecutionProfile
from src.testcase_box.run_profile import RunProfile
from src.testcase_box.storage import BoxStore

PLAN_NEW = "NEW"
PLAN_RERUN = "RERUN"
ENTRY_ELIGIBLE = "ELIGIBLE"
ENTRY_BLOCKED = "BLOCKED"
PLAN_SCHEMA_VERSION = 1

_BLOCKING = frozenset({SEVERITY_ERROR, SEVERITY_REVIEW, SEVERITY_CONFIGURATION})


class RunPlanningError(ValueError):
    """برنامه اصلاً ساخته نمی‌شود (انتخابِ خالی، run ِ ناموجود یا غیرپایانی برای rerun)."""


@dataclass(frozen=True)
class PlanSource:
    """منشأِ case (provenance) — همان رکوردهای box_case_sources، بی‌تغییر."""

    source_type: str
    source_reference: str
    case_version_id: int | None


@dataclass(frozen=True)
class PlanEntry:
    position: int  # = شماره‌ی unit (D2)
    case_id: int | None
    case_key: str | None
    case_status: str | None
    version_id: int | None
    version_number: int | None
    version_fingerprint: str | None
    sources: tuple[PlanSource, ...]
    blocking: tuple[Finding, ...]
    warnings: tuple[Finding, ...]
    preparation_status: str | None
    prepared_fingerprint: str | None
    # فقط برای ورودیِ واجد؛ در to_dict نمی‌آید (از نسخه‌ی سنجاق‌شده بازسازی‌پذیر است)
    prepared_payload: dict | None = field(default=None, repr=False, compare=False)

    @property
    def unit_number(self) -> int:
        return self.position

    @property
    def eligible(self) -> bool:
        return not self.blocking and self.prepared_payload is not None

    @property
    def eligibility(self) -> str:
        return ENTRY_ELIGIBLE if self.eligible else ENTRY_BLOCKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "case_id": self.case_id,
            "case_key": self.case_key,
            "case_status": self.case_status,
            "version_id": self.version_id,
            "version_number": self.version_number,
            "version_fingerprint": self.version_fingerprint,
            "sources": [
                {
                    "source_type": s.source_type,
                    "source_reference": s.source_reference,
                    "case_version_id": s.case_version_id,
                }
                for s in self.sources
            ],
            "eligibility": self.eligibility,
            "blocking": [_finding_dict(item) for item in self.blocking],
            "warnings": [_finding_dict(item) for item in self.warnings],
            "preparation_status": self.preparation_status,
            "prepared_fingerprint": self.prepared_fingerprint,
        }


@dataclass(frozen=True)
class RunPlan:
    kind: str
    rerun_of_run_id: int | None
    profile: RunProfile
    variables: ExecutionProfile
    entries: tuple[PlanEntry, ...]

    @property
    def eligible_entries(self) -> tuple[PlanEntry, ...]:
        return tuple(entry for entry in self.entries if entry.eligible)

    @property
    def blocked_entries(self) -> tuple[PlanEntry, ...]:
        return tuple(entry for entry in self.entries if not entry.eligible)

    @property
    def executable(self) -> bool:
        """دستِ کم یک caseِ واجد — caseهای مسدود به‌صورتِ NOT_EXECUTABLE ثبت خواهند شد."""
        return bool(self.eligible_entries)

    def to_dict(self) -> dict[str, Any]:
        """snapshotِ قطعی و بی‌راز: پروفایل، نامِ متغیرها (بدونِ مقدار) و ورودی‌ها."""
        return {
            "schema": PLAN_SCHEMA_VERSION,
            "kind": self.kind,
            "rerun_of_run_id": self.rerun_of_run_id,
            "profile": self.profile.to_dict(),
            "variables": {
                "profile_id": self.variables.profile_id,
                "resolved": sorted(self.variables.resolved_variables),
                "empty": sorted(self.variables.empty_variables),
            },
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @property
    def fingerprint(self) -> str:
        return sha256_json(self.to_dict())


def _finding_dict(item: Finding) -> dict[str, str]:
    return {
        "code": item.code,
        "severity": item.severity,
        "location": item.location,
        "message": item.message,
    }


# ── سیاستِ بی‌اسکریپت ───────────────────────────────────────────────────────


def _event_locations(payload: Any, path: str = "$") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(payload, dict):
        for key in sorted(payload):
            value = payload[key]
            if key == "event":
                found.append((f"{path}.event", value))
            found.extend(_event_locations(value, f"{path}.{key}"))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(_event_locations(value, f"{path}[{index}]"))
    return found


def script_free_findings(payload: Any, stored_fingerprint: str | None) -> tuple[Finding, ...]:
    """یافته‌های مسدودکننده‌ی سیاستِ بی‌اسکریپتِ V1 — خالی یعنی «بی‌اسکریپت ثابت شد».

    هیچ اسکریپتی اجرا یا تفسیر نمی‌شود؛ بررسی ساختاری و fail-closed است. تأییدِ اسکریپت
    عمداً ورودیِ این تابع نیست."""
    if not isinstance(payload, dict):
        return (finding("EXECUTION_SCRIPT_STRUCTURE_INVALID", "$", where="$"),)
    findings: list[Finding] = []
    computed = script_fingerprint(payload)
    if stored_fingerprint is not None or computed is not None or payload_scripts(payload):
        findings.append(finding("EXECUTION_SCRIPTS_PRESENT", "$"))
    if stored_fingerprint != computed:
        findings.append(finding("EXECUTION_SCRIPT_STRUCTURE_INVALID", "$", where="$"))
    for location, value in _event_locations(payload):
        if value is None:  # فقط غایب یا null مجاز است؛ [] هم مسدود می‌شود
            continue
        entries = value if isinstance(value, list) else [value]
        well_formed = all(
            isinstance(entry, dict) and entry.get("listen") in SCRIPT_EVENTS for entry in entries
        )
        code = "EXECUTION_EVENTS_PRESENT" if well_formed else "EXECUTION_SCRIPT_STRUCTURE_INVALID"
        findings.append(finding(code, location, where=location))
    return ordered(findings)


# ── برنامه‌ریزی ──────────────────────────────────────────────────────────────


def plan_new_run(
    store: BoxStore,
    case_ids: Sequence[int],
    *,
    profile: RunProfile,
    variables: ExecutionProfile,
) -> RunPlan:
    """برنامه‌ی run ِ تازه با نسخه‌ی جاریِ هر case، به ترتیبِ انتخاب. caseِ بایگانی مسدود است."""
    _check_inputs(profile, variables)
    if isinstance(case_ids, str | bytes) or not isinstance(case_ids, Sequence) or not case_ids:
        raise RunPlanningError("Select at least one test case.")
    entries, seen = [], set()
    for position, case_id in enumerate(case_ids, 1):
        if isinstance(case_id, bool) or not isinstance(case_id, int) or case_id < 1:
            entries.append(_blocked(position, None, [finding("SELECTION_INVALID", "$")]))
            continue
        if case_id in seen:
            entries.append(_blocked(position, case_id, [finding("SELECTION_DUPLICATE", "$")]))
            continue
        seen.add(case_id)
        entries.append(_plan_case(store, position, case_id, None, profile, variables, rerun=False))
    return RunPlan(PLAN_NEW, None, profile, variables, tuple(entries))


def plan_rerun(
    store: BoxStore,
    run_id: int,
    *,
    profile: RunProfile,
    variables: ExecutionProfile,
) -> RunPlan:
    """برنامه‌ی rerun ِ کاملِ یک run ِ پایانی با همان نسخه‌های سنجاق‌شده و همان ترتیب (R4)."""
    _check_inputs(profile, variables)
    try:
        run = store.get_run(run_id)
    except NotFoundError:
        raise RunPlanningError(f"Run {run_id} does not exist.") from None
    if run.status not in RUN_FINAL:
        raise RunPlanningError(f"Only a finished run can be rerun; run {run_id} is {run.status}.")
    entries = [
        _plan_case(
            store, position, pinned.case_id, pinned.case_version_id, profile, variables, rerun=True
        )
        for position, pinned in enumerate(store.run_cases(run_id), 1)
    ]
    if not entries:
        raise RunPlanningError(f"Run {run_id} has no pinned test cases.")
    return RunPlan(PLAN_RERUN, run_id, profile, variables, tuple(entries))


def _check_inputs(profile: Any, variables: Any) -> None:
    if not isinstance(profile, RunProfile):
        raise TypeError("profile must be a RunProfile.")
    if not isinstance(variables, ExecutionProfile):
        raise TypeError("variables must be an ExecutionProfile.")


def _blocked(
    position: int,
    case_id: int | None,
    blocking: list[Finding],
    case: BoxCase | None = None,
    version: CaseVersion | None = None,
    sources: tuple[PlanSource, ...] = (),
    warnings: tuple[Finding, ...] = (),
    preparation_status: str | None = None,
) -> PlanEntry:
    return PlanEntry(
        position=position,
        case_id=case_id,
        case_key=case_key(case_id) if case_id is not None and case_id >= 1 else None,
        case_status=case.status if case is not None else None,
        version_id=version.id if version is not None else None,
        version_number=version.version_number if version is not None else None,
        version_fingerprint=version.fingerprint if version is not None else None,
        sources=sources,
        blocking=ordered(blocking),
        warnings=warnings,
        preparation_status=preparation_status,
        prepared_fingerprint=None,
    )


def _plan_case(
    store: BoxStore,
    position: int,
    case_id: int,
    pinned_version_id: int | None,
    profile: RunProfile,
    variables: ExecutionProfile,
    *,
    rerun: bool,
) -> PlanEntry:
    try:
        case = store.get_case(case_id)
    except NotFoundError:
        return _blocked(position, case_id, [finding("CASE_NOT_FOUND", "$")])
    sources = tuple(
        PlanSource(s.source_type, s.source_reference, s.case_version_id)
        for s in store.sources(case_id)
    )
    blocking: list[Finding] = []
    if case.status == CASE_ARCHIVED and not rerun:
        blocking.append(finding("CASE_ARCHIVED", "$"))
    try:
        version = (
            store.current_version(case_id)
            if pinned_version_id is None
            else store.get_version(pinned_version_id)
        )
    except NotFoundError:
        blocking.append(finding("PINNED_VERSION_MISSING", "$"))
        return _blocked(position, case_id, blocking, case=case, sources=sources)
    if version.case_id != case_id:
        blocking.append(finding("PINNED_VERSION_INVALID", "$"))
        return _blocked(position, case_id, blocking, case=case, sources=sources)
    if sha256_json(version.payload) != version.fingerprint:
        blocking.append(finding("VERSION_INTEGRITY_FAILED", "$"))
        return _blocked(position, case_id, blocking, case=case, version=version, sources=sources)

    blocking.extend(script_free_findings(version.payload, version.script_fingerprint))
    # تأییدِ اسکریپت فقط هشدارهای M3/M4 را شکل می‌دهد؛ caseِ دارای اسکریپت در هر حال مسدود است
    approval = store.script_approval_state(version.id)
    prepared = prepare_case(
        version, host_intent=profile.host_intent, profile=variables, approval_state=approval
    )
    warnings = ordered(item for item in prepared.findings if item.severity not in _BLOCKING)
    scripted = bool(blocking) and any(item.code.startswith("EXECUTION_") for item in blocking)
    if not scripted:
        # برای caseِ دارای اسکریپت، یافته‌های بازبینیِ M3/M4 تکراری‌اند — کدِ سیاست کافی است
        blocking.extend(item for item in prepared.findings if item.severity in _BLOCKING)
        if not prepared.prepared:
            blocking.append(finding("EXECUTION_NOT_PREPARED", "$", status=prepared.status))
    if blocking or not prepared.prepared:
        return _blocked(
            position,
            case_id,
            blocking,
            case=case,
            version=version,
            sources=sources,
            warnings=warnings,
            preparation_status=prepared.status,
        )
    return PlanEntry(
        position=position,
        case_id=case_id,
        case_key=case_key(case_id),
        case_status=case.status,
        version_id=version.id,
        version_number=version.version_number,
        version_fingerprint=version.fingerprint,
        sources=sources,
        blocking=(),
        warnings=warnings,
        preparation_status=prepared.status,
        prepared_fingerprint=prepared.prepared_fingerprint,
        prepared_payload=prepared.prepared_payload,
    )


__all__ = [
    "ENTRY_BLOCKED",
    "ENTRY_ELIGIBLE",
    "PLAN_NEW",
    "PLAN_RERUN",
    "PLAN_SCHEMA_VERSION",
    "PlanEntry",
    "PlanSource",
    "RunPlan",
    "RunPlanningError",
    "plan_new_run",
    "plan_rerun",
    "script_free_findings",
]
