"""
tests/unit/execution_gate/test_gate.py — ExecutionGate / GateLease در یک پروسه

قفلِ واقعیِ سیستم‌عامل و پایگاهِ واقعی در tmp؛ فقط برای خطاهای پایگاه اتصال جایگزین می‌شود.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from src.execution_gate import (
    DEFAULT_GATE_DIRECTORY,
    ExecutionGate,
    GateBusy,
    GateLeaseLost,
    GateLeaseReleased,
    GateStatus,
    GateUnavailable,
    ReleaseOutcome,
    _store,
)
from src.execution_gate._oslock import OsLock
from tests.unit.execution_gate.support import events, raw_owner, write_owner


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _busy(gate_dir: Path) -> bool:
    """گیرنده‌ی مستقل (نمونه‌ی تازه و handleِ تازه) Gate را مشغول می‌بیند؟"""
    try:
        lease = ExecutionGate(gate_dir).acquire("BATCH", "probe")
    except GateBusy:
        return True
    lease.release()
    return False


class _FailingConnection:
    """اتصالِ واقعی که روی دستورِ مشخصی OperationalError می‌دهد."""

    def __init__(self, connection: sqlite3.Connection, fragment: str) -> None:
        self._connection, self._fragment = connection, fragment

    def execute(self, sql: str, *args):
        if self._fragment in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._connection.execute(sql, *args)

    def close(self) -> None:
        self._connection.close()


def _fail_on(monkeypatch, fragment: str) -> None:
    real = _store.open_write
    monkeypatch.setattr(_store, "open_write", lambda path: _FailingConnection(real(path), fragment))


class TestBasics:
    def test_first_acquisition_succeeds_and_records_the_owner(self, gate):
        lease = gate.acquire("BOX", "run-1")
        try:
            assert (lease.kind, lease.ref, lease.generation) == ("BOX", "run-1", 1)
            owner = gate.status().owner
            assert (owner.kind, owner.ref, owner.generation) == ("BOX", "run-1", 1)
            assert owner.pid > 0 and owner.host
            assert owner.acquired_at.endswith("Z") and owner.heartbeat_age_seconds() >= 0
        finally:
            lease.release()

    def test_a_second_acquisition_while_held_is_busy(self, gate, gate_dir):
        lease = gate.acquire("BOX", "run-1")
        try:
            with pytest.raises(GateBusy) as caught:
                gate.acquire("BATCH", "b-1")
            assert caught.value.holder.ref == "run-1"
            assert "BOX run-1 holds the execution gate" in str(caught.value)
        finally:
            lease.release()

    def test_heartbeat_and_assert_held_succeed_for_the_owner(self, gate):
        with gate.acquire("BOX", "run-1") as lease:
            before = raw_owner(gate)["heartbeat_at"]
            lease.heartbeat()
            lease.assert_held()
            assert raw_owner(gate)["heartbeat_at"] >= before

    def test_release_permits_a_later_acquisition(self, gate):
        first = gate.acquire("BOX", "run-1")
        assert first.release() is ReleaseOutcome.RELEASED
        second = gate.acquire("BATCH", "b-1")
        assert second.generation == 2
        second.release()
        assert gate.status() == GateStatus(initialized=True, generation=2, owner=None)
        assert [row["event"] for row in events(gate)] == [
            "ACQUIRED",
            "RELEASED",
            "ACQUIRED",
            "RELEASED",
        ]

    def test_context_manager_releases_on_exit_and_on_exception(self, gate, gate_dir):
        with gate.acquire("BOX", "run-1"):
            assert _busy(gate_dir)
        assert not _busy(gate_dir)
        with pytest.raises(KeyError):
            with gate.acquire("BOX", "run-2"):
                raise KeyError("boom")
        assert not _busy(gate_dir)

    def test_releasing_twice_is_safe(self, gate):
        lease = gate.acquire("BOX", "run-1")
        assert lease.release() is ReleaseOutcome.RELEASED
        assert lease.release() is ReleaseOutcome.ALREADY_RELEASED
        assert lease.released

    def test_a_released_lease_cannot_heartbeat_or_assert(self, gate):
        lease = gate.acquire("BOX", "run-1")
        lease.release()
        with pytest.raises(GateLeaseReleased):
            lease.heartbeat()
        with pytest.raises(GateLeaseReleased):
            lease.assert_held()

    def test_heartbeat_works_from_another_thread(self, gate):
        errors: list[BaseException] = []
        with gate.acquire("BOX", "run-1") as lease:

            def beat() -> None:
                try:
                    for _ in range(5):
                        lease.heartbeat()
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            thread = threading.Thread(target=beat)
            thread.start()
            thread.join(timeout=30)
        assert not errors

    @pytest.mark.parametrize(
        ("kind", "ref"),
        [
            ("JOB", "x"),
            ("box", "x"),
            ("BOX", ""),
            ("BOX", "   "),
            ("BOX", "a\nb"),
            ("BOX", "x" * 201),
        ],
    )
    def test_invalid_kind_or_ref_is_rejected_before_any_file_is_touched(
        self, gate, gate_dir, kind, ref
    ):
        with pytest.raises(ValueError):
            gate.acquire(kind, ref)
        assert not gate_dir.exists()

    def test_constructing_a_gate_creates_nothing(self, gate_dir):
        ExecutionGate(gate_dir)
        assert not gate_dir.exists()

    def test_the_default_directory_is_the_agreed_location(self):
        assert DEFAULT_GATE_DIRECTORY.parts[-2:] == ("data", "execution_gate")


class TestTokenNeverExposed:
    def test_status_repr_and_errors_never_contain_the_token(self, gate):
        lease = gate.acquire("BOX", "run-1")
        try:
            token = lease._token
            assert raw_owner(gate)["owner_token"] == token
            with pytest.raises(GateBusy) as caught:
                gate.acquire("BATCH", "b-1")
            texts = [repr(gate.status()), repr(lease), str(caught.value), repr(caught.value.holder)]
            assert all(token not in text for text in texts)
            assert "token" not in GateStatus.__dataclass_fields__
            assert all(token not in str(tuple(row)) for row in events(gate))
        finally:
            lease.release()


class TestStatusIsReadOnly:
    def test_a_missing_directory_is_not_created(self, gate, gate_dir):
        assert gate.status() == GateStatus(initialized=False, generation=0, owner=None)
        assert not gate_dir.exists()

    def test_an_empty_directory_stays_empty(self, gate, gate_dir):
        gate_dir.mkdir()
        assert not gate.status().initialized
        assert list(gate_dir.iterdir()) == []

    def test_status_does_not_change_the_database(self, gate):
        gate.acquire("BOX", "run-1").release()
        before = (_sha(gate.database_path), gate.database_path.stat().st_mtime_ns)
        for _ in range(3):
            gate.status()
        assert (_sha(gate.database_path), gate.database_path.stat().st_mtime_ns) == before
        assert sorted(p.name for p in gate.directory.iterdir()) == ["gate.lock", "gate.sqlite3"]

    def test_status_of_a_corrupt_database_fails_closed(self, gate, gate_dir):
        gate_dir.mkdir()
        gate.database_path.write_bytes(b"not a database at all" * 10)
        with pytest.raises(GateUnavailable):
            gate.status()


class TestIncompatibleDatabases:
    def _assert_untouched_and_unlocked(self, gate, gate_dir, digest):
        with pytest.raises(GateUnavailable):
            gate.acquire("BOX", "run-1")
        assert _sha(gate.database_path) == digest
        lock = OsLock(gate.lock_path)  # قفل پس از شکست آزاد شده است
        assert lock.try_acquire()
        lock.release()

    def test_a_corrupt_file_is_never_overwritten(self, gate, gate_dir):
        gate_dir.mkdir()
        gate.database_path.write_bytes(b"\x00garbage" * 64)
        self._assert_untouched_and_unlocked(gate, gate_dir, _sha(gate.database_path))

    def test_a_foreign_sqlite_database_is_never_touched(self, gate, gate_dir):
        gate_dir.mkdir()
        with closing(sqlite3.connect(gate.database_path)) as c:
            c.execute("CREATE TABLE other (x)")
            c.commit()
        self._assert_untouched_and_unlocked(gate, gate_dir, _sha(gate.database_path))

    def test_an_unsupported_schema_version_is_refused(self, gate, gate_dir):
        gate.acquire("BOX", "run-1").release()
        write_owner(gate, f"PRAGMA user_version = {_store.SCHEMA_VERSION + 1}")
        self._assert_untouched_and_unlocked(gate, gate_dir, _sha(gate.database_path))
        with pytest.raises(GateUnavailable, match="schema version"):
            gate.status()

    def test_an_incomplete_schema_is_refused(self, gate, gate_dir):
        gate.acquire("BOX", "run-1").release()
        write_owner(gate, "DROP TRIGGER tr_gate_events_no_delete")
        self._assert_untouched_and_unlocked(gate, gate_dir, _sha(gate.database_path))

    def test_a_fresh_database_has_identity_schema_and_one_owner_row(self, gate):
        gate.acquire("BOX", "run-1").release()
        with closing(sqlite3.connect(gate.database_path)) as c:
            assert c.execute("PRAGMA application_id").fetchone()[0] == _store.APPLICATION_ID
            assert c.execute("PRAGMA user_version").fetchone()[0] == _store.SCHEMA_VERSION
            assert c.execute("SELECT COUNT(*) FROM gate_owner").fetchone()[0] == 1

    def test_history_and_owner_row_are_protected_by_triggers(self, gate):
        gate.acquire("BOX", "run-1").release()
        for sql in (
            "DELETE FROM gate_events",
            "UPDATE gate_events SET owner_ref = 'x'",
            "DELETE FROM gate_owner",
            "UPDATE gate_owner SET generation = 0",
            "INSERT INTO gate_owner (slot) VALUES (2)",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                write_owner(gate, sql)


class TestDatabaseFailures:
    def test_a_failure_while_opening_the_database_releases_the_os_lock(
        self, gate, gate_dir, monkeypatch
    ):
        def broken(path):
            raise GateUnavailable("The execution gate database cannot be opened; nothing.")

        monkeypatch.setattr(_store, "open_write", broken)
        with pytest.raises(GateUnavailable):
            gate.acquire("BOX", "run-1")
        monkeypatch.undo()
        assert not _busy(gate_dir)

    def test_a_failed_owner_update_changes_nothing_and_releases_the_lock(
        self, gate, gate_dir, monkeypatch
    ):
        gate.acquire("BOX", "run-1").release()
        before = raw_owner(gate)
        _fail_on(monkeypatch, "UPDATE gate_owner SET owner_kind = ?")
        with pytest.raises(GateUnavailable, match="cannot be written"):
            gate.acquire("BOX", "run-2")
        monkeypatch.undo()
        assert tuple(raw_owner(gate)) == tuple(before)
        assert [row["event"] for row in events(gate)] == ["ACQUIRED", "RELEASED"]
        assert not _busy(gate_dir)

    def test_a_heartbeat_failure_keeps_the_lock(self, gate, gate_dir, monkeypatch):
        lease = gate.acquire("BOX", "run-1")
        try:
            _fail_on(monkeypatch, "SET heartbeat_at")
            with pytest.raises(GateUnavailable, match="still held"):
                lease.heartbeat()
            monkeypatch.undo()
            assert _busy(gate_dir)  # خطای پایگاه قفل را آزاد نکرد
            lease.heartbeat()
        finally:
            lease.release()

    def test_an_assert_held_failure_keeps_the_lock(self, gate, gate_dir, monkeypatch):
        lease = gate.acquire("BOX", "run-1")
        try:
            _fail_on(monkeypatch, "SELECT owner_token")
            with pytest.raises(GateUnavailable, match="still held"):
                lease.assert_held()
            monkeypatch.undo()
            assert _busy(gate_dir)
        finally:
            lease.release()

    def test_a_release_failure_frees_the_lock_and_leaves_the_record_for_reconciliation(
        self, gate, gate_dir, monkeypatch
    ):
        lease = gate.acquire("BOX", "run-1")
        _fail_on(monkeypatch, "UPDATE gate_owner SET owner_kind = NULL")
        assert lease.release() is ReleaseOutcome.RECORD_NOT_CLEARED
        monkeypatch.undo()
        assert gate.status().owner.ref == "run-1"  # رکورد ماند
        successor = gate.acquire("BATCH", "b-1")  # قفل آزاد بود؛ رکورد بازپس گرفته شد
        try:
            reclaimed = [row for row in events(gate) if row["event"] == "RECLAIMED"]
            assert len(reclaimed) == 1
            assert reclaimed[0]["previous_owner_ref"] == "run-1"
            assert reclaimed[0]["owner_ref"] == "b-1"
        finally:
            successor.release()


class TestOwnership:
    def test_a_tampered_record_makes_the_lease_lost_without_unlocking(self, gate, gate_dir):
        lease = gate.acquire("BOX", "run-1")
        try:
            write_owner(gate, "UPDATE gate_owner SET owner_token = ?", ("f" * 32,))
            with pytest.raises(GateLeaseLost):
                lease.heartbeat()
            with pytest.raises(GateLeaseLost):
                lease.assert_held()
            assert _busy(gate_dir)
        finally:
            # رکورد مالِ این lease نیست → پاک نمی‌شود؛ فقط قفلِ خودش آزاد می‌شود
            assert lease.release() is ReleaseOutcome.RECORD_NOT_CLEARED
        assert raw_owner(gate)["owner_token"] == "f" * 32

    def test_a_stale_owner_record_never_unblocks_while_the_os_lock_is_held(self, gate, gate_dir):
        gate.acquire("BOX", "run-1").release()
        write_owner(
            gate,
            "UPDATE gate_owner SET owner_kind='BOX', owner_ref='ghost', owner_token=?, "
            "owner_pid=1, owner_host='h', acquired_at='2000-01-01T00:00:00.000Z', "
            "heartbeat_at='2000-01-01T00:00:00.000Z'",
            ("a" * 32,),
        )
        holder = OsLock(gate.lock_path)  # کسی قفلِ سیستم‌عامل را دارد
        assert holder.try_acquire()
        try:
            with pytest.raises(GateBusy) as caught:
                gate.acquire("BATCH", "b-1")
            assert caught.value.holder.ref == "ghost"
        finally:
            holder.release()
        lease = gate.acquire("BATCH", "b-1")  # حالا قفل آزاد است → رکورد بازپس گرفته می‌شود
        try:
            assert lease.generation == 2  # دست‌کاریِ رکورد generation را بالا نبرده بود
            reclaimed = events(gate)[-2]
            assert (reclaimed["event"], reclaimed["previous_owner_ref"]) == ("RECLAIMED", "ghost")
        finally:
            lease.release()

    def test_two_gate_instances_in_one_process_cannot_both_acquire(self, gate_dir):
        first, second = ExecutionGate(gate_dir), ExecutionGate(gate_dir)
        lease = first.acquire("BOX", "run-1")
        try:
            with pytest.raises(GateBusy):
                second.acquire("BATCH", "b-1")
        finally:
            lease.release()
        second.acquire("BATCH", "b-1").release()

    def test_concurrent_threads_never_hold_the_gate_together(self, gate_dir):
        holders, overlaps, wins = [0], [0], [0]
        guard = threading.Lock()
        start = threading.Barrier(8)

        def worker() -> None:
            start.wait()
            for _ in range(20):
                try:
                    lease = ExecutionGate(gate_dir).acquire("BOX", "t")
                except GateBusy:
                    continue
                with guard:
                    holders[0] += 1
                    overlaps[0] = max(overlaps[0], holders[0])
                    wins[0] += 1
                time.sleep(0.002)  # در حالِ نگه داشتن — همپوشانی اگر ممکن بود این‌جا دیده می‌شد
                with guard:
                    holders[0] -= 1
                lease.release()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert overlaps[0] == 1 and wins[0] >= 1


def test_a_network_path_is_refused_on_windows(monkeypatch):
    import os

    if os.name != "nt":
        pytest.skip("UNC paths exist only on Windows")
    with pytest.raises(GateUnavailable, match="local disk"):
        ExecutionGate(r"\\server\share\gate").acquire("BOX", "run-1")
