"""
تست‌های اتصال و migrationِ پایگاهِ داده‌ی TestCase Box — همه روی پایگاه‌های موقت.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from src.testcase_box import db
from src.testcase_box.db import ForeignDatabaseError, SchemaVersionError
from src.testcase_box.migrations import APPLICATION_ID, LATEST_VERSION
from src.testcase_box.models import NewCase
from src.testcase_box.storage import BoxStore

_TABLES = {
    "box_cases",
    "box_case_versions",
    "box_case_sources",
    "box_case_links",
    "box_import_jobs",
    "box_script_approvals",
    "box_runs",
    "box_run_units",
    "box_run_cases",
}


def _tables(path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection, connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in rows}


def test_default_path_is_separate_from_batch():
    assert db.DEFAULT_DB_PATH.parts[-3:] == ("data", "testcase_box", "box.sqlite3")
    assert "batch_testing" not in db.DEFAULT_DB_PATH.parts


def test_open_creates_parent_directory_and_schema(tmp_path):
    path = tmp_path / "nested" / "dir" / "box.sqlite3"
    with BoxStore.open(path) as store:
        assert store.schema_version == LATEST_VERSION
    assert path.exists()
    assert _TABLES <= _tables(path)


def test_connection_settings(tmp_path):
    with BoxStore.open(tmp_path / "box.sqlite3") as store:
        connection = store._db
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID


def test_repeated_open_is_idempotent_and_keeps_data(tmp_path):
    from src.testcase_box.models import NewCase

    path = tmp_path / "box.sqlite3"
    with BoxStore.open(path) as store:
        case = store.create_case(NewCase(title="Keep me", payload={"request": {}}))
    for _ in range(3):
        with BoxStore.open(path) as store:
            assert store.schema_version == LATEST_VERSION
            assert store.get_case(case.id).title == "Keep me"
    with closing(sqlite3.connect(path)) as connection, connection:
        rows = connection.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [row[0] for row in rows] == list(range(1, LATEST_VERSION + 1))


def test_migration_versions_are_tracked(tmp_path):
    connection = db.connect(tmp_path / "box.sqlite3")
    try:
        assert db.schema_version(connection) == 0
        assert db.migrate(connection) == LATEST_VERSION
        assert db.migrate(connection) == LATEST_VERSION
        applied = connection.execute("SELECT applied_at FROM schema_migrations").fetchall()
        assert all(row[0].endswith("Z") for row in applied)
    finally:
        connection.close()


def test_newer_schema_is_rejected(tmp_path):
    path = tmp_path / "box.sqlite3"
    BoxStore.open(path).close()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, 'future', 'x')",
            (LATEST_VERSION + 1,),
        )
    with pytest.raises(SchemaVersionError):
        BoxStore.open(path)


def test_foreign_database_is_refused_and_untouched(tmp_path):
    path = tmp_path / "other.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE batches (id TEXT)")
        connection.execute("CREATE TABLE schema_migrations (version INTEGER)")
        connection.execute("INSERT INTO schema_migrations VALUES (1)")
    before = path.read_bytes()
    with pytest.raises(ForeignDatabaseError):
        BoxStore.open(path)
    assert _tables(path) == {"batches", "schema_migrations"}
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["other.sqlite3"]


def test_non_sqlite_file_is_refused_and_untouched(tmp_path):
    path = tmp_path / "notes.sqlite3"
    path.write_bytes(b"this is not a SQLite database\n" * 200)
    before = path.read_bytes()
    with pytest.raises(ForeignDatabaseError, match="not a SQLite database"):
        BoxStore.open(path)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.sqlite3"]


# ── M2.2 / R1: header check before sqlite3.connect ───────────────────────────


def _files(directory) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir()) if p.is_file()}


@pytest.fixture
def no_sqlite_connect(monkeypatch):
    """هر مسیرِ ردشده باید پیش از هر sqlite3.connect متوقف شود."""

    def refuse(*args, **kwargs):
        raise AssertionError("sqlite3.connect was called on a refusal path")

    monkeypatch.setattr(sqlite3, "connect", refuse)


def _assert_refused(path, match: str) -> None:
    directory = path.parent
    before = _files(directory)
    with pytest.raises(ForeignDatabaseError, match=match):
        BoxStore.open(path)
    assert _files(directory) == before


def _foreign_wal_with_frames(directory) -> Path:
    """کپیِ یک پایگاهِ WALِ بیگانه در لحظه‌ای که frameها هنوز checkpoint نشده‌اند."""
    source = directory / "source"
    source.mkdir()
    live = sqlite3.connect(source / "foreign.db", isolation_level=None)
    live.execute("PRAGMA journal_mode = WAL")
    live.execute("PRAGMA wal_autocheckpoint = 0")
    live.execute("CREATE TABLE batches (x)")
    live.execute("INSERT INTO batches VALUES (1)")
    crashed = directory / "crashed"
    crashed.mkdir()
    for item in source.iterdir():
        shutil.copy(item, crashed / item.name)
    live.close()
    assert {p.name for p in crashed.iterdir()} == {"foreign.db", "foreign.db-wal", "foreign.db-shm"}
    return crashed / "foreign.db"


def _foreign_with_hot_journal(directory) -> Path:
    """کپیِ یک پایگاهِ بیگانه در میانه‌ی تراکنشی که صفحه‌هایش را روی دیسک نوشته است."""
    source = directory / "source"
    source.mkdir()
    live = sqlite3.connect(source / "foreign.db", isolation_level=None)
    live.execute("CREATE TABLE batches (x)")
    live.execute("INSERT INTO batches VALUES (1)")
    live.execute("PRAGMA cache_size = 1")
    live.execute("BEGIN")
    live.execute(
        "INSERT INTO batches SELECT randomblob(4000) FROM "
        "(WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 200) "
        "SELECT i FROM n)"
    )
    crashed = directory / "crashed"
    crashed.mkdir()
    for item in source.iterdir():
        shutil.copy(item, crashed / item.name)
    live.execute("ROLLBACK")
    live.close()
    assert {p.name for p in crashed.iterdir()} == {"foreign.db", "foreign.db-journal"}
    return crashed / "foreign.db"


class TestFileCheck:
    def test_missing_file_gets_the_box_id_in_the_main_file_before_wal(self, tmp_path):
        path = tmp_path / "box.sqlite3"
        with BoxStore.open(path) as first:
            first.create_case(NewCase(title="a", payload={}))
            header = path.read_bytes()[:100]
            assert header.startswith(db.SQLITE_SIGNATURE)
            assert int.from_bytes(header[68:72], "big", signed=True) == APPLICATION_ID
            # اتصالِ دوم در حالی که WAL و فایل‌های جانبی زنده‌اند
            assert Path(f"{path}-wal").exists()
            with BoxStore.open(path) as second:
                second.create_case(NewCase(title="b", payload={}))
                assert len(second.list_cases()) == 2

    def test_empty_file_is_accepted(self, tmp_path):
        path = tmp_path / "box.sqlite3"
        path.write_bytes(b"")
        with BoxStore.open(path) as store:
            assert store.schema_version == LATEST_VERSION

    def test_check_file_reports_new_and_existing(self, tmp_path):
        path = tmp_path / "box.sqlite3"
        assert db.check_file(path) is True
        BoxStore.open(path).close()
        assert db.check_file(path) is False

    @pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
    def test_empty_main_with_sidecar_is_refused(self, tmp_path, no_sqlite_connect, suffix):
        path = tmp_path / "box.sqlite3"
        path.write_bytes(b"")
        Path(f"{path}{suffix}").write_bytes(b"leftover")
        _assert_refused(path, "missing or empty")

    def test_missing_main_with_sidecar_is_refused(self, tmp_path, no_sqlite_connect):
        path = tmp_path / "box.sqlite3"
        Path(f"{path}-journal").write_bytes(b"leftover")
        _assert_refused(path, "missing or empty")
        assert not path.exists()

    def test_foreign_sqlite_without_sidecars(self, tmp_path):
        path = tmp_path / "foreign.db"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE batches (x)")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("connect called"))
            _assert_refused(path, "not a TestCase Box database")

    def test_blank_sqlite_with_application_id_zero_is_refused(self, tmp_path):
        path = tmp_path / "blank.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA user_version = 1")
        assert path.stat().st_size > 0
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("connect called"))
            _assert_refused(path, "not a TestCase Box database")

    def test_other_application_id_is_refused(self, tmp_path):
        path = tmp_path / "other.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA application_id = 7")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("connect called"))
            _assert_refused(path, "not a TestCase Box database")

    def test_foreign_wal_with_uncheckpointed_frames(self, tmp_path):
        path = _foreign_wal_with_frames(tmp_path)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("connect called"))
            _assert_refused(path, "not a TestCase Box database")

    def test_foreign_with_hot_rollback_journal(self, tmp_path):
        path = _foreign_with_hot_journal(tmp_path)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("connect called"))
            _assert_refused(path, "not a TestCase Box database")

    def test_wrong_signature(self, tmp_path, no_sqlite_connect):
        path = tmp_path / "box.sqlite3"
        path.write_bytes(b"SQLite format 2\x00" + b"\x00" * 200)
        _assert_refused(path, "not a SQLite database")

    def test_truncated_header(self, tmp_path, no_sqlite_connect):
        path = tmp_path / "box.sqlite3"
        path.write_bytes(db.SQLITE_SIGNATURE + b"\x00" * 20)
        _assert_refused(path, "truncated")
