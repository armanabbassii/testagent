"""
ui/box_app.py — صفحه‌ی TestCase Box: کتابخانه (M6.1)، مدیریتِ case (M6.2) و واردکردن از
Batch (M6.3)

کنارِ ویزاردِ تک‌issue و Batch Testing در ui/app.py (ناوبریِ st.navigation). مدلِ نمایشی،
فیلتر و پوشاندن در src/testcase_box/library.py است و این‌جا تکرار نمی‌شود. تنها نوشتن‌ها
ویرایشِ عنوان و بایگانی/بازگردانی‌اند و فقط از راهِ APIهای موجودِ BoxStore
(update_case_details، archive_case، restore_case):

    * دیدنِ صفحه هرگز پایگاه را نمی‌سازد: اگر فایلِ DB_PATH نیست، حالتِ خالی نشان داده
      می‌شود و هیچ فایل یا پوشه‌ای ساخته نمی‌شود؛
    * برای هر اجرای صفحه یک اتصالِ SQLiteِ تازه باز و بسته می‌شود (اتصالِ sqlite3 مالِ
      همان threadی است که ساخته‌اش)؛
    * readiness همان M3 است؛ پیکربندی بدونِ پروفایلِ اجرا ارزیابی نمی‌شود؛
    * متنِ اسکریپت و مقدارهای حساس نمایش داده نمی‌شوند (فقط برای نمایش پوشانده می‌شوند)؛
    * کلیدهای session فقط با «box_» یا «_box_» شروع می‌شوند و به وضعیتِ ویزارد
      («workflow_…») یا Batch («batch_…») دست نمی‌زنند.

    * فرمِ عنوان عنوانی را که با آن پر شده به خاطر می‌سپارد؛ اگر عنوان در این فاصله جای
      دیگری عوض شده باشد، ذخیره نمی‌شود و فرم با عنوانِ تازه پر می‌شود؛
    * بایگانی تأیید می‌خواهد و چیزی حذف نمی‌کند؛ caseِ بایگانی‌شده انتخاب‌شده می‌ماند تا
      بشود بازگرداندش، حتی اگر فیلترِ Active آن را از جدول پنهان کند.

    * واردکردن فقط از راهِ APIهای M5 است (discover_attempts، load_attempt، preview_cases،
      import_cases): فهرست، بارگذاری و پیش‌نمایش چیزی نمی‌نویسند و پایگاه را نمی‌سازند؛
      نوشتن فقط در callbackِ دکمه‌ی Import است، با token ِ یک‌بارمصرف (دوبار-کلیک دو بار وارد
      نمی‌کند) و اثرانگشت‌های همان پیش‌نمایشی که کاربر دیده؛ هر case جدا وارد می‌شود و کلِ
      واردکردن اتمی نیست. پیامِ خطا فقط چیزی را می‌گوید که ثبت‌شدنش معلوم است.

    * اجرا (M7.7) در ui/box_runs.py است و فقط از راهِ BoxRunService؛ این صفحه هرگز خودش
      Newman، Gate یا SQLِ نوشتنی را صدا نمی‌زند.

ویرایشِ توضیح در این نسخه نیست.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading

import pandas as pd
import streamlit as st

from src.testcase_box import batch_source as bs
from src.testcase_box import storage as box_storage
from src.testcase_box.batch_import import (
    DEPENDENCIES_NOT_LINKED,
    OUTCOME_ADDED,
    OUTCOME_ALREADY_PRESENT,
    OUTCOME_FAILED,
    OUTCOME_REJECTED,
    PAYLOAD_REJECTED,
    SELECTION_DUPLICATE,
    SELECTION_INVALID,
    SOURCE_CASE_NOT_FOUND,
    SOURCE_CHANGED,
    TITLE_INVALID,
    ImportSelection,
    import_cases,
    preview_cases,
)
from src.testcase_box.batch_source import (
    DEFAULT_BATCH_ARTIFACT_ROOT,
    AttemptRef,
    discover_attempts,
    load_attempt,
)
from src.testcase_box.contract import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_REVIEW,
    SEVERITY_WARNING,
    STATUS_NEEDS_SCRIPT_REVIEW,
    STATUS_NOT_EXECUTABLE,
    STATUS_READY,
)
from src.testcase_box.db import DEFAULT_DB_PATH, StorageError
from src.testcase_box.library import (
    SOURCE_NONE,
    CaseDetail,
    LibraryRow,
    SourceView,
    case_detail,
    filter_rows,
    library_rows,
)
from src.testcase_box.models import (
    CASE_ACTIVE,
    CASE_ARCHIVED,
    IMPORT_COMPLETED,
    IMPORT_FAILED,
    SOURCE_BATCH,
    SOURCE_IMPORT,
    SOURCE_MANUAL,
    TITLE_MAX_CHARS,
    BoxCase,
    case_key,
    validate_title,
)
from src.testcase_box.storage import BoxStore
from ui import box_runs

# پایگاهِ کتابخانه — همان پیش‌فرضِ BoxStore؛ تست‌ها این را به مسیرِ موقت می‌برند
DB_PATH = DEFAULT_DB_PATH
# ریشه‌ی artifactهای Batch — فقط خوانده می‌شود؛ تست‌ها این را هم به مسیرِ موقت می‌برند
BATCH_ROOT = DEFAULT_BATCH_ARTIFACT_ROOT

_SECTION_KEY = "box_section"
_SECTION_LIBRARY = "Library"
_SECTION_IMPORT = "Import from Batch"
_SECTION_RUN = "Run tests"
_IMPORT_RUN_KEY = "box_import_run"
_IMPORT_CASES_PREFIX = "box_import_cases_"
_IMPORT_TITLE_PREFIX = "box_import_title_"
# پیش‌نمایشِ نمایش‌داده‌شده (digest، مرجع، اثرانگشت‌ها) و نتیجه‌ی آخرین Import
_IMPORT_PREVIEW_KEY = "_box_import_preview"
_IMPORT_REPORT_KEY = "_box_import_report"
# token ِ یک‌بارمصرفِ دکمه‌ی Import و پیامِ یک‌باره‌ی کلیکی که چیزی وارد نکرد
_IMPORT_TOKEN_KEY = "_box_import_token"
_IMPORT_NOTICE_KEY = "_box_import_notice"
# یک واردکردن در هر لحظه در این process (همه‌ی sessionها)؛ M5 همچنان محافظِ نهایی است
_IMPORT_LOCK = threading.Lock()

_SEARCH_KEY = "box_search"
_STATUS_KEY = "box_filter_status"
_SOURCE_KEY = "box_filter_source"
_READINESS_KEY = "box_filter_readiness"
_TABLE_KEY_PREFIX = "box_library_table_"
_SELECTED_KEY = "box_selected_case_id"
_VERSION_KEY_PREFIX = "box_version_"
_TITLE_INPUT_PREFIX = "box_title_input_"
_TITLE_BASE_PREFIX = "_box_title_base_"
_TITLE_RESET_PREFIX = "_box_title_reset_"
_CONFIRM_ARCHIVE_KEY = "_box_confirm_archive"
# پیامِ یک‌باره‌ی پس از نوشتن (st.rerun): {"case_id", "kind", "text"}
_FLASH_KEY = "_box_flash"

STATUS_LABELS = {CASE_ACTIVE: "Active", CASE_ARCHIVED: "Archived"}
SOURCE_LABELS = {
    SOURCE_BATCH: "Batch",
    SOURCE_IMPORT: "Import",
    SOURCE_MANUAL: "Manual",
    SOURCE_NONE: "No source",
}
READINESS_LABELS = {
    STATUS_READY: "Ready",
    STATUS_NEEDS_SCRIPT_REVIEW: "Needs script review",
    STATUS_NOT_EXECUTABLE: "Not executable",
}
READINESS_COLORS = {
    STATUS_READY: "green",
    STATUS_NEEDS_SCRIPT_REVIEW: "orange",
    STATUS_NOT_EXECUTABLE: "red",
}
READINESS_ICONS = {
    STATUS_READY: ":material/check_circle:",
    STATUS_NEEDS_SCRIPT_REVIEW: ":material/rate_review:",
    STATUS_NOT_EXECUTABLE: ":material/block:",
}
SEVERITY_LABELS = {
    SEVERITY_ERROR: "Error — blocks execution",
    SEVERITY_REVIEW: "Review — needs a script decision",
    SEVERITY_WARNING: "Warning",
    SEVERITY_INFO: "Info",
}
APPROVAL_LABELS = {
    "NOT_REQUIRED": "Not required (no scripts)",
    "PENDING": "Pending — no decision yet",
    "APPROVED": "Approved",
    "REJECTED": "Rejected",
    "REVOKED": "Revoked",
}


OUTCOME_LABELS = {
    OUTCOME_ADDED: "Added",
    OUTCOME_ALREADY_PRESENT: "Already in library",
    OUTCOME_REJECTED: "Rejected",
    OUTCOME_FAILED: "Failed",
}
CODE_TEXTS = {
    SOURCE_CHANGED: "the Batch run changed after the preview",
    SOURCE_CASE_NOT_FOUND: "the case is no longer in the Batch run",
    SELECTION_DUPLICATE: "the case was selected more than once",
    SELECTION_INVALID: "the selection is not valid",
    TITLE_INVALID: "the title is not valid",
    PAYLOAD_REJECTED: "the request cannot be stored as a test case",
    "LITERAL_CREDENTIAL": "contains a literal credential, which is never stored",
    DEPENDENCIES_NOT_LINKED: "depends on other cases; the dependency is not linked",
    box_storage.SOURCED_CONFLICT: "a library case has this source with a different request",
    box_storage.SOURCED_AMBIGUOUS: "more than one library case matches this source",
    bs.IN_PROGRESS_OR_INTERRUPTED: "the run has not finished (no summary was written)",
    bs.MINIMAL_SUMMARY: "the run stopped early and wrote only a minimal summary",
    bs.STATUS_NOT_ELIGIBLE: "runs with this status cannot be imported",
    bs.INCOMPLETE_WRITE: "the run's files are still being written",
    bs.COLLECTION_MISSING: "the run has no collection",
    bs.STEP4_MISSING: "the run has no step 4 result",
    bs.SUMMARY_MALFORMED: "the run summary is malformed",
    bs.SUMMARY_IDENTITY_MISMATCH: "the run summary belongs to another run",
    bs.SUMMARY_INCONSISTENT: "the run summary is inconsistent",
    bs.STEP_ARTIFACT_INCONSISTENT: "the step results do not match the summary",
    bs.ARTIFACT_MALFORMED: "a run file is malformed",
    bs.ARTIFACT_TOO_LARGE: "a run file is too large",
    bs.ARTIFACT_UNREADABLE: "a run file cannot be read",
    bs.ARTIFACT_CHANGED_WHILE_READING: "a run file changed while it was read",
    bs.COLLECTION_CONTEXT_UNSUPPORTED: "collection-level scripts or auth are not supported",
    bs.CASE_ID_UNAVAILABLE: "the request has no test case id",
    bs.CASE_ID_DUPLICATE: "the test case id appears more than once",
    bs.FOLDER_CONTEXT_UNSUPPORTED: "folder-level scripts or auth are not supported",
    bs.CASE_UNRESOLVED: "step 4 could not resolve this case",
    bs.STEP4_AMBIGUOUS: "step 4 lists this case more than once",
    bs.HOST_INCONSISTENT: "the host variable does not match step 4",
    bs.PATH_INVALID: "the run folder is missing",
    bs.PATH_LINK: "the run folder is a link, which is not allowed",
}


def _label(labels: dict[str, str], value: str) -> str:
    return labels.get(value, value)


def _short(fingerprint: str | None) -> str:
    return fingerprint[:12] + "…" if fingerprint else "—"


# ── صفحه ────────────────────────────────────────────────────────────────────
def render() -> None:
    st.title("TestCase Box", icon=":material/inventory_2:")
    st.caption(
        "A persistent library of test cases, kept separately from the single-issue wizard and "
        "from Batch testing. Here you can browse test cases, edit their titles, archive or "
        "restore them, import them from finished Batch runs, and run script-free test cases "
        "with Newman — one test case per Newman unit. Nothing is ever deleted here."
    )
    section = st.radio(
        "Section",
        [_SECTION_LIBRARY, _SECTION_IMPORT, _SECTION_RUN, box_runs.SECTION_RUNS],
        horizontal=True,
        label_visibility="collapsed",
        key=_SECTION_KEY,
    )
    if section == _SECTION_IMPORT:
        _render_import()
        return
    if section == _SECTION_RUN:
        box_runs.render_new_run(DB_PATH)
        return
    if section == box_runs.SECTION_RUNS:
        box_runs.render_runs(DB_PATH)
        return
    if not DB_PATH.exists():
        _render_empty_library()
        return
    try:
        with BoxStore.open(DB_PATH) as store:
            _render_library(store)
    except (StorageError, sqlite3.Error) as exc:
        st.error(
            f"The TestCase Box library could not be opened ({type(exc).__name__}). "
            "Nothing was changed.",
            icon=":material/error:",
        )


def _render_empty_library() -> None:
    st.info(
        "The library is empty — no TestCase Box database exists yet. Opening this page does "
        "not create one; it is created when test cases are first imported (Import from "
        "Batch above).",
        icon=":material/inventory_2:",
    )


# ── کتابخانه ────────────────────────────────────────────────────────────────
def _render_library(store: BoxStore) -> None:
    rows = library_rows(store)
    if not rows:
        st.info("The library has no test cases yet.", icon=":material/inventory_2:")
        return

    with st.container(horizontal=True, vertical_alignment="bottom"):
        text = st.text_input(
            "Search",
            key=_SEARCH_KEY,
            placeholder="Title, BX key, issue key, case ID or source reference",
        )
        if _STATUS_KEY not in st.session_state:
            st.session_state[_STATUS_KEY] = [CASE_ACTIVE]
        statuses = st.multiselect(
            "Status",
            list(STATUS_LABELS),
            format_func=lambda value: _label(STATUS_LABELS, value),
            key=_STATUS_KEY,
        )
        sources = st.multiselect(
            "Source",
            list(SOURCE_LABELS),
            format_func=lambda value: _label(SOURCE_LABELS, value),
            key=_SOURCE_KEY,
            placeholder="Any source",
        )
        readiness = st.multiselect(
            "Readiness",
            list(READINESS_LABELS),
            format_func=lambda value: _label(READINESS_LABELS, value),
            key=_READINESS_KEY,
            placeholder="Any readiness",
        )

    shown = filter_rows(rows, text=text, statuses=statuses, sources=sources, readiness=readiness)
    flash = st.session_state.pop(_FLASH_KEY, None)
    st.caption(f"Showing {len(shown)} of {len(rows)} test cases.")
    if shown:
        _render_table(shown, (text, statuses, sources, readiness))
    else:
        st.info("No test case matches these filters.", icon=":material/filter_alt_off:")

    selected = st.session_state.get(_SELECTED_KEY)
    if selected is not None and not any(row.case_id == selected for row in rows):
        st.session_state.pop(_SELECTED_KEY, None)
        selected = None
    if st.session_state.get(_CONFIRM_ARCHIVE_KEY) not in (None, selected):
        st.session_state.pop(_CONFIRM_ARCHIVE_KEY, None)
    if selected is None:
        if flash:
            _show_flash(flash)
        return
    if not any(row.case_id == selected for row in shown):
        st.caption(
            "The selected test case is hidden from the list by the current filters. It stays "
            "open below until you select another one."
        )
    _render_details(store, selected, flash if flash and flash["case_id"] == selected else None)


def _show_flash(flash: dict) -> None:
    icons = {"success": ":material/check_circle:", "warning": ":material/warning:"}
    show = {"success": st.success, "warning": st.warning}.get(flash["kind"], st.info)
    show(flash["text"], icon=icons.get(flash["kind"], ":material/info:"))


def library_table(rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Key": row.key,
                "Title": row.title,
                "Status": _label(STATUS_LABELS, row.status),
                "Source": " + ".join(_label(SOURCE_LABELS, s) for s in row.source_types),
                "Readiness": _label(READINESS_LABELS, row.readiness),
                "Version": f"v{row.version_number}",
                "Service (base path · operation)": row.service or "—",
                "Created": row.created_at,
                "Updated": row.updated_at,
            }
            for row in rows
        ]
    )


def _render_table(shown: tuple[LibraryRow, ...], filters) -> None:
    # کلیدِ جدول با فیلترها و ردیف‌های نمایش‌داده عوض می‌شود تا انتخابِ قدیمی (مثلاً پس از
    # بایگانی) به ردیفِ اشتباه اشاره نکند
    ids = tuple(row.case_id for row in shown)
    digest = hashlib.sha256(repr((filters, ids)).encode("utf-8")).hexdigest()[:12]
    event = st.dataframe(
        library_table(shown),
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=_TABLE_KEY_PREFIX + digest,
    )
    picked = event.selection.rows if event is not None else []
    if picked and picked[0] < len(shown):
        st.session_state[_SELECTED_KEY] = shown[picked[0]].case_id
    elif st.session_state.get(_SELECTED_KEY) is None:
        st.caption("Select a test case to see its request, readiness, provenance and versions.")


# ── جزئیات ──────────────────────────────────────────────────────────────────
def _render_details(store: BoxStore, case_id: int, flash: dict | None = None) -> None:
    version_key = f"{_VERSION_KEY_PREFIX}{case_id}"
    options = [version.id for version in store.versions(case_id)]
    if st.session_state.get(version_key) not in options:
        st.session_state[version_key] = store.get_case(case_id).current_version_id
    detail = case_detail(store, case_id, st.session_state[version_key])
    versions = detail.versions

    st.divider()
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        st.subheader(f"{detail.key} · {detail.case.title}", width="content")
        st.badge(_label(STATUS_LABELS, detail.case.status), color="gray")
        for source in sorted({s.source_type for s in detail.sources}) or [SOURCE_NONE]:
            st.badge(_label(SOURCE_LABELS, source), icon=":material/source:", color="blue")
        status = detail.readiness.status
        st.badge(
            _label(READINESS_LABELS, status),
            icon=READINESS_ICONS.get(status),
            color=READINESS_COLORS.get(status, "gray"),
        )
    if flash:
        _show_flash(flash)
    _render_manage(store, detail)
    st.selectbox(
        "Version",
        options,
        format_func=lambda vid: _version_label(versions, vid),
        key=version_key,
        help="The current version is shown by default. Earlier versions are read-only history.",
    )

    overview, request, readiness, provenance, history = st.tabs(
        ["Overview", "Request", "Readiness", "Provenance", "Versions"]
    )
    with overview:
        _render_overview(detail)
    with request:
        _render_request(detail)
    with readiness:
        _render_readiness(detail)
    with provenance:
        _render_provenance(detail)
    with history:
        _render_versions(detail)


# ── مدیریت (M6.2) ───────────────────────────────────────────────────────────
def _render_manage(store: BoxStore, detail: CaseDetail) -> None:
    with st.expander("Manage test case", icon=":material/edit_note:"):
        _render_title_form(store, detail)
        st.divider()
        _render_status_action(store, detail)


def _sync_title_form(case: BoxCase) -> tuple[str, str]:
    """فرم را با عنوانِ فعلی پر می‌کند مگر ویرایشی ثبت‌شده در راه باشد.

    مقدارِ فرم فقط با submit به session می‌رسد؛ پس اگر مقدارِ فرم هنوز همان عنوانی است که با
    آن پر شده (base)، چیزی از دستِ کاربر نمی‌رود و فرم دنبالِ عنوانِ تازه‌ی کتابخانه می‌رود.
    """
    state = st.session_state
    input_key = f"{_TITLE_INPUT_PREFIX}{case.id}"
    base_key = f"{_TITLE_BASE_PREFIX}{case.id}"
    reset = state.pop(f"{_TITLE_RESET_PREFIX}{case.id}", False)
    if reset or base_key not in state or input_key not in state:
        state[input_key] = state[base_key] = case.title
    elif state[base_key] != case.title:
        if state[input_key] == state[base_key]:
            state[input_key] = state[base_key] = case.title
        elif state[input_key] == case.title:
            state[base_key] = case.title
    return input_key, base_key


def _render_title_form(store: BoxStore, detail: CaseDetail) -> None:
    case = detail.case
    input_key, base_key = _sync_title_form(case)
    with st.form(f"box_title_form_{case.id}", border=False):
        st.text_input(
            "Title",
            key=input_key,
            help=f"Up to {TITLE_MAX_CHARS} characters. Only the library title changes; the "
            "request and its versions stay as they are.",
        )
        submitted = st.form_submit_button(
            "Save title", key=f"box_title_save_{case.id}", icon=":material/save:"
        )
    st.caption("The description cannot be edited in this version of the page.")
    if submitted:
        _save_title(store, case.id, st.session_state[input_key], st.session_state[base_key])


def _save_title(store: BoxStore, case_id: int, text: str, base: str) -> None:
    title = text.strip()
    try:
        current = store.get_case(case_id)
        if base != current.title:
            # عنوان پس از پر شدنِ فرم جای دیگری عوض شده — ویرایشِ کهنه رویش نوشته نمی‌شود
            _finish(
                case_id,
                "warning",
                "The title was changed somewhere else after this form was opened. Your edit "
                "was not saved; the form now shows the current title.",
                reset_title=True,
            )
        if title == current.title:
            st.info("The title is unchanged. Nothing was saved.", icon=":material/info:")
            return
        store.update_case_details(case_id, title=title)
    except ValueError as exc:
        message = str(exc)
        st.error(f"Title not saved: {message[:1].upper()}{message[1:]}", icon=":material/error:")
        return
    except (StorageError, sqlite3.Error) as exc:
        st.error(
            f"Title not saved: the library could not be updated ({type(exc).__name__}). "
            "Nothing was changed.",
            icon=":material/error:",
        )
        return
    _finish(case_id, "success", "Title saved.", reset_title=True)


def _render_status_action(store: BoxStore, detail: CaseDetail) -> None:
    case = detail.case
    if case.status == CASE_ARCHIVED:
        st.caption(
            "This test case is archived. Restoring makes it active again; its versions, "
            "sources and history were kept."
        )
        if st.button("Restore", key=f"box_restore_{case.id}", icon=":material/unarchive:"):
            _change_status(store, detail, CASE_ACTIVE)
        return
    if st.session_state.get(_CONFIRM_ARCHIVE_KEY) != case.id:
        st.caption(
            "Archiving hides the test case from the default (Active) library view. Its "
            "versions, sources and history are kept and it can be restored at any time. "
            "Nothing is deleted."
        )
        if not st.button("Archive…", key=f"box_archive_{case.id}", icon=":material/archive:"):
            return
        st.session_state[_CONFIRM_ARCHIVE_KEY] = case.id
    st.warning(
        f"Archive {detail.key} · {case.title}? It will be hidden from the Active view. "
        "Nothing is deleted and it can be restored.",
        icon=":material/archive:",
    )
    with st.container(horizontal=True):
        confirm = st.button(
            "Archive test case", type="primary", key=f"box_archive_confirm_{case.id}"
        )
        cancel = st.button("Cancel", key=f"box_archive_cancel_{case.id}")
    if cancel:
        st.session_state.pop(_CONFIRM_ARCHIVE_KEY, None)
        st.rerun()
    if confirm:
        st.session_state.pop(_CONFIRM_ARCHIVE_KEY, None)
        _change_status(store, detail, CASE_ARCHIVED)


def _change_status(store: BoxStore, detail: CaseDetail, status: str) -> None:
    case_id = detail.case.id
    verb = "archived" if status == CASE_ARCHIVED else "restored"
    try:
        before = store.get_case(case_id).status
        (store.archive_case if status == CASE_ARCHIVED else store.restore_case)(case_id)
    except (StorageError, sqlite3.Error) as exc:
        st.error(
            f"{detail.key} could not be {verb} ({type(exc).__name__}). Nothing was changed.",
            icon=":material/error:",
        )
        return
    if before == status:
        _finish(case_id, "info", f"{detail.key} was already {verb}. Nothing was changed.")
    text = f"{detail.key} {verb}. Its versions, sources and history are unchanged."
    statuses = st.session_state.get(_STATUS_KEY) or []
    if statuses and status not in statuses:
        shown = ", ".join(_label(STATUS_LABELS, s) for s in statuses)
        text += f" It is hidden from the list by the Status filter ({shown}) and stays open below."
    _finish(case_id, "success", text)


def _finish(case_id: int, kind: str, text: str, *, reset_title: bool = False) -> None:
    """پیامِ یک‌باره برای اجرای بعد و اجرای دوباره تا کتابخانه و جزئیات تازه خوانده شوند."""
    st.session_state[_FLASH_KEY] = {"case_id": case_id, "kind": kind, "text": text}
    if reset_title:
        st.session_state[f"{_TITLE_RESET_PREFIX}{case_id}"] = True
    st.rerun()


def _version_label(versions, version_id: int) -> str:
    version = next(v for v in versions if v.id == version_id)
    return f"v{version.number}" + (" (current)" if version.current else "")


def _render_overview(detail: CaseDetail) -> None:
    case, version = detail.case, detail.version
    rows = [
        ("Key", detail.key),
        ("Library title", case.title),
        ("Description", case.description or "—"),
        ("Status", _label(STATUS_LABELS, case.status)),
        ("Created", case.created_at),
        ("Updated", case.updated_at),
        ("Shown version", _version_label(detail.versions, version.id)),
        ("Versions", str(len(detail.versions))),
        ("Payload fingerprint", version.fingerprint),
        ("Script fingerprint", version.script_fingerprint or "— (no scripts)"),
    ]
    st.dataframe(
        pd.DataFrame(rows, columns=["Field", "Value"]),
        hide_index=True,
        key=f"box_overview_{case.id}",
    )


def _render_request(detail: CaseDetail) -> None:
    st.caption(
        f"{detail.masked_values} value(s) masked for display. The stored payload is unchanged."
        if detail.masked_values
        else "No values needed masking. The stored payload is shown as stored."
    )
    st.json(detail.payload, expanded=2)
    st.markdown("**Scripts**")
    if not detail.scripts:
        st.caption("This version has no scripts.")
        return
    st.caption(
        "Script text is hidden. Only the event, its line count and the script fingerprint "
        f"({_short(detail.version.script_fingerprint)}) are shown."
    )
    st.dataframe(
        pd.DataFrame(
            [{"Location": s.location, "Event": s.listen, "Lines": s.lines} for s in detail.scripts]
        ),
        hide_index=True,
        key=f"box_scripts_{detail.case.id}_{detail.version.id}",
    )


def _render_readiness(detail: CaseDetail) -> None:
    readiness = detail.readiness
    st.badge(
        _label(READINESS_LABELS, readiness.status),
        icon=READINESS_ICONS.get(readiness.status),
        color=READINESS_COLORS.get(readiness.status, "gray"),
    )
    st.markdown(
        f"Script approval: **{_label(APPROVAL_LABELS, detail.approval_state)}** — "
        "script approval is not available in this version of the page."
    )
    st.info(detail.configuration_note, icon=":material/tune:")
    st.markdown("**Findings**")
    if readiness.findings:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Severity": _label(SEVERITY_LABELS, f.severity),
                        "Code": f.code,
                        "Location": f.location,
                        "Message": f.message,
                    }
                    for f in readiness.findings
                ]
            ),
            hide_index=True,
            key=f"box_findings_{detail.case.id}_{detail.version.id}",
        )
    else:
        st.caption("No findings.")
    st.markdown("**Required variables**")
    if readiness.required_variables:
        st.dataframe(
            pd.DataFrame(
                [
                    {"Variable": v.name, "Kind": v.kind, "Used at": ", ".join(v.locations)}
                    for v in readiness.required_variables
                ]
            ),
            hide_index=True,
            key=f"box_variables_{detail.case.id}_{detail.version.id}",
        )
        st.caption(
            "Host and token variables are supplied at run time; the other variables must come "
            "from an execution profile, which is not evaluated here."
        )
    else:
        st.caption("No variables are referenced.")


def _render_provenance(detail: CaseDetail) -> None:
    if not detail.sources:
        st.caption("No source is recorded for this test case.")
        return
    for source in detail.sources:
        with st.container(border=True):
            _render_source(detail, source)


def _render_source(detail: CaseDetail, source: SourceView) -> None:
    metadata = source.metadata
    st.markdown(f"**{_label(SOURCE_LABELS, source.source_type)}** · `{source.source_reference}`")
    attached = f"v{source.version_number}" if source.version_number else "the case"
    st.caption(f"Recorded {source.created_at} · attached to {attached}")
    if source.source_type == SOURCE_BATCH:
        rows = [
            ("Batch", metadata.get("batch_id")),
            ("Issue", metadata.get("issue_key")),
            ("Attempt", metadata.get("attempt")),
            ("Execution", metadata.get("execution_id")),
            (
                "Executor-reported status",
                f"{metadata.get('executor_reported_status')} (as reported by the Batch "
                "executor, not the final Batch database status)",
            ),
            ("Original source title", "Not available — Batch does not keep it"),
            ("Title at import", metadata.get("title_origin") or "—"),
            ("Host variable", metadata.get("host_variable") or "—"),
            ("Host basis", metadata.get("host_basis") or "—"),
            ("Base path", metadata.get("base_path") or "—"),
            ("Operation", metadata.get("operation_id") or "—"),
            ("Snapshot fingerprint", metadata.get("snapshot_fingerprint")),
            ("Item fingerprint", metadata.get("item_fingerprint")),
            ("Depends on", ", ".join(metadata.get("depends_on") or []) or "—"),
        ]
        st.dataframe(
            pd.DataFrame(
                [(name, "—" if value in (None, "") else str(value)) for name, value in rows],
                columns=["Field", "Value"],
            ),
            hide_index=True,
            key=f"box_source_{source.id}",
        )
        if metadata.get("depends_on") or metadata.get("consumes"):
            st.warning(
                "DEPENDENCIES_NOT_LINKED — this case depended on other cases in Batch. The "
                "dependency is recorded here only; imported cases do not run as an ordered group.",
                icon=":material/link_off:",
            )
    with st.expander("All source metadata (masked)"):
        st.json(metadata, expanded=False)


def _render_versions(detail: CaseDetail) -> None:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Version": f"v{v.number}",
                    "Current": "Yes" if v.current else "",
                    "Created": v.created_at,
                    "Created by": v.created_by or "—",
                    "Payload fingerprint": v.fingerprint,
                    "Script fingerprint": v.script_fingerprint or "—",
                }
                for v in detail.versions
            ]
        ),
        hide_index=True,
        key=f"box_versions_{detail.case.id}",
    )
    st.caption(
        "Versions are immutable. Use the Version selector above to view an earlier version's "
        "request and readiness."
    )


# ── واردکردن از Batch (M6.3) ────────────────────────────────────────────────
def _render_import() -> None:
    st.markdown(
        "Import test cases from finished Batch runs. Each run is read from the Batch artifacts "
        "folder; nothing is executed, and the Batch run and its database are not changed."
    )
    listings = discover_attempts(BATCH_ROOT)
    if not listings:
        _drop_import_state()
        st.info(
            "No Batch runs were found in the Batch artifacts folder.",
            icon=":material/folder_off:",
        )
        return
    eligible = [listing for listing in listings if listing.eligible]
    st.markdown("**Batch runs**")
    st.caption(
        f"{len(listings)} run(s) found · {len(eligible)} can be imported · "
        f"{len(listings) - len(eligible)} cannot."
    )
    st.dataframe(runs_table(listings), hide_index=True, key="box_import_runs")
    if not eligible:
        _drop_import_state()
        st.info("None of these runs can be imported.", icon=":material/block:")
        return

    references = {listing.ref.reference: listing for listing in eligible}
    if st.session_state.get(_IMPORT_RUN_KEY) not in (None, *references):
        st.session_state.pop(_IMPORT_RUN_KEY, None)
    chosen = st.selectbox(
        "Batch run to import from",
        list(references),
        index=None,
        format_func=lambda reference: _run_label(references[reference]),
        key=_IMPORT_RUN_KEY,
        placeholder="Choose a run that can be imported",
    )
    if chosen is None:
        _drop_import_state()
        return
    notice = st.session_state.pop(_IMPORT_NOTICE_KEY, None)
    if notice is not None and notice["reference"] == chosen:
        show = st.warning if notice["kind"] == "warning" else st.info
        show(notice["text"], icon=":material/info:")
    report = st.session_state.get(_IMPORT_REPORT_KEY)
    if report is not None and report["reference"] != chosen:
        st.session_state.pop(_IMPORT_REPORT_KEY, None)
        report = None
    if report is not None:
        _render_import_report(report)
    _render_import_preview(references[chosen].ref)


def _drop_import_state() -> None:
    st.session_state.pop(_IMPORT_PREVIEW_KEY, None)
    st.session_state.pop(_IMPORT_REPORT_KEY, None)
    st.session_state.pop(_IMPORT_NOTICE_KEY, None)


def _run_label(listing) -> str:
    ref = listing.ref
    status = listing.executor_reported_status or "—"
    return f"{ref.issue_key} · attempt {ref.attempt} · batch {ref.batch_id[:8]} · {status}"


def runs_table(listings) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Issue": listing.ref.issue_key,
                "Attempt": listing.ref.attempt,
                "Batch": listing.ref.batch_id,
                "Executor-reported status": listing.executor_reported_status or "—",
                "Cases": len(listing.case_ids),
                "Can import": "Yes" if listing.eligible else "No",
                "Reason": "; ".join(_problem_text(p.code, p.location) for p in listing.problems)
                or "—",
            }
            for listing in listings
        ]
    )


def _code_text(code: str) -> str:
    text = CODE_TEXTS.get(code)
    return f"{code} — {text}" if text else code


def _problem_text(code: str, location: str) -> str:
    return f"{_code_text(code)} ({location})"


def _digest(*parts: object) -> str:
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()[:12]


def _render_import_preview(ref: AttemptRef) -> None:
    attempt = load_attempt(BATCH_ROOT, ref)
    if not attempt.eligible:
        # بینِ فهرست‌کردن و بارگذاری عوض شده است
        st.session_state.pop(_IMPORT_PREVIEW_KEY, None)
        st.error(
            "This run can no longer be imported: "
            + "; ".join(_problem_text(p.code, p.location) for p in attempt.problems),
            icon=":material/error:",
        )
        return
    try:
        if DB_PATH.exists():
            with BoxStore.open(DB_PATH) as store:
                previews = preview_cases(store, attempt)
        else:
            # بدونِ پایگاه هیچ caseی در کتابخانه نیست؛ پیش‌نمایش پایگاه را نمی‌سازد
            previews = preview_cases(None, attempt)
    except (StorageError, sqlite3.Error) as exc:
        st.session_state.pop(_IMPORT_PREVIEW_KEY, None)
        st.error(
            f"The TestCase Box library could not be opened ({type(exc).__name__}). "
            "Nothing was imported.",
            icon=":material/error:",
        )
        return

    digest = _digest(ref.reference, attempt.snapshot_fingerprint)
    # همان چیزی که کاربر دیده — Import فقط با همین اثرانگشت‌ها انجام می‌شود
    st.session_state[_IMPORT_PREVIEW_KEY] = {
        "digest": digest,
        "reference": ref.reference,
        "ref": ref,
        "snapshot": attempt.snapshot_fingerprint,
        "fingerprints": {
            p.source.case_id: p.source.item_fingerprint for p in previews if p.importable
        },
    }
    importable = [p for p in previews if p.importable]
    st.markdown("**Preview**")
    st.caption(
        f"{len(previews)} case(s) in this run · {len(importable)} can be imported · "
        f"{sum(bool(p.existing_case_ids) for p in previews)} already in the library. "
        "Nothing has been written yet."
    )
    st.dataframe(preview_table(previews), hide_index=True, key=f"box_import_preview_{digest}")
    if any(p.warnings for p in previews):
        st.caption(
            "DEPENDENCIES_NOT_LINKED — some cases depended on other cases in Batch. The "
            "dependency is recorded with the imported case only; imported cases do not run as "
            "an ordered group."
        )
    if not importable:
        st.info("No case in this run can be imported.", icon=":material/block:")
        return

    cases_key = f"{_IMPORT_CASES_PREFIX}{digest}"
    if cases_key not in st.session_state:
        st.session_state[cases_key] = [
            p.source.case_id for p in importable if not p.existing_case_ids
        ]
    titles = {p.source.case_id: p.source.default_title for p in importable}
    selected = st.multiselect(
        "Cases to import",
        list(titles),
        key=cases_key,
        placeholder="Choose cases to import",
        help="Only cases that can be imported are listed. Cases already in the library are "
        "not selected by default; importing them again reports them as already present.",
    )
    if selected:
        with st.expander("Titles (optional)"):
            st.caption(
                "Leave a title empty to use the generated title shown. The original Batch "
                "title is not available."
            )
            for case_id in selected:
                st.text_input(
                    case_id,
                    key=f"{_IMPORT_TITLE_PREFIX}{digest}_{case_id}",
                    placeholder=titles[case_id],
                    help=f"Up to {TITLE_MAX_CHARS} characters.",
                )
    st.caption(
        "Each selected case is imported on its own: some can be added while others are "
        "rejected, and cases added before a failure stay in the library. Importing does not "
        "run anything. The library database is created on the first import."
    )
    token = st.session_state.get(_IMPORT_TOKEN_KEY, 0)
    st.button(
        f"Import {len(selected)} selected case(s)",
        type="primary",
        icon=":material/download:",
        disabled=not selected,
        key=f"box_import_go_{digest}_{token}",
        on_click=_import_selected,
        args=(digest, token, ref.reference),
    )


def preview_table(previews) -> pd.DataFrame:
    rows = []
    for preview in previews:
        source = preview.source
        notes = list(preview.warnings)
        for note in preview.dependencies:
            where = [
                w
                for w, flag in (
                    ("in this run", note.in_selection),
                    ("in the library", note.in_library),
                )
                if flag
            ]
            notes.append(f"needs {note.case_id} ({', '.join(where) or 'not available'})")
        rows.append(
            {
                "Case": source.case_id or f"item {source.position + 1}",
                "Generated title": source.default_title or "—",
                "Method": source.metadata.get("method") or "—",
                "Base path": source.metadata.get("base_path") or "—",
                "Readiness": _label(READINESS_LABELS, preview.readiness.status),
                "Can import": "Yes" if preview.importable else "No",
                "In library": ", ".join(case_key(i) for i in preview.existing_case_ids) or "—",
                "Reason": "; ".join(_code_text(code) for code in preview.rejection_codes) or "—",
                "Notes": "; ".join(notes) or "—",
            }
        )
    return pd.DataFrame(rows)


class _ObservedStore(BoxStore):
    """همان BoxStore؛ فقط ثبت می‌کند import_cases تا کجا رسید تا پیامِ خطا چیزی را ادعا نکند
    که معلوم نیست. رفتارِ M5 و پایگاه هیچ تغییری نمی‌کنند."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.job_requested = False
        self.job_id: int | None = None
        self.finish_status: str | None = None
        self.finish_done = False
        self.cases_added = 0

    def create_import_job(self, *args, **kwargs):
        self.job_requested = True
        job = super().create_import_job(*args, **kwargs)
        self.job_id = job.id
        return job

    def finish_import_job(self, job_id, status, summary=None):
        self.finish_status = status
        job = super().finish_import_job(job_id, status, summary)
        self.finish_done = True
        return job

    def create_case_for_source(self, new):
        result = super().create_case_for_source(new)
        if result.created:
            self.cases_added += 1
        return result


def _crash(store: _ObservedStore | None, exc: Exception) -> dict:
    """مرحله‌ای که واردکردن در آن متوقف شد — فقط از روی چیزهایی که واقعاً رخ داده."""
    entry = {"error": type(exc).__name__, "job_id": None, "added": 0}
    if store is None:
        return {**entry, "stage": "open"}
    entry.update(job_id=store.job_id, added=store.cases_added)
    if not store.job_requested:
        stage = "before_job"
    elif store.job_id is None:
        stage = "job_create"
    elif store.finish_status == IMPORT_COMPLETED and not store.finish_done:
        stage = "completion"
    elif store.finish_status == IMPORT_FAILED:
        stage = "processing" if store.finish_done else "processing_unclosed"
    else:
        stage = "unknown"
    return {**entry, "stage": stage}


# مرحله‌هایی که در آن‌ها هیچ caseی نوشته نشده و تلاشِ دوباره معنا دارد
_RETRYABLE_STAGES = frozenset({"open", "before_job", "job_create"})


def _notice(reference: str, kind: str, text: str) -> None:
    st.session_state[_IMPORT_NOTICE_KEY] = {"reference": reference, "kind": kind, "text": text}


def _import_selected(digest: str, token: int, reference: str) -> None:
    """callbackِ دکمه‌ی Import — پیش از اجرای دوباره‌ی صفحه.

    هر دکمه‌ی رسم‌شده یک token ِ یک‌بارمصرف دارد (در کلید و آرگومانش)؛ اولین کلیکِ پذیرفته
    آن را مصرف می‌کند، پس کلیکِ دوم از همان دکمه (دوبار-کلیک) کاری نمی‌کند — مستقل از این‌که
    مرورگر انتخابِ قدیمی را دوباره بفرستد. قفلِ سراسری هم واردکردنِ هم‌زمان را رد می‌کند.
    فقط پیش‌نمایشی که کاربر دیده (همان digest) و اثرانگشت‌های آن وارد می‌شود.
    """
    state = st.session_state
    if token != state.get(_IMPORT_TOKEN_KEY, 0):
        _notice(
            reference,
            "info",
            "This Import click was already handled. Nothing more was imported.",
        )
        return
    state[_IMPORT_TOKEN_KEY] = token + 1
    preview = state.get(_IMPORT_PREVIEW_KEY)
    if not preview or preview["digest"] != digest:
        _notice(
            reference,
            "warning",
            "The preview changed before your click was handled, for example because the Batch "
            "run was updated. Nothing was imported. Review the preview below and click Import "
            "again.",
        )
        return
    cases_key = f"{_IMPORT_CASES_PREFIX}{digest}"
    selections = []
    for case_id in state.get(cases_key) or []:
        if case_id not in preview["fingerprints"]:
            continue
        title = (state.get(f"{_IMPORT_TITLE_PREFIX}{digest}_{case_id}") or "").strip() or None
        if title is not None:
            try:
                validate_title(title)
            except ValueError as exc:
                message = str(exc)
                # چیزی نوشته نشده؛ انتخاب می‌ماند تا عنوان درست و دوباره Import شود
                state[_IMPORT_REPORT_KEY] = {
                    "reference": reference,
                    "invalid": f"{case_id}: {message[:1].upper()}{message[1:]}",
                }
                return
        selections.append(ImportSelection(case_id, preview["fingerprints"][case_id], title))
    if not selections:
        _notice(
            reference,
            "warning",
            "None of the selected cases can be imported from this preview. Nothing was "
            "imported. Choose the cases again and click Import.",
        )
        return
    if not _IMPORT_LOCK.acquire(blocking=False):
        _notice(
            reference,
            "warning",
            "Another import is running in this app. Nothing was imported; click Import again "
            "when it has finished.",
        )
        return
    store = None
    try:
        store = _ObservedStore.open(DB_PATH)
        with store:
            report = import_cases(
                store,
                BATCH_ROOT,
                preview["ref"],
                selections,
                expected_snapshot_fingerprint=preview["snapshot"],
            )
    except Exception as exc:  # noqa: BLE001 — فقط نامِ کلاس نشان داده می‌شود
        crash = _crash(store, exc)
        state[_IMPORT_REPORT_KEY] = {"reference": reference, "crash": crash}
        if crash["stage"] not in _RETRYABLE_STAGES:
            # ممکن است caseهایی نوشته شده باشند؛ کاربر اول پیش‌نمایشِ تازه را ببیند
            state[cases_key] = []
        return
    finally:
        _IMPORT_LOCK.release()
    state[_IMPORT_REPORT_KEY] = {"reference": reference, "report": report}
    state[cases_key] = []


def _crash_text(crash: dict) -> str:
    error, job, added = crash["error"], crash["job_id"], crash["added"]
    kept = (
        f" {added} test case(s) were added before the error and stay in the library."
        if added
        else " Any test case added before the error stays in the library."
    )
    check = " Check the library and the refreshed preview below before importing again."
    retry = " Nothing was imported; you can click Import again."
    stage = crash["stage"]
    if stage == "open":
        return (
            f"The TestCase Box library could not be opened ({error}). No import job was "
            "created." + retry
        )
    if stage == "before_job":
        return f"The import stopped before an import job was created ({error})." + retry
    if stage == "job_create":
        return (
            f"The import job could not be created or confirmed ({error}). No test case was "
            "imported; you can click Import again."
        )
    if stage == "processing":
        return (
            f"The import stopped with an error ({error}). Import job #{job} was recorded as "
            "failed." + kept + check
        )
    if stage == "processing_unclosed":
        return (
            f"The import stopped with an error ({error}) and import job #{job} could not be "
            "closed; its final state is unknown." + kept + check
        )
    if stage == "completion":
        return (
            f"All selected cases were processed, but import job #{job} could not be marked "
            f"completed ({error}); its final state is unknown." + kept + check
        )
    job_text = f" The state of import job #{job} is unknown." if job else ""
    return f"The import stopped with an error ({error}).{job_text}" + kept + check


def _render_import_report(entry: dict) -> None:
    if "invalid" in entry:
        st.error(
            f"Nothing was imported. {entry['invalid']} Fix the title and click Import again.",
            icon=":material/error:",
        )
        return
    if "crash" in entry:
        st.error(_crash_text(entry["crash"]), icon=":material/error:")
        return
    report = entry["report"]
    counts = report.counts
    total = len(report.outcomes)
    added = counts[OUTCOME_ADDED]
    present = counts[OUTCOME_ALREADY_PRESENT]
    summary = (
        f"Added {added} · Already in library {present} · Rejected {counts[OUTCOME_REJECTED]} "
        f"· Failed {counts[OUTCOME_FAILED]} (import job #{report.job_id})"
    )
    if total and added == total:
        st.success(f"Imported {added} test case(s). {summary}", icon=":material/check_circle:")
    elif added:
        st.warning(
            f"Partly imported: {added} of {total} case(s) were added and stay in the library; "
            f"the others were not. Each case is imported on its own. {summary}",
            icon=":material/warning:",
        )
    elif total and present == total:
        st.info(
            f"Nothing new was imported: all selected cases are already in the library. {summary}",
            icon=":material/info:",
        )
    else:
        st.error(f"No test case was imported. {summary}", icon=":material/error:")
    if any(SOURCE_CHANGED in outcome.codes for outcome in report.outcomes):
        st.warning(
            "The Batch run changed after it was previewed, so the affected cases were not "
            "imported. The preview below has been read again; review it and import again.",
            icon=":material/sync_problem:",
        )
    st.dataframe(report_table(report), hide_index=True, key=f"box_import_report_{report.job_id}")


def report_table(report) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Case": outcome.case_id or "—",
                "Outcome": _label(OUTCOME_LABELS, outcome.outcome),
                "Reason": "; ".join(_code_text(code) for code in outcome.codes) or "—",
                "Library key": case_key(outcome.box_case_id) if outcome.box_case_id else "—",
                "Readiness": _label(READINESS_LABELS, outcome.readiness_status)
                if outcome.readiness_status
                else "—",
                "Notes": "; ".join(
                    [*outcome.warnings, *(["archived in the library"] if outcome.archived else [])]
                )
                or "—",
            }
            for outcome in report.outcomes
        ]
    )


def main() -> None:
    st.set_page_config(page_title="TestCase Box", page_icon=":material/inventory_2:", layout="wide")
    render()


__all__ = ["BATCH_ROOT", "DB_PATH", "library_table", "preview_table", "render", "report_table"]
