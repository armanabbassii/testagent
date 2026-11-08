"""
tests/unit/testcase_box/test_runner_isolation.py — Box و Batch هرگز هم‌زمان اجرا نمی‌شوند (M7.5)

Gate ِ واقعی در tmp؛ Batch با executor و توکنِ ساختگی، Box با موتورِ ساختگی؛ پروسه‌ی جدا فقط
از API ِ مستقلِ Gate (gate_child) استفاده می‌کند. هیچ Newman، Jira یا شبکه‌ای نیست.
"""

from __future__ import annotations

import threading

import pytest

from src.batch_testing import runner as batch_runner_module
from src.batch_testing.runner import REFUSAL_GATE_BUSY as BATCH_GATE_BUSY
from src.batch_testing.runner import RESULT_REFUSED, BatchRunner
from src.batch_testing.storage import BatchStore
from src.execution_gate import ExecutionGate
from src.testcase_box import models as m
from src.testcase_box import runner as runner_module
from src.testcase_box.storage import BoxStore
from tests.unit.batch_testing.test_runner import FakeExecutor, FakeTokenSource
from tests.unit.batch_testing.test_runner import Script as BatchScript
from tests.unit.batch_testing.test_runner import _batch
from tests.unit.execution_gate.support import Child, acquire_eventually, write_owner
from tests.unit.testcase_box.runner_fakes import (
    FakeEngine,
    FakeTokens,
    blocking,
    write_configuration,
)
from tests.unit.testcase_box.test_run_planning import item
from tests.unit.testcase_box.test_runner import Background, cases, gate_busy, plan, runner, units


@pytest.fixture(autouse=True)
def _no_real_defaults(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("No real gate, engine, executor or token provider in unit tests.")

    for name in ("_default_gate", "_default_engine", "_default_token_provider"):
        monkeypatch.setattr(runner_module, name, forbidden)
    monkeypatch.setattr(runner_module, "DEFAULT_ARTIFACT_ROOT", tmp_path / "forbidden-runs")
    monkeypatch.setattr(batch_runner_module, "_default_executor", forbidden)
    monkeypatch.setattr(batch_runner_module, "_default_token_provider", forbidden)
    monkeypatch.setattr(batch_runner_module, "_default_gate", forbidden)
    yield
    for lease in list(runner_module._UNRELEASED_GATES):
        runner_module._UNRELEASED_GATES.remove(lease)
        lease.release()
    alive = [
        t.name for t in threading.enumerate() if t.name.startswith(("box-runner-", "batch-runner-"))
    ]
    assert alive == [], alive


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


def _batch_runner(world) -> tuple[BatchRunner, str]:
    db = world["tmp"] / "batch.sqlite3"
    with BatchStore.open(db) as store:
        batch_id = _batch(store, world["tmp"] / "batch")
    script = BatchScript()
    batch = BatchRunner(
        db,
        executor_factory=lambda on_phase: FakeExecutor(script, on_phase),
        token_provider=FakeTokenSource(),
        gate_factory=lambda: ExecutionGate(world["gate_dir"]),
        heartbeat_interval=0.02,
        poll_interval=0.005,
    )
    return batch, batch_id


def test_box_cannot_start_while_batch_owns_the_gate(world):
    (case,) = cases(world, item())
    planned = plan(world, [case])
    engine, tokens = FakeEngine(), FakeTokens()
    with ExecutionGate(world["gate_dir"]).acquire("BATCH", "batch-7"):
        result = runner(world, engine, tokens).run(planned)
    assert (result.status, result.refusal) == ("REFUSED", "GATE_BUSY")
    assert "BATCH batch-7" in result.message
    assert engine.calls == [] and tokens.calls == 0 and world["store"].list_runs() == []


def test_batch_cannot_start_while_box_owns_the_gate(world):
    (case,) = cases(world, item())
    planned = plan(world, [case])
    started, release = threading.Event(), threading.Event()
    box = Background(lambda: runner(world, FakeEngine([blocking(started, release)])).run(planned))
    try:
        started.wait(20)
        batch, batch_id = _batch_runner(world)
        refused = batch.start(batch_id)
        assert (refused.status, refused.refusal) == (RESULT_REFUSED, BATCH_GATE_BUSY)
        assert "BOX box-plan-" in refused.error
    finally:
        release.set()
    assert box.join().status == "COMPLETED"
    batch, batch_id = _batch_runner(world)
    assert batch.start(batch_id).status == "completed"  # پس از پایانِ Box


def test_the_gate_is_held_for_the_whole_box_session(world):
    selected = cases(world, item(path="a"), item(path="b"))
    planned = plan(world, selected)
    held: list[bool] = []
    tokens = FakeTokens(on_fetch=lambda: held.append(gate_busy(world)))  # پیش از اولین unit
    engine = FakeEngine(on_call=lambda request: held.append(gate_busy(world)))
    result = runner(world, engine, tokens).run(planned)
    assert result.status == "COMPLETED" and held == [True, True, True]
    assert not gate_busy(world)


def test_a_second_box_runner_cannot_execute_concurrently(world):
    first, second = cases(world, item(path="a"), item(path="b"))
    plan_a, plan_b = plan(world, [first]), plan(world, [second])
    started, release = threading.Event(), threading.Event()
    job = Background(lambda: runner(world, FakeEngine([blocking(started, release)])).run(plan_a))
    try:
        started.wait(20)
        other_engine = FakeEngine()
        refused = runner(world, other_engine).run(plan_b)
        assert (refused.status, refused.refusal) == ("REFUSED", "GATE_BUSY")
        assert other_engine.calls == []
    finally:
        release.set()
    assert job.join().status == "COMPLETED"
    assert len(world["store"].list_runs()) == 1


def test_a_separate_process_cannot_take_the_gate_during_a_box_session(world):
    (case,) = cases(world, item())
    planned = plan(world, [case])
    started, release = threading.Event(), threading.Event()
    job = Background(lambda: runner(world, FakeEngine([blocking(started, release)])).run(planned))
    child = None
    try:
        started.wait(20)
        # heartbeatِ کهنه هم راهِ گرفتن نیست، تا وقتی مالکِ زنده قفلِ سیستم‌عامل را دارد
        write_owner(
            ExecutionGate(world["gate_dir"]),
            "UPDATE gate_owner SET heartbeat_at = '2000-01-01T00:00:00.000Z'",
        )
        child = Child(world["gate_dir"], "try")
        assert child.expect("BUSY") == "BUSY"
    finally:
        release.set()
        if child is not None:
            child.close()
    result = job.join()
    assert result.status == "COMPLETED"
    after = Child(world["gate_dir"], "try")
    try:
        assert after.expect("ACQUIRED").startswith("ACQUIRED")
    finally:
        after.close()


def test_a_box_process_holding_the_gate_blocks_the_runner(world):
    (case,) = cases(world, item())
    planned = plan(world, [case])
    holder = Child(world["gate_dir"], "hang")
    try:
        holder.expect("ACQUIRED")
        engine = FakeEngine()
        result = runner(world, engine).run(planned)
        assert (result.status, result.refusal) == ("REFUSED", "GATE_BUSY")
        assert engine.calls == []
    finally:
        holder.close()
    # سیستم‌عامل قفلِ پروسه‌ی مرده را «به‌زودی» آزاد می‌کند — انتظارِ محدود، سپس اجرای واقعی
    acquire_eventually(ExecutionGate(world["gate_dir"])).release()
    result = runner(world).run(planned)
    assert result.status == "COMPLETED"
    ((unit, _),) = units(world, result.run_id)
    assert unit.status == m.UNIT_PASS
    assert [r.status for r in world["store"].list_runs()] == [m.RUN_COMPLETED]
