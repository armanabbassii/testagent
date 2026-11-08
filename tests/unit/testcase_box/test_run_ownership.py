"""
tests/unit/testcase_box/test_run_ownership.py — مالکیتِ اجرای run در BoxStore (v4)

پایگاه‌های موقت؛ اتصال‌های جدا (thread) و پروسه‌های جدا برای هم‌زمانی. هیچ اجرا، Newman یا
Execution Gate ای این‌جا نیست — فقط لایه‌ی ذخیره‌سازی.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from src.testcase_box import models as m
from src.testcase_box.db import (
    ActiveRunError,
    ImmutableRecordError,
    IntegrityViolationError,
    RunOwnershipError,
    now,
)
from src.testcase_box.models import (
    InvalidTransitionError,
    NewCase,
    NewRun,
    NewRunCase,
    NewRunUnit,
    RunLease,
)
from src.testcase_box.storage import BoxStore

REPO_ROOT = Path(__file__).resolve().parents[3]
FUTURE = "9999-12-31T23:59:59.999Z"
PAST = "2000-01-01T00:00:00.000Z"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "box.sqlite3"


@pytest.fixture
def store(path: Path):
    with BoxStore.open(path) as opened:
        yield opened


def _cases(store: BoxStore, count: int = 2) -> list[m.BoxCase]:
    return [store.create_case(NewCase(title=f"Case {n}", payload={})) for n in range(count)]


def _new_run(cases: list[m.BoxCase], *, blocked: int | None = None) -> NewRun:
    """یک case در هر unit (D2)؛ blocked = اندیسِ caseی که NOT_EXECUTABLE ساخته می‌شود."""
    units = []
    for number, case in enumerate(cases, 1):
        status = m.CASE_RUN_NOT_EXECUTABLE if blocked == number - 1 else m.CASE_RUN_QUEUED
        run_case = NewRunCase(case.id, case.current_version_id, 1, status=status)
        units.append(NewRunUnit(number, (run_case,)))
    return NewRun(units=tuple(units))


def _owned(store: BoxStore, **kwargs) -> tuple[m.BoxRun, RunLease]:
    return store.create_owned_run(_new_run(_cases(store), **kwargs), pid=4242, host="host-a")


def _stranger(lease: RunLease) -> RunLease:
    return RunLease(lease.run_id, lease.generation, "0" * 32)


def _state(store: BoxStore) -> dict[str, list[tuple]]:
    db = store._db
    return {
        table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("box_runs", "box_run_units", "box_run_cases")
    }


# ── ساختن و گرفتن ────────────────────────────────────────────────────────────


class TestCreateAndClaim:
    def test_a_run_is_created_already_owned(self, store):
        run, lease = _owned(store)
        assert run.status == m.RUN_QUEUED and lease.generation == 1
        ownership = store.run_ownership(run.id)
        assert ownership.leased and (ownership.owner_pid, ownership.owner_host) == (4242, "host-a")
        assert ownership.heartbeat_at and ownership.requested_action == m.RUN_ACTION_NONE
        assert store.owns_run(lease) and store.active_run().id == run.id

    def test_the_token_never_appears_in_records_or_reprs(self, store):
        run, lease = _owned(store)
        assert lease.token not in repr(lease)
        assert lease.token not in repr(run) and lease.token not in repr(store.run_ownership(run.id))
        assert not hasattr(run, "owner_token")
        with pytest.raises(RunOwnershipError) as caught:
            store.heartbeat_run(_stranger(lease))
        assert lease.token not in str(caught.value)

    def test_a_second_active_run_is_rejected_without_partial_rows(self, store):
        _owned(store)
        before = _state(store)
        with pytest.raises(ActiveRunError):
            store.create_owned_run(_new_run(_cases(store, 1)), pid=1, host="h")
        assert _state(store) == before

    def test_an_unowned_queued_run_can_be_claimed_once(self, store):
        run = store.create_run(_new_run(_cases(store)))
        lease = store.claim_run(run.id, pid=7, host="h")
        assert lease.generation == 1 and store.owns_run(lease)
        with pytest.raises(RunOwnershipError):
            store.claim_run(run.id, pid=8, host="h")

    def test_a_claim_is_rejected_while_another_run_is_active(self, store):
        _owned(store)
        other = store.create_run(_new_run(_cases(store, 1)))
        with pytest.raises(ActiveRunError):
            store.claim_run(other.id, pid=7, host="h")
        assert not store.run_ownership(other.id).leased

    def test_only_queued_runs_can_be_claimed(self, store):
        run = store.create_run(_new_run(_cases(store, 1)))
        store.set_run_case_status(store.run_cases(run.id)[0].id, m.CASE_RUN_CANCELLED)
        store.set_unit_status(store.run_units(run.id)[0].id, m.UNIT_CANCELLED)
        store.set_run_status(run.id, m.RUN_CANCELLED)
        with pytest.raises(InvalidTransitionError):
            store.claim_run(run.id, pid=7, host="h")

    @pytest.mark.parametrize(
        ("pid", "host"), [(0, "h"), (-1, "h"), (True, "h"), (1, ""), (1, "  ")]
    )
    def test_owner_identity_is_validated(self, store, pid, host):
        with pytest.raises(ValueError):
            store.create_owned_run(_new_run(_cases(store, 1)), pid=pid, host=host)


# ── بررسیِ مالکیت در هر تغییر ───────────────────────────────────────────────


class TestOwnerChecks:
    def test_the_owner_can_heartbeat(self, store):
        run, lease = _owned(store)
        first = store.run_ownership(run.id).heartbeat_at
        stamp = store.heartbeat_run(lease)
        assert stamp >= first and store.run_ownership(run.id).heartbeat_at == stamp

    def test_a_non_owner_can_change_nothing(self, store):
        run, lease = _owned(store)
        stranger = _stranger(lease)
        unit = store.run_units(run.id)[0]
        case = store.run_cases(run.id)[0]
        before = _state(store)
        attempts = [
            lambda: store.heartbeat_run(stranger),
            lambda: store.start_run(stranger),
            lambda: store.start_unit(stranger, unit.id),
            lambda: store.finish_case(stranger, case.id, m.CASE_RUN_PASS),
            lambda: store.finish_unit(stranger, unit.id, m.UNIT_CANCELLED, case_status="CANCELLED"),
            lambda: store.finish_run(
                stranger,
                m.RUN_CANCELLED,
                queued_unit_status=m.UNIT_CANCELLED,
                open_case_status=m.CASE_RUN_CANCELLED,
            ),
            lambda: store.release_run_lease(stranger),
        ]
        for attempt in attempts:
            with pytest.raises(RunOwnershipError):
                attempt()
        assert _state(store) == before
        assert not store.owns_run(stranger) and store.owns_run(lease)

    def test_a_wrong_generation_is_rejected_even_with_the_right_token(self, store):
        _, lease = _owned(store)
        with pytest.raises(RunOwnershipError):
            store.heartbeat_run(RunLease(lease.run_id, lease.generation + 1, lease.token))

    def test_legacy_status_methods_refuse_a_leased_run(self, store):
        run, _ = _owned(store)
        unit, case = store.run_units(run.id)[0], store.run_cases(run.id)[0]
        before = _state(store)
        with pytest.raises(RunOwnershipError):
            store.set_run_status(run.id, m.RUN_RUNNING)
        with pytest.raises(RunOwnershipError):
            store.set_unit_status(unit.id, m.UNIT_RUNNING)
        with pytest.raises(RunOwnershipError):
            store.set_run_case_status(case.id, m.CASE_RUN_PASS)
        assert _state(store) == before

    def test_units_of_another_run_cannot_be_touched_with_this_lease(self, store):
        other = store.create_run(_new_run(_cases(store, 1)))
        run, lease = store.create_owned_run(_new_run(_cases(store, 1)), pid=1, host="h")
        store.start_run(lease)
        with pytest.raises(IntegrityViolationError):
            store.start_unit(lease, store.run_units(other.id)[0].id)
        with pytest.raises(IntegrityViolationError):
            store.finish_case(lease, store.run_cases(other.id)[0].id, m.CASE_RUN_PASS)


# ── چرخه‌ی کامل و پایانِ اتمی ────────────────────────────────────────────────


class TestLifecycle:
    def test_a_full_run_with_a_blocked_case(self, store):
        run, lease = _owned(store, blocked=1)
        executable, blocked = store.run_units(run.id)
        store.start_run(lease)
        store.start_unit(lease, executable.id)
        store.finish_unit(
            lease,
            executable.id,
            m.UNIT_PASS,
            case_status=m.CASE_RUN_PASS,
            unit_metadata={"newman_run_id": "r1"},
            case_metadata={"http_status": [200]},
        )
        store.finish_unit(lease, blocked.id, m.UNIT_NOT_RUN, unit_metadata={"reason": "BLOCKED"})
        final = store.finish_run(lease, m.RUN_COMPLETED, metadata={"summary": {"pass": 1}})
        assert final.status == m.RUN_COMPLETED and final.finished_at
        ownership = store.run_ownership(run.id)
        assert not ownership.leased
        assert (ownership.owner_pid, ownership.owner_host, ownership.heartbeat_at) == (None,) * 3
        cases = store.run_cases(run.id)
        assert [c.status for c in cases] == [m.CASE_RUN_PASS, m.CASE_RUN_NOT_EXECUTABLE]
        assert cases[0].result_metadata == {"http_status": [200]}
        assert store.active_run() is None

    def test_finishing_closes_open_children_with_the_supplied_statuses(self, store):
        cases = _cases(store, 3)
        run, lease = store.create_owned_run(_new_run(cases), pid=1, host="h")
        first, second, third = store.run_units(run.id)
        store.start_run(lease)
        store.start_unit(lease, first.id)
        store.finish_unit(lease, first.id, m.UNIT_PASS, case_status=m.CASE_RUN_PASS)
        store.start_unit(lease, second.id)
        store.finish_run(
            lease,
            m.RUN_INTERRUPTED,
            metadata={"stop_reason": "CONFIGURATION_CHANGED"},
            running_unit_status=m.UNIT_ERROR,
            queued_unit_status=m.UNIT_NOT_RUN,
            open_case_status=m.CASE_RUN_NOT_RUN,
            child_metadata={"reason": "CONFIGURATION_CHANGED"},
        )
        units = store.run_units(run.id)
        assert [u.status for u in units] == [m.UNIT_PASS, m.UNIT_ERROR, m.UNIT_NOT_RUN]
        assert units[0].metadata == {} and units[2].metadata == {"reason": "CONFIGURATION_CHANGED"}
        statuses = [c.status for c in store.run_cases(run.id)]
        assert statuses == [m.CASE_RUN_PASS, m.CASE_RUN_NOT_RUN, m.CASE_RUN_NOT_RUN]
        assert store.get_run(run.id).metadata == {"stop_reason": "CONFIGURATION_CHANGED"}
        assert not store.run_ownership(run.id).leased

    def test_a_queued_owned_run_can_be_cancelled_directly(self, store):
        run, lease = _owned(store)
        store.finish_run(
            lease,
            m.RUN_CANCELLED,
            queued_unit_status=m.UNIT_CANCELLED,
            open_case_status=m.CASE_RUN_CANCELLED,
        )
        assert store.get_run(run.id).status == m.RUN_CANCELLED

    def test_finishing_or_releasing_twice_is_refused(self, store):
        _, lease = _owned(store)
        store.finish_run(
            lease,
            m.RUN_CANCELLED,
            queued_unit_status=m.UNIT_CANCELLED,
            open_case_status="CANCELLED",
        )
        with pytest.raises(RunOwnershipError):
            store.finish_run(lease, m.RUN_CANCELLED)
        with pytest.raises(RunOwnershipError):
            store.release_run_lease(lease)

    def test_release_keeps_the_status_and_clears_the_owner(self, store):
        run, lease = _owned(store)
        assert store.release_run_lease(lease).status == m.RUN_QUEUED
        assert not store.run_ownership(run.id).leased
        with pytest.raises(RunOwnershipError):
            store.release_run_lease(lease)

    def test_a_cancellation_request_is_recorded_but_changes_no_status(self, store):
        run, _ = _owned(store)
        ownership = store.request_run_cancel(run.id)
        assert ownership.requested_action == m.RUN_ACTION_CANCEL and ownership.leased
        assert store.get_run(run.id).status == m.RUN_QUEUED

    def test_a_final_run_cannot_receive_a_cancellation_request(self, store):
        run, lease = _owned(store)
        store.finish_run(
            lease,
            m.RUN_CANCELLED,
            queued_unit_status=m.UNIT_CANCELLED,
            open_case_status="CANCELLED",
        )
        with pytest.raises(ImmutableRecordError):
            store.request_run_cancel(run.id)

    def test_finishing_requires_a_final_status(self, store):
        _, lease = _owned(store)
        with pytest.raises(InvalidTransitionError):
            store.finish_run(lease, m.RUN_RUNNING)


class TestAtomicCompletion:
    def test_missing_child_statuses_leave_everything_unchanged(self, store):
        run, lease = _owned(store)
        store.start_run(lease)
        store.start_unit(lease, store.run_units(run.id)[0].id)
        before = _state(store)
        with pytest.raises(InvalidTransitionError):
            store.finish_run(lease, m.RUN_COMPLETED, open_case_status=m.CASE_RUN_ERROR)
        assert _state(store) == before and store.owns_run(lease)

    def test_an_invalid_child_transition_rolls_back_children_already_closed(self, store):
        run, lease = _owned(store)
        store.start_run(lease)
        before = _state(store)
        with pytest.raises(InvalidTransitionError):  # QUEUED → ERROR برای unit مجاز نیست
            store.finish_run(
                lease,
                m.RUN_INTERRUPTED,
                queued_unit_status=m.UNIT_ERROR,
                open_case_status=m.CASE_RUN_NOT_RUN,
            )
        assert _state(store) == before

    def test_a_failure_at_the_final_run_update_rolls_back_everything(self, store, monkeypatch):
        run, lease = _owned(store)
        store.start_run(lease)
        store.start_unit(lease, store.run_units(run.id)[0].id)
        before = _state(store)
        real = store._write

        def failing(sql, params=()):
            if sql.startswith("UPDATE box_runs SET status") and "owner_token = NULL" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return real(sql, params)

        monkeypatch.setattr(store, "_write", failing)
        with pytest.raises(sqlite3.OperationalError):
            store.finish_run(
                lease,
                m.RUN_INTERRUPTED,
                running_unit_status=m.UNIT_ERROR,
                queued_unit_status=m.UNIT_NOT_RUN,
                open_case_status=m.CASE_RUN_ERROR,
            )
        monkeypatch.undo()
        assert _state(store) == before  # نه فرزندِ بسته، نه وضعیتِ پایانی، نه lease ِ پاک‌شده
        assert store.owns_run(lease) and not store._db.in_transaction

    def test_a_failure_while_closing_children_rolls_back_the_first_ones(self, store, monkeypatch):
        cases = _cases(store, 3)
        run, lease = store.create_owned_run(_new_run(cases), pid=1, host="h")
        store.start_run(lease)
        before = _state(store)
        real, closed = store._write, []

        def failing(sql, params=()):
            if sql.startswith("UPDATE box_run_units SET status"):
                closed.append(params)
                if len(closed) == 2:
                    raise sqlite3.OperationalError("disk I/O error")
            return real(sql, params)

        monkeypatch.setattr(store, "_write", failing)
        with pytest.raises(sqlite3.OperationalError):
            store.finish_run(
                lease,
                m.RUN_CANCELLED,
                queued_unit_status=m.UNIT_CANCELLED,
                open_case_status=m.CASE_RUN_CANCELLED,
            )
        monkeypatch.undo()
        assert _state(store) == before

    def test_the_database_forbids_a_final_run_with_an_owner(self, store):
        run, lease = _owned(store)
        for unit in store.run_units(run.id):
            store.finish_unit(lease, unit.id, m.UNIT_CANCELLED, case_status=m.CASE_RUN_CANCELLED)
        with pytest.raises(sqlite3.IntegrityError, match="cannot keep its execution lease"):
            store._db.execute("UPDATE box_runs SET status = 'CANCELLED' WHERE id = ?", (run.id,))

    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE box_runs SET owner_pid = NULL",  # فراداده‌ی ناقص
            "UPDATE box_runs SET heartbeat_at = NULL",
            "UPDATE box_runs SET lease_generation = 0",  # کاهشِ نسل
            "UPDATE box_runs SET owner_token = '" + "a" * 32 + "'",  # مالکِ تازه بی‌نسلِ تازه
            "UPDATE box_runs SET requested_action = 'PAUSE'",
            "UPDATE box_runs SET owner_token = 'short'",
        ],
    )
    def test_the_database_enforces_owner_consistency(self, store, sql):
        _owned(store)
        with pytest.raises(sqlite3.IntegrityError):
            store._db.execute(sql)

    def test_the_database_allows_only_one_lease_and_one_running_run(self, store):
        _owned(store)
        other = store.create_run(_new_run(_cases(store, 1)))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            store._db.execute(
                "UPDATE box_runs SET owner_token = ?, owner_pid = 1, owner_host = 'h', "
                "heartbeat_at = 'x', lease_generation = 1 WHERE id = ?",
                ("b" * 32, other.id),
            )


# ── هم‌زمانی و fencing ───────────────────────────────────────────────────────


class TestConcurrency:
    def test_concurrent_claims_on_separate_connections_create_one_active_run(self, path, store):
        cases = _cases(store, 4)
        barrier, results = threading.Barrier(4), []

        def worker(case: m.BoxCase) -> None:
            with BoxStore.open(path) as own:
                barrier.wait()
                try:
                    own.create_owned_run(_new_run([case]), pid=1, host="h")
                    results.append("ok")
                except ActiveRunError:
                    results.append("active")

        threads = [threading.Thread(target=worker, args=(case,)) for case in cases]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert sorted(results) == ["active", "active", "active", "ok"]
        assert store._db.execute("SELECT COUNT(*) FROM box_runs").fetchone()[0] == 1

    def test_concurrent_claims_from_separate_processes_create_one_active_run(self, path, store):
        cases = _cases(store, 3)
        script = (
            "import sys; sys.path.insert(0, sys.argv[1])\n"
            "from src.testcase_box.storage import BoxStore\n"
            "from src.testcase_box.db import ActiveRunError\n"
            "from src.testcase_box.models import NewRun, NewRunUnit, NewRunCase\n"
            "case, version = int(sys.argv[3]), int(sys.argv[4])\n"
            "run = NewRun(units=(NewRunUnit(1, (NewRunCase(case, version, 1),)),))\n"
            "with BoxStore.open(sys.argv[2]) as store:\n"
            "    try:\n"
            "        store.create_owned_run(run, pid=1, host='h'); print('ok')\n"
            "    except ActiveRunError:\n"
            "        print('active')\n"
        )
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(REPO_ROOT),
                    str(path),
                    str(c.id),
                    str(c.current_version_id),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for c in cases
        ]
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err[-2000:]
            outputs.append(out.strip())
        assert sorted(outputs) == ["active", "active", "ok"]
        assert store._db.execute("SELECT COUNT(*) FROM box_runs").fetchone()[0] == 1

    def test_a_takeover_fences_out_the_previous_owner(self, store):
        run, old = _owned(store)
        assert [o.run_id for o in store.stale_runs(FUTURE)] == [run.id]
        new = store.take_over_run(
            run.id, expected_generation=old.generation, stale_before=FUTURE, pid=99, host="host-b"
        )
        assert new.generation == old.generation + 1
        before = _state(store)
        for attempt in (
            lambda: store.heartbeat_run(old),
            lambda: store.start_run(old),
            lambda: store.release_run_lease(old),
            lambda: store.finish_run(old, m.RUN_CANCELLED),
        ):
            with pytest.raises(RunOwnershipError):
                attempt()
        assert _state(store) == before
        store.heartbeat_run(new)
        ownership = store.run_ownership(run.id)
        assert (ownership.owner_pid, ownership.owner_host) == (99, "host-b")

    def test_a_takeover_is_refused_when_not_stale_or_with_the_wrong_generation(self, store):
        run, lease = _owned(store)
        before = _state(store)
        with pytest.raises(RunOwnershipError):  # heartbeat تازه‌تر از آستانه
            store.take_over_run(run.id, expected_generation=1, stale_before=PAST, pid=2, host="h")
        with pytest.raises(RunOwnershipError):  # نسلِ دیگر
            store.take_over_run(run.id, expected_generation=7, stale_before=FUTURE, pid=2, host="h")
        assert _state(store) == before and store.owns_run(lease)
        assert store.stale_runs(PAST) == []

    def test_a_takeover_of_an_unowned_run_is_refused(self, store):
        run = store.create_run(_new_run(_cases(store, 1)))
        with pytest.raises(RunOwnershipError):
            store.take_over_run(run.id, expected_generation=0, stale_before=FUTURE, pid=2, host="h")

    def test_a_failed_creation_leaves_no_partial_owner_metadata(self, store, monkeypatch):
        cases = _cases(store, 2)
        before = _state(store)
        real = store._write

        def failing(sql, params=()):
            if sql.startswith("INSERT INTO box_run_cases"):
                raise sqlite3.OperationalError("disk I/O error")
            return real(sql, params)

        monkeypatch.setattr(store, "_write", failing)
        with pytest.raises(sqlite3.OperationalError):
            store.create_owned_run(_new_run(cases), pid=1, host="h")
        monkeypatch.undo()
        assert _state(store) == before and store.active_run() is None

    def test_heartbeats_order_with_timestamps(self, store):
        _, lease = _owned(store)
        stamps = [store.heartbeat_run(lease) for _ in range(3)]
        assert stamps == sorted(stamps) and stamps[-1] <= now()
