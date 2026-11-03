"""
src/batch_testing/storage.py — ذخیره‌سازیِ SQLiteِ Batch (sqlite3ِ کتابخانه‌ی استاندارد)

    store = BatchStore.open()          # data/batch_testing/batch.sqlite3
    batch = store.create_batch(NewBatch(...))

  * WAL، foreign keys، busy timeout؛ هر نوشتن داخلِ یک تراکنشِ صریحِ
    «BEGIN IMMEDIATE»؛ هر خطا → rollbackِ کامل.
  * migrationهای شماره‌دار و فقط‌رو‌به‌جلو؛ باز کردنِ دوباره idempotent است و
    پایگاهِ داده‌ای که از کد جدیدتر است رد می‌شود.
  * تاریخچه تغییرناپذیر است: رکوردِ اجرای پایانی، issueهای منجمدِ batch و هر
    حذفی با trigger رد می‌شود — نه فقط در Python.
  * «فقط یک batchِ فعال»: حداکثر یک batch با lease (owner_token)، حداکثر یک
    batchِ RUNNING، حداکثر یک اجرای RUNNING، و برای هر issue حداکثر یک اجرای فعال.
  * نوشتن‌های runner با owner_token: بررسیِ مالکیت و خودِ تغییر در همان تراکنشِ
    IMMEDIATE (قفلِ نوشتن از ابتدا گرفته شده است)، پس runnerی که lease را از
    دست داده هیچ چیزی نمی‌نویسد (OwnershipError و rollback).

هر اتصال مالِ یک thread است (sqlite3 اتصال را بینِ threadها به اشتراک نمی‌گذارد)؛
runner و UI هر کدام BatchStore خودشان را باز می‌کنند.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.batch_testing.models import (
    ACTION_CANCEL,
    ACTION_NONE,
    ACTION_PAUSE,
    BATCH_CANCELLED,
    BATCH_CREATED,
    BATCH_FINAL,
    BATCH_INTERRUPTED,
    BATCH_PAUSED,
    BATCH_RUNNING,
    BATCH_STATUSES,
    BOARD_TYPES,
    EXEC_CANCELLED,
    EXEC_ERROR,
    EXEC_NOT_EXECUTED,
    EXEC_QUEUED,
    EXEC_RUNNING,
    EXECUTION_FINAL,
    EXECUTION_STATUSES,
    FINAL_REVIEW_STATUSES,
    PHASE_DONE,
    PHASE_QUEUED,
    PHASES,
    REQUESTED_ACTIONS,
    REVIEW_NEEDS_REVIEW,
    REVIEW_STATUSES,
    TRIGGERS,
    Batch,
    BatchIssue,
    Execution,
    InvalidTransitionError,
    NewBatch,
    check_batch_transition,
    check_execution_transition,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "batch_testing" / "batch.sqlite3"
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "batch_testing"
BUSY_TIMEOUT_S = 30.0


class StorageError(RuntimeError):
    """خطای پایگاهِ داده — پیام هیچ دادهٔ issue ندارد."""


class SchemaVersionError(StorageError):
    """پایگاهِ داده با نسخه‌ای جدیدتر از این کد ساخته شده است."""


class NotFoundError(StorageError):
    pass


class LeaseError(StorageError):
    """batchِ دیگری فعال است، یا این runner مالکِ lease نیست."""


class OwnershipError(LeaseError):
    """توکنِ داده‌شده (دیگر) مالکِ leaseِ batch نیست — تراکنش rollback شد."""


class ImmutableRecordError(StorageError):
    """تلاش برای تغییر یا حذفِ تاریخچه‌ی تغییرناپذیر."""


class DuplicateAttemptError(StorageError):
    """برای این issue از قبل یک اجرای فعال وجود دارد."""


def _in(values) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


_FINAL_SQL = _in(sorted(EXECUTION_FINAL))

# ── migrationها: هر کدام فهرستی از دستورها (trigger خودش «;» دارد) ───────────
MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "core tables",
        [
            f"""
            CREATE TABLE batches (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                board_id INTEGER NOT NULL,
                board_name TEXT NOT NULL,
                board_type TEXT NOT NULL CHECK (board_type IN {_in(BOARD_TYPES)}),
                column_name TEXT NOT NULL,
                criteria_json TEXT NOT NULL,
                discovery_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN {_in(BATCH_STATUSES)}),
                requested_action TEXT NOT NULL DEFAULT 'NONE'
                    CHECK (requested_action IN {_in(REQUESTED_ACTIONS)}),
                owner_token TEXT,
                owner_pid INTEGER,
                owner_host TEXT,
                heartbeat_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                environment_path TEXT NOT NULL,
                environment_sha256 TEXT NOT NULL,
                globals_path TEXT NOT NULL,
                globals_sha256 TEXT NOT NULL,
                artifact_root TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE batch_issues (
                batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE RESTRICT,
                issue_key TEXT NOT NULL CHECK (length(issue_key) > 0),
                position INTEGER NOT NULL CHECK (position >= 0),
                summary TEXT NOT NULL DEFAULT '',
                issue_type TEXT NOT NULL DEFAULT '',
                status_name TEXT NOT NULL DEFAULT '',
                labels_json TEXT NOT NULL DEFAULT '[]',
                parent_key TEXT NOT NULL DEFAULT '',
                eligibility_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (batch_id, issue_key),
                UNIQUE (batch_id, position)
            )
            """,
            f"""
            CREATE TABLE executions (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                issue_key TEXT NOT NULL,
                attempt INTEGER NOT NULL CHECK (attempt >= 1),
                trigger TEXT NOT NULL CHECK (trigger IN {_in(TRIGGERS)}),
                previous_execution_id TEXT REFERENCES executions(id) ON DELETE RESTRICT,
                status TEXT NOT NULL CHECK (status IN {_in(EXECUTION_STATUSES)}),
                phase TEXT NOT NULL CHECK (phase IN {_in(PHASES)}),
                review_status TEXT NOT NULL DEFAULT 'NONE'
                    CHECK (review_status IN {_in(REVIEW_STATUSES)}),
                review_reasons_json TEXT NOT NULL DEFAULT '[]',
                final_review_status TEXT NOT NULL DEFAULT ''
                    CHECK (final_review_status IN {_in(FINAL_REVIEW_STATUSES)}),
                error_stage TEXT NOT NULL DEFAULT '',
                error_kind TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                heartbeat_at TEXT,
                runner_token TEXT,
                artifact_dir TEXT NOT NULL DEFAULT '',
                collection_path TEXT NOT NULL DEFAULT '',
                task_source_json TEXT NOT NULL DEFAULT '{{}}',
                newman_run_id TEXT NOT NULL DEFAULT '',
                newman_status TEXT NOT NULL DEFAULT '',
                newman_error_kind TEXT NOT NULL DEFAULT '',
                newman_no_assertions INTEGER NOT NULL DEFAULT 0
                    CHECK (newman_no_assertions IN (0, 1)),
                newman_run_dir TEXT NOT NULL DEFAULT '',
                newman_result_path TEXT NOT NULL DEFAULT '',
                requests_executed INTEGER,
                assertions_total INTEGER,
                assertions_failed INTEGER,
                cases_total INTEGER,
                cases_executable INTEGER,
                cases_not_executed INTEGER,
                FOREIGN KEY (batch_id, issue_key)
                    REFERENCES batch_issues(batch_id, issue_key) ON DELETE RESTRICT,
                UNIQUE (batch_id, issue_key, attempt)
            )
            """,
        ],
    ),
    (
        2,
        "indexes, single-active constraints and history protection",
        [
            "CREATE INDEX ix_batches_status ON batches(status)",
            "CREATE INDEX ix_executions_batch_status ON executions(batch_id, status)",
            # حداکثر یک batch با lease و حداکثر یک batchِ RUNNING در کلِ پایگاه.
            "CREATE UNIQUE INDEX ux_batches_single_lease ON batches((1)) WHERE owner_token IS NOT NULL",
            "CREATE UNIQUE INDEX ux_batches_single_running ON batches((1)) WHERE status = 'RUNNING'",
            # اجرا ترتیبی است: حداکثر یک اجرای RUNNING، و برای هر issue یک اجرای فعال.
            "CREATE UNIQUE INDEX ux_executions_single_running ON executions((1)) WHERE status = 'RUNNING'",
            "CREATE UNIQUE INDEX ux_executions_one_active_per_issue ON executions(batch_id, issue_key) "
            "WHERE status IN ('QUEUED', 'RUNNING')",
            f"""
            CREATE TRIGGER tr_executions_final_immutable
            BEFORE UPDATE ON executions WHEN OLD.status IN {_FINAL_SQL}
            BEGIN SELECT RAISE(ABORT, 'immutable: execution record is final'); END
            """,
            """
            CREATE TRIGGER tr_executions_no_delete BEFORE DELETE ON executions
            BEGIN SELECT RAISE(ABORT, 'immutable: execution history cannot be deleted'); END
            """,
            """
            CREATE TRIGGER tr_batch_issues_frozen BEFORE UPDATE ON batch_issues
            BEGIN SELECT RAISE(ABORT, 'immutable: the eligible issue list is frozen'); END
            """,
            """
            CREATE TRIGGER tr_batch_issues_no_delete BEFORE DELETE ON batch_issues
            BEGIN SELECT RAISE(ABORT, 'immutable: the eligible issue list is frozen'); END
            """,
            """
            CREATE TRIGGER tr_batches_no_delete BEFORE DELETE ON batches
            BEGIN SELECT RAISE(ABORT, 'immutable: batch history cannot be deleted'); END
            """,
        ],
    ),
]
LATEST_VERSION = MIGRATIONS[-1][0]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def connect(path: Path | str) -> sqlite3.Connection:
    """اتصالِ autocommit (تراکنش‌ها صریح‌اند) با WAL، foreign keys و busy timeout."""
    connection = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
    mode = _enable_wal(connection)
    if str(mode).lower() != "wal":
        connection.close()
        raise StorageError(f"SQLite WAL mode could not be enabled (journal_mode={mode}).")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def _enable_wal(connection: sqlite3.Connection) -> str:
    """تغییرِ پایگاهِ تازه به WAL قفلِ انحصاریِ کوتاهی می‌خواهد و SQLite آن‌جا busy
    timeout را به کار نمی‌برد؛ اگر اتصالِ دیگری هم‌زمان در حالِ باز کردن باشد، تا
    همان سقفِ BUSY_TIMEOUT_S دوباره تلاش می‌شود."""
    deadline = time.monotonic() + BUSY_TIMEOUT_S
    while True:
        try:
            return connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                connection.close()
                raise
            time.sleep(0.05)


def schema_version(connection: sqlite3.Connection) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if not exists:
        return 0
    return connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[
        0
    ]


def migrate(connection: sqlite3.Connection, target: int | None = None) -> int:
    """migrationهای باقی‌مانده تا target — هر کدام در یک تراکنشِ IMMEDIATE.

    نسخه داخلِ همان تراکنش دوباره خوانده می‌شود، پس دو پروسه که هم‌زمان یک پایگاهِ
    تازه را باز می‌کنند یک migration را دو بار اجرا نمی‌کنند.
    """
    latest = MIGRATIONS[-1][0]
    target = latest if target is None else target
    if schema_version(connection) > latest:
        raise SchemaVersionError(
            f"The batch database has schema version {schema_version(connection)}, newer than "
            f"this code ({latest}). Update the application instead of downgrading."
        )
    for version, name, statements in MIGRATIONS:
        if version > target:
            break
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            if schema_version(connection) >= version:
                connection.execute("COMMIT")
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, now()),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return schema_version(connection)


# ── تبدیلِ ردیف به رکورد ────────────────────────────────────────────────────


def _batch(row: sqlite3.Row) -> Batch:
    return Batch(
        id=row["id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        board_id=row["board_id"],
        board_name=row["board_name"],
        board_type=row["board_type"],
        column_name=row["column_name"],
        criteria=json.loads(row["criteria_json"]),
        discovery=json.loads(row["discovery_json"]),
        status=row["status"],
        requested_action=row["requested_action"],
        owner_token=row["owner_token"],
        owner_pid=row["owner_pid"],
        owner_host=row["owner_host"],
        heartbeat_at=row["heartbeat_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        environment_path=row["environment_path"],
        environment_sha256=row["environment_sha256"],
        globals_path=row["globals_path"],
        globals_sha256=row["globals_sha256"],
        artifact_root=row["artifact_root"],
    )


def _issue(row: sqlite3.Row) -> BatchIssue:
    return BatchIssue(
        batch_id=row["batch_id"],
        issue_key=row["issue_key"],
        position=row["position"],
        summary=row["summary"],
        issue_type=row["issue_type"],
        status_name=row["status_name"],
        labels=tuple(json.loads(row["labels_json"])),
        parent_key=row["parent_key"],
        eligibility=json.loads(row["eligibility_json"]),
    )


def _execution(row: sqlite3.Row) -> Execution:
    values = dict(row)
    return Execution(
        **{
            key: values[key]
            for key in values
            if not key.endswith("_json") and key != "newman_no_assertions"
        },
        review_reasons=json.loads(values["review_reasons_json"]),
        task_source=json.loads(values["task_source_json"]),
        newman_no_assertions=bool(values["newman_no_assertions"]),
    )


# فیلدهایی از اجرا که در طولِ کار (پیش از پایان) قابلِ به‌روزرسانی‌اند.
_PROGRESS_FIELDS = {
    "phase",
    "review_status",
    "review_reasons",
    "final_review_status",
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
}
_FINISH_FIELDS = _PROGRESS_FIELDS | {"error_stage", "error_kind", "error_message"}
_JSON_FIELDS = {"review_reasons", "task_source"}


def _columns(fields: dict[str, Any]) -> dict[str, Any]:
    columns: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _JSON_FIELDS:
            columns[f"{key}_json"] = json.dumps(value, ensure_ascii=False)
        elif key == "newman_no_assertions":
            columns[key] = int(bool(value))
        else:
            columns[key] = value
    return columns


def _check_values(fields: dict[str, Any]) -> None:
    if "phase" in fields and fields["phase"] not in PHASES:
        raise ValueError(f"Unknown execution phase {fields['phase']!r}.")
    if "review_status" in fields and fields["review_status"] not in REVIEW_STATUSES:
        raise ValueError(f"Unknown review status {fields['review_status']!r}.")
    if (
        "final_review_status" in fields
        and fields["final_review_status"] not in FINAL_REVIEW_STATUSES
    ):
        raise ValueError(f"Unknown final review status {fields['final_review_status']!r}.")


class BatchStore:
    """مخزنِ Batch روی یک اتصال — برای هر thread یک نمونه."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self._db = connection
        self.path = path

    @classmethod
    def open(cls, path: Path | str = DEFAULT_DB_PATH) -> BatchStore:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = connect(path)
        try:
            migrate(connection)
        except BaseException:
            connection.close()
            raise
        return cls(connection, path)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> BatchStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return schema_version(self._db)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """«BEGIN IMMEDIATE» … COMMIT؛ هر استثنا → ROLLBACKِ کامل و همان استثنا."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _write(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        try:
            return self._db.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "immutable:" in message:
                raise ImmutableRecordError(message.split("immutable:", 1)[1].strip()) from exc
            raise

    def _check_owner(self, batch_id: str, owner_token: str | None) -> None:
        """داخلِ تراکنشِ جاری؛ None یعنی نوشتنِ بدونِ lease (رفتارِ قبلی)."""
        if owner_token is None:
            return
        row = self._db.execute(
            "SELECT owner_token FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Batch {batch_id} does not exist.")
        if row["owner_token"] != owner_token:
            raise OwnershipError("This runner does not own the batch lease.")

    # ── batch ───────────────────────────────────────────────────────────────
    def create_batch(self, new: NewBatch) -> Batch:
        """batch و فهرستِ منجمدِ issueهایش در یک تراکنش — همه یا هیچ."""
        if new.board_type not in BOARD_TYPES:
            raise ValueError(f"Unknown board type {new.board_type!r}.")
        batch_id = uuid.uuid4().hex
        stamp = now()
        with self.transaction():
            self._write(
                "INSERT INTO batches (id, created_at, updated_at, board_id, board_name, board_type, "
                "column_name, criteria_json, discovery_json, status, environment_path, "
                "environment_sha256, globals_path, globals_sha256, artifact_root) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    stamp,
                    stamp,
                    new.board_id,
                    new.board_name,
                    new.board_type,
                    new.column_name,
                    json.dumps(new.criteria, ensure_ascii=False),
                    json.dumps(new.discovery, ensure_ascii=False),
                    BATCH_CREATED,
                    new.environment_path,
                    new.environment_sha256,
                    new.globals_path,
                    new.globals_sha256,
                    new.artifact_root,
                ),
            )
            for position, issue in enumerate(new.issues):
                self._write(
                    "INSERT INTO batch_issues (batch_id, issue_key, position, summary, issue_type, "
                    "status_name, labels_json, parent_key, eligibility_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        issue.issue_key,
                        position,
                        issue.summary,
                        issue.issue_type,
                        issue.status_name,
                        json.dumps(list(issue.labels), ensure_ascii=False),
                        issue.parent_key,
                        json.dumps(issue.eligibility, ensure_ascii=False),
                    ),
                )
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> Batch:
        row = self._db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Batch {batch_id} does not exist.")
        return _batch(row)

    def list_batches(self, limit: int = 50) -> list[Batch]:
        rows = self._db.execute(
            "SELECT * FROM batches ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_batch(row) for row in rows]

    def active_batch(self) -> Batch | None:
        """batchی که lease دارد یا RUNNING است — حداکثر یکی."""
        row = self._db.execute(
            "SELECT * FROM batches WHERE owner_token IS NOT NULL OR status = ? LIMIT 1",
            (BATCH_RUNNING,),
        ).fetchone()
        return _batch(row) if row else None

    def batch_issues(self, batch_id: str) -> list[BatchIssue]:
        rows = self._db.execute(
            "SELECT * FROM batch_issues WHERE batch_id = ? ORDER BY position", (batch_id,)
        ).fetchall()
        return [_issue(row) for row in rows]

    def transition_batch(
        self,
        batch_id: str,
        target: str,
        *,
        expected: str | None = None,
        owner_token: str | None = None,
    ) -> Batch:
        """گذارِ اعتبارسنجی‌شده؛ با expected، وضعیتِ فعلی هم باید همان باشد؛ با
        owner_token فقط اگر همین توکن مالکِ lease باشد."""
        with self.transaction():
            self._check_owner(batch_id, owner_token)
            current = self.get_batch(batch_id).status
            if expected is not None and current != expected:
                raise InvalidTransitionError(f"The batch is {current}, not {expected}.")
            check_batch_transition(current, target)
            stamp = now()
            assignments = {"status": target, "updated_at": stamp}
            if target == BATCH_RUNNING:
                assignments["started_at"] = self.get_batch(batch_id).started_at or stamp
            if target in (BATCH_PAUSED, BATCH_INTERRUPTED, *BATCH_FINAL):
                assignments["requested_action"] = ACTION_NONE
            if target in BATCH_FINAL:
                assignments["finished_at"] = stamp
            sets = ", ".join(f"{key} = :{key}" for key in assignments)
            try:
                self._write(
                    f"UPDATE batches SET {sets} WHERE id = :id", {**assignments, "id": batch_id}
                )
            except sqlite3.IntegrityError as exc:
                raise LeaseError("Another batch is already RUNNING in this database.") from exc
        return self.get_batch(batch_id)

    def request_action(self, batch_id: str, action: str) -> Batch:
        """PAUSE / CANCEL را برای runner ثبت می‌کند — فقط روی batchِ RUNNING."""
        if action not in (ACTION_PAUSE, ACTION_CANCEL):
            raise ValueError(f"Unknown requested action {action!r}.")
        with self.transaction():
            batch = self.get_batch(batch_id)
            if batch.status != BATCH_RUNNING:
                raise InvalidTransitionError(
                    f"{action} can only be requested for a RUNNING batch, not {batch.status}."
                )
            if batch.requested_action == ACTION_CANCEL and action == ACTION_PAUSE:
                raise InvalidTransitionError("The batch is already being cancelled.")
            self._write(
                "UPDATE batches SET requested_action = ?, updated_at = ? WHERE id = ?",
                (action, now(), batch_id),
            )
        return self.get_batch(batch_id)

    # ── lease و heartbeat (برای runner و بازیابی) ───────────────────────────
    def acquire_lease(self, batch_id: str, token: str, *, pid: int, host: str) -> Batch:
        with self.transaction():
            batch = self.get_batch(batch_id)
            if batch.owner_token and batch.owner_token != token:
                raise LeaseError("This batch is already owned by another runner.")
            try:
                self._write(
                    "UPDATE batches SET owner_token = ?, owner_pid = ?, owner_host = ?, "
                    "heartbeat_at = ?, updated_at = ? WHERE id = ?",
                    (token, pid, host, now(), now(), batch_id),
                )
            except sqlite3.IntegrityError as exc:
                raise LeaseError("Another batch is already active in this database.") from exc
        return self.get_batch(batch_id)

    def heartbeat(self, batch_id: str, token: str) -> None:
        with self.transaction():
            cursor = self._write(
                "UPDATE batches SET heartbeat_at = ? WHERE id = ? AND owner_token = ?",
                (now(), batch_id, token),
            )
            if cursor.rowcount != 1:
                raise OwnershipError("This runner does not own the batch lease.")

    def release_lease(self, batch_id: str, token: str) -> None:
        with self.transaction():
            cursor = self._write(
                "UPDATE batches SET owner_token = NULL, owner_pid = NULL, owner_host = NULL, "
                "updated_at = ? WHERE id = ? AND owner_token = ?",
                (now(), batch_id, token),
            )
            if cursor.rowcount != 1:
                raise OwnershipError("This runner does not own the batch lease.")

    def take_over_lease(
        self,
        batch_id: str,
        *,
        expected_token: str,
        new_token: str,
        stale_before: str,
        pid: int,
        host: str,
    ) -> Batch:
        """leaseِ کهنه در یک UPDATEِ شرطی: فقط اگر هنوز همان مالک است و heartbeatش
        هنوز از stale_before قدیمی‌تر است — وگرنه OwnershipError و هیچ تغییری."""
        with self.transaction():
            stamp = now()
            cursor = self._write(
                "UPDATE batches SET owner_token = ?, owner_pid = ?, owner_host = ?, "
                "heartbeat_at = ?, updated_at = ? WHERE id = ? AND owner_token = ? "
                "AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
                (new_token, pid, host, stamp, stamp, batch_id, expected_token, stale_before),
            )
            if cursor.rowcount != 1:
                raise OwnershipError("The lease is no longer stale or has another owner.")
        return self.get_batch(batch_id)

    def cancel_inactive_batch(self, batch_id: str, *, expected: str) -> tuple[Batch, list[str]]:
        """CREATED / PAUSED / INTERRUPTED → CANCELLED بدونِ runner، همه در یک تراکنش.

        فقط وقتی هیچ runnerی lease ندارد (runner برای هر نوشتنی lease می‌خواهد و
        acquire_lease داخلِ تراکنش بررسی می‌کند، پس هیچ اجرایی هم‌زمان شروع نمی‌شود).
        اجراهای QUEUED → CANCELLED (phase دست‌نخورده)؛ اجراهای پایانی، از جمله
        INTERRUPTED، دست نمی‌خورند و تلاشِ تازه‌ای ساخته نمی‌شود. اجرای RUNNING در
        batchِ غیرفعال یعنی وضعیتِ ناتمام — رد می‌شود و هیچ چیزی تغییر نمی‌کند.
        خروجی: batch و شناسه‌ی اجراهای لغوشده.
        """
        if expected not in (BATCH_CREATED, BATCH_PAUSED, BATCH_INTERRUPTED):
            raise InvalidTransitionError(
                f"A {expected} batch cannot be cancelled without its runner."
            )
        with self.transaction():
            batch = self.get_batch(batch_id)
            if batch.owner_token:
                raise OwnershipError("A runner holds the lease of this batch.")
            if batch.status != expected:
                raise InvalidTransitionError(f"The batch is {batch.status}, not {expected}.")
            check_batch_transition(batch.status, BATCH_CANCELLED)
            running = self._db.execute(
                "SELECT COUNT(*) FROM executions WHERE batch_id = ? AND status = ?",
                (batch_id, EXEC_RUNNING),
            ).fetchone()[0]
            if running:
                raise InvalidTransitionError(
                    "The batch still has a RUNNING execution, so it cannot be cancelled here."
                )
            queued = [
                row["id"]
                for row in self._db.execute(
                    "SELECT id FROM executions WHERE batch_id = ? AND status = ? ORDER BY created_at",
                    (batch_id, EXEC_QUEUED),
                ).fetchall()
            ]
            stamp = now()
            self._write(
                "UPDATE executions SET status = ?, finished_at = ?, heartbeat_at = ? "
                "WHERE batch_id = ? AND status = ?",
                (EXEC_CANCELLED, stamp, stamp, batch_id, EXEC_QUEUED),
            )
            self._write(
                "UPDATE batches SET status = ?, requested_action = ?, finished_at = ?, "
                "updated_at = ? WHERE id = ? AND owner_token IS NULL",
                (BATCH_CANCELLED, ACTION_NONE, stamp, stamp, batch_id),
            )
        return self.get_batch(batch_id), queued

    def stale_leases(self, older_than: str) -> list[Batch]:
        """batchهایی که heartbeatشان از older_than (ISO) قدیمی‌تر است — برای بازیابی."""
        rows = self._db.execute(
            "SELECT * FROM batches WHERE owner_token IS NOT NULL AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
            (older_than,),
        ).fetchall()
        return [_batch(row) for row in rows]

    # ── اجرای issue ─────────────────────────────────────────────────────────
    def create_execution(
        self,
        batch_id: str,
        issue_key: str,
        *,
        trigger: str,
        runner_token: str | None = None,
        owner_token: str | None = None,
    ) -> Execution:
        """تلاشِ بعدی (attempt = بیشینه + ۱) — هرگز رکوردِ قبلی را بازنویسی نمی‌کند."""
        if trigger not in TRIGGERS:
            raise ValueError(f"Unknown execution trigger {trigger!r}.")
        execution_id = uuid.uuid4().hex
        with self.transaction():
            self._check_owner(batch_id, owner_token)
            previous = self._db.execute(
                "SELECT id, attempt FROM executions WHERE batch_id = ? AND issue_key = ? "
                "ORDER BY attempt DESC LIMIT 1",
                (batch_id, issue_key),
            ).fetchone()
            try:
                self._write(
                    "INSERT INTO executions (id, batch_id, issue_key, attempt, trigger, "
                    "previous_execution_id, status, phase, created_at, runner_token) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        execution_id,
                        batch_id,
                        issue_key,
                        (previous["attempt"] if previous else 0) + 1,
                        trigger,
                        previous["id"] if previous else None,
                        EXEC_QUEUED,
                        PHASE_QUEUED,
                        now(),
                        runner_token,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # همان قیدِ «یک اجرای فعال برای هر issue» (نه قیدِ attempt)
                if str(exc).rstrip().endswith("executions.batch_id, executions.issue_key"):
                    raise DuplicateAttemptError(
                        f"{issue_key} already has an active execution in this batch."
                    ) from exc
                raise
        return self.get_execution(execution_id)

    def get_execution(self, execution_id: str) -> Execution:
        row = self._db.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Execution {execution_id} does not exist.")
        return _execution(row)

    def executions(self, batch_id: str, issue_key: str | None = None) -> list[Execution]:
        sql = "SELECT * FROM executions WHERE batch_id = ?"
        params: tuple = (batch_id,)
        if issue_key is not None:
            sql += " AND issue_key = ?"
            params += (issue_key,)
        rows = self._db.execute(sql + " ORDER BY issue_key, attempt", params).fetchall()
        return [_execution(row) for row in rows]

    def latest_executions(self, batch_id: str) -> dict[str, Execution]:
        rows = self._db.execute(
            "SELECT e.* FROM executions e JOIN (SELECT issue_key, MAX(attempt) AS attempt FROM executions "
            "WHERE batch_id = ? GROUP BY issue_key) latest "
            "ON e.issue_key = latest.issue_key AND e.attempt = latest.attempt WHERE e.batch_id = ?",
            (batch_id, batch_id),
        ).fetchall()
        return {row["issue_key"]: _execution(row) for row in rows}

    def start_execution(self, execution_id: str, *, owner_token: str | None = None) -> Execution:
        with self.transaction():
            current = self.get_execution(execution_id)
            self._check_owner(current.batch_id, owner_token)
            check_execution_transition(current.status, EXEC_RUNNING)
            stamp = now()
            try:
                self._write(
                    "UPDATE executions SET status = ?, started_at = ?, heartbeat_at = ? WHERE id = ?",
                    (EXEC_RUNNING, stamp, stamp, execution_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateAttemptError("Another execution is already RUNNING.") from exc
        return self.get_execution(execution_id)

    def update_execution(
        self, execution_id: str, *, owner_token: str | None = None, **fields: Any
    ) -> Execution:
        """پیشرفتِ یک اجرای فعال (phase، مسیرها، شمارش‌ها، فراداده‌ی Newman، …)."""
        unknown = set(fields) - _PROGRESS_FIELDS
        if unknown:
            raise ValueError(
                f"These execution fields cannot be updated: {', '.join(sorted(unknown))}."
            )
        _check_values(fields)
        with self.transaction():
            current = self.get_execution(execution_id)
            self._check_owner(current.batch_id, owner_token)
            if current.final:
                raise ImmutableRecordError("execution record is final")
            columns = {**_columns(fields), "heartbeat_at": now()}
            sets = ", ".join(f"{key} = :{key}" for key in columns)
            self._write(
                f"UPDATE executions SET {sets} WHERE id = :id", {**columns, "id": execution_id}
            )
        return self.get_execution(execution_id)

    def finish_execution(
        self, execution_id: str, status: str, *, owner_token: str | None = None, **fields: Any
    ) -> Execution:
        """وضعیتِ پایانی + همه‌ی فراداده در یک تراکنش؛ بعد از آن رکورد تغییرناپذیر است.

        قواعد: ERROR بدونِ error_kind پذیرفته نیست؛ NOT_EXECUTED فقط با
        review_status=NEEDS_REVIEW (اجرانشدن بدونِ دلیلِ بازبینی یعنی خطای برنامه).
        """
        unknown = set(fields) - _FINISH_FIELDS
        if unknown:
            raise ValueError(f"These execution fields cannot be set: {', '.join(sorted(unknown))}.")
        _check_values(fields)
        with self.transaction():
            current = self.get_execution(execution_id)
            self._check_owner(current.batch_id, owner_token)
            check_execution_transition(current.status, status)
            review = fields.get("review_status", current.review_status)
            if status == EXEC_ERROR and not (fields.get("error_kind") or current.error_kind):
                raise ValueError("An ERROR execution needs an error_kind.")
            if status == EXEC_NOT_EXECUTED and review != REVIEW_NEEDS_REVIEW:
                raise ValueError("A NOT_EXECUTED execution must be NEEDS_REVIEW.")
            stamp = now()
            columns = {
                **_columns(fields),
                "status": status,
                "phase": fields.get("phase", PHASE_DONE),
                "finished_at": stamp,
                "heartbeat_at": stamp,
            }
            sets = ", ".join(f"{key} = :{key}" for key in columns)
            self._write(
                f"UPDATE executions SET {sets} WHERE id = :id", {**columns, "id": execution_id}
            )
        return self.get_execution(execution_id)

    def active_executions(self) -> list[Execution]:
        rows = self._db.execute(
            "SELECT * FROM executions WHERE status IN (?, ?) ORDER BY created_at",
            (EXEC_QUEUED, EXEC_RUNNING),
        ).fetchall()
        return [_execution(row) for row in rows]


__all__ = [
    "BUSY_TIMEOUT_S",
    "DEFAULT_ARTIFACT_ROOT",
    "DEFAULT_DB_PATH",
    "LATEST_VERSION",
    "MIGRATIONS",
    "BatchStore",
    "DuplicateAttemptError",
    "ImmutableRecordError",
    "LeaseError",
    "NotFoundError",
    "OwnershipError",
    "SchemaVersionError",
    "StorageError",
    "connect",
    "migrate",
    "now",
    "schema_version",
]
