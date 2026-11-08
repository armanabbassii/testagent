"""
src/testcase_box/storage.py — مخزنِ SQLiteِ TestCase Box

    store = BoxStore.open()          # data/testcase_box/box.sqlite3 (فقط با فراخوانیِ صریح)
    case = store.create_case(NewCase(title=..., payload={...}))

  * هر نوشتنِ چندمرحله‌ای داخلِ یک «BEGIN IMMEDIATE»؛ هر خطا → rollbackِ کامل.
  * تغییرِ محتوا = نسخه‌ی تازه (add_version)؛ نسخه‌ها تغییرناپذیرند و case به‌جای حذف
    بایگانی می‌شود، پس تاریخچه و اجراهای گذشته همیشه نسخه‌ی خود را دارند.
  * تأییدِ اسکریپت به (نسخه، اثرانگشتِ اسکریپت) بسته است و به نسخه‌ی دیگر منتقل نمی‌شود.
  * run همه‌ی unitها و نسخه‌های سنجاق‌شده‌اش را در یک تراکنش می‌سازد؛ rerun از
    pinned_versions همان نسخه‌ها را می‌گیرد، نه نسخه‌ی جاریِ case را.

مالکیتِ اجرای run (v4): create_owned_run / claim_run یک RunLease (توکنِ مبهم + generation)
فقط به گیرنده می‌دهند؛ هر تغییرِ اجرایی (شروع، heartbeat، نتیجه‌ی unit و case، پایان، آزادسازی)
آن lease را می‌خواهد و مالکیت در همان تراکنشِ IMMEDIATE (با مقایسه‌ی ثابت‌زمان) بررسی می‌شود.
finish_run بستنِ فرزندانِ باز، وضعیتِ پایانی و پاک کردنِ lease را در یک تراکنش انجام می‌دهد.
متدهای قدیمیِ بی‌مالک (set_run_status، …) runِ دارای lease را رد می‌کنند. این ماژول خودش
اجرا، heartbeatِ دوره‌ای یا بازپس‌گیریِ زمانی ندارد — take_over_run فقط عملیاتِ شرطی است و
تصمیمِ کهنگی با بازیابی (milestoneِ بعد) است. این مالکیت جدا از Execution Gate ِ مشترک است.
هر اتصال مالِ یک thread است؛ هر thread BoxStoreِ خودش را باز می‌کند.
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.testcase_box.db import (
    DEFAULT_DB_PATH,
    ActiveRunError,
    ImmutableRecordError,
    IntegrityViolationError,
    NotFoundError,
    RunOwnershipError,
    StorageError,
    connect,
    migrate,
    abort,
    now,
    schema_version,
)
from src.testcase_box.models import (
    APPROVAL_APPROVED,
    APPROVAL_STATUSES,
    CASE_ACTIVE,
    CASE_ARCHIVED,
    CASE_STATUSES,
    IMPORT_PENDING,
    LINK_DEPENDS_ON,
    RUN_ACTION_CANCEL,
    RUN_CASE_FINAL,
    RUN_FINAL,
    RUN_QUEUED,
    RUN_RUNNING,
    SCRIPTS_NOT_REQUIRED,
    SCRIPTS_PENDING,
    SOURCE_BATCH,
    UNIT_FINAL,
    UNIT_QUEUED,
    UNIT_RUNNING,
    BoxCase,
    BoxRun,
    CaseLink,
    CaseSource,
    CaseVersion,
    ImportJob,
    NewCase,
    NewLink,
    NewRun,
    NewSource,
    NewVersion,
    RunCase,
    RunLease,
    RunOwnership,
    RunUnit,
    InvalidTransitionError,
    ScriptApproval,
    check_import_transition,
    check_run_case_transition,
    check_run_transition,
    check_unit_transition,
    from_json,
    is_sha256,
    script_fingerprint,
    sha256_json,
    to_json,
    validate_json,
    validate_title,
)


class ArchivedCaseError(StorageError):
    """caseِ بایگانی‌شده نسخه‌ی تازه نمی‌گیرد؛ اول بازگردانی شود."""


# نتیجه‌ی create_case_for_source
SOURCED_CREATED = "CREATED"
SOURCED_ALREADY_PRESENT = "ALREADY_PRESENT"
SOURCED_CONFLICT = "SOURCE_CONFLICT"
SOURCED_AMBIGUOUS = "SOURCE_AMBIGUOUS"


@dataclass(frozen=True)
class SourcedCase:
    """case فقط برای CREATED و ALREADY_PRESENT پر است؛ existing_sources ردیف‌های همان هویت‌اند."""

    outcome: str
    case: BoxCase | None
    existing_sources: tuple[CaseSource, ...] = ()

    @property
    def created(self) -> bool:
        return self.outcome == SOURCED_CREATED


def _load(text: str, kind: type = dict) -> Any:
    try:
        return from_json(text, kind)
    except ValueError as exc:
        raise StorageError(f"Corrupt JSON in the TestCase Box database: {exc}") from exc


# ── تبدیلِ ردیف به رکورد ────────────────────────────────────────────────────


def _case(row: sqlite3.Row) -> BoxCase:
    return BoxCase(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        current_version_id=row["current_version_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _version(row: sqlite3.Row) -> CaseVersion:
    return CaseVersion(
        id=row["id"],
        case_id=row["case_id"],
        version_number=row["version_number"],
        payload=_load(row["payload_json"]),
        fingerprint=row["fingerprint"],
        script_fingerprint=row["script_fingerprint"],
        created_at=row["created_at"],
        created_by=row["created_by"],
    )


def _source(row: sqlite3.Row) -> CaseSource:
    return CaseSource(
        id=row["id"],
        case_id=row["case_id"],
        case_version_id=row["case_version_id"],
        source_type=row["source_type"],
        source_reference=row["source_reference"],
        metadata=_load(row["source_metadata_json"]),
        created_at=row["created_at"],
    )


def _link(row: sqlite3.Row) -> CaseLink:
    return CaseLink(
        id=row["id"],
        source_case_id=row["source_case_id"],
        target_case_id=row["target_case_id"],
        link_type=row["link_type"],
        metadata=_load(row["metadata_json"]),
        created_at=row["created_at"],
    )


def _import_job(row: sqlite3.Row) -> ImportJob:
    return ImportJob(
        id=row["id"],
        source_type=row["source_type"],
        filename=row["filename"],
        status=row["status"],
        summary=_load(row["summary_json"]),
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _approval(row: sqlite3.Row) -> ScriptApproval:
    return ScriptApproval(
        id=row["id"],
        case_version_id=row["case_version_id"],
        script_fingerprint=row["script_fingerprint"],
        approval_status=row["approval_status"],
        review_findings=_load(row["review_findings_json"], list),
        approved_at=row["approved_at"],
        created_at=row["created_at"],
    )


def _run(row: sqlite3.Row) -> BoxRun:
    return BoxRun(
        id=row["id"],
        status=row["status"],
        retry_of_run_id=row["retry_of_run_id"],
        metadata=_load(row["metadata_json"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _unit(row: sqlite3.Row) -> RunUnit:
    return RunUnit(
        id=row["id"],
        run_id=row["run_id"],
        unit_number=row["unit_number"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        metadata=_load(row["metadata_json"]),
    )


def _run_case(row: sqlite3.Row) -> RunCase:
    return RunCase(
        id=row["id"],
        run_id=row["run_id"],
        run_unit_id=row["run_unit_id"],
        case_id=row["case_id"],
        case_version_id=row["case_version_id"],
        execution_order=row["execution_order"],
        status=row["status"],
        result_metadata=_load(row["result_metadata_json"]),
    )


def _sql_in(values) -> str:
    return "(" + ", ".join(f"'{value}'" for value in sorted(values)) + ")"


class BoxStore:
    def __init__(self, connection: sqlite3.Connection, path: Path):
        """فقط با اتصالی که foreign keys روشن دارد (مثلِ db.connect) — وگرنه قیدهای
        مالکیتِ نسخه و run بی‌صدا خاموش می‌مانند."""
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise StorageError("BoxStore needs a connection with foreign keys enabled.")
        self._db = connection
        self.path = path

    @classmethod
    def open(cls, path: Path | str = DEFAULT_DB_PATH) -> BoxStore:
        """ساخت (در صورتِ نبود) و migrate — هرگز فایلِ موجود را حذف یا بازنویسی نمی‌کند."""
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

    def __enter__(self) -> BoxStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return schema_version(self._db)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """«BEGIN IMMEDIATE» … COMMIT؛ هر استثنا (از جمله شکستِ خودِ COMMIT) → ROLLBACKِ
        کامل و همان استثنا."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
            self._db.execute("COMMIT")
        except BaseException as exc:
            abort(self._db, exc)
            raise

    def _write(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        try:
            return self._db.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "immutable:" in message:
                raise ImmutableRecordError(message.split("immutable:", 1)[1].strip()) from exc
            if "integrity:" in message:
                message = message.split("integrity:", 1)[1].strip()
            raise IntegrityViolationError(message) from exc

    def _one(self, sql: str, params: tuple, what: str) -> sqlite3.Row:
        row = self._db.execute(sql, params).fetchone()
        if row is None:
            raise NotFoundError(f"{what} does not exist.")
        return row

    # ── cases ───────────────────────────────────────────────────────────────
    def create_case(self, new: NewCase) -> BoxCase:
        """case، نسخه‌ی ۱، منشأها و رابطه‌هایش در یک تراکنش — همه یا هیچ."""
        with self.transaction():
            case_id = self._insert_case(new, now())
        return self.get_case(case_id)

    def _insert_case(self, new: NewCase, stamp: str) -> int:
        """بدنه‌ی create_case — فقط داخلِ تراکنشِ فراخواننده."""
        case_id = self._write(
            "INSERT INTO box_cases (title, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (new.title, new.description, CASE_ACTIVE, stamp, stamp),
        ).lastrowid
        version_id = self._insert_version(
            case_id, 1, new.payload, new.fingerprint, new.created_by, stamp
        )
        self._write(
            "UPDATE box_cases SET current_version_id = ? WHERE id = ?", (version_id, case_id)
        )
        for source in new.sources:
            self._insert_source(case_id, version_id, source, stamp)
        for link in new.links:
            self._insert_link(case_id, link, stamp)
        return case_id

    def create_case_for_source(self, new: NewCase) -> SourcedCase:
        """مسیرِ رسمیِ واردکردنِ منشأِ BATCH: اول هویت، بعد اثرانگشت — همه در یک
        «BEGIN IMMEDIATE» تا دو واردکننده‌ی هم‌زمان caseِ تکراری نسازند.

        new باید دقیقاً یک منشأ داشته باشد، از نوعِ BATCH، با item_fingerprint در
        فراداده برابر با اثرانگشتِ payload؛ رابطه مجاز نیست. سپس:
          * هویتِ (BATCH، source_reference) نیست → case، نسخه‌ی ۱ و منشأ (CREATED)؛
          * هست و همه‌ی ردیف‌ها مالِ یک case و اثرانگشتِ ثبت‌شده‌ی منشأ و نسخه‌ی
            همان منشأ برابر است → ALREADY_PRESENT (caseِ بایگانی‌شده بازگردانده نمی‌شود)؛
          * اثرانگشتِ متفاوت یا فراداده‌ی ناخوانا → SOURCE_CONFLICT؛ هیچ case یا نسخه‌ای ساخته نمی‌شود؛
          * ردیف‌ها مالِ چند case → SOURCE_AMBIGUOUS؛ هیچ نوشتنی.

        schema یکتاییِ سراسریِ (source_type، source_reference) را تضمین نمی‌کند (UNIQUE فقط
        با case_id است)؛ این تضمین از این مسیرِ سریال‌شده می‌آید. create_case و add_version
        بدونِ تغییر می‌مانند.
        """
        if len(new.sources) != 1 or new.sources[0].source_type != SOURCE_BATCH:
            raise ValueError("A BATCH import needs exactly one BATCH source.")
        if new.links:
            raise ValueError("A BATCH import does not create links.")
        source = new.sources[0]
        fingerprint = sha256_json(new.payload)
        if new.fingerprint not in (None, fingerprint):
            raise ValueError("A BATCH import uses the canonical payload fingerprint.")
        if source.metadata.get("item_fingerprint") != fingerprint:
            raise ValueError("The BATCH source item_fingerprint must match the payload.")
        with self.transaction():
            existing = tuple(self.find_sources(source.source_type, source.source_reference))
            if not existing:
                case_id = self._insert_case(new, now())
                return SourcedCase(SOURCED_CREATED, self.get_case(case_id))
            if len({row.case_id for row in existing}) > 1:
                return SourcedCase(SOURCED_AMBIGUOUS, None, existing)
            for row in existing:
                version = (
                    self.get_version(row.case_version_id)
                    if row.case_version_id is not None
                    else None
                )
                if (
                    row.metadata.get("item_fingerprint") != fingerprint
                    or version is None
                    or version.case_id != row.case_id
                    or version.fingerprint != fingerprint
                ):
                    return SourcedCase(SOURCED_CONFLICT, None, existing)
            return SourcedCase(
                SOURCED_ALREADY_PRESENT, self.get_case(existing[0].case_id), existing
            )

    def get_case(self, case_id: int) -> BoxCase:
        return _case(
            self._one("SELECT * FROM box_cases WHERE id = ?", (case_id,), f"Test case {case_id}")
        )

    def list_cases(self, status: str | None = None) -> list[BoxCase]:
        if status is None:
            rows = self._db.execute("SELECT * FROM box_cases ORDER BY id").fetchall()
        else:
            if status not in CASE_STATUSES:
                raise ValueError(f"Unknown case status {status!r}.")
            rows = self._db.execute(
                "SELECT * FROM box_cases WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        return [_case(row) for row in rows]

    def update_case_details(
        self, case_id: int, *, title: str | None = None, description: str | None = None
    ) -> BoxCase:
        """فقط عنوان/توضیح (فرا‌داده‌ی قابلِ ویرایش) — محتوای اجرایی نسخه‌دار است."""
        if title is not None:
            validate_title(title)
        if description is not None and not isinstance(description, str):
            raise ValueError("description must be a string.")
        with self.transaction():
            current = self.get_case(case_id)
            self._write(
                "UPDATE box_cases SET title = ?, description = ?, updated_at = ? WHERE id = ?",
                (
                    current.title if title is None else title,
                    current.description if description is None else description,
                    now(),
                    case_id,
                ),
            )
        return self.get_case(case_id)

    def archive_case(self, case_id: int) -> BoxCase:
        return self._set_case_status(case_id, CASE_ARCHIVED)

    def restore_case(self, case_id: int) -> BoxCase:
        return self._set_case_status(case_id, CASE_ACTIVE)

    def _set_case_status(self, case_id: int, status: str) -> BoxCase:
        """بایگانی/بازگردانی؛ تکرارش بی‌اثر است و هیچ نسخه، منشأ یا اجرایی حذف نمی‌شود."""
        with self.transaction():
            if self.get_case(case_id).status != status:
                self._write(
                    "UPDATE box_cases SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now(), case_id),
                )
        return self.get_case(case_id)

    # ── versions ────────────────────────────────────────────────────────────
    def _insert_version(
        self,
        case_id: int,
        number: int,
        payload: dict[str, Any],
        fingerprint: str | None,
        created_by: str | None,
        stamp: str,
    ) -> int:
        return self._write(
            "INSERT INTO box_case_versions (case_id, version_number, payload_json, fingerprint, "
            "script_fingerprint, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                case_id,
                number,
                to_json(payload),
                fingerprint or sha256_json(payload),
                script_fingerprint(payload),
                stamp,
                created_by,
            ),
        ).lastrowid

    def add_version(self, case_id: int, new: NewVersion) -> CaseVersion:
        """نسخه‌ی بعدی (max + 1) و جابه‌جاییِ نسخه‌ی جاری در یک تراکنش."""
        stamp = now()
        with self.transaction():
            if self.get_case(case_id).archived:
                raise ArchivedCaseError(f"Test case {case_id} is archived; restore it first.")
            number = self._db.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM box_case_versions "
                "WHERE case_id = ?",
                (case_id,),
            ).fetchone()[0]
            version_id = self._insert_version(
                case_id, number, new.payload, new.fingerprint, new.created_by, stamp
            )
            self._write(
                "UPDATE box_cases SET current_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, stamp, case_id),
            )
            for source in new.sources:
                self._insert_source(case_id, version_id, source, stamp)
        return self.get_version(version_id)

    def get_version(self, version_id: int) -> CaseVersion:
        return _version(
            self._one(
                "SELECT * FROM box_case_versions WHERE id = ?",
                (version_id,),
                f"Version {version_id}",
            )
        )

    def current_version(self, case_id: int) -> CaseVersion:
        return self.get_version(self.get_case(case_id).current_version_id)

    def versions(self, case_id: int) -> list[CaseVersion]:
        rows = self._db.execute(
            "SELECT * FROM box_case_versions WHERE case_id = ? ORDER BY version_number",
            (case_id,),
        ).fetchall()
        return [_version(row) for row in rows]

    def versions_with_fingerprint(self, fingerprint: str) -> list[CaseVersion]:
        rows = self._db.execute(
            "SELECT * FROM box_case_versions WHERE fingerprint = ? ORDER BY id", (fingerprint,)
        ).fetchall()
        return [_version(row) for row in rows]

    # ── provenance ──────────────────────────────────────────────────────────
    def _insert_source(
        self, case_id: int, version_id: int | None, source: NewSource, stamp: str
    ) -> int:
        return self._write(
            "INSERT INTO box_case_sources (case_id, case_version_id, source_type, "
            "source_reference, source_metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                case_id,
                version_id,
                source.source_type,
                source.source_reference,
                to_json(source.metadata),
                stamp,
            ),
        ).lastrowid

    def add_source(
        self, case_id: int, source: NewSource, *, case_version_id: int | None = None
    ) -> CaseSource:
        with self.transaction():
            self.get_case(case_id)
            source_id = self._insert_source(case_id, case_version_id, source, now())
        return _source(
            self._db.execute("SELECT * FROM box_case_sources WHERE id = ?", (source_id,)).fetchone()
        )

    def sources(self, case_id: int) -> list[CaseSource]:
        rows = self._db.execute(
            "SELECT * FROM box_case_sources WHERE case_id = ? ORDER BY id", (case_id,)
        ).fetchall()
        return [_source(row) for row in rows]

    def find_sources(self, source_type: str, source_reference: str) -> list[CaseSource]:
        rows = self._db.execute(
            "SELECT * FROM box_case_sources WHERE source_type = ? AND source_reference = ? "
            "ORDER BY id",
            (source_type, source_reference),
        ).fetchall()
        return [_source(row) for row in rows]

    # ── links ───────────────────────────────────────────────────────────────
    def _insert_link(self, source_case_id: int, link: NewLink, stamp: str) -> int:
        self.get_case(link.target_case_id)
        if link.link_type == LINK_DEPENDS_ON and self._depends_on(
            link.target_case_id, source_case_id
        ):
            raise IntegrityViolationError(
                f"Linking {source_case_id} -> {link.target_case_id} would create a dependency cycle."
            )
        return self._write(
            "INSERT INTO box_case_links (source_case_id, target_case_id, link_type, metadata_json, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (source_case_id, link.target_case_id, link.link_type, to_json(link.metadata), stamp),
        ).lastrowid

    def _depends_on(self, start: int, goal: int) -> bool:
        """آیا start (مستقیم یا غیرمستقیم) به goal وابسته است؟"""
        row = self._db.execute(
            "WITH RECURSIVE reach(id) AS ("
            "  SELECT ? UNION"
            "  SELECT l.target_case_id FROM box_case_links l JOIN reach r ON l.source_case_id = r.id"
            "  WHERE l.link_type = ?"
            ") SELECT 1 FROM reach WHERE id = ? LIMIT 1",
            (start, LINK_DEPENDS_ON, goal),
        ).fetchone()
        return row is not None

    def add_link(self, source_case_id: int, link: NewLink) -> CaseLink:
        with self.transaction():
            self.get_case(source_case_id)
            link_id = self._insert_link(source_case_id, link, now())
        return _link(
            self._db.execute("SELECT * FROM box_case_links WHERE id = ?", (link_id,)).fetchone()
        )

    def links(self, case_id: int) -> list[CaseLink]:
        """رابطه‌های خروجی و ورودیِ یک case."""
        rows = self._db.execute(
            "SELECT * FROM box_case_links WHERE source_case_id = ? OR target_case_id = ? "
            "ORDER BY id",
            (case_id, case_id),
        ).fetchall()
        return [_link(row) for row in rows]

    def remove_link(self, link_id: int) -> None:
        """رابطه تاریخچه نیست؛ حذفش هیچ نسخه یا اجرایی را لمس نمی‌کند."""
        with self.transaction():
            if self._write("DELETE FROM box_case_links WHERE id = ?", (link_id,)).rowcount == 0:
                raise NotFoundError(f"Link {link_id} does not exist.")

    # ── import jobs ─────────────────────────────────────────────────────────
    def create_import_job(
        self, source_type: str, filename: str, summary: dict[str, Any] | None = None
    ) -> ImportJob:
        """فقط فراداده — محتوای فایلِ بارگذاری‌شده هرگز ذخیره نمی‌شود."""
        if not isinstance(source_type, str) or not source_type.strip():
            raise ValueError("source_type must be a non-empty string.")
        if not isinstance(filename, str):
            raise ValueError("filename must be a string.")
        summary = {} if summary is None else summary
        validate_json("import summary", summary)
        with self.transaction():
            job_id = self._write(
                "INSERT INTO box_import_jobs (source_type, filename, status, summary_json, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (source_type, filename, IMPORT_PENDING, to_json(summary), now()),
            ).lastrowid
        return self.get_import_job(job_id)

    def get_import_job(self, job_id: int) -> ImportJob:
        return _import_job(
            self._one(
                "SELECT * FROM box_import_jobs WHERE id = ?", (job_id,), f"Import job {job_id}"
            )
        )

    def finish_import_job(
        self, job_id: int, status: str, summary: dict[str, Any] | None = None
    ) -> ImportJob:
        if summary is not None:
            validate_json("import summary", summary)
        with self.transaction():
            current = self.get_import_job(job_id)
            check_import_transition(current.status, status)
            self._write(
                "UPDATE box_import_jobs SET status = ?, summary_json = ?, completed_at = ? "
                "WHERE id = ?",
                (status, to_json(current.summary if summary is None else summary), now(), job_id),
            )
        return self.get_import_job(job_id)

    # ── script approvals ────────────────────────────────────────────────────
    def record_script_decision(
        self,
        version_id: int,
        reviewed_fingerprint: str,
        status: str,
        review_findings: list[Any] | None = None,
    ) -> ScriptApproval:
        """تصمیمِ بازبین برای اسکریپت‌هایی که دیده است (اثرانگشتشان باید با نسخه بخواند)."""
        if status not in APPROVAL_STATUSES:
            raise ValueError(f"Unknown approval status {status!r}.")
        if not is_sha256(reviewed_fingerprint):
            raise ValueError("reviewed_fingerprint must be a lowercase SHA-256 hex digest.")
        findings = [] if review_findings is None else review_findings
        validate_json("review findings", findings, list)
        stamp = now()
        with self.transaction():
            version = self.get_version(version_id)
            if version.script_fingerprint is None:
                raise IntegrityViolationError(f"Version {version_id} has no scripts to approve.")
            if version.script_fingerprint != reviewed_fingerprint:
                raise IntegrityViolationError(
                    f"The reviewed scripts do not match version {version_id}."
                )
            approval_id = self._write(
                "INSERT INTO box_script_approvals (case_version_id, script_fingerprint, "
                "approval_status, review_findings_json, approved_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    reviewed_fingerprint,
                    status,
                    to_json(findings),
                    stamp if status == APPROVAL_APPROVED else None,
                    stamp,
                ),
            ).lastrowid
        return _approval(
            self._db.execute(
                "SELECT * FROM box_script_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        )

    def script_approvals(self, version_id: int) -> list[ScriptApproval]:
        rows = self._db.execute(
            "SELECT * FROM box_script_approvals WHERE case_version_id = ? ORDER BY id",
            (version_id,),
        ).fetchall()
        return [_approval(row) for row in rows]

    def script_approval_state(self, version_id: int) -> str:
        """NOT_REQUIRED (بی‌اسکریپت)، PENDING (بی‌تصمیم) یا آخرین تصمیم برای همین اثرانگشت."""
        version = self.get_version(version_id)
        if version.script_fingerprint is None:
            return SCRIPTS_NOT_REQUIRED
        row = self._db.execute(
            "SELECT approval_status FROM box_script_approvals "
            "WHERE case_version_id = ? AND script_fingerprint = ? ORDER BY id DESC LIMIT 1",
            (version_id, version.script_fingerprint),
        ).fetchone()
        return SCRIPTS_PENDING if row is None else row["approval_status"]

    def scripts_approved(self, version_id: int) -> bool:
        return self.script_approval_state(version_id) == APPROVAL_APPROVED

    # ── runs ────────────────────────────────────────────────────────────────
    def create_run(self, new: NewRun) -> BoxRun:
        """run، unitها و نسخه‌های سنجاق‌شده در یک تراکنش — همه یا هیچ (بی‌مالک)."""
        stamp = now()
        with self.transaction():
            run_id = self._insert_run(new, stamp)
        return self.get_run(run_id)

    def _insert_run(self, new: NewRun, stamp: str, owner: tuple | None = None) -> int:
        """داخلِ تراکنشِ فراخواننده. owner = (token، pid، host) → run با lease ِ نسلِ ۱."""
        if new.retry_of_run_id is not None:
            self.get_run(new.retry_of_run_id)
        if owner is None:
            # همان INSERT ِ v1–v3 — ستون‌های v4 مقدارِ پیش‌فرضِ «بی‌مالک» می‌گیرند
            run_id = self._write(
                "INSERT INTO box_runs (status, retry_of_run_id, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (RUN_QUEUED, new.retry_of_run_id, to_json(new.metadata), stamp),
            ).lastrowid
        else:
            token, pid, host = owner
            run_id = self._write(
                "INSERT INTO box_runs (status, retry_of_run_id, metadata_json, created_at, "
                "owner_token, owner_pid, owner_host, heartbeat_at, lease_generation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    RUN_QUEUED,
                    new.retry_of_run_id,
                    to_json(new.metadata),
                    stamp,
                    token,
                    pid,
                    host,
                    stamp,
                ),
            ).lastrowid
        for unit in new.units:
            unit_id = self._write(
                "INSERT INTO box_run_units (run_id, unit_number, status, metadata_json) "
                "VALUES (?, ?, ?, ?)",
                (run_id, unit.unit_number, UNIT_QUEUED, to_json(unit.metadata)),
            ).lastrowid
            for case in unit.cases:
                version_owner = self.get_version(case.case_version_id).case_id
                if version_owner != case.case_id:
                    raise IntegrityViolationError(
                        f"Version {case.case_version_id} does not belong to test case "
                        f"{case.case_id}."
                    )
                self._write(
                    "INSERT INTO box_run_cases (run_id, run_unit_id, case_id, "
                    "case_version_id, execution_order, status, result_metadata_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        unit_id,
                        case.case_id,
                        case.case_version_id,
                        case.execution_order,
                        case.status,
                        to_json(case.result_metadata),
                    ),
                )
        return run_id

    def get_run(self, run_id: int) -> BoxRun:
        return _run(self._one("SELECT * FROM box_runs WHERE id = ?", (run_id,), f"Run {run_id}"))

    def list_runs(self, limit: int = 50) -> list[BoxRun]:
        rows = self._db.execute(
            "SELECT * FROM box_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_run(row) for row in rows]

    def get_run_unit(self, unit_id: int) -> RunUnit:
        return _unit(
            self._one("SELECT * FROM box_run_units WHERE id = ?", (unit_id,), f"Run unit {unit_id}")
        )

    def run_units(self, run_id: int) -> list[RunUnit]:
        rows = self._db.execute(
            "SELECT * FROM box_run_units WHERE run_id = ? ORDER BY unit_number", (run_id,)
        ).fetchall()
        return [_unit(row) for row in rows]

    def get_run_case(self, run_case_id: int) -> RunCase:
        return _run_case(
            self._one(
                "SELECT * FROM box_run_cases WHERE id = ?",
                (run_case_id,),
                f"Run case {run_case_id}",
            )
        )

    def run_cases(self, run_id: int, *, unit_id: int | None = None) -> list[RunCase]:
        sql = (
            "SELECT c.* FROM box_run_cases c JOIN box_run_units u ON u.id = c.run_unit_id "
            "WHERE c.run_id = ?"
        )
        params: tuple = (run_id,)
        if unit_id is not None:
            sql += " AND c.run_unit_id = ?"
            params += (unit_id,)
        rows = self._db.execute(sql + " ORDER BY u.unit_number, c.execution_order", params)
        return [_run_case(row) for row in rows.fetchall()]

    def pinned_versions(self, run_id: int) -> list[CaseVersion]:
        """نسخه‌های دقیقِ همان اجرا (به ترتیبِ unit و execution_order) — مستقل از نسخه‌ی
        جاریِ caseها؛ پایه‌ی rerun.

        قیدِ طراحیِ M7: rerun از unitها و execution_orderِ ذخیره‌شده‌ی همین run بازسازی
        می‌شود، هرگز از box_case_links ِ فعلی — رابطه‌ها مالِ case‌اند، نه نسخه، و حذفشان
        سابقه ندارد."""
        self.get_run(run_id)
        rows = self._db.execute(
            "SELECT v.* FROM box_run_cases c "
            "JOIN box_run_units u ON u.id = c.run_unit_id "
            "JOIN box_case_versions v ON v.id = c.case_version_id "
            "WHERE c.run_id = ? ORDER BY u.unit_number, c.execution_order",
            (run_id,),
        ).fetchall()
        return [_version(row) for row in rows]

    def set_run_status(
        self, run_id: int, status: str, *, metadata: dict[str, Any] | None = None
    ) -> BoxRun:
        if metadata is not None:
            validate_json("run metadata", metadata)
        stamp = now()
        with self.transaction():
            current = self.get_run(run_id)
            self._check_unleased(run_id)
            check_run_transition(current.status, status)
            if status in RUN_FINAL:
                self._check_children_final(run_id, status)
            self._write(
                "UPDATE box_runs SET status = ?, metadata_json = ?, started_at = ?, "
                "finished_at = ? WHERE id = ?",
                (
                    status,
                    to_json(current.metadata if metadata is None else metadata),
                    stamp if status == RUN_RUNNING else current.started_at,
                    stamp if status in RUN_FINAL else None,
                    run_id,
                ),
            )
        return self.get_run(run_id)

    def _check_children_final(self, run_id: int, status: str) -> None:
        """سیاستِ پایانی‌شدن: هیچ بازنویسیِ خودکاری نیست — runner/recovery باید هر unit و
        run caseِ باز را خودش صریحاً پایانی کند (مثلاً NOT_RUN یا CANCELLED)."""
        open_units = self._db.execute(
            f"SELECT COUNT(*) FROM box_run_units WHERE run_id = ? "
            f"AND status NOT IN {_sql_in(UNIT_FINAL)}",
            (run_id,),
        ).fetchone()[0]
        open_cases = self._db.execute(
            f"SELECT COUNT(*) FROM box_run_cases WHERE run_id = ? "
            f"AND status NOT IN {_sql_in(RUN_CASE_FINAL)}",
            (run_id,),
        ).fetchone()[0]
        if open_units or open_cases:
            raise InvalidTransitionError(
                f"Run {run_id} cannot become {status}: {open_units} unit(s) and "
                f"{open_cases} case(s) are not final yet."
            )

    def _check_unit_cases_final(self, unit_id: int, status: str) -> None:
        """همان سیاستِ run در سطحِ unit: هر run caseِ باز را خودِ فراخواننده صریحاً
        پایانی می‌کند؛ هیچ وضعیتی خودکار بازنویسی نمی‌شود."""
        open_cases = self._db.execute(
            f"SELECT COUNT(*) FROM box_run_cases WHERE run_unit_id = ? "
            f"AND status NOT IN {_sql_in(RUN_CASE_FINAL)}",
            (unit_id,),
        ).fetchone()[0]
        if open_cases:
            raise InvalidTransitionError(
                f"Unit {unit_id} cannot become {status}: {open_cases} case(s) are not final yet."
            )

    def _check_run_open(self, run_id: int) -> None:
        if self.get_run(run_id).final:
            raise ImmutableRecordError(f"Run {run_id} is final; its records cannot change.")

    def _check_unit_open(self, unit_id: int) -> None:
        if self.get_run_unit(unit_id).status in UNIT_FINAL:
            raise ImmutableRecordError(f"Unit {unit_id} is final; its run cases cannot change.")

    def set_unit_status(
        self, unit_id: int, status: str, *, metadata: dict[str, Any] | None = None
    ) -> RunUnit:
        if metadata is not None:
            validate_json("unit metadata", metadata)
        stamp = now()
        with self.transaction():
            current = self.get_run_unit(unit_id)
            self._check_unleased(current.run_id)
            self._check_run_open(current.run_id)
            check_unit_transition(current.status, status)
            if status in UNIT_FINAL:
                self._check_unit_cases_final(unit_id, status)
            self._write(
                "UPDATE box_run_units SET status = ?, metadata_json = ?, started_at = ?, "
                "finished_at = ? WHERE id = ?",
                (
                    status,
                    to_json(current.metadata if metadata is None else metadata),
                    stamp if status == UNIT_RUNNING else current.started_at,
                    stamp if status in UNIT_FINAL else None,
                    unit_id,
                ),
            )
        return self.get_run_unit(unit_id)

    def set_run_case_status(
        self, run_case_id: int, status: str, *, result_metadata: dict[str, Any] | None = None
    ) -> RunCase:
        if result_metadata is not None:
            validate_json("result metadata", result_metadata)
        with self.transaction():
            current = self.get_run_case(run_case_id)
            self._check_unleased(current.run_id)
            self._check_run_open(current.run_id)
            self._check_unit_open(current.run_unit_id)
            check_run_case_transition(current.status, status)
            self._write(
                "UPDATE box_run_cases SET status = ?, result_metadata_json = ? WHERE id = ?",
                (
                    status,
                    to_json(
                        current.result_metadata if result_metadata is None else result_metadata
                    ),
                    run_case_id,
                ),
            )
        return self.get_run_case(run_case_id)

    # ── مالکیتِ اجرای run (v4) ───────────────────────────────────────────────
    _CLEAR_OWNER = "owner_token = NULL, owner_pid = NULL, owner_host = NULL, heartbeat_at = NULL"

    @staticmethod
    def _owner_identity(pid: int, host: str) -> None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            raise ValueError("pid must be a positive integer.")
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host must be a non-empty string.")

    def _check_unleased(self, run_id: int) -> None:
        """متدهای بی‌مالک (set_*_status) به runِ دارای lease دست نمی‌زنند. پیش از v4 هیچ
        leaseی وجود ندارد (پایگاهِ قدیمی که هنوز migrate نشده، مثلاً در تست‌های migration)."""
        if schema_version(self._db) < 4:
            return
        row = self._one("SELECT owner_token FROM box_runs WHERE id = ?", (run_id,), f"Run {run_id}")
        if row["owner_token"] is not None:
            raise RunOwnershipError(
                f"Run {run_id} is owned by an execution; only its lease can change it."
            )

    def _check_lease(self, lease: RunLease) -> sqlite3.Row:
        """داخلِ تراکنشِ IMMEDIATE: lease هنوز همان مالک و همان نسل است (مقایسه‌ی ثابت‌زمان)."""
        if not isinstance(lease, RunLease) or not isinstance(lease.token, str):
            raise RunOwnershipError("A valid run lease is required.")
        row = self._one(
            "SELECT * FROM box_runs WHERE id = ?", (lease.run_id,), f"Run {lease.run_id}"
        )
        stored = row["owner_token"]
        if (
            stored is None
            or not hmac.compare_digest(stored.encode(), lease.token.encode())
            or row["lease_generation"] != lease.generation
        ):
            raise RunOwnershipError(f"This caller does not own run {lease.run_id}.")
        return row

    def _check_no_other_active(self, run_id: int | None = None) -> None:
        row = self._db.execute(
            "SELECT id FROM box_runs WHERE (status = ? OR owner_token IS NOT NULL) "
            "AND id IS NOT ? LIMIT 1",
            (RUN_RUNNING, run_id),
        ).fetchone()
        if row is not None:
            raise ActiveRunError(f"Run {row['id']} is already active; nothing was changed.")

    def _owned_write(self, lease: RunLease, sql: str, params: tuple) -> None:
        """UPDATE ِ box_runs فقط اگر هنوز همان مالک و همان نسل — سدِ دوم پس از _check_lease."""
        cursor = self._write(
            f"{sql} WHERE id = ? AND owner_token = ? AND lease_generation = ?",
            (*params, lease.run_id, lease.token, lease.generation),
        )
        if cursor.rowcount != 1:
            raise RunOwnershipError(f"This caller does not own run {lease.run_id}.")

    @staticmethod
    def _active_index_error(exc: IntegrityViolationError) -> bool:
        return "ux_box_runs_single" in str(exc)

    def create_owned_run(self, new: NewRun, *, pid: int, host: str) -> tuple[BoxRun, RunLease]:
        """run (QUEUED)، unitها، نسخه‌های سنجاق‌شده و lease ِ نسلِ ۱ در یک تراکنش. runِ فعالِ
        دیگری (RUNNING یا دارای lease) → ActiveRunError و هیچ چیزی ساخته نمی‌شود."""
        self._owner_identity(pid, host)
        token, stamp = secrets.token_hex(16), now()
        with self.transaction():
            self._check_no_other_active()
            try:
                run_id = self._insert_run(new, stamp, owner=(token, pid, host))
            except IntegrityViolationError as exc:
                if self._active_index_error(exc):
                    raise ActiveRunError("Another run is already active.") from None
                raise
        return self.get_run(run_id), RunLease(run_id, 1, token)

    def claim_run(self, run_id: int, *, pid: int, host: str) -> RunLease:
        """runِ QUEUED ِ بی‌مالک را می‌گیرد (نسلِ بعدی). runِ مالک‌دار یا فعالِ دیگر → خطا."""
        self._owner_identity(pid, host)
        token, stamp = secrets.token_hex(16), now()
        with self.transaction():
            row = self._one("SELECT * FROM box_runs WHERE id = ?", (run_id,), f"Run {run_id}")
            if row["owner_token"] is not None:
                raise RunOwnershipError(f"Run {run_id} already has an owner.")
            if row["status"] != RUN_QUEUED:
                raise InvalidTransitionError(
                    f"Only a QUEUED run can be claimed, not {row['status']}."
                )
            self._check_no_other_active(run_id)
            generation = row["lease_generation"] + 1
            try:
                cursor = self._write(
                    "UPDATE box_runs SET owner_token = ?, owner_pid = ?, owner_host = ?, "
                    "heartbeat_at = ?, lease_generation = ? "
                    "WHERE id = ? AND owner_token IS NULL AND lease_generation = ?",
                    (token, pid, host, stamp, generation, run_id, row["lease_generation"]),
                )
            except IntegrityViolationError as exc:
                if self._active_index_error(exc):
                    raise ActiveRunError("Another run is already active.") from None
                raise
            if cursor.rowcount != 1:
                raise RunOwnershipError(f"Run {run_id} already has an owner.")
        return RunLease(run_id, generation, token)

    def start_run(self, lease: RunLease, *, metadata: dict[str, Any] | None = None) -> BoxRun:
        """QUEUED → RUNNING، فقط با lease."""
        if metadata is not None:
            validate_json("run metadata", metadata)
        stamp = now()
        with self.transaction():
            row = self._check_lease(lease)
            check_run_transition(row["status"], RUN_RUNNING)
            try:
                self._owned_write(
                    lease,
                    "UPDATE box_runs SET status = ?, started_at = ?, metadata_json = ?",
                    (
                        RUN_RUNNING,
                        stamp,
                        row["metadata_json"] if metadata is None else to_json(metadata),
                    ),
                )
            except IntegrityViolationError as exc:
                if self._active_index_error(exc):
                    raise ActiveRunError("Another run is already RUNNING.") from None
                raise
        return self.get_run(lease.run_id)

    def heartbeat_run(self, lease: RunLease) -> str:
        """heartbeatِ همین مالک؛ زمانِ ثبت‌شده را برمی‌گرداند."""
        stamp = now()
        with self.transaction():
            self._check_lease(lease)
            self._owned_write(lease, "UPDATE box_runs SET heartbeat_at = ?", (stamp,))
        return stamp

    def owns_run(self, lease: RunLease) -> bool:
        try:
            self._check_lease(lease)
        except (RunOwnershipError, NotFoundError):
            return False
        return True

    def request_run_cancel(self, run_id: int) -> RunOwnership:
        """درخواستِ لغو از هر session (مثلِ request_action ِ Batch) — فقط برای runِ QUEUED یا
        RUNNING. خودِ لغو را مالکِ اجرا انجام می‌دهد؛ این متد هیچ وضعیتی را عوض نمی‌کند."""
        with self.transaction():
            row = self._one("SELECT * FROM box_runs WHERE id = ?", (run_id,), f"Run {run_id}")
            if row["status"] in RUN_FINAL:
                raise ImmutableRecordError(
                    f"Run {run_id} is {row['status']}; it cannot be cancelled."
                )
            self._write(
                "UPDATE box_runs SET requested_action = ? WHERE id = ?", (RUN_ACTION_CANCEL, run_id)
            )
        return self.run_ownership(run_id)

    def _unit_of(self, lease: RunLease, unit_id: int) -> RunUnit:
        unit = self.get_run_unit(unit_id)
        if unit.run_id != lease.run_id:
            raise IntegrityViolationError(f"Unit {unit_id} does not belong to run {lease.run_id}.")
        return unit

    def start_unit(self, lease: RunLease, unit_id: int) -> RunUnit:
        """unit: QUEUED → RUNNING، فقط با lease و فقط در runِ RUNNING."""
        stamp = now()
        with self.transaction():
            row = self._check_lease(lease)
            if row["status"] != RUN_RUNNING:
                raise InvalidTransitionError(f"Run {lease.run_id} is {row['status']}, not RUNNING.")
            unit = self._unit_of(lease, unit_id)
            check_unit_transition(unit.status, UNIT_RUNNING)
            self._write(
                "UPDATE box_run_units SET status = ?, started_at = ? WHERE id = ?",
                (UNIT_RUNNING, stamp, unit_id),
            )
        return self.get_run_unit(unit_id)

    def finish_case(
        self,
        lease: RunLease,
        run_case_id: int,
        status: str,
        *,
        result_metadata: dict[str, Any] | None = None,
    ) -> RunCase:
        """نتیجه‌ی پایانیِ یک run case، فقط با lease."""
        if result_metadata is not None:
            validate_json("result metadata", result_metadata)
        with self.transaction():
            self._check_lease(lease)
            case = self.get_run_case(run_case_id)
            if case.run_id != lease.run_id:
                raise IntegrityViolationError(
                    f"Run case {run_case_id} does not belong to run {lease.run_id}."
                )
            self._close_case(case, status, result_metadata)
        return self.get_run_case(run_case_id)

    def _close_case(self, case: RunCase, status: str, metadata: dict[str, Any] | None) -> None:
        check_run_case_transition(case.status, status)
        self._write(
            "UPDATE box_run_cases SET status = ?, result_metadata_json = ? WHERE id = ?",
            (
                status,
                to_json(case.result_metadata if metadata is None else metadata),
                case.id,
            ),
        )

    def _close_unit(
        self, unit: RunUnit, status: str, metadata: dict[str, Any] | None, stamp: str
    ) -> None:
        check_unit_transition(unit.status, status)
        self._write(
            "UPDATE box_run_units SET status = ?, finished_at = ?, metadata_json = ? WHERE id = ?",
            (status, stamp, to_json(unit.metadata if metadata is None else metadata), unit.id),
        )

    def finish_unit(
        self,
        lease: RunLease,
        unit_id: int,
        status: str,
        *,
        case_status: str | None = None,
        unit_metadata: dict[str, Any] | None = None,
        case_metadata: dict[str, Any] | None = None,
    ) -> RunUnit:
        """run caseهای بازِ unit (با case_status) و سپس خودِ unit (status ِ پایانی) در یک
        تراکنش، فقط با lease. caseِ باز بی case_status → InvalidTransitionError و هیچ تغییر."""
        if status not in UNIT_FINAL:
            raise InvalidTransitionError(f"{status!r} is not a final unit status.")
        for name, value in (("unit metadata", unit_metadata), ("result metadata", case_metadata)):
            if value is not None:
                validate_json(name, value)
        stamp = now()
        with self.transaction():
            self._check_lease(lease)
            unit = self._unit_of(lease, unit_id)
            for case in self.run_cases(lease.run_id, unit_id=unit_id):
                if case.status in RUN_CASE_FINAL:
                    continue
                if case_status is None:
                    raise InvalidTransitionError(
                        f"Unit {unit_id} has unfinished cases; a case status is required."
                    )
                self._close_case(case, case_status, case_metadata)
            self._close_unit(unit, status, unit_metadata, stamp)
        return self.get_run_unit(unit_id)

    def finish_run(
        self,
        lease: RunLease,
        status: str,
        *,
        metadata: dict[str, Any] | None = None,
        queued_unit_status: str | None = None,
        running_unit_status: str | None = None,
        open_case_status: str | None = None,
        child_metadata: dict[str, Any] | None = None,
        running_case_status: str | None = None,
        running_child_metadata: dict[str, Any] | None = None,
    ) -> BoxRun:
        """پایانِ اتمیِ run: بررسیِ مالکیت → بستنِ run caseهای باز (open_case_status) → بستنِ
        unitهای QUEUED / RUNNING (با وضعیتِ همان دسته) → وضعیتِ پایانیِ run و پاک کردنِ
        کاملِ lease — همه در یک تراکنش. هر شکست (از جمله گذارِ نامعتبر یا فرزندِ بازی که
        وضعیتش داده نشده) → rollbackِ کامل؛ هیچ بستنِ خودکار و بی‌دستوری نیست.

        child_metadata (مثلاً دلیلِ توقف) با فراداده‌ی هر فرزندی که این‌جا بسته می‌شود ادغام
        می‌شود؛ فرزندانِ از قبل پایانی دست نمی‌خورند.

        (M7.6) running_case_status / running_child_metadata — اختیاری و سازگار با قبل: caseهای
        بازِ unitی که هنوز RUNNING است (نتیجه‌اش نامعلوم) وضعیت و فراداده‌ی جدا می‌گیرند، تا
        بازیابی همه را در همین یک تراکنش ببندد. بدونِ آن‌ها رفتار همان قبلی است."""
        if status not in RUN_FINAL:
            raise InvalidTransitionError(f"{status!r} is not a final run status.")
        for name, value in (
            ("run metadata", metadata),
            ("child metadata", child_metadata),
            ("running child metadata", running_child_metadata),
        ):
            if value is not None:
                validate_json(name, value)
        extra = child_metadata or {}
        running_extra = running_child_metadata if running_child_metadata is not None else extra
        stamp = now()
        with self.transaction():
            row = self._check_lease(lease)
            check_run_transition(row["status"], status)
            running_units = {
                unit.id for unit in self.run_units(lease.run_id) if unit.status == UNIT_RUNNING
            }
            for case in self.run_cases(lease.run_id):
                if case.status in RUN_CASE_FINAL:
                    continue
                in_running = case.run_unit_id in running_units
                target_case = (
                    running_case_status
                    if in_running and running_case_status is not None
                    else open_case_status
                )
                if target_case is None:
                    raise InvalidTransitionError(
                        f"Run {lease.run_id} has unfinished cases; a case status is required."
                    )
                merged = {**case.result_metadata, **(running_extra if in_running else extra)}
                self._close_case(case, target_case, merged)
            for unit in self.run_units(lease.run_id):
                if unit.status in UNIT_FINAL:
                    continue
                target = queued_unit_status if unit.status == UNIT_QUEUED else running_unit_status
                if target is None:
                    raise InvalidTransitionError(
                        f"Run {lease.run_id} has {unit.status} units; a unit status is required."
                    )
                unit_extra = running_extra if unit.status == UNIT_RUNNING else extra
                self._close_unit(unit, target, {**unit.metadata, **unit_extra}, stamp)
            self._owned_write(
                lease,
                f"UPDATE box_runs SET status = ?, finished_at = ?, metadata_json = ?, "
                f"{self._CLEAR_OWNER}",
                (status, stamp, row["metadata_json"] if metadata is None else to_json(metadata)),
            )
        return self.get_run(lease.run_id)

    def release_run_lease(self, lease: RunLease) -> BoxRun:
        """lease ِ runِ هنوز-غیرپایانی را بی‌تغییرِ وضعیت پس می‌دهد (مثلاً ردِ پیش از شروع).
        runِ پایانی lease ندارد، پس lease ِ آن دیگر معتبر نیست (RunOwnershipError)."""
        with self.transaction():
            self._check_lease(lease)
            self._owned_write(lease, f"UPDATE box_runs SET {self._CLEAR_OWNER}", ())
        return self.get_run(lease.run_id)

    # ── خواندنِ مالکیت و پشتیبانیِ بازیابی ───────────────────────────────────
    @staticmethod
    def _ownership(row: sqlite3.Row) -> RunOwnership:
        return RunOwnership(
            run_id=row["id"],
            status=row["status"],
            leased=row["owner_token"] is not None,
            owner_pid=row["owner_pid"],
            owner_host=row["owner_host"],
            heartbeat_at=row["heartbeat_at"],
            lease_generation=row["lease_generation"],
            requested_action=row["requested_action"],
        )

    def run_ownership(self, run_id: int) -> RunOwnership:
        return self._ownership(
            self._one("SELECT * FROM box_runs WHERE id = ?", (run_id,), f"Run {run_id}")
        )

    def active_run(self) -> BoxRun | None:
        """runِ RUNNING یا دارای lease (حداکثر یکی — ایندکس‌های یکتای v4)."""
        row = self._db.execute(
            "SELECT * FROM box_runs WHERE status = ? OR owner_token IS NOT NULL "
            "ORDER BY id LIMIT 1",
            (RUN_RUNNING,),
        ).fetchone()
        return None if row is None else _run(row)

    def active_runs(self) -> list[BoxRun]:
        """همه‌ی runهای RUNNING یا دارای lease (M7.6 — برای بازرسیِ بازیابی؛ فقط خواندن)."""
        rows = self._db.execute(
            "SELECT * FROM box_runs WHERE status = ? OR owner_token IS NOT NULL ORDER BY id",
            (RUN_RUNNING,),
        ).fetchall()
        return [_run(row) for row in rows]

    def stale_runs(self, older_than: str) -> list[RunOwnership]:
        """runهای دارای lease با heartbeatِ قدیمی‌تر از older_than (قالبِ now()) — فقط خواندن.
        کهنگی به‌تنهایی اجازه‌ی گرفتن نمی‌دهد؛ تصمیم با بازیابیِ بعدی (و Gate ِ مشترک) است."""
        rows = self._db.execute(
            "SELECT * FROM box_runs WHERE owner_token IS NOT NULL AND heartbeat_at < ? ORDER BY id",
            (older_than,),
        ).fetchall()
        return [self._ownership(row) for row in rows]

    def take_over_run(
        self,
        run_id: int,
        *,
        expected_generation: int,
        stale_before: str,
        pid: int,
        host: str,
    ) -> RunLease:
        """بازپس‌گیریِ کنترل‌شده برای بازیابیِ بعدی: یک UPDATE ِ شرطی — هنوز دارای lease، هنوز
        همان نسل (fencing) و heartbeatِ هنوز کهنه — وگرنه RunOwnershipError و هیچ تغییر.
        نسل یکی بالا می‌رود، پس lease ِ مالکِ قبلی دیگر هیچ چیزی را تغییر نمی‌دهد."""
        self._owner_identity(pid, host)
        token, stamp = secrets.token_hex(16), now()
        with self.transaction():
            cursor = self._write(
                "UPDATE box_runs SET owner_token = ?, owner_pid = ?, owner_host = ?, "
                "heartbeat_at = ?, lease_generation = lease_generation + 1 "
                "WHERE id = ? AND owner_token IS NOT NULL AND lease_generation = ? "
                "AND heartbeat_at < ?",
                (token, pid, host, stamp, run_id, expected_generation, stale_before),
            )
            if cursor.rowcount != 1:
                raise RunOwnershipError(
                    f"Run {run_id} is no longer stale or has a different owner; nothing changed."
                )
        return RunLease(run_id, expected_generation + 1, token)
