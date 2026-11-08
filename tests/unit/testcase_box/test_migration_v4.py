"""
tests/unit/testcase_box/test_migration_v4.py — migrationِ v3 → v4 (مالکیتِ اجرای run)

همه روی پایگاه‌های موقت. fixtureِ v3 داده‌ی واقعی‌نما دارد: caseهای واردشده از Batch با
نسخه و منشأ، caseِ بایگانی‌شده، import job، رابطه، تأییدِ اسکریپت، runِ پایانیِ تاریخی و
runِ QUEUED. پایگاهِ واقعیِ data/testcase_box هرگز باز نمی‌شود.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from src.testcase_box import db
from src.testcase_box import models as m
from src.testcase_box.db import ImmutableRecordError, SchemaVersionError
from src.testcase_box.migrations import LATEST_VERSION, MIGRATIONS
from src.testcase_box.models import NewCase, NewLink, NewRun, NewRunCase, NewRunUnit, NewSource
from src.testcase_box.storage import BoxStore

_V4_SHA256 = "8ad9f32ed6cabe6b4f9fcc38d9a797d081e53a8e6f6e5fb3522ccc42beef874d"
_V4_COLUMNS = (
    "owner_token",
    "owner_pid",
    "owner_host",
    "heartbeat_at",
    "lease_generation",
    "requested_action",
)
_V4_OBJECTS = {
    "ux_box_runs_single_running",
    "ux_box_runs_single_lease",
    "tr_box_runs_owner_complete_insert",
    "tr_box_runs_owner_complete_update",
    "tr_box_runs_final_without_lease",
    "tr_box_runs_lease_generation",
    "tr_box_runs_new_owner_generation",
}
_TABLES = (
    "box_cases",
    "box_case_versions",
    "box_case_sources",
    "box_case_links",
    "box_import_jobs",
    "box_script_approvals",
    "box_run_units",
    "box_run_cases",
)
_V3_RUN_COLUMNS = "id, status, retry_of_run_id, metadata_json, created_at, started_at, finished_at"


def _snapshot(connection: sqlite3.Connection) -> dict[str, list[tuple]]:
    data = {
        table: [tuple(r) for r in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in _TABLES
    }
    data["box_runs"] = [
        tuple(r) for r in connection.execute(f"SELECT {_V3_RUN_COLUMNS} FROM box_runs ORDER BY id")
    ]
    return data


def _objects(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index', 'trigger') "
        "AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0]: row[1] for row in rows}


def _columns(connection: sqlite3.Connection) -> list[str]:
    return [row[1] for row in connection.execute("PRAGMA table_info(box_runs)")]


def _batch_case(store: BoxStore, ref: str, *, scripts: bool) -> m.BoxCase:
    payload = {
        "name": ref,
        "request": {"method": "GET", "url": {"raw": "{{base_url}}/x", "host": ["{{base_url}}"]}},
    }
    if scripts:
        payload["event"] = [{"listen": "test", "script": {"exec": ["pm.test('ok', () => {})"]}}]
    source = NewSource(
        m.SOURCE_BATCH,
        ref,
        {
            "execution_id": "exec-1",
            "snapshot_fingerprint": "f" * 64,
            "item_fingerprint": m.sha256_json(payload),
            "position": 1,
        },
    )
    return store.create_case_for_source(
        NewCase(title=f"Imported {ref}", payload=payload, sources=(source,))
    ).case


@pytest.fixture
def v3_database(tmp_path):
    path = tmp_path / "box.sqlite3"
    connection = db.connect(path)
    assert db.migrate(connection, target=3) == 3
    store = BoxStore(connection, path)
    job = store.create_import_job(m.SOURCE_BATCH, "PODIUM-1/attempt_1")
    first = _batch_case(store, "PODIUM-1/attempt_1/TC-001", scripts=True)
    second = _batch_case(store, "PODIUM-1/attempt_1/TC-002", scripts=False)
    archived = _batch_case(store, "PODIUM-1/attempt_1/TC-003", scripts=False)
    store.archive_case(archived.id)
    store.add_link(second.id, NewLink(first.id))
    version = store.current_version(first.id)
    store.record_script_decision(version.id, version.script_fingerprint, m.APPROVAL_APPROVED)
    store.finish_import_job(job.id, m.IMPORT_COMPLETED, {"added": 3})
    history = store.create_run(
        NewRun(units=(NewRunUnit(1, (NewRunCase(second.id, second.current_version_id, 1),)),))
    )
    unit = store.run_units(history.id)[0]
    store.set_run_status(history.id, m.RUN_RUNNING)
    store.set_unit_status(unit.id, m.UNIT_RUNNING)
    store.set_run_case_status(
        store.run_cases(history.id)[0].id, m.CASE_RUN_PASS, result_metadata={"http": [200]}
    )
    store.set_unit_status(unit.id, m.UNIT_PASS)
    store.set_run_status(history.id, m.RUN_COMPLETED)
    queued = store.create_run(
        NewRun(
            units=(NewRunUnit(1, (NewRunCase(first.id, version.id, 1),)),),
            retry_of_run_id=history.id,
        )
    )
    yield path, connection, store, {"history": history.id, "queued": queued.id, "job": job.id}
    connection.close()


def test_the_v4_migration_is_frozen():
    assert hashlib.sha256(json.dumps(MIGRATIONS[3]).encode()).hexdigest() == _V4_SHA256
    assert LATEST_VERSION == 4


def test_a_fresh_database_initializes_directly_to_v4(tmp_path):
    with BoxStore.open(tmp_path / "box.sqlite3") as store:
        assert store.schema_version == 4
        connection = store._db
        assert set(_V4_COLUMNS) <= set(_columns(connection))
        assert _V4_OBJECTS <= set(_objects(connection))
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
        assert versions == [1, 2, 3, 4]


def test_v3_data_ids_relationships_and_provenance_survive(v3_database):
    _, connection, store, ids = v3_database
    before, objects_before = _snapshot(connection), _objects(connection)
    sources_before = store.find_sources(m.SOURCE_BATCH, "PODIUM-1/attempt_1/TC-001")
    pinned_before = store.pinned_versions(ids["queued"])

    assert db.migrate(connection) == 4

    assert _snapshot(connection) == before  # هیچ ردیف، شناسه یا رابطه‌ای عوض نشد
    objects_after = _objects(connection)
    for name, sql in objects_before.items():
        if name != "box_runs":
            assert objects_after[name] == sql  # trigger و ایندکس‌های قبلی همان‌اند
    assert _V4_OBJECTS <= set(objects_after)
    defaults = connection.execute(
        "SELECT owner_token, owner_pid, owner_host, heartbeat_at, lease_generation, "
        "requested_action FROM box_runs ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in defaults] == [(None, None, None, None, 0, "NONE")] * 2
    assert store.find_sources(m.SOURCE_BATCH, "PODIUM-1/attempt_1/TC-001") == sources_before
    assert store.pinned_versions(ids["queued"]) == pinned_before
    assert store.get_import_job(ids["job"]).status == m.IMPORT_COMPLETED


def test_existing_rules_still_hold_after_the_upgrade(v3_database):
    _, connection, store, ids = v3_database
    db.migrate(connection)
    with pytest.raises(m.InvalidTransitionError):
        store.set_run_status(ids["history"], m.RUN_CANCELLED)  # runِ پایانی تغییر نمی‌کند
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):  # و خودِ پایگاه هم
        connection.execute(
            "UPDATE box_runs SET metadata_json = '{}' WHERE id = ?", (ids["history"],)
        )
    with pytest.raises(ImmutableRecordError):
        store.request_run_cancel(ids["history"])
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("UPDATE box_case_versions SET created_by = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("DELETE FROM box_case_sources")
    store.set_run_status(ids["queued"], m.RUN_RUNNING)  # گذارِ مجاز همچنان کار می‌کند


def test_a_failed_v4_migration_rolls_back_completely(v3_database, monkeypatch):
    _, connection, _, _ = v3_database
    before, objects_before = _snapshot(connection), _objects(connection)
    version, name, statements = MIGRATIONS[3]
    monkeypatch.setattr(
        db, "MIGRATIONS", [*MIGRATIONS[:3], (version, name, [*statements, "CREATE TABLE broken ("])]
    )
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(connection)
    assert db.schema_version(connection) == 3
    assert not connection.in_transaction
    assert not set(_V4_COLUMNS) & set(_columns(connection))  # ALTERها هم برگشتند
    assert _objects(connection) == objects_before
    assert _snapshot(connection) == before


def test_a_v3_database_that_violates_v4_rules_is_not_repaired(v3_database):
    _, connection, store, ids = v3_database
    other = store.create_run(NewRun(units=(NewRunUnit(1, (NewRunCase(1, 1, 1),)),)))
    store.set_run_status(ids["queued"], m.RUN_RUNNING)
    store.set_run_status(other.id, m.RUN_RUNNING)  # v3 دو runِ RUNNING را اجازه می‌داد
    before = _snapshot(connection)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.migrate(connection)
    assert db.schema_version(connection) == 3
    assert not set(_V4_COLUMNS) & set(_columns(connection))
    assert _snapshot(connection) == before  # هیچ «تعمیرِ» بی‌صدایی


def test_reopening_a_v4_database_does_not_rerun_the_migration(v3_database):
    path, connection, _, _ = v3_database
    db.migrate(connection)
    applied = connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
    objects = _objects(connection)
    for _ in range(2):
        with BoxStore.open(path) as reopened:
            assert reopened.schema_version == 4
    assert connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall() == (
        applied
    )
    assert _objects(connection) == objects


def test_a_newer_schema_version_is_refused_without_changes(v3_database):
    path, connection, _, _ = v3_database
    db.migrate(connection)
    connection.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (99, 'future', 'x')"
    )
    before = _snapshot(connection)
    with pytest.raises(SchemaVersionError):
        db.migrate(connection)
    with pytest.raises(SchemaVersionError):
        BoxStore.open(path)
    assert _snapshot(connection) == before


def test_a_foreign_database_is_never_migrated(tmp_path):
    path = tmp_path / "other.sqlite3"
    with sqlite3.connect(path) as other:
        other.execute("CREATE TABLE something (x)")
    other.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(db.ForeignDatabaseError):
        BoxStore.open(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
