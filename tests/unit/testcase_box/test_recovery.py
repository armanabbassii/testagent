"""
tests/unit/testcase_box/test_recovery.py — بازیابیِ امنِ runهای رهاشده (M7.6)

پایگاه، Gate و artifact در tmp؛ runهای رهاشده با همان APIهای مالکیت‌دار ساخته می‌شوند (نه SQL)؛
پروسه‌های جدا فقط پروسه‌های Python ِ همین تست‌اند. هیچ Newman/Node/HTTP/Jira/LLM.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src.execution_gate import ExecutionGate
from src.testcase_box import models as m
from src.testcase_box import recovery
from src.testcase_box import runner as runner_module
from src.testcase_box.db import RunOwnershipError
from src.testcase_box.recovery import inspect_runs, recover_run
from src.testcase_box.runner import BoxRunner
from src.testcase_box.storage import BoxStore
from tests.unit.execution_gate.support import Child, acquire_eventually
from tests.unit.testcase_box.runner_fakes import (
    FakeEngine,
    blocking,
    published,
    write_configuration,
)
from tests.unit.testcase_box.test_run_planning import event, item
from tests.unit.testcase_box.test_runner import Background, cases, gate_busy, plan, runner, units

REPO_ROOT = Path(__file__).resolve().parents[3]
CHILD = Path(__file__).with_name("box_run_child.py")
FAST = 0.05  # آستانه‌ی کهنگی در تست‌ها


@pytest.fixture(autouse=True)
def _no_real_defaults(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("No real gate, engine or token provider in unit tests.")

    for name in ("_default_gate", "_default_engine", "_default_token_provider"):
        monkeypatch.setattr(runner_module, name, forbidden)
    monkeypatch.setattr(recovery, "_default_gate", forbidden)
    monkeypatch.setattr(runner_module, "DEFAULT_ARTIFACT_ROOT", tmp_path / "forbidden-runs")
    yield
    for lease in list(runner_module._UNRELEASED_GATES):
        runner_module._UNRELEASED_GATES.remove(lease)
        lease.release()
    assert not (tmp_path / "forbidden-runs").exists()


@pytest.fixture
def world(tmp_path):
    environment, globals_path = write_configuration(tmp_path / "config")
    store = BoxStore.open(tmp_path / "box.sqlite3")
    yield {
        "db": tmp_path / "box.sqlite3",
        "store": store,
        "environment": environment,
        "globals": globals_path,
        "gate_dir": tmp_path / "gate",
        "runs": tmp_path / "runs",
        "tmp": tmp_path,
    }
    store.close()


def _gate(world):
    return lambda: ExecutionGate(world["gate_dir"])


def _recover(world, run_id, **kwargs):
    kwargs.setdefault("stale_after", FAST)
    return recover_run(world["db"], run_id, gate_factory=_gate(world), **kwargs)


def _abandon(world, payloads=None, *, finished=(), running=None, start=True, cancel=False):
    """runی که پروسه‌اش وسطِ کار مُرد: finished = [(position, unit_status, case_status, verification)]."""
    selected = cases(world, *(payloads or (item(path="a"), item(path="b"), item(path="c"))))
    planned = plan(world, selected)
    store = world["store"]
    run, lease = store.create_owned_run(BoxRunner._new_run(planned), pid=4242, host="old-host")
    if start:
        store.start_run(lease)
    by_number = {u.unit_number: u for u in store.run_units(run.id)}
    for position, unit_status, case_status, verification in finished:
        unit = by_number[position]
        store.start_unit(lease, unit.id)
        store.finish_unit(
            lease,
            unit.id,
            unit_status,
            case_status=case_status,
            unit_metadata={"classification": "DONE", "verification": verification},
            case_metadata={"verification": verification},
        )
    if running is not None:
        store.start_unit(lease, by_number[running].id)
    if cancel:
        store.request_run_cancel(run.id)
    time.sleep(FAST * 2)  # heartbeat کهنه می‌شود (پروسه‌ای heartbeat نمی‌زند)
    return run.id, lease


def _snapshot(world, run_id):
    store = world["store"]
    return (
        store.get_run(run_id),
        store.run_ownership(run_id),
        store.run_units(run_id),
        store.run_cases(run_id),
    )


# ── Gate و مالکیت ────────────────────────────────────────────────────────────


class TestGateAndOwnership:
    def test_a_live_gate_holder_blocks_recovery_even_with_a_stale_heartbeat(self, world):
        run_id, _ = _abandon(world, running=1)
        holder = Child(world["gate_dir"], "hang")  # پروسه‌ی زنده‌ی هنگ‌کرده با قفلِ سیستم‌عامل
        try:
            holder.expect("ACQUIRED")
            before = _snapshot(world, run_id)
            result = _recover(world, run_id)
            assert (result.status, result.reason) == ("BLOCKED", "GATE_BUSY")
            assert _snapshot(world, run_id) == before  # هیچ takeover ای
        finally:
            holder.close()

    @pytest.mark.parametrize("kind", ["BATCH", "BOX"])
    def test_an_active_batch_or_box_gate_owner_blocks_recovery(self, world, kind):
        run_id, _ = _abandon(world, running=1)
        with ExecutionGate(world["gate_dir"]).acquire(kind, "other-session"):
            result = _recover(world, run_id)
        assert (result.status, result.reason) == ("BLOCKED", "GATE_BUSY")
        assert world["store"].run_ownership(run_id).lease_generation == 1

    def test_an_unavailable_gate_blocks_recovery(self, world):
        run_id, _ = _abandon(world, running=1)
        world["gate_dir"].mkdir(parents=True)
        (world["gate_dir"] / "gate.sqlite3").write_bytes(b"corrupt" * 30)
        result = _recover(world, run_id)
        assert (result.status, result.reason) == ("BLOCKED", "GATE_UNAVAILABLE")
        assert world["store"].get_run(run_id).status == m.RUN_RUNNING

    def test_a_fresh_heartbeat_is_not_recoverable(self, world):
        run_id, _ = _abandon(world, running=1)
        result = _recover(world, run_id, stale_after=3600)
        assert (result.status, result.reason) == ("BLOCKED", "NOT_STALE")

    def test_a_changed_generation_is_refused(self, world):
        run_id, _ = _abandon(world, running=1)
        result = _recover(world, run_id, expected_generation=7)
        assert (result.status, result.reason) == ("BLOCKED", "OWNER_CHANGED")
        assert world["store"].get_run(run_id).status == m.RUN_RUNNING

    def test_a_stale_owner_is_taken_over_and_fenced_out(self, world):
        run_id, old = _abandon(world, running=1)
        result = _recover(world, run_id, expected_generation=1)
        assert (result.status, result.final_status) == ("RECOVERED", m.RUN_INTERRUPTED)
        with pytest.raises(RunOwnershipError):  # مالکِ قبلی هیچ چیزی نمی‌نویسد
            world["store"].heartbeat_run(old)
        ownership = world["store"].run_ownership(run_id)
        assert not ownership.leased and ownership.lease_generation == 2
        assert not gate_busy(world)  # Gate پس از بازیابی آزاد شد

    def test_two_simultaneous_recoveries_finalize_once(self, world):
        run_id, _ = _abandon(world, running=1)
        barrier, results = threading.Barrier(2), []

        def attempt():
            barrier.wait()
            results.append(_recover(world, run_id))

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        statuses = sorted(r.status for r in results)
        assert statuses.count("RECOVERED") == 1
        assert set(statuses) <= {"RECOVERED", "BLOCKED", "ALREADY_FINAL"}
        assert world["store"].run_ownership(run_id).lease_generation == 2

    def test_recovery_is_idempotent(self, world):
        run_id, _ = _abandon(
            world, finished=[(1, m.UNIT_PASS, m.CASE_RUN_PASS, "UNVERIFIED")], running=2
        )
        first = _recover(world, run_id)
        before = _snapshot(world, run_id)
        second = _recover(world, run_id)
        assert first.status == "RECOVERED"
        assert (second.status, second.final_status) == ("ALREADY_FINAL", m.RUN_INTERRUPTED)
        assert _snapshot(world, run_id) == before  # نه generation، نه فراداده، نه فرزندی تغییر کرد

    def test_a_failed_finalization_is_not_reported_as_recovered(self, world, monkeypatch):
        run_id, _ = _abandon(world, running=1)

        def broken(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(BoxStore, "finish_run", broken)
        failed = _recover(world, run_id)
        monkeypatch.undo()
        assert failed.status == "FAILED" and "stays recoverable" in failed.message
        assert world["store"].get_run(run_id).status == m.RUN_RUNNING  # نه نهایی، نه بی‌مالک
        assert world["store"].run_ownership(run_id).leased
        assert not gate_busy(world)  # بازیابی هیچ پروسه‌ای شروع نکرد
        time.sleep(FAST * 2)
        assert _recover(world, run_id).status == "RECOVERED"  # دوباره قابلِ بازیابی

    def test_inspection_is_read_only_and_explains_the_state(self, world):
        run_id, _ = _abandon(world, running=1)
        before = _snapshot(world, run_id)
        (candidate,) = inspect_runs(world["db"], gate_factory=_gate(world), stale_after=FAST)
        assert candidate.run_id == run_id and candidate.stale and candidate.recoverable
        assert candidate.open_units == 3 and candidate.lease_generation == 1
        (fresh,) = inspect_runs(world["db"], gate_factory=_gate(world), stale_after=3600)
        assert (fresh.recoverable, fresh.blocked_reason) == (False, "NOT_STALE")
        assert _snapshot(world, run_id) == before
        assert "token" not in repr(candidate).lower()


# ── طبقه‌بندیِ نتیجه ─────────────────────────────────────────────────────────


def _rows(world, run_id):
    return [(u.status, c.status, u.metadata.get("classification")) for u, c in units(world, run_id)]


class TestClassification:
    def test_stopped_before_any_unit_started(self, world):
        run_id, _ = _abandon(world, start=False)  # QUEUED و مالک‌دار
        result = _recover(world, run_id)
        assert result.final_status == m.RUN_INTERRUPTED
        assert {u.status for u, _ in units(world, run_id)} == {m.UNIT_NOT_RUN}

    def test_stopped_while_the_first_unit_was_running(self, world):
        run_id, _ = _abandon(world, running=1)
        result = _recover(world, run_id)
        rows = _rows(world, run_id)
        assert rows[0] == (m.UNIT_ERROR, m.CASE_RUN_ERROR, "OUTCOME_UNKNOWN")  # هرگز PASS/FAIL
        assert [r[:2] for r in rows[1:]] == [(m.UNIT_NOT_RUN, m.CASE_RUN_NOT_RUN)] * 2
        run = world["store"].get_run(run_id)
        assert run.status == m.RUN_INTERRUPTED
        assert run.metadata["stop_reason"] == "RECOVERED_AFTER_APPLICATION_TERMINATION"
        assert run.metadata["recovery"]["unknown_outcome_units"] == [1]
        assert run.metadata["recovery"]["previous_owner_host"] == "old-host"
        assert result.reason == "RECOVERED_AFTER_APPLICATION_TERMINATION"

    def test_completed_pass_and_fail_outcomes_are_preserved(self, world):
        run_id, _ = _abandon(
            world,
            finished=[
                (1, m.UNIT_PASS, m.CASE_RUN_PASS, "UNVERIFIED"),
                (2, m.UNIT_FAIL, m.CASE_RUN_FAIL, "FAILED"),
            ],
        )  # بینِ unitها مُرد
        before = {u.id: (u.status, u.metadata) for u in world["store"].run_units(run_id)[:2]}
        _recover(world, run_id)
        after = {u.id: (u.status, u.metadata) for u in world["store"].run_units(run_id)[:2]}
        assert after == before
        assert _rows(world, run_id)[2][:2] == (m.UNIT_NOT_RUN, m.CASE_RUN_NOT_RUN)
        summary = world["store"].get_run(run_id).metadata["summary"]
        assert (summary["unverified"], summary["failed"], summary["not_run"]) == (1, 1, 1)
        assert summary["verified_pass"] == 0

    def test_all_units_done_but_finalization_lost_becomes_completed(self, world):
        run_id, _ = _abandon(
            world,
            finished=[
                (1, m.UNIT_PASS, m.CASE_RUN_PASS, "VERIFIED"),
                (2, m.UNIT_PASS, m.CASE_RUN_PASS, "UNVERIFIED"),
                (3, m.UNIT_ERROR, m.CASE_RUN_ERROR, "NOT_VERIFIED"),
            ],
        )
        result = _recover(world, run_id)
        assert (result.final_status, result.reason) == (m.RUN_COMPLETED, "RECOVERED_FINALIZATION")
        run = world["store"].get_run(run_id)
        assert (
            "stop_reason" not in run.metadata
            and run.metadata["recovery"]["reason"] == "RECOVERED_FINALIZATION"
        )

    def test_a_pending_cancellation_is_preserved_as_cancelled(self, world):
        run_id, _ = _abandon(
            world,
            finished=[(1, m.UNIT_PASS, m.CASE_RUN_PASS, "UNVERIFIED")],
            running=2,
            cancel=True,
        )
        result = _recover(world, run_id)
        assert result.final_status == m.RUN_CANCELLED
        rows = _rows(world, run_id)
        assert rows[0][0] == m.UNIT_PASS
        assert rows[1] == (m.UNIT_ERROR, m.CASE_RUN_ERROR, "OUTCOME_UNKNOWN")
        assert rows[2][:2] == (m.UNIT_CANCELLED, m.CASE_RUN_CANCELLED)
        assert world["store"].get_run(run_id).metadata["stop_reason"] == "USER_CANCELLED"

    def test_blocked_units_stay_not_executable(self, world):
        run_id, _ = _abandon(world, payloads=(item(path="a"), item(event())), start=False)
        _recover(world, run_id)
        (_, first), (blocked_unit, blocked_case) = units(world, run_id)
        assert (
            blocked_unit.status == m.UNIT_NOT_RUN
            and blocked_case.status == m.CASE_RUN_NOT_EXECUTABLE
        )


# ── با runner ِ واقعی ─────────────────────────────────────────────────────────


class TestWithTheRunner:
    def test_recovery_after_a_failed_finish_run(self, world, monkeypatch):
        selected = cases(world, item(path="a"), item(path="b"))
        planned = plan(world, selected)
        real, calls = BoxStore.finish_run, []

        def fail_once(self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("disk I/O error")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(BoxStore, "finish_run", fail_once)
        result = runner(world).run(planned)
        assert result.status == "INTERRUPTED" and result.stop_reason == "STORAGE_FAILED"
        assert world["store"].get_run(result.run_id).status == m.RUN_RUNNING
        time.sleep(FAST * 2)
        recovered = _recover(world, result.run_id)
        assert (recovered.status, recovered.final_status) == ("RECOVERED", m.RUN_COMPLETED)
        assert [u.status for u, _ in units(world, result.run_id)] == [m.UNIT_PASS, m.UNIT_PASS]

    def test_unconfirmed_termination_blocks_recovery_until_the_gate_is_released(self, world):
        (case,) = cases(world, item())
        planned = plan(world, [case])
        started, release = threading.Event(), threading.Event()
        engine = FakeEngine([blocking(started, release, confirmed=False)])
        job = Background(lambda: runner(world, engine).run(planned))
        started.wait(20)
        world["store"].request_run_cancel(world["store"].active_run().id)
        result = job.join()
        assert result.status == "TERMINATION_UNCONFIRMED"
        time.sleep(FAST * 2)
        blocked = _recover(world, result.run_id)
        assert (blocked.status, blocked.reason) == ("BLOCKED", "GATE_BUSY")  # Gate قرنطینه است
        quarantined = runner_module._UNRELEASED_GATES.pop()
        quarantined.release()  # همان چیزی که با خروجِ پروسه‌ی برنامه رخ می‌دهد
        recovered = _recover(world, result.run_id)
        assert recovered.status == "RECOVERED" and recovered.final_status == m.RUN_CANCELLED
        ((unit, case_row),) = units(world, result.run_id)
        assert (unit.status, unit.metadata["classification"]) == (m.UNIT_ERROR, "OUTCOME_UNKNOWN")

    def test_recovery_after_a_lost_lease(self, world):
        (case,) = cases(world, item())
        planned = plan(world, [case])
        started, release = threading.Event(), threading.Event()
        job = Background(
            lambda: runner(world, FakeEngine([blocking(started, release)])).run(planned)
        )
        started.wait(20)
        run_id = world["store"].active_run().id
        world["store"].take_over_run(
            run_id, expected_generation=1, stale_before="9999-12-31T23:59:59.999Z", pid=7, host="x"
        )
        result = job.join()
        assert result.stop_reason == "LEASE_LOST"
        time.sleep(FAST * 2)
        recovered = _recover(world, run_id)
        assert recovered.status == "RECOVERED" and recovered.final_status == m.RUN_INTERRUPTED

    def test_a_runner_finalized_stop_is_already_final(self, world):
        (case,) = cases(world, item())
        result = runner(
            world,
            FakeEngine([lambda r, c: published(r, status="ERROR", error_kind="NODE_NOT_FOUND")]),
        ).run(plan(world, [case]))
        assert world["store"].get_run(result.run_id).status in m.RUN_FINAL
        assert _recover(world, result.run_id).status == "ALREADY_FINAL"


def test_recovery_after_the_application_process_is_killed(world):
    """پروسه‌ی برنامه وسطِ unit کشته می‌شود؛ پس از آن (و فقط پس از آن) بازیابی ممکن است."""
    (case,) = cases(world, item())
    world["gate_dir"].mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            sys.executable,
            str(CHILD),
            str(REPO_ROOT),
            str(world["db"]),
            str(world["gate_dir"]),
            str(world["runs"]),
            str(world["environment"]),
            str(world["globals"]),
            str(case.id),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
    )
    try:
        pid = int(proc.stdout.readline().split()[1])
        assert proc.stdout.readline().strip() == "ENGINE", proc.stderr.read()[-2000:]
        run_id = world["store"].active_run().id
        # برنامه هنوز زنده است: heartbeat تازه (NOT_STALE) یا — حتی اگر کهنه دیده شود — Gate دستِ
        # همان پروسه است (GATE_BUSY). هر دو یعنی هیچ takeover ای.
        for stale_after in (FAST, 0.0):
            blocked = _recover(world, run_id, stale_after=stale_after)
            assert (blocked.status, blocked.reason in ("NOT_STALE", "GATE_BUSY")) == (
                "BLOCKED",
                True,
            )
        assert world["store"].run_ownership(run_id).lease_generation == 1
        from tests.unit.test_execution.test_newman_supervision import _terminate

        _terminate(pid)  # crash ِ پروسه‌ی برنامه
        acquire_eventually(ExecutionGate(world["gate_dir"])).release()
        time.sleep(FAST * 2)
        recovered = _recover(world, run_id)
        assert (recovered.status, recovered.final_status) == ("RECOVERED", m.RUN_INTERRUPTED)
        ((unit, case_row),) = units(world, run_id)
        assert (unit.status, case_row.status) == (m.UNIT_ERROR, m.CASE_RUN_ERROR)
        assert unit.metadata["classification"] == "OUTCOME_UNKNOWN"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)
        for stream in (proc.stdout, proc.stderr):
            stream.close()
