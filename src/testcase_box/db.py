"""
src/testcase_box/db.py — اتصال و migrationِ پایگاهِ داده‌ی TestCase Box (sqlite3)

همان قراردادهای Batch (کپی، نه import — Box به ذخیره‌سازیِ Batch وابسته نیست):

  * اتصالِ autocommit با WAL، foreign keys و busy timeout؛ تراکنش‌ها صریح‌اند؛
  * migrationهای شماره‌دار و فقط‌رو‌به‌جلو (migrations.MIGRATIONS)، هر کدام در یک
    تراکنشِ «BEGIN IMMEDIATE»؛ اجرای دوباره idempotent است؛
  * پایگاهِ داده‌ی جدیدتر از کد رد می‌شود؛ فایلی که پایگاهِ Box نیست (application_id
    متفاوت) هرگز migrate یا بازنویسی نمی‌شود.

بررسیِ هویت پیش از sqlite3.connect: خودِ SQLite هنگامِ باز کردن ممکن است WALِ
checkpointنشده یا hot journal را بازیابی کند و فایلِ بیگانه را تغییر دهد؛ پس سرآیندِ
۱۰۰ بایتیِ فایل و وجودِ فایل‌های جانبی (-wal/-shm/-journal) اول با خواندنِ خالص
بررسی می‌شوند. پایگاهِ تازه application_id را پیش از روشن شدنِ WAL مستقیم در فایلِ
اصلی می‌نویسد تا سرآیند همیشه هویتِ Box را نشان دهد.

محدودیت: بینِ خواندنِ سرآیند و باز کردنِ فایل پنجره‌ی کوتاهی هست (TOCTOU). این بررسی
در برابرِ مسیرِ اشتباه یا فایلِ کپی‌شده محافظت می‌کند، نه در برابرِ پروسه‌ای که همان
لحظه فایل را عمداً عوض کند.

هیچ چیز هنگامِ import ساخته نمی‌شود؛ فقط BoxStore.open() پایگاهِ داده را می‌سازد.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from src.testcase_box.migrations import APPLICATION_ID, LATEST_VERSION, MIGRATIONS

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "testcase_box" / "box.sqlite3"
BUSY_TIMEOUT_S = 30.0
SQLITE_SIGNATURE = b"SQLite format 3\x00"
HEADER_SIZE = 100
APPLICATION_ID_OFFSET = 68
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class StorageError(RuntimeError):
    """خطای پایگاهِ داده — پیام هیچ محتوای تست‌کیس ندارد."""


class SchemaVersionError(StorageError):
    """پایگاهِ داده با نسخه‌ای جدیدتر از این کد ساخته شده است."""


class ForeignDatabaseError(StorageError):
    """فایل یک پایگاهِ داده‌ی دیگر است، نه TestCase Box — دست‌نخورده رها شد."""


class NotFoundError(StorageError):
    pass


class ImmutableRecordError(StorageError):
    """تلاش برای تغییر یا حذفِ تاریخچه‌ی تغییرناپذیر."""


class IntegrityViolationError(StorageError):
    """قیدِ یکپارچگی (کلیدِ خارجی، یکتایی، CHECK یا trigger) رد کرد — rollback شد."""


class RunOwnershipError(StorageError):
    """فراخواننده مالکِ اجرای این run نیست (یا run را مالکِ دیگری در اختیار دارد) — هیچ چیزی
    تغییر نکرد. پیام هرگز توکنِ مالک را ندارد."""


class ActiveRunError(StorageError):
    """runِ فعالِ دیگری (RUNNING یا دارای lease) هست — هیچ runی ساخته یا گرفته نشد."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def check_file(path: Path | str) -> bool:
    """بررسیِ فقط‌خواندنیِ فایل پیش از هر sqlite3.connect — True یعنی پایگاهِ تازه.

    پذیرفته: فایلِ ناموجود یا خالی (بی‌فایلِ جانبی)، یا SQLite با application_id ِ Box.
    هر چیزِ دیگر ForeignDatabaseError است و هیچ فایلی (اصلی یا جانبی) لمس نمی‌شود.
    """
    path = Path(path)
    sidecars = [suffix for suffix in SIDECAR_SUFFIXES if Path(f"{path}{suffix}").exists()]
    try:
        with open(path, "rb") as handle:
            header = handle.read(HEADER_SIZE)
    except FileNotFoundError:
        header = b""
    if not header:
        if sidecars:
            raise ForeignDatabaseError(
                f"The database file is missing or empty but {', '.join(sidecars)} file(s) "
                "exist next to it; nothing was changed. Remove the leftover files to start fresh."
            )
        return True
    if len(header) < HEADER_SIZE:
        raise ForeignDatabaseError("The file is a truncated SQLite header; it was left untouched.")
    if header[: len(SQLITE_SIGNATURE)] != SQLITE_SIGNATURE:
        raise ForeignDatabaseError("The file is not a SQLite database; it was left untouched.")
    stored_id = int.from_bytes(
        header[APPLICATION_ID_OFFSET : APPLICATION_ID_OFFSET + 4], "big", signed=True
    )
    if stored_id != APPLICATION_ID:
        raise ForeignDatabaseError(
            "The file is not a TestCase Box database; it was left untouched."
        )
    return False


def connect(path: Path | str) -> sqlite3.Connection:
    """اتصالِ autocommit (تراکنش‌ها صریح‌اند) با WAL، foreign keys و busy timeout.

    هویتِ فایل پیش از sqlite3.connect (check_file) و دوباره پس از آن (check_identity)
    بررسی می‌شود؛ پایگاهِ تازه هویتِ Box را پیش از WAL در فایلِ اصلی ثبت می‌کند.
    """
    new = check_file(path)
    connection = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
    try:
        if new:
            # هنوز در حالتِ rollback journal است، پس مستقیم در فایلِ اصلی نوشته می‌شود
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        check_identity(connection)
    except sqlite3.DatabaseError as exc:
        connection.close()
        if getattr(exc, "sqlite_errorname", "") == "SQLITE_NOTADB":
            raise ForeignDatabaseError(
                "The file is not a SQLite database; it was left untouched."
            ) from exc
        raise
    except BaseException:
        connection.close()
        raise
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


def check_identity(connection: sqlite3.Connection) -> None:
    """فقط پایگاهِ خالی یا پایگاهی با application_id ِ Box پذیرفته می‌شود."""
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    if application_id == APPLICATION_ID:
        return
    has_tables = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    if application_id != 0 or has_tables:
        raise ForeignDatabaseError(
            "The file is not a TestCase Box database; it was left untouched."
        )


def migrate(connection: sqlite3.Connection, target: int | None = None) -> int:
    """migrationهای باقی‌مانده تا target — هر کدام در یک تراکنشِ IMMEDIATE.

    نسخه داخلِ همان تراکنش دوباره خوانده می‌شود، پس دو پروسه که هم‌زمان یک پایگاهِ
    تازه را باز می‌کنند یک migration را دو بار اجرا نمی‌کنند.
    """
    check_identity(connection)
    target = LATEST_VERSION if target is None else target
    if schema_version(connection) > LATEST_VERSION:
        raise SchemaVersionError(
            f"The TestCase Box database has schema version {schema_version(connection)}, newer "
            f"than this code ({LATEST_VERSION}). Update the application instead of downgrading."
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
        except BaseException as exc:
            abort(connection, exc)
            raise
    return schema_version(connection)


def abort(connection: sqlite3.Connection, error: BaseException) -> None:
    """ROLLBACK فقط اگر تراکنشی هنوز باز است (SQLite بعضی خطاها را خودش rollback می‌کند،
    و COMMITِ ناموفق تراکنش را باز می‌گذارد). شکستِ خودِ ROLLBACK هرگز جای خطای اصلی
    را نمی‌گیرد؛ فقط به‌صورتِ یادداشت به آن افزوده می‌شود."""
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except Exception as rollback_error:
        error.add_note(
            f"TestCase Box: rollback also failed ({type(rollback_error).__name__}); "
            "the connection may still hold an open transaction."
        )
