"""
src/execution_gate/gate.py — ExecutionGate و GateLease

    gate = ExecutionGate()                       # data/execution_gate/
    with gate.acquire("BOX", "run-42") as lease:  # GateBusy / GateUnavailable
        lease.heartbeat()                         # دوره‌ای، از هر thread
        lease.assert_held()                       # درست پیش از هر کارِ اجرایی
    gate.status()                                 # فقط‌خواندنی

گرفتن (acquire)، به این ترتیب:
  1. قفلِ غیرمسدودکننده‌ی سیستم‌عامل روی gate.lock — اگر دیگری دارد: GateBusy (رکوردِ
     پایگاه فقط برای نمایشِ دارنده خوانده می‌شود)؛
  2. با قفلِ در دست، یک تراکنشِ IMMEDIATE: رکوردِ مالک ادعا می‌شود و generation یکی بالا
     می‌رود. اگر رکورد هنوز مالکِ دیگری را نشان می‌دهد، آن مالک قفلِ سیستم‌عامل را از دست
     داده (مرده یا آزاد کرده است) پس RECLAIMED ثبت و رکورد بازپس گرفته می‌شود؛
  3. اگر گامِ ۲ به هر دلیلی شکست بخورد، قفلِ سیستم‌عامل آزاد و GateUnavailable داده می‌شود.
lease فقط پس از هر دو گام برگردانده می‌شود.

قاعده‌ی heartbeatِ کهنه: کهنگیِ heartbeat هرگز به گرفتنِ Gate منجر نمی‌شود. فقط قفلِ
سیستم‌عامل تصمیم می‌گیرد؛ پروسه‌ی زنده ولی هنگ‌کرده همه را مسدود نگه می‌دارد تا واقعاً
بسته شود. heartbeat فقط برای نمایش و برای این است که خودِ مالک بفهمد رکوردش دست نخورده.

آزادسازی (release): اول رکورد پاک می‌شود، بعد قفلِ سیستم‌عامل — در هر حال، حتی اگر پاک کردنِ
رکورد شکست بخورد. release یعنی «مالک کارش را تمام کرده»؛ نگه داشتنِ قفل پس از آن هیچ ایمنی‌ای
نمی‌آورد و فقط قفلِ دائمی می‌سازد. رکوردِ باقی‌مانده بی‌خطر است: فقط دارنده‌ی بعدیِ قفلِ
سیستم‌عامل آن را بازپس می‌گیرد. خطای پایگاه در heartbeat و assert_held هرگز قفل را آزاد
نمی‌کند — فقط GateUnavailable می‌دهد و تصمیمِ توقف با فراخواننده است.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from . import _oslock, _store
from .errors import GateBusy, GateError, GateLeaseLost, GateLeaseReleased, GateUnavailable

DEFAULT_GATE_DIRECTORY = Path(__file__).resolve().parents[2] / "data" / "execution_gate"
LOCK_FILENAME = "gate.lock"
DATABASE_FILENAME = "gate.sqlite3"
KINDS = _store.KINDS
REF_MAX_CHARS = 200


@dataclass(frozen=True)
class GateOwner:
    """رکوردِ ثبت‌شده‌ی مالک — بدونِ توکن."""

    kind: str
    ref: str
    pid: int
    host: str
    acquired_at: str
    heartbeat_at: str
    generation: int

    def heartbeat_age_seconds(self, moment: datetime | None = None) -> float:
        stamp = datetime.fromisoformat(self.heartbeat_at.replace("Z", "+00:00"))
        return ((moment or datetime.now(UTC)) - stamp).total_seconds()


@dataclass(frozen=True)
class GateStatus:
    """تصویرِ فقط‌خواندنیِ پایگاه. owner فقط رکورد است؛ اینکه قفلِ سیستم‌عامل الان دستِ
    کسی هست یا نه را فقط acquire() می‌فهمد."""

    initialized: bool
    generation: int
    owner: GateOwner | None


class ReleaseOutcome(StrEnum):
    RELEASED = "RELEASED"  # رکورد پاک و قفلِ سیستم‌عامل آزاد شد
    RECORD_NOT_CLEARED = "RECORD_NOT_CLEARED"  # قفل آزاد شد؛ رکورد برای دارنده‌ی بعدی ماند
    ALREADY_RELEASED = "ALREADY_RELEASED"


def _validate(kind: str, ref: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}.")
    if (
        not isinstance(ref, str)
        or not ref.strip()
        or len(ref) > REF_MAX_CHARS
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in ref)
    ):
        raise ValueError(
            f"ref must be a non-empty single-line string of at most {REF_MAX_CHARS} characters."
        )


def _owner(row: sqlite3.Row) -> GateOwner | None:
    if row["owner_token"] is None:
        return None
    return GateOwner(
        kind=row["owner_kind"],
        ref=row["owner_ref"],
        pid=row["owner_pid"],
        host=row["owner_host"],
        acquired_at=row["acquired_at"],
        heartbeat_at=row["heartbeat_at"],
        generation=row["generation"],
    )


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass  # تراکنش باز نبود یا اتصال از دست رفته؛ بستنِ اتصال rollback می‌کند


class ExecutionGate:
    """Gate ِ یک پوشه‌ی محلی. ساختنش هیچ فایلی نمی‌سازد."""

    def __init__(self, directory: Path | str = DEFAULT_GATE_DIRECTORY) -> None:
        self.directory = Path(directory)

    @property
    def lock_path(self) -> Path:
        return self.directory / LOCK_FILENAME

    @property
    def database_path(self) -> Path:
        return self.directory / DATABASE_FILENAME

    def acquire(self, kind: str, ref: str) -> GateLease:
        _validate(kind, ref)
        _oslock.ensure_supported()
        # فقط رشته (abspath)، نه resolve — resolve ِ مسیرِ UNC خودش به شبکه سر می‌زند
        if os.name == "nt" and os.path.abspath(self.directory).startswith("\\\\"):
            raise GateUnavailable(
                "The execution gate must be on a local disk, not a network path; "
                "nothing was started."
            )
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GateUnavailable(
                f"The execution gate directory cannot be created ({type(exc).__name__}); "
                "nothing was started."
            ) from None
        lock = _oslock.OsLock(self.lock_path)
        if not lock.try_acquire():
            raise GateBusy(self._recorded_holder())
        token = uuid.uuid4().hex
        try:
            owner = self._claim(kind, ref, token)
        except GateError:
            lock.release()
            raise
        except (sqlite3.Error, OSError) as exc:
            lock.release()
            raise GateUnavailable(
                f"The execution gate owner record cannot be written ({type(exc).__name__}); "
                "nothing was started."
            ) from None
        except BaseException:
            lock.release()
            raise
        return GateLease(self, lock, token, owner)

    def _claim(self, kind: str, ref: str, token: str) -> GateOwner:
        connection = _store.open_write(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM gate_owner WHERE slot = 1").fetchone()
                previous = _owner(row)
                stamp = _store.now()
                owner = GateOwner(
                    kind,
                    ref,
                    os.getpid(),
                    socket.gethostname(),
                    stamp,
                    stamp,
                    row["generation"] + 1,
                )
                if previous is not None:
                    # قفل را داریم، پس مالکِ ثبت‌شده آن را از دست داده است
                    self._event(connection, _store.EVENT_RECLAIMED, owner, previous)
                cursor = connection.execute(
                    "UPDATE gate_owner SET owner_kind = ?, owner_ref = ?, owner_token = ?, "
                    "owner_pid = ?, owner_host = ?, acquired_at = ?, heartbeat_at = ?, "
                    "generation = ? WHERE slot = 1 AND generation = ?",
                    (
                        kind,
                        ref,
                        token,
                        owner.pid,
                        owner.host,
                        stamp,
                        stamp,
                        owner.generation,
                        row["generation"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise GateUnavailable(
                        "The execution gate owner record changed during acquisition; "
                        "nothing was started."
                    )
                self._event(connection, _store.EVENT_ACQUIRED, owner)
                connection.execute("COMMIT")
            except BaseException:
                _rollback(connection)
                raise
        finally:
            connection.close()
        return owner

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        event: str,
        owner: GateOwner,
        previous: GateOwner | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO gate_events (at, event, generation, owner_kind, owner_ref, owner_pid, "
            "owner_host, previous_generation, previous_owner_kind, previous_owner_ref, "
            "previous_owner_pid, previous_owner_host) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _store.now(),
                event,
                owner.generation,
                owner.kind,
                owner.ref,
                owner.pid,
                owner.host,
                previous.generation if previous else None,
                previous.kind if previous else None,
                previous.ref if previous else None,
                previous.pid if previous else None,
                previous.host if previous else None,
            ),
        )

    def status(self) -> GateStatus:
        """فقط‌خواندنی: هیچ پوشه، فایلِ قفل یا پایگاهی ساخته، migrate یا تعمیر نمی‌شود."""
        try:
            connection = _store.open_read(self.database_path)
        except sqlite3.Error as exc:
            raise GateUnavailable(
                f"The execution gate status cannot be read ({type(exc).__name__})."
            ) from None
        if connection is None:
            return GateStatus(initialized=False, generation=0, owner=None)
        try:
            row = connection.execute("SELECT * FROM gate_owner WHERE slot = 1").fetchone()
        except sqlite3.Error as exc:
            raise GateUnavailable(
                f"The execution gate status cannot be read ({type(exc).__name__})."
            ) from None
        finally:
            connection.close()
        return GateStatus(initialized=True, generation=row["generation"], owner=_owner(row))

    def _recorded_holder(self) -> GateOwner | None:
        """فقط برای پیامِ GateBusy — هر خطایی یعنی «نامعلوم»، نه شکستِ acquire."""
        try:
            return self.status().owner
        except GateError:
            return None


class GateLease:
    """مالکیتِ Gate: قفلِ سیستم‌عامل (handleِ زنده تا release) + رکوردِ پایگاه با توکنِ
    مخفی. thread-safe؛ همه‌ی عملیات‌ها پشتِ یک mutex اند."""

    def __init__(
        self, gate: ExecutionGate, lock: _oslock.OsLock, token: str, owner: GateOwner
    ) -> None:
        self._gate = gate
        self._lock = lock
        self._token = token
        self._owner = owner
        self._mutex = threading.Lock()
        self._released = False

    def __repr__(self) -> str:
        state = "released" if self._released else "held"
        return f"GateLease({self.kind} {self.ref!r}, generation={self.generation}, {state})"

    @property
    def kind(self) -> str:
        return self._owner.kind

    @property
    def ref(self) -> str:
        return self._owner.ref

    @property
    def generation(self) -> int:
        return self._owner.generation

    @property
    def acquired_at(self) -> str:
        return self._owner.acquired_at

    @property
    def released(self) -> bool:
        return self._released

    def __enter__(self) -> GateLease:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    # ── مالکیت ──────────────────────────────────────────────────────────────
    def _check_held(self) -> None:
        if self._released:
            raise GateLeaseReleased("This execution gate lease was already released.")
        if not self._lock.verify():
            raise GateLeaseLost("This lease no longer holds the execution gate lock file.")

    def _database_error(self, what: str, exc: BaseException) -> GateUnavailable:
        return GateUnavailable(
            f"The execution gate {what} could not be verified ({type(exc).__name__}); "
            "the lock is still held."
        )

    def heartbeat(self) -> None:
        """رکورد هنوز مالِ همین lease است و heartbeat ثبت شد؛ وگرنه GateLeaseLost.
        خطای پایگاه → GateUnavailable، قفل آزاد نمی‌شود."""
        with self._mutex:
            self._check_held()
            try:
                connection = _store.open_write(self._gate.database_path)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        cursor = connection.execute(
                            "UPDATE gate_owner SET heartbeat_at = ? "
                            "WHERE slot = 1 AND owner_token = ? AND generation = ?",
                            (_store.now(), self._token, self.generation),
                        )
                        if cursor.rowcount != 1:
                            raise GateLeaseLost(
                                "The execution gate record no longer belongs to this lease."
                            )
                        connection.execute("COMMIT")
                    except BaseException:
                        _rollback(connection)
                        raise
                finally:
                    connection.close()
            except GateLeaseLost:
                raise
            except GateUnavailable as exc:
                raise self._database_error("heartbeat", exc) from None
            except (sqlite3.Error, OSError) as exc:
                raise self._database_error("heartbeat", exc) from None

    def assert_held(self) -> None:
        """حصار پیش از کارِ اجرایی: قفلِ سیستم‌عامل و رکورد (در تراکنشِ IMMEDIATE) هر دو
        مالِ همین lease اند؛ وگرنه GateLeaseLost یا GateUnavailable."""
        with self._mutex:
            self._check_held()
            try:
                connection = _store.open_write(self._gate.database_path)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        row = connection.execute(
                            "SELECT owner_token, generation FROM gate_owner WHERE slot = 1"
                        ).fetchone()
                    finally:
                        _rollback(connection)
                finally:
                    connection.close()
            except GateUnavailable as exc:
                raise self._database_error("ownership", exc) from None
            except (sqlite3.Error, OSError) as exc:
                raise self._database_error("ownership", exc) from None
            if row["owner_token"] != self._token or row["generation"] != self.generation:
                raise GateLeaseLost("The execution gate record no longer belongs to this lease.")

    def release(self) -> ReleaseOutcome:
        """idempotent. رکورد (فقط اگر هنوز مالِ همین lease است) پاک می‌شود، سپس — در هر حال —
        قفلِ سیستم‌عاملِ همین handle آزاد می‌شود."""
        with self._mutex:
            if self._released:
                return ReleaseOutcome.ALREADY_RELEASED
            self._released = True
            cleared = False
            try:
                cleared = self._clear_record()
            except (GateError, sqlite3.Error, OSError):
                cleared = False  # رکورد می‌ماند؛ دارنده‌ی بعدیِ قفل آن را بازپس می‌گیرد
            finally:
                self._lock.release()
            return ReleaseOutcome.RELEASED if cleared else ReleaseOutcome.RECORD_NOT_CLEARED

    def _clear_record(self) -> bool:
        connection = _store.open_write(self._gate.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE gate_owner SET owner_kind = NULL, owner_ref = NULL, "
                    "owner_token = NULL, owner_pid = NULL, owner_host = NULL, "
                    "acquired_at = NULL, heartbeat_at = NULL "
                    "WHERE slot = 1 AND owner_token = ? AND generation = ?",
                    (self._token, self.generation),
                )
                cleared = cursor.rowcount == 1
                if cleared:
                    ExecutionGate._event(connection, _store.EVENT_RELEASED, self._owner)
                connection.execute("COMMIT")
            except BaseException:
                _rollback(connection)
                raise
        finally:
            connection.close()
        return cleared


__all__ = [
    "DATABASE_FILENAME",
    "DEFAULT_GATE_DIRECTORY",
    "KINDS",
    "LOCK_FILENAME",
    "ExecutionGate",
    "GateLease",
    "GateOwner",
    "GateStatus",
    "ReleaseOutcome",
]
