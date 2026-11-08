"""
ui/box_runs.py — بخشِ اجرای TestCase Box در همان صفحه‌ی ui/box_app.py (M7.7)

    render_new_run(db_path)     # انتخابِ case، پروفایل، پیش‌نمایشِ برنامه، شروع
    render_runs(db_path)        # تاریخچه، جزئیات و پیشرفتِ زنده، لغو، artifactها، rerun، بازیابی

این ماژول فقط ارکستراسیون است: هیچ قاعده‌ای (برنامه‌ریزی، Gate، lease، بی‌اسکریپت، Newman، بازیابی)
این‌جا پیاده نمی‌شود — همه از BoxRunService (src/testcase_box/run_service.py)، run_views و recovery
می‌آیند. UI هرگز Newman یا موتور را صدا نمی‌زند، هرگز پروسه‌ای را نمی‌کشد و هرگز SQLی نمی‌نویسد.

قراردادهای M6.3 حفظ شده‌اند: هر دکمه‌ی عملیاتی (شروع، شروعِ rerun، لغو، بازیابی) token ِ یک‌بارمصرف
دارد، پس دوبار-کلیک دو کار انجام نمی‌دهد؛ پیام فقط پس از موفقیتِ واقعیِ عملیات نشان داده می‌شود؛
انتخاب‌ها با خطا پاک نمی‌شوند. PASS ِ بی‌assertion همیشه «UNVERIFIED» نمایش داده می‌شود. هیچ
توکن (دسترسی یا مالکیت) یا مقدارِ متغیری نمایش داده نمی‌شود — فقط نامِ متغیرها.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from src.testcase_box.db import StorageError
from src.testcase_box.models import (
    CASE_ACTIVE,
    CASE_ARCHIVED,
    RUN_FINAL,
    RUN_QUEUED,
    RUN_RUNNING,
)
from src.testcase_box.profiles import HostIntent
from src.testcase_box.run_planning import RunPlan
from src.testcase_box.run_profile import (
    DEFAULT_PROCESS_TIMEOUT_S,
    DEFAULT_REQUEST_TIMEOUT_MS,
    PROCESS_TIMEOUT_RANGE_S,
    REQUEST_TIMEOUT_RANGE_MS,
    RunProfile,
    RunProfileError,
)
from src.testcase_box.run_service import BoxRunService, PlanningProblem, get_run_service
from src.testcase_box.run_views import (
    RESULT_VERIFIED_PASS,
    UnitRow,
    VERIFICATION_TEXT,
    run_artifacts,
    run_summary,
    unit_rows,
)
from src.testcase_box.storage import BoxStore

REFRESH_S = 2.0
_CASES_KEY = "box_run_cases"
_HOST_KEY = "box_run_host"
_REQUEST_KEY = "box_run_request_timeout"
_PROCESS_KEY = "box_run_process_timeout"
_PLAN_KEY = "_box_run_plan"
_PLAN_INPUTS_KEY = "_box_run_plan_inputs"
_RERUN_KEY = "_box_rerun_plan"
_TOKEN_KEY = "_box_run_token"
_NOTICE_KEY = "_box_run_notice"
_RUN_PICK_KEY = "box_runs_selected"
_SECTION_KEY = "box_section"
SECTION_RUNS = "Runs"


def _result_label(row: UnitRow) -> str:
    """همان طبقه‌بندیِ شمارش‌ها (UnitRow.result) — PASS بدونِ VERIFIED هرگز تأییدشده نیست (R1)."""
    return "PASS (verified)" if row.result == RESULT_VERIFIED_PASS else row.result


_STOP_TEXT = {
    "USER_CANCELLED": "Cancelled by a user.",
    "GATE_LOST": "Stopped: the shared execution gate was lost.",
    "GATE_UNAVAILABLE": "Stopped: the shared execution gate could not be confirmed.",
    "LEASE_LOST": "Stopped: the runner lost ownership of the run.",
    "HEARTBEAT_FAILED": "Stopped: the run heartbeat kept failing.",
    "CONFIGURATION_CHANGED": "Stopped: the Postman Environment or Globals changed during the run.",
    "AUTHENTICATION_FAILED": "Stopped: no access token could be obtained.",
    "INTEGRITY_FAILED": "Stopped: a pinned test case failed its pre-execution integrity check.",
    "ENGINE_ENVIRONMENT_ERROR": "Stopped: the Newman engine environment is not usable.",
    "ARTIFACT_PUBLICATION_FAILED": "Stopped: run artifacts could not be written.",
    "STORAGE_FAILED": "Stopped: the run could not be saved.",
    "RUNNER_EXCEPTION": "Stopped: the runner failed unexpectedly.",
    "RECOVERED_AFTER_APPLICATION_TERMINATION": (
        "Recovered after the application stopped while this run was active."
    ),
}


# ── ابزارهای مشترک ───────────────────────────────────────────────────────────


def service(db_path: Path) -> BoxRunService:
    return get_run_service(db_path)


def _token() -> int:
    return st.session_state.get(_TOKEN_KEY, 0)


def _consume(token: int) -> bool:
    """token ِ یک‌بارمصرف (همان الگوی M6.3): کلیکِ دوم از همان دکمه کاری نمی‌کند."""
    if token != st.session_state.get(_TOKEN_KEY, 0):
        _notice("info", "This click was already handled. Nothing more was done.")
        return False
    st.session_state[_TOKEN_KEY] = token + 1
    return True


def _notice(kind: str, text: str) -> None:
    st.session_state[_NOTICE_KEY] = {"kind": kind, "text": text}


def _show_notice(slot=None) -> None:
    """پیام را مصرف و نشان می‌دهد. پیش از هر st.rerun ِ احتمالی صدا زده نشود — وگرنه پیام گم
    می‌شود؛ برای همین render_runs جایش را بالای صفحه نگه می‌دارد و آخرِ کار پُرش می‌کند."""
    notice = st.session_state.pop(_NOTICE_KEY, None)
    if notice is None:
        return
    target = slot if slot is not None else st
    show = {"success": target.success, "warning": target.warning, "error": target.error}.get(
        notice["kind"], target.info
    )
    show(notice["text"])


def _storage_error(exc: Exception) -> None:
    st.error(
        f"The TestCase Box library could not be opened ({type(exc).__name__}). Nothing was changed.",
        icon=":material/error:",
    )


def _plan_table(plan: RunPlan, titles: dict[int, str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Unit": entry.position,
                "Test case": entry.case_key or "—",
                "Title": titles.get(entry.case_id, ""),
                "Version": entry.version_number,
                "Will run": "Yes" if entry.eligible else "No",
                "Blocking reasons": " ".join(f.message for f in entry.blocking),
                "Warnings": ", ".join(sorted({f.code for f in entry.warnings})),
            }
            for entry in plan.entries
        ]
    )


def _render_plan(plan: RunPlan, titles: dict[int, str]) -> None:
    eligible, blocked = len(plan.eligible_entries), len(plan.blocked_entries)
    a, b, c = st.columns(3)
    a.metric("Newman units", len(plan.entries))
    b.metric("Will run", eligible)
    c.metric("Blocked", blocked)
    st.caption(
        "Each test case runs in its own Newman unit (1 test case = 1 unit); no variables or "
        "state pass from one test case to another."
    )
    st.dataframe(_plan_table(plan, titles), hide_index=True, width="stretch")
    if blocked:
        st.warning(
            f"{blocked} selected test case(s) will not run; their reasons are listed above. "
            "Test cases with scripts or any Postman event entry — including an empty "
            "'event: []' — never run in this version.",
            icon=":material/block:",
        )
    profile = plan.profile
    st.caption(
        f"Execution profile: {profile.host_intent.value} host, request timeout "
        f"{profile.request_timeout_ms / 1000:g} s, process timeout {profile.process_timeout_s / 60:g} "
        "min, bail off, TLS certificates always verified."
    )
    names = sorted(plan.variables.resolved_variables)
    empty = sorted(plan.variables.empty_variables)
    st.caption(
        "Variables with a value (names only): "
        + (", ".join(names) or "none")
        + (f". Defined but empty: {', '.join(empty)}" if empty else "")
        + ". The access token is injected at run time and never stored."
    )


# ── اجرای تازه ───────────────────────────────────────────────────────────────


def render_new_run(db_path: Path) -> None:
    _show_notice()
    if not db_path.exists():
        st.info("The library is empty. Import test cases from Batch first.", icon=":material/info:")
        return
    try:
        with BoxStore.open(db_path) as store:
            active = store.list_cases(CASE_ACTIVE)
            archived = store.list_cases(CASE_ARCHIVED)
            versions = {case.id: store.current_version(case.id).version_number for case in active}
    except (StorageError, sqlite3.Error) as exc:
        _storage_error(exc)
        return
    if not active:
        st.info("There is no active test case to run.", icon=":material/info:")
        return
    labels = {case.id: f"{case.key} · {case.title} · v{versions[case.id]}" for case in active}
    st.multiselect(
        "Test cases to run",
        list(labels),
        format_func=labels.get,
        key=_CASES_KEY,
        help="Units run in the order you select them.",
    )
    if archived:
        st.caption(
            "Archived test cases cannot be selected for a new run: "
            + ", ".join(case.key for case in archived)
            + ". To execute an archived test case, rerun a previous run that pinned it."
        )
    with st.container(border=True):
        st.markdown("**Execution profile**")
        st.radio(
            "Host",
            [HostIntent.NORMAL.value, HostIntent.VDI.value],
            index=None,
            horizontal=True,
            key=_HOST_KEY,
            help="NORMAL uses {{base_url}}, VDI uses {{base_url_vdi}}. It is never guessed.",
        )
        left, right = st.columns(2)
        left.number_input(
            "Request timeout (ms)",
            min_value=REQUEST_TIMEOUT_RANGE_MS[0],
            max_value=REQUEST_TIMEOUT_RANGE_MS[1],
            value=DEFAULT_REQUEST_TIMEOUT_MS,
            step=1000,
            key=_REQUEST_KEY,
        )
        right.number_input(
            "Process timeout (s)",
            min_value=PROCESS_TIMEOUT_RANGE_S[0],
            max_value=PROCESS_TIMEOUT_RANGE_S[1],
            value=DEFAULT_PROCESS_TIMEOUT_S,
            step=60,
            key=_PROCESS_KEY,
        )
        st.caption("Bail is always off and TLS certificates are always verified.")
    selected = st.session_state.get(_CASES_KEY) or []
    st.button(
        "Preview run plan",
        icon=":material/fact_check:",
        disabled=not selected,
        key="box_run_preview",
        on_click=_preview_new,
        args=(str(db_path),),
    )
    plan = st.session_state.get(_PLAN_KEY)
    if plan is not None and st.session_state.get(_PLAN_INPUTS_KEY) != _inputs():
        # انتخاب یا پروفایل پس از پیش‌نمایش عوض شده: برنامه‌ی دیده‌شده دیگر همان نیست
        st.session_state.pop(_PLAN_KEY, None)
        st.caption("The selection or profile changed. Preview the run plan again.")
        return
    if plan is None:
        return
    st.subheader("Run plan")
    _render_plan(plan, {case.id: case.title for case in active})
    token = _token()
    st.button(
        "Start run",
        type="primary",
        icon=":material/play_arrow:",
        disabled=not plan.executable,
        key=f"box_run_start_{plan.fingerprint[:12]}_{token}",
        on_click=_start,
        args=(str(db_path), plan.fingerprint, token, _PLAN_KEY),
    )
    if not plan.executable:
        st.caption("No selected test case can run, so the run cannot start.")


def _profile_from_inputs() -> RunProfile:
    host = st.session_state.get(_HOST_KEY)
    if host is None:
        raise RunProfileError("Choose the host (NORMAL or VDI) for this run.")
    return RunProfile(
        host_intent=host,
        request_timeout_ms=int(st.session_state.get(_REQUEST_KEY, DEFAULT_REQUEST_TIMEOUT_MS)),
        process_timeout_s=int(st.session_state.get(_PROCESS_KEY, DEFAULT_PROCESS_TIMEOUT_S)),
    )


def _inputs() -> tuple:
    return (
        tuple(st.session_state.get(_CASES_KEY) or ()),
        st.session_state.get(_HOST_KEY),
        st.session_state.get(_REQUEST_KEY, DEFAULT_REQUEST_TIMEOUT_MS),
        st.session_state.get(_PROCESS_KEY, DEFAULT_PROCESS_TIMEOUT_S),
    )


def _preview_new(db_path: str) -> None:
    st.session_state.pop(_PLAN_KEY, None)
    st.session_state[_PLAN_INPUTS_KEY] = _inputs()
    try:
        profile = _profile_from_inputs()
        plan = service(Path(db_path)).plan_new(
            list(st.session_state.get(_CASES_KEY) or []), profile
        )
    except RunProfileError as exc:
        _notice("warning", f"The execution profile is not valid: {exc}")
        return
    except PlanningProblem as exc:
        _notice("error", f"The run could not be planned: {exc}")
        return
    except (StorageError, sqlite3.Error) as exc:
        _notice("error", f"The TestCase Box library could not be opened ({type(exc).__name__}).")
        return
    st.session_state[_PLAN_KEY] = plan


def _start(db_path: str, fingerprint: str, token: int, plan_key: str) -> None:
    if not _consume(token):
        return
    plan = st.session_state.get(plan_key)
    if plan is None or plan.fingerprint != fingerprint:
        _notice("warning", "The plan changed before your click was handled. Nothing was started.")
        return
    result = service(Path(db_path)).start(plan)
    if not result.ok:
        _notice("error", f"The run did not start: {result.message}")
        return
    st.session_state.pop(plan_key, None)
    st.session_state[_RUN_PICK_KEY] = result.run_id
    st.session_state[_SECTION_KEY] = SECTION_RUNS
    _notice("success", f"Run #{result.run_id} started.")


# ── تاریخچه و جزئیات ─────────────────────────────────────────────────────────


def _history(store: BoxStore, runs) -> pd.DataFrame:
    rows = []
    for run in runs:
        summary = run_summary(store, run)
        rows.append(
            {
                "Run": run.id,
                "Status": run.status,
                "Created": run.created_at,
                "Started": run.started_at or "",
                "Finished": run.finished_at or "",
                "Test cases": summary.total,
                "PASS (verified)": summary.verified,
                "UNVERIFIED": summary.unverified,
                "FAIL": summary.counts["FAIL"],
                "ERROR": summary.counts["ERROR"],
                "NOT_RUN": summary.counts["NOT_RUN"],
                "CANCELLED": summary.counts["CANCELLED"],
                "Rerun of": run.retry_of_run_id or "",
                "Stop / recovery": summary.recovery_reason or summary.stop_reason,
            }
        )
    return pd.DataFrame(rows)


def render_runs(db_path: Path) -> None:
    slot = st.empty()  # پیام پس از fragment پُر می‌شود (نگاه کنید به _show_notice)
    if not db_path.exists():
        _show_notice(slot)
        st.info("There is no run yet.", icon=":material/info:")
        return
    svc = service(db_path)
    try:
        _render_recovery(svc)
        with BoxStore.open(db_path) as store:
            runs = store.list_runs(100)
            history = _history(store, runs) if runs else None
    except (StorageError, sqlite3.Error) as exc:
        _show_notice(slot)
        _storage_error(exc)
        return
    if not runs:
        _show_notice(slot)
        st.info("There is no run yet. Start one in the Run tests section.", icon=":material/info:")
        return
    st.subheader("Run history")
    st.dataframe(history, hide_index=True, width="stretch")
    ids = [run.id for run in runs]
    selected = st.session_state.get(_RUN_PICK_KEY)
    if selected not in ids:
        selected = ids[0]
        st.session_state[_RUN_PICK_KEY] = selected
    st.selectbox("Run", ids, format_func=lambda run_id: f"Run #{run_id}", key=_RUN_PICK_KEY)
    run = next(r for r in runs if r.id == st.session_state[_RUN_PICK_KEY])
    live = run.status in (RUN_QUEUED, RUN_RUNNING) or (
        svc.job is not None and svc.job.running and svc.job.run_id == run.id
    )
    st.fragment(_render_details, run_every=REFRESH_S if live else None)(str(db_path), run.id, live)
    _show_notice(slot)


def _render_details(db_path: str, run_id: int, was_live: bool) -> None:
    svc = service(Path(db_path))
    try:
        with BoxStore.open(db_path) as store:
            run = store.get_run(run_id)
            summary = run_summary(store, run)
            rows = unit_rows(store, run_id)
            titles = {case.id: case.title for case in store.list_cases()}
            artifacts = run_artifacts(svc.artifact_root, store, run)
    except (StorageError, sqlite3.Error) as exc:
        _storage_error(exc)
        return
    live = run.status in (RUN_QUEUED, RUN_RUNNING)
    if was_live and not live and not (svc.job is not None and svc.job.running):
        st.rerun()  # اجرا تمام شد — بازخوانیِ کامل، نوسازیِ خودکار متوقف می‌شود
    # دکمه‌های داخلِ fragment فقط fragment را دوباره اجرا می‌کنند؛ پیامشان این‌جا دیده می‌شود
    # (پس از st.rerun، تا پیام پیش از بازخوانیِ کامل مصرف و گم نشود)
    _show_notice()
    st.subheader(f"Run #{run.id} — {run.status}")
    st.progress(summary.progress, text=f"{summary.done} of {summary.total} unit(s) finished")
    cols = st.columns(6)
    for col, (label, value) in zip(
        cols,
        (
            ("PASS (verified)", summary.verified),
            ("UNVERIFIED", summary.unverified),
            ("FAIL", summary.counts["FAIL"]),
            ("ERROR", summary.counts["ERROR"]),
            ("NOT_RUN", summary.counts["NOT_RUN"]),
            ("CANCELLED", summary.counts["CANCELLED"]),
        ),
        strict=True,
    ):
        col.metric(label, value)
    details = [f"Created {run.created_at}"]
    if run.started_at:
        details.append(f"started {run.started_at}")
    if run.finished_at:
        details.append(f"finished {run.finished_at}")
    if summary.current_unit is not None:
        details.append(f"current unit {summary.current_unit}")
    details.append(f"{summary.remaining} unit(s) remaining")
    if run.retry_of_run_id:
        details.append(f"rerun of run #{run.retry_of_run_id}")
    st.caption(" · ".join(details))
    if summary.stop_reason:
        st.warning(_STOP_TEXT.get(summary.stop_reason, summary.stop_reason))
    if summary.recovery_reason:
        st.info(f"Recovered by the recovery service ({summary.recovery_reason}).")
    _render_job_message(svc, run.id)
    if summary.unverified:
        st.info(f"UNVERIFIED: {VERIFICATION_TEXT['UNVERIFIED']}", icon=":material/help:")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Unit": row.unit_number,
                    "Test case": row.case_key,
                    "Title": titles.get(row.case_id, ""),
                    "Pinned version": row.version_number,
                    "Result": _result_label(row),
                    "Engine status": row.status,
                    "Verification": row.verification or "—",
                    "Classification": row.classification or "—",
                    "HTTP": ", ".join(str(code) for code in row.http_status),
                }
                for row in rows
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    profile = run.metadata.get("profile")
    if isinstance(profile, dict):
        st.caption(
            "Execution profile: "
            + ", ".join(f"{k}={profile[k]}" for k in sorted(profile) if k != "schema")
        )
    if live:
        token = _token()
        st.button(
            "Cancel run",
            icon=":material/stop:",
            key=f"box_run_cancel_{run.id}_{token}",
            on_click=_cancel,
            args=(db_path, run.id, token),
        )
    _render_artifacts(artifacts)
    if run.status in RUN_FINAL:
        _render_rerun(db_path, run.id, titles)


def _render_job_message(svc: BoxRunService, run_id: int) -> None:
    job = svc.job
    if job is None or job.result is None or job.run_id != run_id:
        return
    result = job.result
    if result.status == "TERMINATION_UNCONFIRMED":
        st.error(
            "The runner could not confirm that the Newman process stopped. The run was not "
            "finalized and the shared execution gate is kept until this application exits; "
            "recover the run after a restart.",
            icon=":material/error:",
        )
    for note in result.cleanup_errors:
        st.warning(note)


def _cancel(db_path: str, run_id: int, token: int) -> None:
    if not _consume(token):
        return
    result = service(Path(db_path)).cancel(run_id)
    _notice("info" if result.ok else "warning", result.message)


def _render_artifacts(artifacts) -> None:
    st.markdown("**Artifacts**")
    if not artifacts:
        st.caption("No artifact was recorded for this run.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Unit": entry.unit_number,
                    "File": entry.name,
                    "Available": "Yes" if entry.available else "No",
                    "Note": entry.reason,
                }
                for entry in artifacts
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    for index, entry in enumerate(a for a in artifacts if a.available):
        try:
            data = entry.path.read_bytes()
        except OSError:
            st.caption(f"Unit {entry.unit_number} {entry.name}: the file could not be read.")
            continue
        st.download_button(
            f"Download unit {entry.unit_number} {entry.name}",
            data=data,
            file_name=f"unit_{entry.unit_number:03d}_{entry.name}",
            key=f"box_artifact_{index}_{entry.unit_number}_{entry.name}",
        )


# ── rerun ────────────────────────────────────────────────────────────────────


def _render_rerun(db_path: str, run_id: int, titles: dict[int, str]) -> None:
    st.markdown("**Rerun**")
    st.caption(
        "A rerun executes the complete original run as a new run: the same pinned test case "
        "versions in the same order, even if a test case was archived or changed since. "
        "The original run is never modified."
    )
    st.button(
        "Prepare rerun",
        icon=":material/replay:",
        key=f"box_rerun_prepare_{run_id}",
        on_click=_prepare_rerun,
        args=(db_path, run_id),
    )
    plan = st.session_state.get(_RERUN_KEY)
    if plan is None or plan.rerun_of_run_id != run_id:
        return
    _render_plan(plan, titles)
    missing = sorted(
        {
            f.message
            for entry in plan.entries
            for f in entry.blocking
            if f.code
            in (
                "VARIABLE_UNAVAILABLE",
                "VARIABLE_EMPTY",
                "PROFILE_HOST_MISSING",
                "PROFILE_HOST_EMPTY",
            )
        }
    )
    if missing:
        st.warning(
            "Required variables are missing from the current Postman Environment/Globals and "
            "must be supplied there again: " + " ".join(missing)
        )
    token = _token()
    st.button(
        "Start rerun",
        type="primary",
        icon=":material/play_arrow:",
        disabled=not plan.executable,
        key=f"box_rerun_start_{run_id}_{token}",
        on_click=_start,
        args=(db_path, plan.fingerprint, token, _RERUN_KEY),
    )


def _prepare_rerun(db_path: str, run_id: int) -> None:
    st.session_state.pop(_RERUN_KEY, None)
    try:
        st.session_state[_RERUN_KEY] = service(Path(db_path)).plan_rerun(run_id)
    except PlanningProblem as exc:
        _notice("error", f"The rerun could not be planned: {exc}")
    except (StorageError, sqlite3.Error) as exc:
        _notice("error", f"The TestCase Box library could not be opened ({type(exc).__name__}).")


# ── بازیابی ──────────────────────────────────────────────────────────────────


def _render_recovery(svc: BoxRunService) -> None:
    candidates = svc.inspect_recovery()
    if not candidates:
        return
    st.subheader("Active and stale runs")
    st.caption(
        "A stale heartbeat alone does not prove that a run is dead. Recovery first acquires "
        "the shared execution gate and refuses while any Batch or Box execution — including "
        "a hung process — still holds it. There is no way to force recovery."
    )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Run": c.run_id,
                    "Status": c.status,
                    "Last heartbeat": c.heartbeat_at or "—",
                    "Heartbeat age (s)": None
                    if c.heartbeat_age_s is None
                    else round(c.heartbeat_age_s),
                    "Recorded gate holder": c.gate_holder or "none recorded",
                    "Recovery": "may be attempted"
                    if c.recoverable
                    else f"blocked: {c.blocked_reason}",
                }
                for c in candidates
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    for candidate in candidates:
        if not candidate.recoverable:
            continue
        token = _token()
        st.button(
            f"Recover run #{candidate.run_id}",
            icon=":material/healing:",
            key=f"box_recover_{candidate.run_id}_{candidate.lease_generation}_{token}",
            on_click=_recover,
            args=(str(svc.store_path), candidate.run_id, candidate.lease_generation, token),
        )


def _recover(db_path: str, run_id: int, generation: int, token: int) -> None:
    if not _consume(token):
        return
    result = service(Path(db_path)).recover(run_id, expected_generation=generation)
    kind = {"RECOVERED": "success", "ALREADY_FINAL": "info", "BLOCKED": "warning"}.get(
        result.status, "error"
    )
    _notice(kind, f"Run #{run_id}: {result.message}")
    if result.status == "RECOVERED":
        st.session_state[_RUN_PICK_KEY] = run_id


__all__ = ["SECTION_RUNS", "render_new_run", "render_runs", "service"]
