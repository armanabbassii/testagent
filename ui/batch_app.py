"""
ui/batch_app.py — صفحه‌ی Batch Testing (Milestone 4.6)

کنارِ ویزاردِ تک‌issue در ui/app.py (ناوبریِ st.navigation). این صفحه فقط نمایش و
کنترل است:

    * کشفِ بُرد و issueها با همان سرویسِ فقط‌خواندنیِ src/batch_testing/discovery.py
      و همان کلاینتِ Jiraِ ویزارد (ui.pipeline.jira_session)؛
    * ساختِ batch با build_new_batch (هشِ Environment/Globals همان لحظه)؛
    * اجرا، Resume، Retry، Pause و Cancel فقط از راهِ BatchService/BatchRunner —
      runner در threadِ پس‌زمینه‌ی سرویسِ پروسه است، نه در اجرای صفحه؛
    * وضعیتِ معتبر همیشه از SQLite (BatchStore) خوانده می‌شود؛ st.session_state فقط
      انتخاب‌های نمایشی را نگه می‌دارد (نما، batchِ انتخاب‌شده، نتیجه‌ی کشف).

هیچ منطقِ Jira، خطِ لوله، Newman یا artifact این‌جا تکرار نشده و هیچ نوشتنی در
Jira انجام نمی‌شود. artifactها فقط از فهرستِ مجازِ src/batch_testing/artifacts.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from src.batch_testing.artifacts import execution_artifacts, read_artifact
from src.batch_testing.discovery import DiscoveryError, DiscoveryResult, discover
from src.batch_testing.models import (
    ACTION_CANCEL,
    ACTION_NONE,
    ACTION_PAUSE,
    BATCH_CANCELLED,
    BATCH_COMPLETED,
    BATCH_CREATED,
    BATCH_FINAL,
    BATCH_INTERRUPTED,
    BATCH_PAUSED,
    BATCH_RUNNING,
    EXEC_CANCELLED,
    EXEC_ERROR,
    EXEC_FAIL,
    EXEC_INTERRUPTED,
    EXEC_NOT_EXECUTED,
    EXEC_PASS,
    EXEC_QUEUED,
    EXEC_RUNNING,
    REVIEW_NEEDS_REVIEW,
    Batch,
    Execution,
)
from src.batch_testing.runner import (
    RESULT_CANCELLED,
    RESULT_COMPLETED,
    RESULT_PAUSED,
    RESULT_REFUSED,
    RESULT_RETRIED,
    configuration_problem,
)
from src.batch_testing.service import (
    MODE_RETRY,
    BatchJob,
    BatchService,
    ConfigurationError,
    configuration_snapshot,
    create_batch,
    get_service,
)
from src.batch_testing.storage import DEFAULT_DB_PATH
from src.batch_testing.summary import (
    HEALTH_LIVE,
    HEALTH_STALE,
    STATUS_NOT_STARTED,
    BatchCounts,
    IssueRow,
    count,
    current_row,
    issue_rows,
    runner_health,
)
from src.task_sources.jira.agile import list_boards
from src.task_sources.jira.errors import JiraError
from ui.pipeline import jira_session

# پایگاهِ داده — همان پیش‌فرضِ BatchStore؛ تست‌ها این را به پایگاهِ موقت می‌برند
DB_PATH = DEFAULT_DB_PATH
REFRESH_S = 2.0
HISTORY_LIMIT = 100

VIEW_NEW = "New batch"
VIEW_BATCH = "Batch"
VIEW_HISTORY = "History"
VIEWS = (VIEW_NEW, VIEW_BATCH, VIEW_HISTORY)

_VIEW_KEY = "batch_view"
_PENDING_VIEW_KEY = "_batch_pending_view"
_BATCH_KEY = "batch_selected_id"
_BOARDS_KEY = "batch_boards"
_DISCOVERY_KEY = "batch_discovery"
_FLASH_KEY = "_batch_flash"
_RECOVERY_SHOWN_KEY = "_batch_recovery_shown"

EXECUTION_ICONS = {
    EXEC_PASS: "✅",
    EXEC_FAIL: "❌",
    EXEC_ERROR: "⚠️",
    EXEC_NOT_EXECUTED: "⏭️",
    EXEC_CANCELLED: "⛔",
    EXEC_INTERRUPTED: "⏸️",
    EXEC_RUNNING: "▶️",
    EXEC_QUEUED: "⏳",
    STATUS_NOT_STARTED: "·",
}
BATCH_COLORS = {
    BATCH_CREATED: "gray",
    BATCH_RUNNING: "blue",
    BATCH_PAUSED: "orange",
    BATCH_INTERRUPTED: "orange",
    BATCH_COMPLETED: "green",
    BATCH_CANCELLED: "red",
}
_RESULT_MESSAGES = {
    RESULT_COMPLETED: "The batch run finished.",
    RESULT_PAUSED: "The batch paused after the current issue.",
    RESULT_CANCELLED: "The runner confirmed the cancellation.",
    RESULT_RETRIED: "The retry finished.",
}


def service() -> BatchService:
    """سرویسِ پروسه (همان نمونه برای هر rerun و session) — بارِ اول بازیابیِ آغازین."""
    return get_service(DB_PATH)


# ── قالب‌بندی ────────────────────────────────────────────────────────────────
def when(stamp: str | None) -> str:
    """«2026-10-31T09:15:02.123Z» → «2026-10-31 09:15:02 UTC»."""
    if not stamp:
        return "—"
    return stamp[:19].replace("T", " ") + " UTC"


def execution_label(status: str) -> str:
    text = "Not started" if status == STATUS_NOT_STARTED else status
    return f"{EXECUTION_ICONS.get(status, '')} {text}".strip()


def review_label(execution: Execution | None) -> str:
    if execution is None:
        return "—"
    return "NEEDS_REVIEW" if execution.review_status == REVIEW_NEEDS_REVIEW else "—"


def batch_label(batch: Batch) -> str:
    return f"{when(batch.created_at)} · {batch.board_name} · {batch.status} · {batch.id[:8]}"


def issue_table(rows: list[IssueRow]) -> pd.DataFrame:
    """یک ردیف برای هر issue — وضعیتِ اجرا و بازبینی در ستون‌های جدا."""
    records = []
    for row in rows:
        latest = row.latest
        records.append(
            {
                "Issue": row.issue_key,
                "Summary": row.issue.summary,
                "Execution": execution_label(row.status),
                "Phase": latest.phase if latest else "—",
                "Attempt": latest.attempt if latest else 0,
                "Review": review_label(latest),
                "Final review": (latest.final_review_status or "—") if latest else "—",
                "Started": when(latest.started_at) if latest else "—",
                "Finished": when(latest.finished_at) if latest else "—",
                "Error kind": (latest.error_kind or "") if latest else "",
            }
        )
    return pd.DataFrame(records, columns=list(_ISSUE_COLUMNS))


_ISSUE_COLUMNS = (
    "Issue",
    "Summary",
    "Execution",
    "Phase",
    "Attempt",
    "Review",
    "Final review",
    "Started",
    "Finished",
    "Error kind",
)


# ── پیام‌های کوتاه‌مدت ──────────────────────────────────────────────────────
def _flash(ok: bool, message: str) -> None:
    st.session_state[_FLASH_KEY] = (ok, message)


def _show_flash() -> None:
    flash = st.session_state.pop(_FLASH_KEY, None)
    if flash:
        ok, message = flash
        (st.success if ok else st.error)(message)


def _go_to(view: str, batch_id: str = "") -> None:
    """تغییرِ نما پیش از ساختِ ویجت در rerunِ بعدی (ویجتِ ساخته‌شده قابلِ تغییر نیست)."""
    st.session_state[_PENDING_VIEW_KEY] = view
    if batch_id:
        st.session_state[_BATCH_KEY] = batch_id


# ── صفحه ────────────────────────────────────────────────────────────────────
def render() -> None:
    svc = service()
    st.title("Batch testing", icon=":material/playlist_play:")
    st.caption(
        "Runs Steps 1–5 and Newman for every eligible issue of a Jira board, one issue at a "
        "time. Jira is only read. The batch state lives in the local batch database, so a "
        "page refresh never restarts or duplicates a run."
    )
    _render_recovery_notice(svc)

    pending = st.session_state.pop(_PENDING_VIEW_KEY, None)
    if pending in VIEWS:
        st.session_state[_VIEW_KEY] = pending
    elif _VIEW_KEY not in st.session_state:
        st.session_state[_VIEW_KEY] = VIEW_BATCH if _has_active_batch(svc) else VIEW_NEW
    view = st.segmented_control("View", VIEWS, key=_VIEW_KEY, required=True)
    _show_flash()

    if view == VIEW_NEW:
        _render_new_batch(svc)
    elif view == VIEW_HISTORY:
        _render_history(svc)
    else:
        _render_batch_view(svc)


def _has_active_batch(svc: BatchService) -> bool:
    if svc.busy:
        return True
    with svc.open_store() as store:
        return store.active_batch() is not None


def _render_recovery_notice(svc: BatchService) -> None:
    if svc.recovery_error:
        st.warning(svc.recovery_error, icon=":material/warning:")
    recovered = [report for report in svc.recovery_reports if report.recovered]
    if recovered and not st.session_state.get(_RECOVERY_SHOWN_KEY):
        st.session_state[_RECOVERY_SHOWN_KEY] = True
        lines = ", ".join(f"{report.batch_id[:8]} → {report.batch_status}" for report in recovered)
        st.info(
            f"Recovered {len(recovered)} batch run(s) whose runner had stopped: {lines}. "
            "Nothing was resumed automatically — use Resume when you are ready.",
            icon=":material/healing:",
        )


# ── ساختِ batch ──────────────────────────────────────────────────────────────
def _render_new_batch(svc: BatchService) -> None:
    st.subheader("1 · Board", divider="gray")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        name = st.text_input(
            "Board name contains", key="batch_board_filter", placeholder="All boards"
        )
        load = st.button("Load boards", icon=":material/sync:", key="batch_load_boards")
    if load:
        try:
            with jira_session() as client:
                st.session_state[_BOARDS_KEY] = list_boards(client, name=name.strip())
        except JiraError as exc:
            st.error(f"The boards could not be loaded: {exc}", icon=":material/error:")
            return
        st.session_state.pop(_DISCOVERY_KEY, None)

    boards = st.session_state.get(_BOARDS_KEY)
    if boards is None:
        st.info("Load the Jira boards you can see to start a new batch.")
        return
    if not boards:
        st.warning("No board matches this filter, or this account cannot see any board.")
        return
    board = st.selectbox(
        "Board",
        boards,
        format_func=lambda item: (
            f"{item.name} · #{item.id} · {item.type or 'unknown type'}"
            + (f" · {item.project_key}" if item.project_key else "")
        ),
        key="batch_board",
    )
    if st.button("Discover issues", icon=":material/search:", key="batch_discover"):
        try:
            with st.spinner("Reading the board from Jira…"), jira_session() as client:
                st.session_state[_DISCOVERY_KEY] = discover(client, board.id)
        except (JiraError, DiscoveryError) as exc:
            st.session_state.pop(_DISCOVERY_KEY, None)
            st.error(f"The board could not be evaluated: {exc}", icon=":material/error:")
            return

    result: DiscoveryResult | None = st.session_state.get(_DISCOVERY_KEY)
    if result is None or result.context.board_id != board.id:
        st.info("Discover the issues of the selected board.")
        return
    _render_discovery(svc, result)


def _sprint_cell(candidate) -> dict[str, str]:
    """ستونِ اطلاعاتیِ اسپرینت — فقط بُردِ scrum؛ در حکم اثری ندارد."""
    if candidate.in_active_sprint is None:
        return {}
    return {"Active sprint": "Yes" if candidate.in_active_sprint else "No"}


def _render_discovery(svc: BatchService, result: DiscoveryResult) -> None:
    context, criteria = result.context, result.criteria
    st.subheader("2 · Issues", divider="gray")
    st.caption(
        f"Board **{context.board_name}** (#{context.board_id}, {context.board_type}). "
        f"Eligible only when all three hold: status is exactly '{criteria.status}'; the issue "
        f"is in the '{context.column_name}' column of this board (by the board's status-to-column "
        f"mapping); and the exact label '{criteria.label}' is present."
        + (
            " Active-sprint membership is shown for information only; it does not affect "
            "eligibility."
            if context.is_scrum
            else ""
        )
    )
    eligible, ineligible = result.eligible, result.excluded
    with st.container(horizontal=True):
        st.metric("Discovered", len(result.candidates), border=True)
        st.metric("Eligible", len(eligible), border=True)
        st.metric("Not eligible", len(ineligible), border=True)

    if ineligible:
        with st.expander(f"Not eligible ({len(ineligible)}) — with reasons"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Issue": candidate.key,
                            "Type": candidate.issue_type,
                            "Status": candidate.status_name,
                            "Labels": ", ".join(candidate.labels),
                            "Parent": candidate.parent_key,
                            "Failed criteria": ", ".join(candidate.failed_criteria),
                            "Reasons": "; ".join(candidate.reasons),
                        }
                        for candidate in ineligible
                    ]
                ),
                hide_index=True,
            )
    if not eligible:
        st.warning("No issue on this board meets all eligibility criteria.")
        return

    st.caption("All eligible issues are included. Untick an issue to leave it out of the batch.")
    edited = st.data_editor(
        pd.DataFrame(
            [
                {
                    "Include": True,
                    "Issue": candidate.key,
                    "Summary": candidate.summary,
                    "Type": candidate.issue_type,
                    "Status": candidate.status_name,
                    "Labels": ", ".join(candidate.labels),
                    "Parent": candidate.parent_key,
                    "Eligibility": "Eligible",
                    **_sprint_cell(candidate),
                }
                for candidate in eligible
            ]
        ),
        column_config={"Include": st.column_config.CheckboxColumn("Include", width="small")},
        disabled=[
            "Issue",
            "Summary",
            "Type",
            "Status",
            "Labels",
            "Parent",
            "Eligibility",
            "Active sprint",
        ],
        hide_index=True,
        key=f"batch_include_{context.board_id}",
    )
    left_out = [str(key) for key, keep in zip(edited["Issue"], edited["Include"]) if not keep]
    selected = len(eligible) - len(left_out)
    if left_out:
        st.caption(f"Left out by you ({len(left_out)}): {', '.join(left_out)}")

    st.subheader("3 · Configuration", divider="gray")
    try:
        snapshot = configuration_snapshot()
    except ConfigurationError as exc:
        snapshot = None
        st.error(f"{exc} A batch cannot be created.", icon=":material/error:")
    else:
        st.caption(
            "The batch records these files by hash and refuses to run if they change. "
            "Their contents are never shown."
        )
        for label, path, digest in (
            ("Environment", snapshot.environment_path, snapshot.environment_sha256),
            ("Globals", snapshot.globals_path, snapshot.globals_sha256),
        ):
            st.markdown(f"- **{label}**: `{Path(path).name}` · SHA-256 `{digest[:12]}…`")

    if selected == 0:
        st.warning("Select at least one issue to create a batch.")
    if st.button(
        f"Create batch with {selected} issue{'s' if selected != 1 else ''}",
        type="primary",
        icon=":material/add_task:",
        disabled=snapshot is None or selected == 0,
        key="batch_create",
    ):
        try:
            with svc.open_store() as store:
                batch = create_batch(store, result, left_out)  # هش همین لحظه دوباره خوانده می‌شود
        except (ValueError, ConfigurationError) as exc:
            st.error(str(exc), icon=":material/error:")
            return
        st.session_state.pop(_DISCOVERY_KEY, None)
        _flash(True, f"Batch {batch.id[:8]} was created with {selected} issue(s). Start it below.")
        _go_to(VIEW_BATCH, batch.id)
        st.rerun()


# ── داشبوردِ batch ───────────────────────────────────────────────────────────
def _render_batch_view(svc: BatchService) -> None:
    with svc.open_store() as store:
        batches = store.list_batches(HISTORY_LIMIT)
        selected = st.session_state.get(_BATCH_KEY)
        if selected and selected not in {batch.id for batch in batches}:
            try:
                batches.insert(0, store.get_batch(selected))
            except Exception:  # noqa: BLE001 — batchِ حذف‌شده/نامعتبر در session
                selected = None
        active = store.active_batch()
    if not batches:
        st.info("There is no batch yet. Create one in the New batch view.")
        return
    if not selected:
        job = svc.job
        selected = (job.batch_id if job else "") or (active.id if active else "") or batches[0].id
    ids = [batch.id for batch in batches]
    labels = {batch.id: batch_label(batch) for batch in batches}
    choice = st.selectbox(
        "Batch", ids, index=ids.index(selected), format_func=labels.get, key="batch_pick"
    )
    st.session_state[_BATCH_KEY] = choice
    live = _is_live(svc, next(batch for batch in batches if batch.id == choice))
    st.fragment(_dashboard, run_every=REFRESH_S if live else None)(svc, choice, live)


def _is_live(svc: BatchService, batch: Batch) -> bool:
    job = svc.job_for(batch.id)
    return batch.status == BATCH_RUNNING or (job is not None and job.running)


def _dashboard(svc: BatchService, batch_id: str, was_live: bool) -> None:
    with svc.open_store() as store:
        batch = store.get_batch(batch_id)
        rows = issue_rows(store.batch_issues(batch_id), store.executions(batch_id))
    if was_live and not _is_live(svc, batch):
        st.rerun()  # اجرا تمام شد — بازخوانیِ کاملِ صفحه، نوسازیِ خودکار متوقف می‌شود
    counts = count(rows)
    health = runner_health(batch, stale_after=svc.stale_after)
    job = svc.job_for(batch_id)

    # اجرای RUNNINGِ باقی‌مانده در batchِ ایستاده — هیچ runnerی آن را پیش نمی‌برد
    leftover = (
        [row.issue_key for row in rows if row.status == EXEC_RUNNING]
        if batch.status in (BATCH_PAUSED, BATCH_INTERRUPTED)
        else []
    )

    _render_batch_header(batch, counts, health)
    _render_controls(svc, batch, health, job, leftover)
    _render_progress(batch, rows, counts, leftover)
    _render_issues(svc, batch, rows)


def _render_batch_header(batch: Batch, counts: BatchCounts, health: str) -> None:
    with st.container(horizontal=True, vertical_alignment="center"):
        st.subheader(f"Batch {batch.id[:8]}", width="content")
        st.badge(batch.status, color=BATCH_COLORS.get(batch.status, "gray"))
        if health == HEALTH_LIVE:
            st.badge("runner live", icon=":material/favorite:", color="blue")
        elif health == HEALTH_STALE:
            st.badge("runner lost", icon=":material/heart_broken:", color="red")
    st.caption(
        f"Board **{batch.board_name}** (#{batch.board_id}, {batch.board_type}, column "
        f"'{batch.column_name}') · ID `{batch.id}` · Created {when(batch.created_at)} · "
        f"Started {when(batch.started_at)} · Finished {when(batch.finished_at)} · "
        f"Last heartbeat {when(batch.heartbeat_at)}"
    )
    with st.container(horizontal=True):
        for label, value in (
            ("Selected", counts.total),
            ("Completed", counts.completed),
            ("Pending", counts.pending),
            ("Running", counts.running),
            ("Interrupted", counts.interrupted),
        ):
            st.metric(label, value, border=True)
    with st.container(horizontal=True):
        for label, value in (
            ("Passed", counts.passed),
            ("Failed", counts.failed),
            ("Technical errors", counts.errors),
            ("Not executed", counts.not_executed),
            ("Cancelled", counts.cancelled),
            ("Needs review", counts.needs_review),
        ):
            st.metric(label, value, border=True)


def _render_controls(
    svc: BatchService, batch: Batch, health: str, job: BatchJob | None, leftover: list[str]
) -> None:
    status, requested = batch.status, batch.requested_action
    retry_running = job is not None and job.running and job.mode == MODE_RETRY
    hosted_here = job is not None and job.running
    elsewhere = svc.busy and not hosted_here
    problem = "" if status == BATCH_RUNNING else configuration_problem(batch)

    with st.container(horizontal=True):
        if status == BATCH_CREATED and st.button(
            "Start",
            type="primary",
            icon=":material/play_arrow:",
            disabled=svc.busy or bool(problem),
            key="batch_start",
        ):
            _act(svc.start(batch.id))
        if status == BATCH_RUNNING and st.button(
            "Pause",
            icon=":material/pause:",
            disabled=health != HEALTH_LIVE or requested != ACTION_NONE,
            help="The current issue finishes first.",
            key="batch_pause",
        ):
            _act(svc.pause(batch.id))
        if status in (BATCH_PAUSED, BATCH_INTERRUPTED) and st.button(
            "Resume",
            type="primary",
            icon=":material/play_arrow:",
            disabled=svc.busy or health == HEALTH_LIVE or bool(problem),
            help="Completed issues are kept; interrupted issues run again as new attempts.",
            key="batch_resume",
        ):
            _act(svc.resume(batch.id))
        cancel_batch = status == BATCH_RUNNING and requested != ACTION_CANCEL
        cancel_retry = retry_running and not job.cancel_requested
        if (cancel_batch or cancel_retry) and st.button(
            "Cancel retry" if cancel_retry else "Cancel batch",
            icon=":material/stop:",
            key="batch_cancel",
        ):
            _act(svc.cancel(batch.id))
        if status in (BATCH_CREATED, BATCH_PAUSED, BATCH_INTERRUPTED) and not hosted_here:
            # لغوِ مستقیم و پایانی (بدونِ resume) — با تأییدِ دوم
            with st.popover("Cancel batch", icon=":material/stop:", disabled=bool(leftover)):
                st.caption(
                    "Cancels the batch without running it. Queued issues are cancelled; "
                    "finished results, interrupted attempts and artifacts are kept. "
                    "This cannot be undone."
                )
                if st.button("Yes, cancel the batch", type="primary", key="batch_cancel_inactive"):
                    _act(svc.cancel(batch.id))
        if (
            status == BATCH_RUNNING
            and health != HEALTH_LIVE
            and not hosted_here
            and st.button("Recover", icon=":material/healing:", key="batch_recover")
        ):
            reports = [report for report in svc.recover() if report.batch_id == batch.id]
            recovered = any(report.recovered for report in reports)
            _flash(
                recovered,
                "The stopped run was recovered; use Resume to continue."
                if recovered
                else "Nothing was recovered — the runner may still be alive.",
            )
            st.rerun()

    if elsewhere:
        st.caption("Another batch run is in progress in this application; wait for it to finish.")
    if requested == ACTION_PAUSE and status == BATCH_RUNNING:
        st.info("Pause requested — the current issue finishes before the batch pauses.")
    if requested == ACTION_CANCEL and status == BATCH_RUNNING:
        st.warning("Cancellation requested — waiting for the runner to confirm it.")
    if retry_running:
        number = _retry_attempt(svc, job)
        attempt = f" (attempt {number})" if number else ""
        st.info(f"Retrying {job.issue_key}{attempt}.", icon=":material/replay:")
        if job.cancel_requested:
            st.warning("Cancellation requested — waiting for the runner to confirm it.")
    if status == BATCH_RUNNING and health == HEALTH_STALE:
        st.warning(
            "The runner of this batch stopped sending heartbeats. Use Recover to mark the "
            "stopped run as interrupted, then Resume.",
            icon=":material/heart_broken:",
        )
    elif status == BATCH_RUNNING and not hosted_here and health == HEALTH_LIVE:
        st.caption("This batch is running in another application process.")
    if status in BATCH_FINAL and not hosted_here and health == HEALTH_LIVE:
        # retry در پروسه‌ی دیگری اجرا می‌شود؛ request_cancel() فقط آن‌جا در دسترس است
        st.caption(
            "A retry of this batch is running in another server process; it can only be "
            "cancelled from that process."
        )
    if leftover:
        st.warning(
            f"{', '.join(leftover)} is still marked RUNNING by a run that stopped before "
            "finishing it. Nothing is running it now, but it is never overwritten from here, so "
            "the batch cannot be cancelled directly. Use Resume: it takes this batch's lease "
            "first, marks that run INTERRUPTED (results and artifacts are kept) and continues; "
            "you can then Pause or Cancel, and the issue in progress finishes first.",
            icon=":material/warning:",
        )
    if problem:
        st.warning(f"{problem} Restore the file before running this batch.")
    if job is not None and not job.running and job.result is not None:
        _render_job_result(job)


def _retry_attempt(svc: BatchService, job: BatchJob) -> int:
    """شماره‌ی تلاشِ تازه‌ی retry، وقتی ساخته شده باشد."""
    with svc.open_store() as store:
        latest = store.latest_executions(job.batch_id).get(job.issue_key)
    return latest.attempt if latest and latest.status in (EXEC_QUEUED, EXEC_RUNNING) else 0


def _render_job_result(job: BatchJob) -> None:
    result = job.result
    message = _RESULT_MESSAGES.get(result.status, f"The last run ended: {result.status}.")
    if result.status == RESULT_REFUSED or result.error:
        st.error(f"{message} {result.error}".strip(), icon=":material/error:")
    else:
        st.caption(f"Last run in this application: {message}")


def _act(outcome) -> None:
    _flash(outcome.ok, outcome.message)
    st.rerun()


def _render_progress(
    batch: Batch, rows: list[IssueRow], counts: BatchCounts, leftover: list[str]
) -> None:
    st.progress(
        counts.progress,
        text=f"{counts.completed} of {counts.total} issues completed",
    )
    # اجرای باقی‌مانده «در حالِ اجرا» نشان داده نمی‌شود؛ هشدارِ کنترل‌ها توضیحش را دارد
    current = None if leftover else current_row(rows)
    if current is not None and current.latest is not None:
        st.markdown(
            f":material/play_circle: **{current.issue_key}** — phase "
            f"`{current.latest.phase}` · attempt {current.latest.attempt} · last update "
            f"{when(current.latest.heartbeat_at or current.latest.started_at)}"
        )
    elif batch.status == BATCH_RUNNING:
        st.caption("Waiting for the next issue…")


def _render_issues(svc: BatchService, batch: Batch, rows: list[IssueRow]) -> None:
    st.subheader("Issues", divider="gray")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        statuses = st.multiselect(
            "Execution status",
            sorted({row.status for row in rows}),
            format_func=execution_label,
            key=f"batch_filter_status_{batch.id}",
            placeholder="All",
        )
        needle = st.text_input("Issue key or summary", key=f"batch_filter_text_{batch.id}")
    shown = [
        row
        for row in rows
        if (not statuses or row.status in statuses)
        and (
            not needle
            or needle.casefold() in row.issue_key.casefold()
            or needle.casefold() in row.issue.summary.casefold()
        )
    ]
    event = st.dataframe(
        issue_table(shown),
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"batch_issue_table_{batch.id}",
    )
    picked = event.selection.rows if event is not None else []
    if not picked or picked[0] >= len(shown):
        st.caption("Select an issue to see its attempts, review reasons and artifacts.")
        return
    _render_issue_details(svc, batch, shown[picked[0]])


def _render_issue_details(svc: BatchService, batch: Batch, row: IssueRow) -> None:
    latest = row.latest
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f"**{row.issue_key}** — {row.issue.summary}")
            st.badge(execution_label(row.status), color="gray")
            if latest is not None and latest.review_status == REVIEW_NEEDS_REVIEW:
                st.badge("NEEDS_REVIEW", icon=":material/rate_review:", color="orange")
        st.caption(
            f"{row.issue.issue_type or 'Issue'}"
            + (f" · parent {row.issue.parent_key}" if row.issue.parent_key else "")
            + (f" · labels {', '.join(row.issue.labels)}" if row.issue.labels else "")
        )
        _render_retry(svc, batch, row)
        if not row.attempts:
            st.caption("No attempt yet.")
            return
        for execution in reversed(row.attempts):
            _render_attempt(batch, row, execution, expanded=execution is latest)


def _render_retry(svc: BatchService, batch: Batch, row: IssueRow) -> None:
    latest = row.latest
    reason = ""
    if batch.status not in BATCH_FINAL:
        reason = "Issues can be retried once the batch is completed or cancelled."
    elif latest is None or not latest.final:
        reason = "Only an issue with a finished attempt can be retried."
    elif svc.busy:
        reason = "A run is already in progress in this application."
    elif configuration_problem(batch):
        reason = configuration_problem(batch)
    if st.button(
        "Retry this issue",
        icon=":material/replay:",
        disabled=bool(reason),
        help=reason or "Runs Steps 1–5 and Newman again as a new attempt.",
        key=f"batch_retry_{batch.id}_{row.issue_key}",
    ):
        _act(svc.retry(batch.id, row.issue_key))


def _render_attempt(batch: Batch, row: IssueRow, execution: Execution, *, expanded: bool) -> None:
    title = (
        f"Attempt {execution.attempt} · {execution.trigger} · "
        f"{execution_label(execution.status)}"
        + (" · NEEDS_REVIEW" if execution.review_status == REVIEW_NEEDS_REVIEW else "")
    )
    with st.expander(title, expanded=expanded):
        previous = next(
            (item for item in row.attempts if item.id == execution.previous_execution_id), None
        )
        st.caption(
            f"Started {when(execution.started_at)} · Finished {when(execution.finished_at)}"
            + (f" · follows attempt {previous.attempt}" if previous else "")
        )
        st.markdown(
            f"- Execution: **{execution.status}** (phase `{execution.phase}`)\n"
            f"- Review: **{execution.review_status}** · Final review: "
            f"**{execution.final_review_status or '—'}**\n"
            f"- Newman: {execution.newman_status or '—'} · requests "
            f"{_number(execution.requests_executed)} · assertions failed "
            f"{_number(execution.assertions_failed)} of {_number(execution.assertions_total)}\n"
            f"- Test cases: {_number(execution.cases_executable)} runnable of "
            f"{_number(execution.cases_total)}"
        )
        if execution.error_kind or execution.error_message:
            st.error(
                f"**{execution.error_kind or 'Error'}** at `{execution.error_stage or '—'}`: "
                f"{execution.error_message}",
                icon=":material/error:",
            )
        if execution.review_reasons:
            st.markdown("**Why it needs review**")
            st.markdown("\n".join(f"- {_reason(item)}" for item in execution.review_reasons))
        _render_artifacts(batch, row, execution)


def _number(value: int | None) -> str:
    return "—" if value is None else str(value)


def _reason(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    code = item.get("code") or item.get("kind") or ""
    message = item.get("message") or ""
    case = item.get("case_id") or ""
    return " · ".join(str(part) for part in (code, case, message) if part)


def _render_artifacts(batch: Batch, row: IssueRow, execution: Execution) -> None:
    artifacts = execution_artifacts(batch, execution)
    available = [artifact for artifact in artifacts if artifact.available]
    if not available:
        st.caption(artifacts[0].reason if artifacts else "No artifact is available.")
        return
    st.markdown("**Artifacts**")
    with st.container(horizontal=True, gap="small"):
        for artifact in available:
            st.download_button(
                artifact.label,
                data=lambda name=artifact.name: read_artifact(batch, execution, name),
                file_name=f"{row.issue_key}_attempt{execution.attempt}_{artifact.name}",
                mime=artifact.mime,
                icon=":material/download:",
                on_click="ignore",
                key=f"batch_artifact_{execution.id}_{artifact.name}",
            )
    missing = [artifact.label for artifact in artifacts if not artifact.available]
    if missing:
        st.caption(f"Unavailable for this attempt: {', '.join(missing)}.")


# ── تاریخچه ─────────────────────────────────────────────────────────────────
def history_table(svc: BatchService, batches: list[Batch]) -> pd.DataFrame:
    records = []
    with svc.open_store() as store:
        for batch in batches:
            counts = count(issue_rows(store.batch_issues(batch.id), store.executions(batch.id)))
            records.append(
                {
                    "Batch": batch.id[:8],
                    "Board": batch.board_name,
                    "Created": when(batch.created_at),
                    "Finished": when(batch.finished_at),
                    "Status": batch.status,
                    "Issues": counts.total,
                    "PASS": counts.passed,
                    "FAIL": counts.failed,
                    "ERROR": counts.errors,
                    "NOT_EXECUTED": counts.not_executed,
                }
            )
    return pd.DataFrame(records)


def _render_history(svc: BatchService) -> None:
    with svc.open_store() as store:
        batches = store.list_batches(HISTORY_LIMIT)
    with st.container(horizontal=True, vertical_alignment="center"):
        st.subheader("Batch history", width="content")
        if st.button(
            "Check for stopped runs",
            icon=":material/healing:",
            help="Marks runs whose runner stopped as interrupted. Nothing is resumed.",
            key="batch_history_recover",
        ):
            recovered = [report for report in svc.recover() if report.recovered]
            _flash(True, f"{len(recovered)} stopped run(s) recovered. Nothing was resumed.")
            st.rerun()
    if not batches:
        st.info("There is no batch yet.")
        return
    event = st.dataframe(
        history_table(svc, batches),
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="batch_history_table",
    )
    picked = event.selection.rows if event is not None else []
    if (
        picked
        and picked[0] < len(batches)
        and st.button(
            f"Open batch {batches[picked[0]].id[:8]}",
            type="primary",
            icon=":material/open_in_new:",
            key="batch_history_open",
        )
    ):
        _go_to(VIEW_BATCH, batches[picked[0]].id)
        st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="Batch testing", page_icon=":material/playlist_play:", layout="wide"
    )
    render()


__all__ = [
    "DB_PATH",
    "VIEWS",
    "batch_label",
    "execution_label",
    "history_table",
    "issue_table",
    "render",
    "review_label",
    "service",
    "when",
]
