"""
src/testcase_box/batch_source.py — خواندنِ فقط‌خواندنیِ artifactهای منتشرشده‌ی Batch (M5)

    listings = discover_attempts(root)
    attempt = load_attempt(root, AttemptRef(batch_id, "DEMO-1", 1))

منبع فقط فایل‌های نهاییِ یک تلاش است، نه پایگاهِ Batch (BatchStore.open پوشه می‌سازد،
WAL را روشن و migrate می‌کند). هیچ چیزی از src.batch_testing import نمی‌شود؛ ثابت‌های
لازم این‌جا آینه شده‌اند و تست‌ها هم‌خوانی‌شان را با Batch بررسی می‌کنند.

واقعیت‌های کدِ Batch (IssueExecutor/ArtifactWriter):
  * پوشه‌ی <root>/<batch_id>/<issue_key>/execution_<n> با mkdir(exist_ok=False) ساخته می‌شود
    و هر فایل فقط یک بار (‎.<name>.tmp سپس os.replace) نوشته می‌شود؛
  * ترتیب: step1..5.json ← collection.json (فقط اگر اعتبارسنج‌های قدمِ ۴ پاس شوند و
    قدمِ ۵ مسدود نکند) ← newman/ ← execution_summary.json که همیشه آخر است؛
  * اگر خلاصه پاک‌سازی نشود فقط {execution_id، status، error_kind} نوشته می‌شود و اگر
    پروسه بمیرد هیچ خلاصه‌ای نیست.

تلاش فقط وقتی واجدِ شرایط است که همه‌ی بررسی‌های load_attempt بگذرند (وجودِ فایل کافی
نیست). وضعیتِ خلاصه نظرِ اجراگر است (executor_reported_status) و ممکن است با وضعیتِ نهاییِ
پایگاهِ Batch فرق کند (مثلاً CANCELLED → INTERRUPTED، یا مرگِ پروسه پس از نوشتنِ خلاصه)؛
وضعیتِ پایگاه و وضعیتِ کلِ batch از artifact در دسترس نیست.

یکپارچگی: هر جزءِ مسیر پیش از الحاق با الگو بررسی می‌شود؛ هیچ جزئی symlink یا junction
نیست و realpath باید دقیقاً همان مسیر باشد؛ هر فایل یک بار خوانده می‌شود (lstat ← open ←
fstat با همان dev/ino/size)؛ JSON با UTF-8ِ سخت‌گیر، بی BOM، بی کلیدِ تکراری و بی NaN.
collection_sha256 و snapshot از همان بایت‌های خوانده‌شده و item_fingerprint از همان
دادهِ تجزیه‌شده می‌آیند. هیچ فایلی نوشته و هیچ پوشه‌ای ساخته نمی‌شود.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from src.testcase_box.db import REPO_ROOT
from src.testcase_box.models import SOURCE_BATCH, from_json, sha256_json, to_json
from src.testcase_box.payload import inspect_payload

# ── آینه‌ی ثابت‌های Batch (تست‌ها هم‌خوانی را بررسی می‌کنند) ──────────────────────
DEFAULT_BATCH_ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "batch_testing"
SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")  # executor._SAFE_COMPONENT
CASE_ID_RE = re.compile(r"[A-Za-z]+-\d+")  # نامِ itemِ پاک‌سازی‌شده = گروهِ _CASE_ID_PREFIX
EXECUTION_DIR_RE = re.compile(r"^execution_([1-9]\d{0,5})$")
SUMMARY_FILE = "execution_summary.json"
COLLECTION_FILE = "collection.json"
STEP_FILES = tuple(f"step{step}.json" for step in range(1, 6))
REQUIRED_STEP_FILE = "step4.json"
OUTCOME_FIELDS = frozenset(
    {
        "status",
        "phase",
        "review_status",
        "review_reasons",
        "final_review_status",
        "error_stage",
        "error_kind",
        "error_message",
        "artifact_dir",
        "collection_path",
        "task_source",
        "newman_run_id",
        "newman_status",
        "newman_error_kind",
        "newman_no_assertions",
        "newman_run_dir",
        "newman_result_path",
        "requests_executed",
        "assertions_total",
        "assertions_failed",
        "cases_total",
        "cases_executable",
        "cases_not_executed",
        "newman_executed",
        "not_executed_reason",
        "step_artifacts",
    }
)
SUMMARY_KEYS = OUTCOME_FIELDS | {"batch_id", "issue_key", "execution_id", "attempt"}
MINIMAL_SUMMARY_KEYS = frozenset({"execution_id", "status", "error_kind"})
EXECUTION_FINAL = frozenset({"PASS", "FAIL", "ERROR", "NOT_EXECUTED", "CANCELLED", "INTERRUPTED"})
ELIGIBLE_STATUSES = frozenset({"PASS", "FAIL", "ERROR", "CANCELLED"})
PHASES = frozenset(
    {
        "queued",
        "jira_fetch",
        "step1",
        "step2",
        "step3",
        "step4",
        "step5",
        "newman",
        "finalize",
        "done",
    }
)
REVIEW_STATUSES = frozenset({"NONE", "NEEDS_REVIEW"})
FINAL_REVIEW_STATUSES = frozenset({"", "ready", "needs_review", "blocked"})
HOST_BASES = frozenset({"default", "task", "service"})
HOST_VARIABLES = frozenset({"base_url", "base_url_vdi"})

MAX_BYTES = {SUMMARY_FILE: 1 << 20, COLLECTION_FILE: 20 << 20}
MAX_STEP_BYTES = 5 << 20
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
_IDENT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_.\-/{}*]{0,300}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})

# ── کدهای مسئله ───────────────────────────────────────────────────────────────
ROOT_INVALID = "ROOT_INVALID"
PATH_INVALID = "PATH_INVALID"
PATH_LINK = "PATH_LINK"
INCOMPLETE_WRITE = "INCOMPLETE_WRITE"
IN_PROGRESS_OR_INTERRUPTED = "IN_PROGRESS_OR_INTERRUPTED"
MINIMAL_SUMMARY = "MINIMAL_SUMMARY"
SUMMARY_MALFORMED = "SUMMARY_MALFORMED"
SUMMARY_IDENTITY_MISMATCH = "SUMMARY_IDENTITY_MISMATCH"
STATUS_NOT_ELIGIBLE = "STATUS_NOT_ELIGIBLE"
SUMMARY_INCONSISTENT = "SUMMARY_INCONSISTENT"
COLLECTION_MISSING = "COLLECTION_MISSING"
STEP4_MISSING = "STEP4_MISSING"
STEP_ARTIFACT_INCONSISTENT = "STEP_ARTIFACT_INCONSISTENT"
ARTIFACT_MALFORMED = "ARTIFACT_MALFORMED"
ARTIFACT_TOO_LARGE = "ARTIFACT_TOO_LARGE"
ARTIFACT_UNREADABLE = "ARTIFACT_UNREADABLE"
ARTIFACT_CHANGED_WHILE_READING = "ARTIFACT_CHANGED_WHILE_READING"
COLLECTION_CONTEXT_UNSUPPORTED = "COLLECTION_CONTEXT_UNSUPPORTED"
CASE_ID_UNAVAILABLE = "SOURCE_CASE_ID_UNAVAILABLE"
CASE_ID_DUPLICATE = "SOURCE_CASE_ID_DUPLICATE"
FOLDER_CONTEXT_UNSUPPORTED = "FOLDER_CONTEXT_UNSUPPORTED"
CASE_UNRESOLVED = "SOURCE_CASE_UNRESOLVED"
STEP4_AMBIGUOUS = "STEP4_AMBIGUOUS"
HOST_INCONSISTENT = "SOURCE_HOST_INCONSISTENT"


class _SourceError(Exception):
    def __init__(self, code: str, location: str) -> None:
        super().__init__(code)
        self.code, self.location = code, location


def _safe_component(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(SAFE_COMPONENT_RE.match(value))
        and not value.endswith(".")
        and value.split(".")[0].lower() not in _WINDOWS_RESERVED
    )


@dataclass(frozen=True)
class AttemptRef:
    batch_id: str
    issue_key: str
    attempt: int

    def __post_init__(self) -> None:
        if not _safe_component(self.batch_id) or not _safe_component(self.issue_key):
            raise ValueError("The batch id or issue key is not a safe artifact path component.")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise ValueError("The attempt must be an integer.")
        if not EXECUTION_DIR_RE.match(f"execution_{self.attempt}"):
            raise ValueError("The attempt must be a positive integer.")

    @property
    def directory(self) -> str:
        return f"execution_{self.attempt}"

    @property
    def components(self) -> tuple[str, str, str]:
        return (self.batch_id, self.issue_key, self.directory)

    @property
    def reference(self) -> str:
        return "batch/" + "/".join(self.components)

    def case_reference(self, case_id: str) -> str:
        return f"{self.reference}/{case_id}"


@dataclass(frozen=True)
class SourceProblem:
    code: str
    location: str  # نسبی و امن: نامِ فایل یا case


@dataclass(frozen=True)
class SourceCase:
    """یک itemِ درخواست از collection.json؛ item نسخه‌ای مستقل از همان دادهِ تجزیه‌شده است."""

    position: int
    case_id: str | None
    source_reference: str | None
    item: dict
    item_fingerprint: str
    default_title: str | None
    metadata: dict
    problems: tuple[SourceProblem, ...]

    @property
    def importable(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class BatchAttempt:
    ref: AttemptRef
    eligible: bool
    problems: tuple[SourceProblem, ...]
    execution_id: str | None = None
    executor_reported_status: str | None = None
    snapshot_fingerprint: str | None = None
    files: tuple[tuple[str, str], ...] = ()  # (نامِ نسبی، sha256 ِ بایت‌ها) — مرتب
    cases: tuple[SourceCase, ...] = field(default=())

    def case(self, case_id: str) -> SourceCase | None:
        matches = [item for item in self.cases if item.case_id == case_id]
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class AttemptListing:
    ref: AttemptRef
    eligible: bool
    problems: tuple[SourceProblem, ...]
    executor_reported_status: str | None
    case_ids: tuple[str, ...]


# ── مسیر و خواندن ───────────────────────────────────────────────────────────
def _resolve_root(root: Path | str) -> Path:
    try:
        lexical = Path(root)
        if lexical.is_symlink() or lexical.is_junction():
            raise _SourceError(ROOT_INVALID, "root")
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _SourceError(ROOT_INVALID, "root") from exc
    if not resolved.is_dir():
        raise _SourceError(ROOT_INVALID, "root")
    return resolved


def _same_path(actual: str, expected: Path) -> bool:
    return os.path.normcase(actual) == os.path.normcase(str(expected))


def _checked(path: Path, *, directory: bool, location: str) -> os.stat_result:
    """lstat، نوعِ مورد انتظار، نه symlink/junction، و realpath دقیقاً همان مسیر."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _SourceError(ARTIFACT_UNREADABLE, location) from exc
    if stat.S_ISLNK(info.st_mode) or path.is_junction():
        raise _SourceError(PATH_LINK, location)
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected:
        raise _SourceError(PATH_INVALID, location)
    if not _same_path(os.path.realpath(path), path):
        raise _SourceError(PATH_LINK, location)
    return info


def _read_bytes(directory: Path, name: str, limit: int) -> bytes:
    """یک بار خواندن؛ FileNotFoundError یعنی فایل نیست."""
    path = directory / name
    before = _checked(path, directory=False, location=name)
    if before.st_size > limit:
        raise _SourceError(ARTIFACT_TOO_LARGE, name)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _SourceError(ARTIFACT_UNREADABLE, name) from exc
    try:
        after = os.fstat(descriptor)
        identity = ("st_dev", "st_ino", "st_size")
        if any(getattr(before, key) != getattr(after, key) for key in identity):
            raise _SourceError(ARTIFACT_CHANGED_WHILE_READING, name)
        chunks, total = [], 0
        while True:
            chunk = os.read(descriptor, 1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise _SourceError(ARTIFACT_TOO_LARGE, name)
            chunks.append(chunk)
    except OSError as exc:
        raise _SourceError(ARTIFACT_UNREADABLE, name) from exc
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) != before.st_size:
        raise _SourceError(ARTIFACT_CHANGED_WHILE_READING, name)
    return data


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"unsupported constant {name}")


def _parse(data: bytes, name: str) -> dict:
    try:
        text = data.decode("utf-8", errors="strict")
        if text.startswith("﻿"):
            raise ValueError("BOM")
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _SourceError(ARTIFACT_MALFORMED, name) from exc
    if not isinstance(value, dict):
        raise _SourceError(ARTIFACT_MALFORMED, name)
    return value


# ── کشف ─────────────────────────────────────────────────────────────────────
def _children(directory: Path) -> list[str]:
    try:
        return sorted(entry.name for entry in os.scandir(directory))
    except OSError:
        return []


def discover_attempts(root: Path | str = DEFAULT_BATCH_ARTIFACT_ROOT) -> tuple[AttemptListing, ...]:
    """هر پوشه‌ی تلاش با نامِ امن، واجد یا ناواجد با دلیل. ریشه‌ی نامعتبر → ()."""
    try:
        base = _resolve_root(root)
    except _SourceError:
        return ()
    listings = []
    for batch_id in _children(base):
        if not _safe_component(batch_id):
            continue
        for issue_key in _children(base / batch_id):
            if not _safe_component(issue_key):
                continue
            for name in _children(base / batch_id / issue_key):
                match = EXECUTION_DIR_RE.match(name)
                if not match:
                    continue
                attempt = load_attempt(base, AttemptRef(batch_id, issue_key, int(match.group(1))))
                listings.append(
                    AttemptListing(
                        attempt.ref,
                        attempt.eligible,
                        attempt.problems,
                        attempt.executor_reported_status,
                        tuple(case.case_id for case in attempt.cases if case.case_id),
                    )
                )
    return tuple(listings)


# ── بارگذاریِ یک تلاش ────────────────────────────────────────────────────────
def load_attempt(root: Path | str, ref: AttemptRef) -> BatchAttempt:
    """همه‌ی بررسی‌های واجدِ شرایط بودن و یکپارچگی؛ هرگز استثنای منبع بیرون نمی‌دهد."""
    if not isinstance(ref, AttemptRef):
        raise TypeError("ref must be an AttemptRef.")
    try:
        return _load(_resolve_root(root), ref)
    except _SourceError as error:
        return BatchAttempt(ref, False, (SourceProblem(error.code, error.location),))


def _load(base: Path, ref: AttemptRef) -> BatchAttempt:
    directory = base
    for index, component in enumerate(ref.components):
        directory = directory / component
        location = "/".join(ref.components[: index + 1])
        try:
            _checked(directory, directory=True, location=location)
        except FileNotFoundError as exc:
            raise _SourceError(PATH_INVALID, location) from exc

    names = _children(directory)
    if any(name.startswith(".") and name.endswith(".tmp") for name in names):
        raise _SourceError(INCOMPLETE_WRITE, ref.directory)
    files: dict[str, str] = {}
    documents: dict[str, dict] = {}

    def read(name: str, limit: int) -> dict | None:
        try:
            data = _read_bytes(directory, name, limit)
        except FileNotFoundError:
            return None
        files[name] = hashlib.sha256(data).hexdigest()
        documents[name] = _parse(data, name)
        return documents[name]

    summary = read(SUMMARY_FILE, MAX_BYTES[SUMMARY_FILE])
    if summary is None:
        raise _SourceError(IN_PROGRESS_OR_INTERRUPTED, SUMMARY_FILE)
    status = _check_summary(summary, ref)

    step_artifacts = summary["step_artifacts"]
    listed = set(step_artifacts)
    if not listed <= set(STEP_FILES):
        raise _SourceError(STEP_ARTIFACT_INCONSISTENT, SUMMARY_FILE)
    for name in STEP_FILES:
        present = name in names
        if present != (name in listed):
            raise _SourceError(STEP_ARTIFACT_INCONSISTENT, name)
        if present:
            if not _points_to(step_artifacts[name], ref, name):
                raise _SourceError(STEP_ARTIFACT_INCONSISTENT, name)
            if read(name, MAX_STEP_BYTES) is None:
                raise _SourceError(STEP_ARTIFACT_INCONSISTENT, name)
    if REQUIRED_STEP_FILE not in documents:
        raise _SourceError(STEP4_MISSING, REQUIRED_STEP_FILE)

    collection = read(COLLECTION_FILE, MAX_BYTES[COLLECTION_FILE])
    if collection is None:
        raise _SourceError(COLLECTION_MISSING, COLLECTION_FILE)

    snapshot = sha256_json(
        {"attempt": ref.reference, "files": [[name, files[name]] for name in sorted(files)]}
    )
    cases = _cases(collection, documents, ref, summary, files, snapshot)
    return BatchAttempt(
        ref=ref,
        eligible=True,
        problems=(),
        execution_id=summary["execution_id"],
        executor_reported_status=status,
        snapshot_fingerprint=snapshot,
        files=tuple(sorted(files.items())),
        cases=cases,
    )


def _points_to(value: Any, ref: AttemptRef, name: str) -> bool:
    """مسیرِ مطلقِ ثبت‌شده در خلاصه — فقط چهار جزءِ آخرش مقایسه می‌شود."""
    if not isinstance(value, str) or not value:
        return False
    parts = PurePath(value).parts
    return tuple(parts[-4:]) == (*ref.components, name)


def _check_summary(summary: dict, ref: AttemptRef) -> str:
    keys = set(summary)
    if keys == MINIMAL_SUMMARY_KEYS:
        raise _SourceError(MINIMAL_SUMMARY, SUMMARY_FILE)
    if keys != SUMMARY_KEYS:
        raise _SourceError(SUMMARY_MALFORMED, SUMMARY_FILE)
    attempt = summary["attempt"]
    if (
        summary["batch_id"] != ref.batch_id
        or summary["issue_key"] != ref.issue_key
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt != ref.attempt
    ):
        raise _SourceError(SUMMARY_IDENTITY_MISMATCH, SUMMARY_FILE)
    execution_id = summary["execution_id"]
    if not isinstance(execution_id, str) or not _IDENT_RE.match(execution_id):
        raise _SourceError(SUMMARY_MALFORMED, SUMMARY_FILE)
    status, phase = summary["status"], summary["phase"]
    if status not in EXECUTION_FINAL or phase not in PHASES:
        raise _SourceError(SUMMARY_MALFORMED, SUMMARY_FILE)
    if status not in ELIGIBLE_STATUSES:
        raise _SourceError(STATUS_NOT_ELIGIBLE, SUMMARY_FILE)
    if not isinstance(summary["step_artifacts"], dict) or not isinstance(
        summary["task_source"], dict
    ):
        raise _SourceError(SUMMARY_MALFORMED, SUMMARY_FILE)
    # PASS/FAIL یعنی Newman تا آخر روی همین کالکشن اجرا شد؛ ERROR/CANCELLED با کالکشن فقط
    # در مرحله‌ی newman ممکن است (کالکشن درست پیش از Newman نوشته می‌شود).
    if status in ("PASS", "FAIL"):
        consistent = phase == "done" and summary["newman_executed"] is True
    else:
        consistent = phase == "newman"
    if not consistent or not _points_to(summary["collection_path"], ref, COLLECTION_FILE):
        raise _SourceError(SUMMARY_INCONSISTENT, SUMMARY_FILE)
    artifact_dir = summary["artifact_dir"]
    if not isinstance(artifact_dir, str) or PurePath(artifact_dir).parts[-3:] != ref.components:
        raise _SourceError(SUMMARY_INCONSISTENT, SUMMARY_FILE)
    return status


# ── caseها ──────────────────────────────────────────────────────────────────
def _blank(value: Any) -> bool:
    return value is None or value in ("", {}, [])


def _ident(value: Any) -> str | None:
    return value if isinstance(value, str) and _IDENT_RE.match(value) else None


def _leaves(collection: dict) -> list[tuple[dict, bool]]:
    """(item، آیا folderِ والدی event/auth دارد) به ترتیبِ ظاهر شدن."""
    leaves: list[tuple[dict, bool]] = []

    def walk(items: Any, context: bool) -> None:
        if not isinstance(items, list):
            raise _SourceError(ARTIFACT_MALFORMED, COLLECTION_FILE)
        for entry in items:
            if not isinstance(entry, dict):
                raise _SourceError(ARTIFACT_MALFORMED, COLLECTION_FILE)
            if "item" in entry:
                folder_context = not _blank(entry.get("event")) or not _blank(entry.get("auth"))
                walk(entry["item"], context or folder_context)
            elif isinstance(entry.get("request"), dict):
                leaves.append((entry, context))
            else:
                raise _SourceError(ARTIFACT_MALFORMED, COLLECTION_FILE)

    walk(collection.get("item"), False)
    return leaves


def _cases(
    collection: dict,
    documents: dict[str, dict],
    ref: AttemptRef,
    summary: dict,
    files: dict[str, str],
    snapshot: str,
) -> tuple[SourceCase, ...]:
    if not isinstance(collection.get("info"), dict):
        raise _SourceError(ARTIFACT_MALFORMED, COLLECTION_FILE)
    if not _blank(collection.get("event")) or not _blank(collection.get("auth")):
        raise _SourceError(COLLECTION_CONTEXT_UNSUPPORTED, COLLECTION_FILE)
    variables = collection.get("variable")
    if variables is not None and not isinstance(variables, list):
        raise _SourceError(ARTIFACT_MALFORMED, COLLECTION_FILE)
    variable_names = sorted(
        {
            entry["key"]
            for entry in variables or []
            if isinstance(entry, dict) and _ident(entry.get("key"))
        }
    )
    leaves = _leaves(collection)
    names = [
        item.get("name")
        if isinstance(item.get("name"), str) and CASE_ID_RE.fullmatch(item["name"])
        else None
        for item, _ in leaves
    ]
    step4 = documents[REQUIRED_STEP_FILE]
    requests = _rows(step4.get("requests"))
    unresolved = {_ident(row.get("test_case_id")) for row in _rows(step4.get("unresolved"))}
    base = {
        "batch_id": ref.batch_id,
        "issue_key": ref.issue_key,
        "attempt": ref.attempt,
        "execution_id": summary["execution_id"],
        "executor_reported_status": summary["status"],
        "db_status": "unavailable",
        "review_status": _enum(summary["review_status"], REVIEW_STATUSES),
        "final_review_status": _enum(summary["final_review_status"], FINAL_REVIEW_STATUSES),
        "task_description_sha256": _sha(summary["task_source"].get("task_description_sha256")),
        "artifact": "/".join((*ref.components, COLLECTION_FILE)),
        "collection_sha256": files[COLLECTION_FILE],
        "summary_sha256": files[SUMMARY_FILE],
        "snapshot_fingerprint": snapshot,
        "collection_variable_names": variable_names,
        "source_title": None,
        "source_title_available": False,
    }
    cases = []
    for position, ((leaf, folder_context), case_id) in enumerate(zip(leaves, names, strict=True)):
        problems = []
        location = case_id or f"item[{position}]"
        if case_id is None:
            problems.append(SourceProblem(CASE_ID_UNAVAILABLE, location))
        elif names.count(case_id) > 1:
            problems.append(SourceProblem(CASE_ID_DUPLICATE, location))
        if folder_context:
            problems.append(SourceProblem(FOLDER_CONTEXT_UNSUPPORTED, location))
        item = from_json(to_json(leaf))
        fingerprint = sha256_json(item)
        metadata = {**base, "item_fingerprint": fingerprint}
        if case_id is not None:
            if case_id in unresolved:
                problems.append(SourceProblem(CASE_UNRESOLVED, location))
            rows = [row for row in requests if row.get("test_case_id") == case_id]
            if len(rows) > 1:
                problems.append(SourceProblem(STEP4_AMBIGUOUS, location))
            row = rows[0] if len(rows) == 1 else {}
            host = _enum(row.get("host_variable"), HOST_VARIABLES) or None
            actual = inspect_payload(item).host_variable
            if host is not None and host != actual:
                problems.append(SourceProblem(HOST_INCONSISTENT, location))
            metadata.update(
                default_title=f"{ref.issue_key} · {case_id}",
                host_variable=host,
                host_basis=_enum(row.get("host_basis"), HOST_BASES) or None,
                base_path=_path(row.get("base_path")),
                method=_enum(row.get("method"), _METHODS) or None,
                operation_id=_operation_id(documents.get("step2.json"), case_id),
                **_title_hash(documents.get("step1.json"), case_id),
                **_dependencies(documents.get("step3.json"), case_id),
            )
        cases.append(
            SourceCase(
                position=position,
                case_id=case_id,
                source_reference=ref.case_reference(case_id) if case_id else None,
                item=item,
                item_fingerprint=fingerprint,
                default_title=metadata.get("default_title"),
                metadata=metadata,
                problems=tuple(problems),
            )
        )
    return tuple(cases)


def _rows(value: Any) -> list[dict]:
    return [row for row in value or [] if isinstance(row, dict)] if isinstance(value, list) else []


def _enum(value: Any, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else ""


def _sha(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA256_RE.match(value) else None


def _path(value: Any) -> str | None:
    return value if isinstance(value, str) and value and _PATH_RE.match(value) else None


def _operation_id(step2: dict | None, case_id: str) -> str | None:
    found = {
        _ident((row.get("api") or {}).get("operation_id"))
        for row in _rows((step2 or {}).get("mappings"))
        if row.get("test_case_id") == case_id and isinstance(row.get("api"), dict)
    } - {None}
    return found.pop() if len(found) == 1 else None


def _title_hash(step1: dict | None, case_id: str) -> dict:
    """عنوانِ اصلی در Batch فقط به شکلِ هش ذخیره می‌شود؛ خودِ عنوان در دسترس نیست."""
    titles = [
        row.get("title")
        for row in _rows((step1 or {}).get("test_cases"))
        if row.get("id") == case_id
    ]
    if len(titles) == 1 and isinstance(titles[0], dict) and titles[0].get("redacted") is True:
        sha, chars = _sha(titles[0].get("sha256")), titles[0].get("chars")
        if sha and isinstance(chars, int) and not isinstance(chars, bool):
            return {"source_title_sha256": sha, "source_title_chars": chars}
    return {"source_title_sha256": None, "source_title_chars": None}


def _dependencies(step3: dict | None, case_id: str) -> dict:
    """فقط نام‌ها/شناسه‌ها برای ردیابی — هیچ رابطه یا ترتیبی از آن ساخته نمی‌شود."""
    depends_on: set[str] = set()
    for row in _rows((step3 or {}).get("execution_order")):
        if row.get("test_case_id") == case_id and isinstance(row.get("depends_on"), list):
            depends_on |= {value for value in map(_ident, row["depends_on"]) if value}
    consumes: dict[str, set[str]] = {}
    produces: set[str] = set()
    for row in _rows((step3 or {}).get("data_dependencies")):
        name = _ident(row.get("variable_name"))
        producer = _ident((row.get("source") or {}).get("test_case_id"))
        targets = {_ident(target.get("test_case_id")) for target in _rows(row.get("targets"))}
        if not name:
            continue
        if producer == case_id:
            produces.add(name)
        if case_id in targets and producer != case_id:
            consumes.setdefault(name, set())
            if producer:
                consumes[name].add(producer)
    depends_on.discard(case_id)
    return {
        "depends_on": sorted(depends_on),
        "consumes": [
            {"variable": name, "producers": sorted(consumes[name])} for name in sorted(consumes)
        ],
        "produces": sorted(produces),
    }


__all__ = [
    "DEFAULT_BATCH_ARTIFACT_ROOT",
    "SOURCE_BATCH",
    "AttemptListing",
    "AttemptRef",
    "BatchAttempt",
    "SourceCase",
    "SourceProblem",
    "discover_attempts",
    "load_attempt",
]
