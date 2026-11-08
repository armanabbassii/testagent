"""
تست‌های migrationِ TestCase Box — ارتقای پایگاهِ v1 به v2 روی پایگاه‌های موقت.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from src.testcase_box import db
from src.testcase_box import models as m
from src.testcase_box.db import ImmutableRecordError
from src.testcase_box.migrations import LATEST_VERSION, MIGRATIONS
from src.testcase_box.models import NewCase, NewRun, NewRunCase, NewRunUnit, NewSource
from src.testcase_box.storage import BoxStore

# SQLِ migrationِ v1 منتشر شده است و هرگز نباید تغییر کند
_V1_SHA256 = "808a5de2a4440c0439c790e80c2eee9ef60fafe272ddedca6adee9c6aa2ff317"
_V2_TRIGGERS = {
    "tr_box_runs_initial_status",
    "tr_box_run_units_initial_status",
    "tr_box_run_cases_initial_status",
    "tr_box_import_jobs_initial_status",
    "tr_box_runs_children_final",
    "tr_box_run_units_run_final_update",
    "tr_box_run_units_run_final_insert",
    "tr_box_run_cases_run_final_update",
    "tr_box_run_cases_run_final_insert",
    "tr_box_runs_history",
    "tr_box_cases_history",
}
_TABLES = (
    "box_cases",
    "box_case_versions",
    "box_case_sources",
    "box_case_links",
    "box_import_jobs",
    "box_script_approvals",
    "box_runs",
    "box_run_units",
    "box_run_cases",
)


def _triggers(connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    return {row[0] for row in rows}


def _snapshot(connection) -> dict[str, list[tuple]]:
    return {
        table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in _TABLES
    }


@pytest.fixture
def v1_database(tmp_path):
    """پایگاهِ v1 با داده‌ی واقعی — از جمله یک ردیفِ ناسازگار که v1 اجازه می‌داد."""
    path = tmp_path / "box.sqlite3"
    connection = db.connect(path)
    assert db.migrate(connection, target=1) == 1
    store = BoxStore(connection, path)
    case = store.create_case(
        NewCase(
            title="v1 case",
            payload={"event": [{"listen": "test", "script": {"exec": ["x()"]}}]},
            sources=(NewSource(m.SOURCE_MANUAL, "typed"),),
        )
    )
    version = store.current_version(case.id)
    store.record_script_decision(version.id, version.script_fingerprint, m.APPROVAL_APPROVED)
    store.finish_import_job(store.create_import_job("CSV", "a.csv").id, m.IMPORT_COMPLETED)
    run = store.create_run(NewRun(units=(NewRunUnit(1, (NewRunCase(case.id, version.id, 1),)),)))
    # v1 اجازه می‌داد: runِ پایانی با فرزندِ باز (بدونِ trigger های v2)
    connection.execute("UPDATE box_runs SET status = 'RUNNING' WHERE id = ?", (run.id,))
    connection.execute("UPDATE box_runs SET status = 'INTERRUPTED' WHERE id = ?", (run.id,))
    yield path, connection, run.id
    connection.close()


def test_migration_v1_is_frozen():
    digest = hashlib.sha256(json.dumps(MIGRATIONS[0]).encode()).hexdigest()
    assert digest == _V1_SHA256
    assert [version for version, _, _ in MIGRATIONS] == list(range(1, LATEST_VERSION + 1))


def test_v1_database_upgrades_without_touching_rows(v1_database):
    path, connection, _ = v1_database
    before = _snapshot(connection)
    v1_applied = connection.execute(
        "SELECT applied_at FROM schema_migrations WHERE version = 1"
    ).fetchone()[0]
    assert not (_V2_TRIGGERS & _triggers(connection))

    assert db.migrate(connection, target=2) == 2
    assert _snapshot(connection) == before
    assert _V2_TRIGGERS <= _triggers(connection)
    rows = connection.execute("SELECT version, name, applied_at FROM schema_migrations").fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        (1, "library and run tables"),
        (2, "integrity hardening"),
    ]
    assert rows[0][2] == v1_applied

    assert db.migrate(connection, target=2) == 2  # تکرار بی‌اثر است
    assert _snapshot(connection) == before
    with BoxStore.open(path) as reopened:
        assert reopened.schema_version == LATEST_VERSION


def test_existing_open_children_of_a_final_run_become_frozen(v1_database):
    _, connection, run_id = v1_database
    db.migrate(connection)
    with pytest.raises(sqlite3.IntegrityError, match="the run is final; its units"):
        connection.execute(
            "UPDATE box_run_units SET status = 'RUNNING' WHERE run_id = ?", (run_id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="the run is final; its run cases"):
        connection.execute("UPDATE box_run_cases SET status = 'PASS' WHERE run_id = ?", (run_id,))
    statuses = connection.execute(
        "SELECT status FROM box_run_cases WHERE run_id = ?", (run_id,)
    ).fetchall()
    assert [row[0] for row in statuses] == [m.CASE_RUN_QUEUED]  # نه بازنویسی، نه تغییر


def test_upgraded_database_enforces_new_rules(v1_database):
    _, connection, _ = v1_database
    db.migrate(connection)
    with pytest.raises(sqlite3.IntegrityError, match="invalid initial run status"):
        connection.execute(
            "INSERT INTO box_runs (status, metadata_json, created_at) "
            "VALUES ('COMPLETED', '{}', 'x')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="case history columns"):
        connection.execute("UPDATE box_cases SET created_at = 'y'")


def test_failed_v2_migration_leaves_v1_intact(v1_database, monkeypatch):
    _, connection, _ = v1_database
    before = _snapshot(connection)
    version, name, statements = MIGRATIONS[1]
    monkeypatch.setattr(
        db, "MIGRATIONS", [MIGRATIONS[0], (version, name, [*statements, "CREATE TABLE broken ("])]
    )
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(connection)
    assert db.schema_version(connection) == 1
    assert not connection.in_transaction
    assert not (_V2_TRIGGERS & _triggers(connection))
    assert _snapshot(connection) == before


# ── M2.2: frozen v2/v3 SQL, v2 → v3 upgrade, COMMIT failure ──────────────────

_V2_SHA256 = "b7fbd61f5ecd4cbc99af878da45ddf22de201a8af8d537a542c99197216e3fa8"
_V3_SHA256 = "3305a987c3aa1a03aa488107e352de949d7c94b6549be31eb16b84f3920491bf"
_V3_TRIGGERS = {
    "tr_box_run_units_children_final",
    "tr_box_run_cases_unit_final_update",
    "tr_box_run_cases_unit_final_insert",
}


@pytest.mark.parametrize(
    ("index", "digest"), [(0, _V1_SHA256), (1, _V2_SHA256), (2, _V3_SHA256)], ids=["v1", "v2", "v3"]
)
def test_published_migrations_are_frozen(index, digest):
    assert hashlib.sha256(json.dumps(MIGRATIONS[index]).encode()).hexdigest() == digest


@pytest.fixture
def v2_database(tmp_path):
    """پایگاهِ v2 با یک unitِ پایانی که caseِ بازش را v2 هنوز اجازه می‌داد."""
    path = tmp_path / "box.sqlite3"
    connection = db.connect(path)
    assert db.migrate(connection, target=2) == 2
    store = BoxStore(connection, path)
    case = store.create_case(NewCase(title="v2 case", payload={}))
    run = store.create_run(
        NewRun(units=(NewRunUnit(1, (NewRunCase(case.id, case.current_version_id, 1),)),))
    )
    store.set_run_status(run.id, m.RUN_RUNNING)
    unit = store.run_units(run.id)[0]
    # SQLِ خام: Pythonِ M2.2 این حالت را رد می‌کند، اما triggerهای v2 اجازه می‌دادند
    connection.execute("UPDATE box_run_units SET status = 'RUNNING' WHERE id = ?", (unit.id,))
    connection.execute("UPDATE box_run_units SET status = 'PASS' WHERE id = ?", (unit.id,))
    yield path, connection, store, store.run_cases(run.id)[0]
    connection.close()


def test_v2_database_upgrades_to_v3_without_touching_rows(v2_database):
    path, connection, _, _ = v2_database
    before = _snapshot(connection)
    assert not (_V3_TRIGGERS & _triggers(connection))

    assert db.migrate(connection, target=3) == 3
    assert _snapshot(connection) == before
    assert (_V2_TRIGGERS | _V3_TRIGGERS) <= _triggers(connection)
    names = connection.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [tuple(row) for row in names][-1] == (3, "unit finalization integrity")

    assert db.migrate(connection, target=3) == 3
    assert _snapshot(connection) == before
    with BoxStore.open(path) as reopened:  # باز کردنِ عادی تا آخرین نسخه ادامه می‌دهد
        assert reopened.schema_version == LATEST_VERSION


def test_open_case_of_a_legacy_final_unit_becomes_frozen(v2_database):
    _, connection, store, run_case = v2_database
    db.migrate(connection)
    with pytest.raises(sqlite3.IntegrityError, match="the unit is final; its run cases"):
        connection.execute("UPDATE box_run_cases SET status = 'PASS' WHERE id = ?", (run_case.id,))
    with pytest.raises(ImmutableRecordError, match="is final"):
        store.set_run_case_status(run_case.id, m.CASE_RUN_PASS)
    assert store.get_run_case(run_case.id).status == m.CASE_RUN_QUEUED  # بازنویسی نشد


def test_failed_v3_migration_leaves_v2_intact(v2_database, monkeypatch):
    _, connection, _, _ = v2_database
    before = _snapshot(connection)
    version, name, statements = MIGRATIONS[2]
    monkeypatch.setattr(
        db,
        "MIGRATIONS",
        [*MIGRATIONS[:2], (version, name, [*statements, "CREATE TABLE broken ("])],
    )
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(connection)
    assert db.schema_version(connection) == 2
    assert not connection.in_transaction
    assert not (_V3_TRIGGERS & _triggers(connection))
    assert _snapshot(connection) == before


def test_commit_failure_inside_migrate_rolls_back(tmp_path, monkeypatch):
    """COMMIT خودش شکست می‌خورد (کلیدِ خارجیِ deferred) — rollback و همان خطا."""
    connection = db.connect(tmp_path / "box.sqlite3")
    try:
        db.migrate(connection)
        monkeypatch.setattr(
            db,
            "MIGRATIONS",
            [
                *MIGRATIONS,
                (
                    LATEST_VERSION + 1,
                    "deferred failure",
                    [
                        "PRAGMA defer_foreign_keys = ON",
                        "INSERT INTO box_case_sources (case_id, source_type, source_reference, "
                        "source_metadata_json, created_at) VALUES (999, 'MANUAL', 'x', '{}', 'x')",
                    ],
                ),
            ],
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.migrate(connection, target=LATEST_VERSION + 1)
        assert not connection.in_transaction
        assert db.schema_version(connection) == LATEST_VERSION
        assert connection.execute("SELECT COUNT(*) FROM box_case_sources").fetchone()[0] == 0
    finally:
        connection.close()
