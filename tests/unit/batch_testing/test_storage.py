"""
تست‌های ذخیره‌سازیِ SQLiteِ Batch (Milestone 4.1) — همه روی پایگاه‌های موقت.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from src.batch_testing import models as m
from src.batch_testing import storage
from src.batch_testing.models import IssueSnapshot, NewBatch
from src.batch_testing.storage import (
    LATEST_VERSION,
    BatchStore,
    DuplicateAttemptError,
    ImmutableRecordError,
    LeaseError,
    NotFoundError,
    SchemaVersionError,
)


def _new_batch(keys=("DEMO-1", "DEMO-2"), **overrides) -> NewBatch:
    values = {
        "board_id": 12340,
        "board_name": "Podium board",
        "board_type": "scrum",
        "column_name": "QC Test",
        "criteria": {"column": "QC TEST", "status": "Manual Test", "label": "backend"},
        "discovery": {
            "eligible": list(keys),
            "excluded": [{"key": "DEMO-9", "reasons": ["label 'backend' is missing"]}],
        },
        "issues": tuple(
            IssueSnapshot(
                issue_key=key,
                summary=f"Summary {key}",
                issue_type="Story",
                status_name="Manual Test",
                labels=("backend",),
                eligibility={"reasons": [], "in_active_sprint": True},
            )
            for key in keys
        ),
        "environment_path": "postmanEnv/dev.postman_environment.json",
        "environment_sha256": "a" * 64,
        "globals_path": "postmanEnv/workspace.postman_globals.json",
        "globals_sha256": "b" * 64,
        "artifact_root": "artifacts/batch_testing",
    }
    values.update(overrides)
    return NewBatch(**values)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "batch_testing" / "batch.sqlite3"


@pytest.fixture
def store(db_path: Path):
    with BatchStore.open(db_path) as opened:
        yield opened


def _running_execution(store: BatchStore, batch_id: str, key: str = "DEMO-1"):
    execution = store.create_execution(batch_id, key, trigger=m.TRIGGER_INITIAL)
    return store.start_execution(execution.id)


class TestInitialisation:
    def test_a_fresh_database_is_created_with_wal_and_foreign_keys(self, db_path):
        with BatchStore.open(db_path) as store:
            assert db_path.is_file()
            assert store.schema_version == LATEST_VERSION
            connection = store._db
            assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert {"schema_migrations", "batches", "batch_issues", "executions"} <= tables

    def test_reopening_keeps_data_and_does_not_reapply_migrations(self, db_path):
        with BatchStore.open(db_path) as first:
            batch = first.create_batch(_new_batch())
            applied = first._db.execute(
                "SELECT version, applied_at FROM schema_migrations"
            ).fetchall()
        with BatchStore.open(db_path) as second:
            assert second.get_batch(batch.id).board_name == "Podium board"
            again = second._db.execute(
                "SELECT version, applied_at FROM schema_migrations"
            ).fetchall()
        assert [tuple(row) for row in again] == [tuple(row) for row in applied]

    def test_migrate_is_idempotent(self, store):
        assert storage.migrate(store._db) == LATEST_VERSION
        assert storage.migrate(store._db) == LATEST_VERSION
        count = store._db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert count == len(storage.MIGRATIONS)

    def test_an_older_schema_is_migrated_forward_with_its_data(self, db_path):
        db_path.parent.mkdir(parents=True)
        connection = storage.connect(db_path)
        assert storage.migrate(connection, target=1) == 1
        old = BatchStore(connection, db_path)
        batch = old.create_batch(_new_batch())
        execution = old.create_execution(batch.id, "DEMO-1", trigger=m.TRIGGER_INITIAL)
        # v1 has no history protection yet
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'tr_executions_no_delete'"
        ).fetchone()
        connection.close()

        with BatchStore.open(db_path) as upgraded:
            assert upgraded.schema_version == 2
            assert upgraded.get_execution(execution.id).attempt == 1
            with pytest.raises(ImmutableRecordError):
                upgraded._write("DELETE FROM executions WHERE id = ?", (execution.id,))

    def test_a_newer_database_is_refused(self, db_path):
        with BatchStore.open(db_path) as store:
            store._db.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (99, 'future', 'x')"
            )
        with pytest.raises(SchemaVersionError, match="newer than this code"):
            BatchStore.open(db_path)

    def test_a_failed_migration_rolls_back_completely(self, db_path, monkeypatch):
        broken = [
            *storage.MIGRATIONS,
            (3, "broken", ["CREATE TABLE extra (id INTEGER)", "NOT SQL"]),
        ]
        monkeypatch.setattr(storage, "MIGRATIONS", broken)
        monkeypatch.setattr(storage, "LATEST_VERSION", 3)
        with pytest.raises(sqlite3.OperationalError):
            BatchStore.open(db_path)
        connection = storage.connect(db_path)
        assert storage.schema_version(connection) == 2
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'extra'").fetchone()
        connection.close()

    def test_two_processes_opening_a_fresh_database_apply_migrations_once(self, db_path):
        db_path.parent.mkdir(parents=True)
        errors: list[BaseException] = []

        def open_it():
            try:
                BatchStore.open(db_path).close()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=open_it) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        with BatchStore.open(db_path) as store:
            versions = [
                row[0] for row in store._db.execute("SELECT version FROM schema_migrations")
            ]
        assert versions == [1, 2]


class TestBatches:
    def test_a_batch_stores_its_frozen_snapshot_and_reasons(self, store):
        batch = store.create_batch(_new_batch())
        assert batch.status == m.BATCH_CREATED
        assert batch.requested_action == m.ACTION_NONE
        assert batch.discovery["excluded"][0]["reasons"] == ["label 'backend' is missing"]
        issues = store.batch_issues(batch.id)
        assert [(i.position, i.issue_key) for i in issues] == [(0, "DEMO-1"), (1, "DEMO-2")]
        assert issues[0].labels == ("backend",)
        assert issues[0].eligibility == {"reasons": [], "in_active_sprint": True}

    def test_the_eligible_list_cannot_change_after_creation(self, store):
        batch = store.create_batch(_new_batch())
        with pytest.raises(ImmutableRecordError, match="frozen"):
            store._write("UPDATE batch_issues SET summary = 'x' WHERE batch_id = ?", (batch.id,))
        with pytest.raises(ImmutableRecordError, match="frozen"):
            store._write("DELETE FROM batch_issues WHERE batch_id = ?", (batch.id,))
        with pytest.raises(ImmutableRecordError):
            store._write("DELETE FROM batches WHERE id = ?", (batch.id,))

    def test_a_duplicate_issue_rolls_back_the_whole_batch(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store.create_batch(_new_batch(keys=("DEMO-1", "DEMO-1")))
        assert store.list_batches() == []

    def test_an_unknown_board_type_is_rejected(self, store):
        with pytest.raises(ValueError):
            store.create_batch(_new_batch(board_type="simple"))

    def test_missing_records_are_reported(self, store):
        with pytest.raises(NotFoundError):
            store.get_batch("nope")
        with pytest.raises(NotFoundError):
            store.get_execution("nope")


class TestBatchTransitions:
    @pytest.mark.parametrize(
        "path",
        [
            [m.BATCH_RUNNING, m.BATCH_PAUSED, m.BATCH_RUNNING, m.BATCH_COMPLETED],
            [m.BATCH_RUNNING, m.BATCH_CANCELLED],
            [m.BATCH_RUNNING, m.BATCH_INTERRUPTED, m.BATCH_RUNNING, m.BATCH_COMPLETED],
            [m.BATCH_CANCELLED],
            [m.BATCH_RUNNING, m.BATCH_PAUSED, m.BATCH_CANCELLED],
        ],
    )
    def test_valid_paths(self, store, path):
        batch = store.create_batch(_new_batch())
        for status in path:
            batch = store.transition_batch(batch.id, status)
        assert batch.status == path[-1]
        if path[-1] in m.BATCH_FINAL:
            assert batch.finished_at

    @pytest.mark.parametrize(
        ("path", "invalid"),
        [
            ([], m.BATCH_PAUSED),
            ([], m.BATCH_COMPLETED),
            ([m.BATCH_RUNNING, m.BATCH_COMPLETED], m.BATCH_RUNNING),
            ([m.BATCH_CANCELLED], m.BATCH_RUNNING),
            ([m.BATCH_RUNNING, m.BATCH_PAUSED], m.BATCH_COMPLETED),
        ],
    )
    def test_invalid_transitions_change_nothing(self, store, path, invalid):
        batch = store.create_batch(_new_batch())
        for status in path:
            batch = store.transition_batch(batch.id, status)
        with pytest.raises(m.InvalidTransitionError):
            store.transition_batch(batch.id, invalid)
        assert store.get_batch(batch.id).status == batch.status

    def test_expected_status_guards_against_races(self, store):
        batch = store.create_batch(_new_batch())
        with pytest.raises(m.InvalidTransitionError, match="not PAUSED"):
            store.transition_batch(batch.id, m.BATCH_RUNNING, expected=m.BATCH_PAUSED)

    def test_pause_and_cancel_requests(self, store):
        batch = store.create_batch(_new_batch())
        with pytest.raises(m.InvalidTransitionError):
            store.request_action(batch.id, m.ACTION_PAUSE)  # not running yet
        store.transition_batch(batch.id, m.BATCH_RUNNING)
        assert store.request_action(batch.id, m.ACTION_PAUSE).requested_action == m.ACTION_PAUSE
        assert store.request_action(batch.id, m.ACTION_CANCEL).requested_action == m.ACTION_CANCEL
        with pytest.raises(m.InvalidTransitionError, match="being cancelled"):
            store.request_action(batch.id, m.ACTION_PAUSE)
        assert store.transition_batch(batch.id, m.BATCH_CANCELLED).requested_action == m.ACTION_NONE
        with pytest.raises(ValueError):
            store.request_action(batch.id, "STOP")

    def test_reaching_paused_clears_the_request(self, store):
        batch = store.create_batch(_new_batch())
        store.transition_batch(batch.id, m.BATCH_RUNNING)
        store.request_action(batch.id, m.ACTION_PAUSE)
        assert store.transition_batch(batch.id, m.BATCH_PAUSED).requested_action == m.ACTION_NONE

    def test_only_one_batch_can_run_at_a_time(self, store):
        first, second = store.create_batch(_new_batch()), store.create_batch(_new_batch())
        store.transition_batch(first.id, m.BATCH_RUNNING)
        with pytest.raises(LeaseError, match="already RUNNING"):
            store.transition_batch(second.id, m.BATCH_RUNNING)
        assert store.get_batch(second.id).status == m.BATCH_CREATED
        assert store.active_batch().id == first.id


class TestLease:
    def test_lease_heartbeat_and_release(self, store):
        batch = store.create_batch(_new_batch())
        leased = store.acquire_lease(batch.id, "token-1", pid=111, host="pc")
        assert (leased.owner_token, leased.owner_pid, leased.owner_host) == ("token-1", 111, "pc")
        before = leased.heartbeat_at
        store.heartbeat(batch.id, "token-1")
        assert store.get_batch(batch.id).heartbeat_at >= before
        with pytest.raises(LeaseError):
            store.heartbeat(batch.id, "someone-else")
        with pytest.raises(LeaseError):
            store.acquire_lease(batch.id, "token-2", pid=222, host="pc")
        store.release_lease(batch.id, "token-1")
        assert store.get_batch(batch.id).owner_token is None
        with pytest.raises(LeaseError):
            store.release_lease(batch.id, "token-1")

    def test_only_one_batch_can_hold_a_lease(self, store):
        first, second = store.create_batch(_new_batch()), store.create_batch(_new_batch())
        store.acquire_lease(first.id, "t1", pid=1, host="pc")
        with pytest.raises(LeaseError, match="already active"):
            store.acquire_lease(second.id, "t2", pid=2, host="pc")

    def test_stale_leases_are_listed_for_recovery(self, store, monkeypatch):
        batch = store.create_batch(_new_batch())
        monkeypatch.setattr(storage, "now", lambda: "2026-01-01T00:00:00.000Z")
        store.acquire_lease(batch.id, "t1", pid=1, host="pc")
        assert [b.id for b in store.stale_leases("2026-01-01T00:01:00.000Z")] == [batch.id]
        assert store.stale_leases("2025-12-31T23:59:00.000Z") == []


class TestExecutions:
    def test_attempts_are_numbered_and_linked(self, store):
        batch = store.create_batch(_new_batch())
        first = _running_execution(store, batch.id)
        store.finish_execution(first.id, m.EXEC_INTERRUPTED)
        second = store.create_execution(batch.id, "DEMO-1", trigger=m.TRIGGER_RESUME)
        assert (second.attempt, second.previous_execution_id, second.trigger) == (
            2,
            first.id,
            "resume",
        )
        assert store.get_execution(first.id).status == m.EXEC_INTERRUPTED  # untouched

    def test_no_second_active_attempt_for_the_same_issue(self, store):
        batch = store.create_batch(_new_batch())
        store.create_execution(batch.id, "DEMO-1", trigger=m.TRIGGER_INITIAL)
        with pytest.raises(DuplicateAttemptError):
            store.create_execution(batch.id, "DEMO-1", trigger=m.TRIGGER_RETRY)

    def test_a_duplicate_attempt_number_is_rejected_by_the_database(self, store):
        batch = store.create_batch(_new_batch())
        first = store.create_execution(batch.id, "DEMO-1", trigger=m.TRIGGER_INITIAL)
        store.finish_execution(first.id, m.EXEC_CANCELLED)
        with pytest.raises(sqlite3.IntegrityError, match="attempt"):
            store._write(
                "INSERT INTO executions (id, batch_id, issue_key, attempt, trigger, status, phase, created_at) "
                "VALUES ('x', ?, 'DEMO-1', 1, 'retry', 'QUEUED', 'queued', 'now')",
                (batch.id,),
            )

    def test_foreign_keys_are_enforced(self, store):
        batch = store.create_batch(_new_batch())
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            store.create_execution(batch.id, "NOT-IN-BATCH", trigger=m.TRIGGER_INITIAL)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            store._write(
                "INSERT INTO batch_issues (batch_id, issue_key, position) VALUES ('missing', 'X-1', 0)"
            )

    def test_only_one_execution_runs_at_a_time(self, store):
        batch = store.create_batch(_new_batch())
        _running_execution(store, batch.id, "DEMO-1")
        other = store.create_execution(batch.id, "DEMO-2", trigger=m.TRIGGER_INITIAL)
        with pytest.raises(DuplicateAttemptError, match="already RUNNING"):
            store.start_execution(other.id)
        assert store.get_execution(other.id).status == m.EXEC_QUEUED

    @pytest.mark.parametrize(
        "final", [m.EXEC_PASS, m.EXEC_FAIL, m.EXEC_CANCELLED, m.EXEC_INTERRUPTED]
    )
    def test_valid_execution_transitions(self, store, final):
        batch = store.create_batch(_new_batch())
        execution = _running_execution(store, batch.id)
        finished = store.finish_execution(execution.id, final)
        assert (finished.status, finished.phase) == (final, m.PHASE_DONE)
        assert finished.finished_at and finished.final

    @pytest.mark.parametrize(
        ("start", "target"),
        [
            (m.EXEC_QUEUED, m.EXEC_PASS),
            (m.EXEC_QUEUED, m.EXEC_NOT_EXECUTED),
            (m.EXEC_RUNNING, m.EXEC_QUEUED),
            (m.EXEC_RUNNING, m.EXEC_RUNNING),
        ],
    )
    def test_invalid_execution_transitions(self, store, start, target):
        batch = store.create_batch(_new_batch())
        execution = store.create_execution(batch.id, "DEMO-1", trigger=m.TRIGGER_INITIAL)
        if start == m.EXEC_RUNNING:
            execution = store.start_execution(execution.id)
        with pytest.raises(m.InvalidTransitionError):
            store.finish_execution(execution.id, target)
        assert store.get_execution(execution.id).status == start

    def test_final_records_are_immutable(self, store):
        batch = store.create_batch(_new_batch())
        execution = _running_execution(store, batch.id)
        store.finish_execution(execution.id, m.EXEC_PASS)
        with pytest.raises(m.InvalidTransitionError):
            store.finish_execution(execution.id, m.EXEC_FAIL)
        with pytest.raises(ImmutableRecordError):
            store.update_execution(execution.id, phase=m.PHASE_STEP1)
        with pytest.raises(ImmutableRecordError, match="final"):
            store._write("UPDATE executions SET error_message = 'x' WHERE id = ?", (execution.id,))
        with pytest.raises(ImmutableRecordError, match="deleted"):
            store._write("DELETE FROM executions WHERE id = ?", (execution.id,))

    def test_progress_and_newman_metadata_are_stored(self, store):
        batch = store.create_batch(_new_batch())
        execution = _running_execution(store, batch.id)
        store.update_execution(
            execution.id,
            phase=m.PHASE_NEWMAN,
            artifact_dir="artifacts/batch_testing/b/DEMO-1/e",
            collection_path="artifacts/batch_testing/b/DEMO-1/e/collection.json",
            task_source={
                "issue_key": "DEMO-1",
                "sources": [{"type": "description", "sha256": "c" * 64}],
            },
            cases_total=3,
            cases_executable=2,
            cases_not_executed=1,
        )
        finished = store.finish_execution(
            execution.id,
            m.EXEC_FAIL,
            final_review_status="needs_review",
            newman_run_id="20261031T100000Z_abcd1234",
            newman_status="FAIL",
            newman_no_assertions=False,
            newman_run_dir="artifacts/batch_testing/b/DEMO-1/e/newman/run",
            newman_result_path="artifacts/batch_testing/b/DEMO-1/e/newman/run/result.json",
            requests_executed=2,
            assertions_total=2,
            assertions_failed=1,
        )
        assert finished.task_source["sources"][0]["type"] == "description"
        assert (finished.cases_executable, finished.cases_not_executed) == (2, 1)
        assert (finished.newman_status, finished.assertions_failed) == ("FAIL", 1)
        assert finished.final_review_status == "needs_review"

    def test_unknown_fields_and_values_are_rejected(self, store):
        batch = store.create_batch(_new_batch())
        execution = _running_execution(store, batch.id)
        with pytest.raises(ValueError, match="cannot be updated"):
            store.update_execution(execution.id, status=m.EXEC_PASS)
        with pytest.raises(ValueError, match="phase"):
            store.update_execution(execution.id, phase="step9")
        with pytest.raises(ValueError, match="review status"):
            store.finish_execution(execution.id, m.EXEC_PASS, review_status="MAYBE")


class TestReviewIndependence:
    def test_execution_and_review_statuses_are_independent(self, store):
        batch = store.create_batch(_new_batch())
        execution = _running_execution(store, batch.id)
        finished = store.finish_execution(
            execution.id,
            m.EXEC_PASS,
            review_status=m.REVIEW_NEEDS_REVIEW,
            review_reasons=[{"kind": "UNMATCHED", "message": "ENV-2 matched no service"}],
            final_review_status="ready",
        )
        assert (finished.status, finished.review_status, finished.final_review_status) == (
            "PASS",
            "NEEDS_REVIEW",
            "ready",
        )
        assert finished.review_reasons[0]["kind"] == "UNMATCHED"

    def test_not_executed_requires_needs_review(self, store):
        batch = store.create_batch(_new_batch())
        execution = _running_execution(store, batch.id)
        with pytest.raises(ValueError, match="NEEDS_REVIEW"):
            store.finish_execution(execution.id, m.EXEC_NOT_EXECUTED)
        assert store.get_execution(execution.id).status == m.EXEC_RUNNING
        done = store.finish_execution(
            execution.id, m.EXEC_NOT_EXECUTED, review_status=m.REVIEW_NEEDS_REVIEW
        )
        assert done.status == m.EXEC_NOT_EXECUTED

    def test_error_requires_an_error_kind(self, store):
        batch = store.create_batch(_new_batch())
        execution = _running_execution(store, batch.id)
        with pytest.raises(ValueError, match="error_kind"):
            store.finish_execution(execution.id, m.EXEC_ERROR)
        done = store.finish_execution(
            execution.id,
            m.EXEC_ERROR,
            error_stage=m.PHASE_STEP2,
            error_kind="STEP_FAILED",
            error_message="API mapping failed",
        )
        assert (done.error_stage, done.error_kind) == ("step2", "STEP_FAILED")

    def test_latest_executions_per_issue(self, store):
        batch = store.create_batch(_new_batch())
        first = _running_execution(store, batch.id, "DEMO-1")
        store.finish_execution(first.id, m.EXEC_FAIL)
        retry = store.create_execution(batch.id, "DEMO-1", trigger=m.TRIGGER_RETRY)
        latest = store.latest_executions(batch.id)
        assert latest["DEMO-1"].id == retry.id
        assert [e.attempt for e in store.executions(batch.id, "DEMO-1")] == [1, 2]
        assert [e.id for e in store.active_executions()] == [retry.id]


class TestTransactionsAndConcurrency:
    def test_a_failed_transaction_rolls_back(self, store):
        batch = store.create_batch(_new_batch())
        with pytest.raises(RuntimeError), store.transaction() as connection:
            connection.execute(
                "UPDATE batches SET board_name = 'changed' WHERE id = ?", (batch.id,)
            )
            raise RuntimeError("boom")
        assert store.get_batch(batch.id).board_name == "Podium board"

    def test_readers_see_committed_data_while_another_connection_writes(self, db_path):
        with BatchStore.open(db_path) as writer, BatchStore.open(db_path) as reader:
            batch = writer.create_batch(_new_batch())
            with writer.transaction() as connection:
                connection.execute(
                    "UPDATE batches SET board_name = 'pending' WHERE id = ?", (batch.id,)
                )
                # WAL: the reader is not blocked and sees only committed data
                assert reader.get_batch(batch.id).board_name == "Podium board"
            assert reader.get_batch(batch.id).board_name == "pending"

    def test_concurrent_attempts_for_one_issue_never_duplicate(self, db_path):
        with BatchStore.open(db_path) as setup:
            batch = setup.create_batch(_new_batch())
        results: list[str] = []
        lock = threading.Lock()

        def try_create():
            with BatchStore.open(db_path) as store:
                try:
                    store.create_execution(batch.id, "DEMO-1", trigger=m.TRIGGER_INITIAL)
                    outcome = "created"
                except DuplicateAttemptError:
                    outcome = "duplicate"
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=try_create) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == ["created"] + ["duplicate"] * 5

    def test_concurrent_lease_acquisition_has_one_winner(self, db_path):
        with BatchStore.open(db_path) as setup:
            batches = [setup.create_batch(_new_batch()) for _ in range(4)]
        winners: list[str] = []
        lock = threading.Lock()

        def acquire(batch_id: str, token: str):
            with BatchStore.open(db_path) as store:
                try:
                    store.acquire_lease(batch_id, token, pid=1, host="pc")
                except LeaseError:
                    return
            with lock:
                winners.append(token)

        threads = [
            threading.Thread(target=acquire, args=(batch.id, f"t{index}"))
            for index, batch in enumerate(batches)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(winners) == 1


# ── نوشتن‌های token-guarded و گرفتنِ اتمیِ leaseِ کهنه (اصلاحِ Milestone 4.5) ──

PAST = "2000-01-01T00:00:00.000Z"
FUTURE = "2999-01-01T00:00:00.000Z"


class TestOwnershipGuards:
    @staticmethod
    def _owned_by_b(store: BatchStore) -> tuple[str, m.Execution, m.Execution]:
        batch = store.create_batch(_new_batch())
        store.acquire_lease(batch.id, "B", pid=2, host="pc")
        store.transition_batch(batch.id, m.BATCH_RUNNING, owner_token="B")
        queued = store.create_execution(batch.id, "DEMO-2", trigger=m.TRIGGER_INITIAL)
        running = store.start_execution(
            store.create_execution(batch.id, "DEMO-1", trigger=m.TRIGGER_INITIAL).id,
            owner_token="B",
        )
        return batch.id, running, queued

    def test_every_guarded_write_is_refused_for_a_stale_token(self, store):
        batch_id, running, queued = self._owned_by_b(store)
        before_batch = store.get_batch(batch_id)
        before = store.executions(batch_id)
        attempts = [
            lambda: store.transition_batch(batch_id, m.BATCH_PAUSED, owner_token="A"),
            lambda: store.create_execution(
                batch_id, "DEMO-1", trigger=m.TRIGGER_RETRY, owner_token="A"
            ),
            lambda: store.start_execution(queued.id, owner_token="A"),
            lambda: store.update_execution(running.id, owner_token="A", phase=m.PHASE_NEWMAN),
            lambda: store.finish_execution(running.id, m.EXEC_PASS, owner_token="A"),
            lambda: store.finish_execution(queued.id, m.EXEC_CANCELLED, owner_token="A"),
            lambda: store.heartbeat(batch_id, "A"),
            lambda: store.release_lease(batch_id, "A"),
        ]
        for attempt in attempts:
            with pytest.raises(storage.OwnershipError):
                attempt()
        after_batch = store.get_batch(batch_id)
        assert (after_batch.status, after_batch.owner_token, after_batch.heartbeat_at) == (
            before_batch.status,
            "B",
            before_batch.heartbeat_at,
        )
        assert store.executions(batch_id) == before  # همه rollback شدند

    def test_the_owner_token_and_legacy_unguarded_writes_still_work(self, store):
        batch_id, running, queued = self._owned_by_b(store)
        store.update_execution(running.id, owner_token="B", phase=m.PHASE_NEWMAN)
        store.finish_execution(running.id, m.EXEC_PASS, owner_token="B")
        store.finish_execution(queued.id, m.EXEC_CANCELLED)  # بدونِ توکن: رفتارِ قبلی
        assert (
            store.transition_batch(batch_id, m.BATCH_COMPLETED, owner_token="B").status
            == m.BATCH_COMPLETED
        )

    def test_ownership_error_is_a_lease_error(self):
        assert issubclass(storage.OwnershipError, LeaseError)


class TestLeaseTakeover:
    def test_a_stale_lease_is_replaced_in_one_step(self, store):
        batch = store.create_batch(_new_batch())
        store.acquire_lease(batch.id, "dead", pid=1, host="gone")
        taken = store.take_over_lease(
            batch.id,
            expected_token="dead",
            new_token="recovery",
            stale_before=FUTURE,
            pid=7,
            host="here",
        )
        assert (taken.owner_token, taken.owner_pid, taken.owner_host) == ("recovery", 7, "here")
        assert taken.heartbeat_at is not None

    def test_a_healthy_lease_cannot_be_taken_over(self, store):
        batch = store.create_batch(_new_batch())
        store.acquire_lease(batch.id, "alive", pid=1, host="pc")
        before = store.get_batch(batch.id)
        with pytest.raises(storage.OwnershipError):
            store.take_over_lease(
                batch.id,
                expected_token="alive",
                new_token="thief",
                stale_before=PAST,  # heartbeat تازه‌تر از این است
                pid=2,
                host="pc",
            )
        assert store.get_batch(batch.id) == before

    def test_a_changed_owner_cannot_be_taken_over_with_stale_information(self, store):
        batch = store.create_batch(_new_batch())
        store.acquire_lease(batch.id, "other", pid=1, host="pc")
        before = store.get_batch(batch.id)
        with pytest.raises(storage.OwnershipError):
            store.take_over_lease(
                batch.id,
                expected_token="dead",
                new_token="recovery",
                stale_before=FUTURE,
                pid=2,
                host="pc",
            )
        assert store.get_batch(batch.id) == before

    def test_concurrent_takeovers_have_exactly_one_winner(self, db_path):
        with BatchStore.open(db_path) as setup:
            batch = setup.create_batch(_new_batch())
            setup.acquire_lease(batch.id, "dead", pid=1, host="gone")
        barrier = threading.Barrier(4)
        winners: list[str] = []
        lock = threading.Lock()

        def take(token: str) -> None:
            with BatchStore.open(db_path) as store:
                barrier.wait()
                try:
                    store.take_over_lease(
                        batch.id,
                        expected_token="dead",
                        new_token=token,
                        stale_before=FUTURE,
                        pid=1,
                        host="pc",
                    )
                except storage.OwnershipError:
                    return
            with lock:
                winners.append(token)

        threads = [threading.Thread(target=take, args=(f"r{index}",)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(winners) == 1
        with BatchStore.open(db_path) as check:
            assert check.get_batch(batch.id).owner_token == winners[0]


# ── لغوِ batchِ غیرفعال بدونِ runner (اصلاحِ Milestone 4.6) ─────────────────


class TestCancelInactiveBatch:
    @staticmethod
    def _stopped(store: BatchStore, target: str) -> tuple[str, m.Execution, m.Execution]:
        """batchی با DEMO-1 پایانی و DEMO-2 هنوز QUEUED، بعد PAUSED یا INTERRUPTED بی‌lease."""
        batch = store.create_batch(_new_batch())
        store.acquire_lease(batch.id, "B", pid=1, host="pc")
        store.transition_batch(batch.id, m.BATCH_RUNNING, owner_token="B")
        first = store.start_execution(
            store.create_execution(batch.id, "DEMO-1", trigger=m.TRIGGER_INITIAL).id
        )
        status = m.EXEC_PASS if target == m.BATCH_PAUSED else m.EXEC_INTERRUPTED
        first = store.finish_execution(
            first.id, status, error_kind="RUNNER_LOST" if status != m.EXEC_PASS else ""
        )
        queued = store.create_execution(batch.id, "DEMO-2", trigger=m.TRIGGER_INITIAL)
        store.transition_batch(batch.id, target, owner_token="B")
        store.release_lease(batch.id, "B")
        return batch.id, first, queued

    def test_a_created_batch_is_cancelled_without_any_attempt(self, store):
        batch = store.create_batch(_new_batch())
        cancelled, ids = store.cancel_inactive_batch(batch.id, expected=m.BATCH_CREATED)
        assert (cancelled.status, ids, store.executions(batch.id)) == (m.BATCH_CANCELLED, [], [])
        assert cancelled.finished_at and cancelled.started_at is None

    @pytest.mark.parametrize("target", [m.BATCH_PAUSED, m.BATCH_INTERRUPTED])
    def test_queued_runs_are_cancelled_and_history_is_kept(self, store, target):
        batch_id, first, queued = self._stopped(store, target)
        cancelled, ids = store.cancel_inactive_batch(batch_id, expected=target)
        assert cancelled.status == m.BATCH_CANCELLED and ids == [queued.id]
        assert store.get_execution(first.id) == first  # PASS / INTERRUPTED دست‌نخورده
        after = store.get_execution(queued.id)
        assert (after.status, after.phase, after.attempt) == (m.EXEC_CANCELLED, "queued", 1)
        assert len(store.executions(batch_id)) == 2  # هیچ تلاشِ تازه‌ای

    def test_a_batch_with_a_lease_is_refused(self, store):
        batch_id, _, _ = self._stopped(store, m.BATCH_PAUSED)
        store.acquire_lease(batch_id, "runner", pid=2, host="pc")
        before = (store.get_batch(batch_id), store.executions(batch_id))
        with pytest.raises(storage.OwnershipError):
            store.cancel_inactive_batch(batch_id, expected=m.BATCH_PAUSED)
        assert (store.get_batch(batch_id), store.executions(batch_id)) == before

    def test_a_running_execution_is_never_overwritten(self, store):
        batch_id, _, queued = self._stopped(store, m.BATCH_INTERRUPTED)
        store.start_execution(queued.id)  # وضعیتِ ناتمام: اجرای RUNNING در batchِ بی‌runner
        before = (store.get_batch(batch_id), store.executions(batch_id))
        with pytest.raises(m.InvalidTransitionError, match="RUNNING execution"):
            store.cancel_inactive_batch(batch_id, expected=m.BATCH_INTERRUPTED)
        assert (store.get_batch(batch_id), store.executions(batch_id)) == before

    @pytest.mark.parametrize(
        "expected", [m.BATCH_RUNNING, m.BATCH_COMPLETED, m.BATCH_CANCELLED, m.BATCH_INTERRUPTED]
    )
    def test_other_or_changed_states_are_refused(self, store, expected):
        batch_id, _, _ = self._stopped(store, m.BATCH_PAUSED)
        before = (store.get_batch(batch_id), store.executions(batch_id))
        with pytest.raises(m.InvalidTransitionError):
            store.cancel_inactive_batch(batch_id, expected=expected)
        assert (store.get_batch(batch_id), store.executions(batch_id)) == before

    def test_the_queue_and_the_batch_change_in_one_transaction(self, store, monkeypatch):
        batch_id, _, queued = self._stopped(store, m.BATCH_PAUSED)
        real_write, writes = store._write, []

        def failing_second_write(sql, params=()):
            writes.append(sql)
            if len(writes) == 2:  # پس از لغوِ صف، پیش از تغییرِ batch
                raise sqlite3.OperationalError("disk I/O error")
            return real_write(sql, params)

        monkeypatch.setattr(store, "_write", failing_second_write)
        with pytest.raises(sqlite3.OperationalError):
            store.cancel_inactive_batch(batch_id, expected=m.BATCH_PAUSED)
        monkeypatch.undo()
        assert store.get_execution(queued.id).status == m.EXEC_QUEUED  # rollback
        assert store.get_batch(batch_id).status == m.BATCH_PAUSED
