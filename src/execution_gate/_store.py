"""
src/execution_gate/_store.py — پایگاهِ SQLiteِ Gate: رکوردِ مالک و تاریخچه‌ی فقط-افزودنی

پایگاه مرجعِ انحصار نیست (آن قفلِ سیستم‌عامل است)؛ فقط ثبت می‌کند چه کسی، از کی و با کدام
generation قفل را گرفته و آخرین heartbeat کی بوده است.

  * نوشتن فقط وقتی انجام می‌شود که فراخواننده قفلِ سیستم‌عامل را دارد؛ پس ساختنِ پایگاهِ تازه
    هم هم‌زمان نیست. ساختن در یک تراکنشِ IMMEDIATE است: application_id، user_version،
    جدول‌ها و ردیفِ تکِ مالک با هم commit می‌شوند یا هیچ‌کدام.
  * هویت پیش از sqlite3.connect با خواندنِ خامِ سرآیند بررسی می‌شود (magic،
    application_id، user_version) تا فایلِ بیگانه یا نسخه‌ی دیگر هرگز باز، تعمیر یا
    بازنویسی نشود — حتی rollbackِ hot journal ِ پایگاهِ بیگانه.
  * journal_mode پیش‌فرض (DELETE) با synchronous=FULL؛ WAL لازم نیست (نوشتن‌ها کوچک و کم‌اند)
    و اتصالِ فقط‌خواندنی (mode=ro) بدونِ ساختنِ فایلِ جانبی کار می‌کند.
  * توکنِ مالک فقط در gate_owner است (برای نوشتنِ شرطی)؛ در تاریخچه و خروجیِ status نیست.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .errors import GateUnavailable

APPLICATION_ID = 0x45584754  # «EXGT»
SCHEMA_VERSION = 1
BUSY_TIMEOUT_S = 5.0
KINDS = ("BATCH", "BOX")
EVENT_ACQUIRED = "ACQUIRED"
EVENT_RELEASED = "RELEASED"
EVENT_RECLAIMED = "RECLAIMED"

_MAGIC = b"SQLite format 3\x00"
_KINDS_SQL = "('BATCH', 'BOX')"
_OWNER_COLUMNS = (
    "owner_kind",
    "owner_ref",
    "owner_token",
    "owner_pid",
    "owner_host",
    "acquired_at",
    "heartbeat_at",
)
_ALL_NULL = " AND ".join(f"{c} IS NULL" for c in _OWNER_COLUMNS)
_ALL_SET = " AND ".join(f"{c} IS NOT NULL" for c in _OWNER_COLUMNS)

SCHEMA: tuple[str, ...] = (
    f"""
    CREATE TABLE gate_owner (
        slot INTEGER PRIMARY KEY CHECK (slot = 1),
        owner_kind TEXT CHECK (owner_kind IS NULL OR owner_kind IN {_KINDS_SQL}),
        owner_ref TEXT CHECK (owner_ref IS NULL OR length(trim(owner_ref)) > 0),
        owner_token TEXT CHECK (owner_token IS NULL OR length(owner_token) = 32),
        owner_pid INTEGER CHECK (owner_pid IS NULL OR owner_pid > 0),
        owner_host TEXT,
        acquired_at TEXT,
        heartbeat_at TEXT,
        generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
        CHECK (({_ALL_NULL}) OR ({_ALL_SET}))
    )
    """,
    f"""
    CREATE TABLE gate_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        event TEXT NOT NULL CHECK (event IN ('{EVENT_ACQUIRED}', '{EVENT_RELEASED}',
                                             '{EVENT_RECLAIMED}')),
        generation INTEGER NOT NULL,
        owner_kind TEXT NOT NULL CHECK (owner_kind IN {_KINDS_SQL}),
        owner_ref TEXT NOT NULL,
        owner_pid INTEGER NOT NULL,
        owner_host TEXT NOT NULL,
        previous_generation INTEGER,
        previous_owner_kind TEXT,
        previous_owner_ref TEXT,
        previous_owner_pid INTEGER,
        previous_owner_host TEXT
    )
    """,
    "INSERT INTO gate_owner (slot) VALUES (1)",
    """
    CREATE TRIGGER tr_gate_owner_no_delete BEFORE DELETE ON gate_owner
    BEGIN SELECT RAISE(ABORT, 'immutable: the gate owner row cannot be deleted'); END
    """,
    """
    CREATE TRIGGER tr_gate_owner_generation BEFORE UPDATE OF generation ON gate_owner
    WHEN NEW.generation < OLD.generation
    BEGIN SELECT RAISE(ABORT, 'integrity: the gate generation never decreases'); END
    """,
    """
    CREATE TRIGGER tr_gate_events_no_update BEFORE UPDATE ON gate_events
    BEGIN SELECT RAISE(ABORT, 'immutable: gate history cannot be changed'); END
    """,
    """
    CREATE TRIGGER tr_gate_events_no_delete BEFORE DELETE ON gate_events
    BEGIN SELECT RAISE(ABORT, 'immutable: gate history cannot be deleted'); END
    """,
)
_EXPECTED_OBJECTS = {
    ("table", "gate_owner"),
    ("table", "gate_events"),
    ("trigger", "tr_gate_owner_no_delete"),
    ("trigger", "tr_gate_owner_generation"),
    ("trigger", "tr_gate_events_no_update"),
    ("trigger", "tr_gate_events_no_delete"),
}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _unavailable(what: str, exc: BaseException | None = None) -> GateUnavailable:
    detail = f" ({type(exc).__name__})" if exc is not None else ""
    return GateUnavailable(f"The execution gate database {what}{detail}; nothing was started.")


def _header(path: Path) -> bytes | None:
    """۱۰۰ بایتِ اول؛ None یعنی فایل نیست."""
    try:
        with open(path, "rb") as handle:
            return handle.read(100)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _unavailable("cannot be read", exc) from None


def _check_header(header: bytes) -> None:
    if len(header) < 100 or not header.startswith(_MAGIC):
        raise _unavailable("is not a SQLite database")
    if int.from_bytes(header[68:72], "big") != APPLICATION_ID:
        raise _unavailable("belongs to another application")
    version = int.from_bytes(header[60:64], "big")
    if version != SCHEMA_VERSION:
        raise _unavailable(f"has unsupported schema version {version}")


def _verify(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise _unavailable("belongs to another application")
    if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise _unavailable("has an unsupported schema version")
    objects = {
        (row[0], row[1])
        for row in connection.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    if not _EXPECTED_OBJECTS <= objects:
        raise _unavailable("has an incomplete schema")
    if connection.execute("SELECT COUNT(*) FROM gate_owner WHERE slot = 1").fetchone()[0] != 1:
        raise _unavailable("has no owner record")


def _initialize(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]:
            raise _unavailable("is not empty but has no gate identity")
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        for statement in SCHEMA:
            connection.execute(statement)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def open_write(path: Path) -> sqlite3.Connection:
    """اتصالِ نوشتنی — فقط با قفلِ سیستم‌عامل. پایگاهِ نبود یا خالی ساخته می‌شود."""
    header = _header(path)
    new = header is None or len(header) == 0
    if not new:
        _check_header(header)
    try:
        connection = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S, isolation_level=None)
    except sqlite3.Error as exc:
        raise _unavailable("cannot be opened", exc) from None
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
        connection.execute("PRAGMA synchronous = FULL")
        if new:
            _initialize(connection)
        _verify(connection)
    except GateUnavailable:
        connection.close()
        raise
    except sqlite3.Error as exc:
        connection.close()
        raise _unavailable("cannot be used", exc) from None
    except BaseException:
        connection.close()
        raise
    return connection


def open_read(path: Path) -> sqlite3.Connection | None:
    """اتصالِ فقط‌خواندنی؛ None یعنی Gate هنوز ساخته نشده. هیچ فایلی ساخته یا تعمیر نمی‌شود."""
    header = _header(path)
    if header is None or len(header) == 0:
        return None
    _check_header(header)
    try:
        connection = sqlite3.connect(
            f"{Path(path).resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=BUSY_TIMEOUT_S,
            isolation_level=None,
        )
    except sqlite3.Error as exc:
        raise _unavailable("cannot be opened", exc) from None
    try:
        connection.row_factory = sqlite3.Row
        _verify(connection)
    except GateUnavailable:
        connection.close()
        raise
    except sqlite3.Error as exc:
        connection.close()
        raise _unavailable("cannot be read", exc) from None
    except BaseException:
        connection.close()
        raise
    return connection


__all__ = [
    "APPLICATION_ID",
    "BUSY_TIMEOUT_S",
    "EVENT_ACQUIRED",
    "EVENT_RECLAIMED",
    "EVENT_RELEASED",
    "KINDS",
    "SCHEMA",
    "SCHEMA_VERSION",
    "now",
    "open_read",
    "open_write",
]
