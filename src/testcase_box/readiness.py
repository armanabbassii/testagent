"""
src/testcase_box/readiness.py — ارزیابیِ قطعیِ readiness ِ یک نسخه‌ی تغییرناپذیر

    readiness = assess(version, store.script_approval_state(version.id))   # خالص
    readiness = assess_version(store, version_id)                           # فقط‌خواندنی

وضعیت (contract.status_for):
  * NOT_EXECUTABLE — دستِ کم یک یافته‌ی مسدودکننده (ERROR)؛ hard rejection ها (مثلِ
    sendRequest) و ناهمخوانیِ اثرانگشت حتی با تأیید هم مسدودکننده‌اند؛
  * NEEDS_SCRIPT_REVIEW — مسدودکننده‌ای نیست ولی تصمیمِ بازبینیِ اسکریپت هنوز لازم است؛
  * READY — نه مسدودکننده و نه بازبینیِ معوق. یافته‌های INFO (از جمله SCRIPT_APPROVED)
    و WARNING هرگز وضعیت را عوض نمی‌کنند.

READY فقط یعنی «مطابقِ قراردادِ M3». موفقیت یا امنیتِ اجرای واقعی را تضمین نمی‌کند:
متغیرهای EXTERNAL هنوز در برابرِ environment/globals یا caseهای تولیدکننده سنجیده
نشده‌اند (این کار مرحله‌ی اجراست)، و تأییدِ اسکریپت یک بازبینیِ ایستاست.

نتیجه ذخیره نمی‌شود — نسخه‌ی تغییرناپذیر منبعِ حقیقت است و readiness هر بار از نو
محاسبه می‌شود. هیچ درخواست، اسکریپت، توکن، شبکه یا فایلی درگیر نیست.
"""

from __future__ import annotations

from typing import Any

from src.testcase_box.contract import (
    HOST_VARIABLES,
    KIND_EXTERNAL,
    KIND_HOST,
    KIND_TOKEN,
    TOKEN_VARIABLE,
    Finding,
    Readiness,
    RequiredVariable,
    ScriptReview,
    finding,
    ordered,
    status_for,
)
from src.testcase_box.models import (
    APPROVAL_APPROVED,
    APPROVAL_REJECTED,
    APPROVAL_REVOKED,
    CaseVersion,
)
from src.testcase_box.payload import PayloadReport, inspect_payload
from src.testcase_box.scripts import ScriptReport, inspect_scripts
from src.testcase_box.storage import BoxStore

_APPROVAL_FINDINGS = {
    APPROVAL_APPROVED: "SCRIPT_APPROVED",
    APPROVAL_REJECTED: "SCRIPT_REJECTED",
    APPROVAL_REVOKED: "SCRIPT_APPROVAL_REVOKED",
}


def assess(version: CaseVersion, approval_state: str) -> Readiness:
    return assess_payload(
        version.payload,
        stored_script_fingerprint=version.script_fingerprint,
        approval_state=approval_state,
        version_id=version.id,
    )


def assess_version(store: BoxStore, version_id: int) -> Readiness:
    """فقط خواندن: نسخه و آخرین تصمیمِ بازبینی؛ هیچ چیزی نوشته نمی‌شود."""
    version = store.get_version(version_id)
    return assess(version, store.script_approval_state(version_id))


def assess_payload(
    payload: Any,
    *,
    stored_script_fingerprint: str | None,
    approval_state: str,
    version_id: int | None = None,
) -> Readiness:
    payload_report = inspect_payload(payload)
    script_report = inspect_scripts(payload, stored_script_fingerprint)
    findings: list[Finding] = [*payload_report.findings, *script_report.findings]
    review_required = (
        stored_script_fingerprint is not None or script_report.computed_fingerprint is not None
    )
    if review_required:
        # PENDING، NOT_REQUIRED ِ ناسازگار یا وضعیتِ ناشناخته → بازبینی لازم (fail-closed)
        findings.append(
            finding(_APPROVAL_FINDINGS.get(approval_state, "SCRIPT_REVIEW_REQUIRED"), "$")
        )
    required, provided = _variables(payload_report, script_report)
    result = ordered(findings)
    return Readiness(
        version_id=version_id,
        status=status_for(result),
        host_variable=payload_report.host_variable,
        findings=result,
        required_variables=required,
        provided_variables=provided,
        script_review=ScriptReview(
            required=review_required,
            fingerprint=stored_script_fingerprint,
            approval_state=approval_state,
            hard_rejections=script_report.hard_rejections,
        ),
    )


def _variables(
    payload_report: PayloadReport, script_report: ScriptReport
) -> tuple[tuple[RequiredVariable, ...], tuple[str, ...]]:
    """ارجاعِ بخش‌های درخواست با set در اسکریپتِ prerequest ِ همین case تأمین می‌شود؛
    خواندنِ اسکریپت با هر set ای در همین case. بقیه EXTERNAL است و فقط فهرست می‌شود."""
    set_before_request = {w.name for w in script_report.writes if w.listen == "prerequest"}
    set_anywhere = {w.name for w in script_report.writes}
    locations: dict[str, set[str]] = {}
    kinds: dict[str, str] = {}

    def need(name: str, location: str, satisfied_by: set[str]) -> None:
        if name.startswith("$"):
            return  # متغیرِ پویای Postman
        if name in HOST_VARIABLES:
            kind = KIND_HOST
        elif name == TOKEN_VARIABLE:
            kind = KIND_TOKEN
        elif name in satisfied_by:
            return
        else:
            kind = KIND_EXTERNAL
        kinds[name] = kind
        locations.setdefault(name, set()).add(location)

    for reference in payload_report.references:
        need(reference.name, reference.location, set_before_request)
    for read in script_report.reads:
        need(read.name, read.location, set_anywhere)
    required = tuple(
        RequiredVariable(name, kinds[name], tuple(sorted(locations[name])))
        for name in sorted(locations)
    )
    return required, tuple(sorted(set_anywhere))
