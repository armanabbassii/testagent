"""
تست‌های خلاصه‌ی داشبورد (Milestone 4.6) — شمارش، پیشرفت، تاریخچه‌ی تلاش‌ها.

روی BatchStoreِ واقعی با پایگاهِ موقت؛ هیچ runner یا سرویسِ خارجی‌ای اجرا نمی‌شود.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.batch_testing import models as m
from src.batch_testing.storage import BatchStore
from src.batch_testing.summary import (
    HEALTH_LIVE,
    HEALTH_NONE,
    HEALTH_STALE,
    STATUS_NOT_STARTED,
    BatchCounts,
    count,
    current_row,
    issue_rows,
    runner_health,
)
from tests.unit.batch_testing.test_storage import _new_batch
from ui.batch_app import issue_table

ALL = tuple(f"DEMO-{index}" for index in range(1, 10))


@pytest.fixture
def store(tmp_path: Path):
    with BatchStore.open(tmp_path / "batch.sqlite3") as opened:
        yield opened


def _finished(store: BatchStore, batch_id: str, key: str, status: str, **fields):
    execution = store.create_execution(batch_id, key, trigger=fields.pop("trigger", "initial"))
    store.start_execution(execution.id)
    if status == m.EXEC_ERROR:
        fields.setdefault("error_kind", "NEWMAN_ERROR")
    if status == m.EXEC_NOT_EXECUTED:
        fields.setdefault("review_status", m.REVIEW_NEEDS_REVIEW)
    return store.finish_execution(execution.id, status, **fields)


def _rows(store: BatchStore, batch_id: str):
    return issue_rows(store.batch_issues(batch_id), store.executions(batch_id))


class TestAggregation:
    def test_every_execution_status_is_counted_where_it_belongs(self, store):
        batch_id = store.create_batch(_new_batch(ALL)).id
        for key, status in zip(
            ALL,
            (
                m.EXEC_PASS,
                m.EXEC_FAIL,
                m.EXEC_ERROR,
                m.EXEC_NOT_EXECUTED,
                m.EXEC_CANCELLED,
                m.EXEC_INTERRUPTED,
            ),
        ):
            _finished(store, batch_id, key, status)
        running = store.create_execution(batch_id, "DEMO-7", trigger="initial")
        store.start_execution(running.id)
        store.create_execution(batch_id, "DEMO-8", trigger="initial")  # QUEUED
        # DEMO-9 هنوز هیچ تلاشی ندارد
        rows = _rows(store, batch_id)
        counts = count(rows)
        assert counts == BatchCounts(
            total=9,
            completed=5,
            pending=2,
            running=1,
            passed=1,
            failed=1,
            errors=1,
            not_executed=1,
            cancelled=1,
            interrupted=1,
            needs_review=1,
        )
        assert counts.progress == pytest.approx(5 / 9)
        assert current_row(rows).issue_key == "DEMO-7"
        assert rows[-1].status == STATUS_NOT_STARTED

    def test_progress_counts_every_terminal_outcome_not_only_pass(self, store):
        batch_id = store.create_batch(_new_batch(("DEMO-1", "DEMO-2", "DEMO-3"))).id
        _finished(store, batch_id, "DEMO-1", m.EXEC_FAIL)
        _finished(store, batch_id, "DEMO-2", m.EXEC_ERROR)
        _finished(store, batch_id, "DEMO-3", m.EXEC_CANCELLED)
        counts = count(_rows(store, batch_id))
        assert (counts.completed, counts.passed, counts.progress) == (3, 0, 1.0)

    def test_a_created_batch_is_all_pending(self, store):
        batch_id = store.create_batch(_new_batch()).id
        counts = count(_rows(store, batch_id))
        assert (counts.total, counts.pending, counts.completed, counts.progress) == (2, 2, 0, 0.0)
        assert count([]).progress == 0.0


class TestReviewIndependence:
    def test_pass_with_needs_review_stays_a_pass(self, store):
        batch_id = store.create_batch(_new_batch(("DEMO-1",))).id
        _finished(
            store,
            batch_id,
            "DEMO-1",
            m.EXEC_PASS,
            review_status=m.REVIEW_NEEDS_REVIEW,
            review_reasons=[{"kind": "CASE_BLOCKED", "code": "MISSING_HOST"}],
            final_review_status="needs_review",
        )
        rows = _rows(store, batch_id)
        counts = count(rows)
        assert (counts.passed, counts.errors, counts.needs_review) == (1, 0, 1)
        assert rows[0].status == m.EXEC_PASS
        table = issue_table(rows).iloc[0]
        assert "PASS" in table["Execution"] and "ERROR" not in table["Execution"]
        assert table["Review"] == "NEEDS_REVIEW"
        assert table["Final review"] == "needs_review"


class TestAttemptHistory:
    def test_the_latest_attempt_counts_and_earlier_attempts_are_kept(self, store):
        batch_id = store.create_batch(_new_batch(("DEMO-1",))).id
        first = _finished(store, batch_id, "DEMO-1", m.EXEC_INTERRUPTED)
        second = _finished(store, batch_id, "DEMO-1", m.EXEC_FAIL, trigger="resume")
        third = _finished(store, batch_id, "DEMO-1", m.EXEC_PASS, trigger="retry")
        (row,) = _rows(store, batch_id)
        assert [attempt.attempt for attempt in row.attempts] == [1, 2, 3]
        assert row.latest.id == third.id and row.status == m.EXEC_PASS
        assert third.previous_execution_id == second.id
        assert second.previous_execution_id == first.id
        counts = count([row])
        assert (counts.passed, counts.interrupted, counts.failed) == (1, 0, 0)
        assert issue_table([row]).iloc[0]["Attempt"] == 3

    def test_an_interrupted_issue_is_neither_completed_nor_pending(self, store):
        batch_id = store.create_batch(_new_batch(("DEMO-1", "DEMO-2"))).id
        _finished(store, batch_id, "DEMO-1", m.EXEC_INTERRUPTED)
        _finished(store, batch_id, "DEMO-2", m.EXEC_PASS)
        counts = count(_rows(store, batch_id))
        assert (counts.completed, counts.interrupted, counts.pending) == (1, 1, 0)
        assert counts.progress == 0.5


class TestRunnerHealth:
    def test_health_follows_the_lease_and_its_heartbeat(self, store):
        batch = store.create_batch(_new_batch())
        assert runner_health(batch) == HEALTH_NONE
        owned = store.acquire_lease(batch.id, "runner", pid=1, host="pc")
        assert runner_health(owned) == HEALTH_LIVE
        later = lambda: datetime.now(UTC) + timedelta(seconds=120)  # noqa: E731
        assert runner_health(owned, clock=later) == HEALTH_STALE
