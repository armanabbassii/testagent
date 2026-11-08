"""
tests/unit/execution_gate/test_processes.py — پروسه‌های واقعاً جدا (نه thread)

هر انتظار سقفِ زمان دارد و fixtureِ spawn در پایان همه‌ی فرزندان را می‌کشد و جمع می‌کند.
پس از کشتنِ دارنده، گرفتن با تلاشِ دوباره‌ی محدود انجام می‌شود چون Windows زمانِ آزاد شدنِ
قفلِ پروسه‌ی مرده را تضمین نمی‌کند — ولی هرگز پیش از مرگِ واقعیِ دارنده موفق نمی‌شود.
"""

from __future__ import annotations

import os
import signal
import time

import pytest

from src.execution_gate import ExecutionGate, GateBusy
from tests.unit.execution_gate.support import (
    acquire_eventually,
    events,
    raw_owner,
    write_owner,
)


def _assert_busy(gate: ExecutionGate) -> None:
    with pytest.raises(GateBusy):
        gate.acquire("BATCH", "probe")


class TestCrossProcess:
    def test_b_cannot_acquire_while_a_holds_and_can_after_a_releases(self, gate, spawn):
        a = spawn("hold")
        assert a.expect("ACQUIRED") == "ACQUIRED 1"
        assert spawn("try").expect("BUSY") == "BUSY"  # پروسه‌ی B
        _assert_busy(gate)  # پروسه‌ی تست هم
        a.send("release")
        assert a.expect("RELEASED") == "RELEASED RELEASED"
        assert a.wait() == 0
        assert spawn("try").expect("ACQUIRED") == "ACQUIRED 2"

    def test_the_test_process_holding_blocks_a_child(self, gate, spawn):
        with gate.acquire("BATCH", "parent"):
            assert spawn("try").expect("BUSY") == "BUSY"
        assert spawn("try").expect("ACQUIRED") == "ACQUIRED 2"

    def test_a_killed_holder_releases_only_when_the_os_frees_its_lock(self, gate, spawn):
        a = spawn("hold")
        a.expect("ACQUIRED")
        _assert_busy(gate)
        a.kill()  # بدونِ release — مثلِ crash
        lease = acquire_eventually(gate)
        try:
            reclaimed = [row for row in events(gate) if row["event"] == "RECLAIMED"]
            assert len(reclaimed) == 1 and reclaimed[0]["previous_owner_pid"] == a.pid
            assert lease.generation == 2
        finally:
            lease.release()

    def test_a_hard_exit_after_acquisition_is_recovered(self, gate, spawn):
        a = spawn("exit")
        a.expect("ACQUIRED")
        a.wait()
        lease = acquire_eventually(gate)
        try:
            assert events(gate)[-2]["event"] == "RECLAIMED"
        finally:
            lease.release()

    def test_a_crash_between_os_lock_and_record_leaves_a_free_record(self, gate, spawn):
        gate.acquire("BOX", "first").release()
        a = spawn("oslock")
        assert a.expect("LOCKED") == "LOCKED"
        _assert_busy(gate)
        assert raw_owner(gate)["owner_token"] is None  # رکورد هیچ‌وقت ادعا نشد
        a.kill()
        lease = acquire_eventually(gate)
        try:
            assert lease.generation == 2
            assert "RECLAIMED" not in [row["event"] for row in events(gate)]
        finally:
            lease.release()

    def test_a_crash_after_clearing_the_record_but_before_unlocking(self, gate, spawn):
        a = spawn("cleared")
        a.expect("ACQUIRED")
        a.wait()
        lease = acquire_eventually(gate)
        try:
            assert [row["event"] for row in events(gate)] == ["ACQUIRED", "RELEASED", "ACQUIRED"]
        finally:
            lease.release()

    def test_a_grandchild_does_not_inherit_the_lock(self, gate, spawn):
        a = spawn("spawn")
        a.expect("ACQUIRED")
        grandchild = int(a.expect("SPAWNED").split()[1])
        try:
            a.kill()  # فرزندِ فرزند زنده می‌ماند
            lease = acquire_eventually(gate)
            lease.release()
        finally:
            try:
                os.kill(grandchild, signal.SIGTERM)
            except OSError:
                pass


class TestHungHolder:
    def test_a_live_holder_with_a_stale_heartbeat_keeps_blocking(self, gate, spawn):
        a = spawn("hang")
        a.expect("ACQUIRED")
        first = raw_owner(gate)["heartbeat_at"]
        time.sleep(1.0)
        assert raw_owner(gate)["heartbeat_at"] == first  # هیچ heartbeatی نمی‌زند
        # heartbeat را بسیار کهنه می‌کنیم: هیچ آستانه‌ی زمانی‌ای نباید اجازه‌ی گرفتن بدهد
        write_owner(gate, "UPDATE gate_owner SET heartbeat_at = '2000-01-01T00:00:00.000Z'")
        assert gate.status().owner.heartbeat_age_seconds() > 10 * 365 * 24 * 3600
        for _ in range(3):
            assert spawn("try").expect("BUSY") == "BUSY"
            _assert_busy(gate)
        assert a.proc.poll() is None  # دارنده هنوز زنده است
        a.kill()
        lease = acquire_eventually(gate)
        try:
            reclaimed = events(gate)[-2]
            assert (reclaimed["event"], reclaimed["previous_owner_pid"]) == ("RECLAIMED", a.pid)
        finally:
            lease.release()
