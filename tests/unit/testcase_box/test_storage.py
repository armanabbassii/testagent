"""
تست‌های مخزنِ SQLiteِ TestCase Box — همه روی پایگاه‌های موقت (tmp_path).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from src.testcase_box import models as m
from src.testcase_box.db import (
    ImmutableRecordError,
    IntegrityViolationError,
    NotFoundError,
    StorageError,
)
from src.testcase_box.models import (
    InvalidTransitionError,
    NewCase,
    NewLink,
    NewRun,
    NewRunCase,
    NewRunUnit,
    NewSource,
    NewVersion,
)
from src.testcase_box.storage import ArchivedCaseError, BoxStore


@pytest.fixture
def store(tmp_path):
    with BoxStore.open(tmp_path / "box.sqlite3") as opened:
        yield opened


def _payload(path: str = "/a", script: str | None = None) -> dict:
    item = {"name": "item", "request": {"method": "GET", "url": {"raw": "{{base_url}}" + path}}}
    if script is not None:
        item["event"] = [{"listen": "test", "script": {"exec": [script]}}]
    return item


def _case(store: BoxStore, title: str = "Case", **kwargs) -> m.BoxCase:
    payload = kwargs.pop("payload", _payload())
    return store.create_case(NewCase(title=title, payload=payload, **kwargs))


def _count(store: BoxStore, table: str) -> int:
    return store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _single_run(store: BoxStore, *cases: m.BoxCase) -> m.BoxRun:
    return store.create_run(
        NewRun(
            units=(
                NewRunUnit(
                    1,
                    tuple(
                        NewRunCase(c.id, c.current_version_id, order)
                        for order, c in enumerate(cases, start=1)
                    ),
                ),
            )
        )
    )


# ── cases and versions ────────────────────────────────────────────────────────


class TestCases:
    def test_create_and_retrieve(self, store):
        case = _case(store, "Login works", description="desc", created_by=None)
        assert case.key == f"BX-{case.id:06d}"
        assert case.status == m.CASE_ACTIVE
        assert case.created_at.endswith("Z")
        version = store.current_version(case.id)
        assert version.version_number == 1
        assert version.payload == _payload()
        assert version.fingerprint == m.sha256_json(_payload())
        assert version.script_fingerprint is None
        assert version.created_by is None
        assert store.list_cases() == [case]
        assert store.list_cases(m.CASE_ARCHIVED) == []

    def test_missing_case(self, store):
        with pytest.raises(NotFoundError):
            store.get_case(999)

    def test_update_creates_new_version_and_keeps_old(self, store):
        case = _case(store)
        first = store.current_version(case.id)
        second = store.add_version(case.id, NewVersion(payload=_payload("/b")))
        assert second.version_number == 2
        assert second.fingerprint != first.fingerprint
        assert store.get_case(case.id).current_version_id == second.id
        assert [v.id for v in store.versions(case.id)] == [first.id, second.id]
        assert store.get_version(first.id) == first

    def test_explicit_fingerprint_is_kept(self, store):
        digest = "a" * 64
        case = _case(store, fingerprint=digest)
        assert store.current_version(case.id).fingerprint == digest
        assert [v.case_id for v in store.versions_with_fingerprint(digest)] == [case.id]

    def test_details_are_editable_without_a_new_version(self, store):
        case = _case(store)
        updated = store.update_case_details(case.id, title="Renamed", description="new")
        assert (updated.title, updated.description) == ("Renamed", "new")
        assert len(store.versions(case.id)) == 1
        with pytest.raises(ValueError):
            store.update_case_details(case.id, title=" ")

    def test_versions_are_immutable(self, store):
        case = _case(store)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("UPDATE box_case_versions SET payload_json = '{}'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("DELETE FROM box_case_versions WHERE case_id = ?", (case.id,))
        with pytest.raises(ImmutableRecordError):
            store._write("UPDATE box_case_versions SET created_by = 'x'")

    def test_version_number_is_unique_per_case(self, store):
        case = _case(store)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            store._db.execute(
                "INSERT INTO box_case_versions (case_id, version_number, payload_json, "
                "fingerprint, created_at) VALUES (?, 1, '{}', ?, 'now')",
                (case.id, "b" * 64),
            )

    def test_current_version_cannot_belong_to_another_case(self, store):
        first, second = _case(store, "A"), _case(store, "B")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            store._db.execute(
                "UPDATE box_cases SET current_version_id = ? WHERE id = ?",
                (second.current_version_id, first.id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="current version"):
            store._db.execute("UPDATE box_cases SET current_version_id = NULL")

    def test_cases_cannot_be_deleted(self, store):
        case = _case(store)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("DELETE FROM box_cases WHERE id = ?", (case.id,))

    def test_database_rejects_invalid_values(self, store):
        case = _case(store)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            store._db.execute("UPDATE box_cases SET status = 'DELETED' WHERE id = ?", (case.id,))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            store._db.execute(
                "INSERT INTO box_case_versions (case_id, version_number, payload_json, "
                "fingerprint, created_at) VALUES (?, 9, '[1]', ?, 'now')",
                (case.id, "c" * 64),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            store._db.execute(
                "INSERT INTO box_case_versions (case_id, version_number, payload_json, "
                "fingerprint, created_at) VALUES (?, 9, '{}', 'not-a-hash', 'now')",
                (case.id,),
            )


class TestArchive:
    def test_archive_keeps_versions_sources_and_runs(self, store):
        case = _case(store, sources=(NewSource(m.SOURCE_MANUAL, "typed"),))
        store.add_version(case.id, NewVersion(payload=_payload("/b")))
        run = _single_run(store, store.get_case(case.id))

        archived = store.archive_case(case.id)
        assert archived.archived
        assert store.archive_case(case.id) == archived  # idempotent
        assert len(store.versions(case.id)) == 2
        assert len(store.sources(case.id)) == 1
        assert [v.case_id for v in store.pinned_versions(run.id)] == [case.id]
        assert store.list_cases(m.CASE_ACTIVE) == []
        assert store.list_cases(m.CASE_ARCHIVED) == [archived]

    def test_archived_case_takes_no_new_version_until_restored(self, store):
        case = _case(store)
        store.archive_case(case.id)
        with pytest.raises(ArchivedCaseError):
            store.add_version(case.id, NewVersion(payload=_payload("/b")))
        assert len(store.versions(case.id)) == 1
        store.restore_case(case.id)
        assert store.add_version(case.id, NewVersion(payload=_payload("/b"))).version_number == 2


# ── provenance ────────────────────────────────────────────────────────────────


class TestSources:
    def test_sources_are_recorded_per_version(self, store):
        batch_source = NewSource(
            m.SOURCE_BATCH,
            "batch-1/PODIUM-1/execution-7/TC-001",
            {"batch_id": "batch-1", "issue_key": "PODIUM-1", "attempt": 2},
        )
        case = _case(store, sources=(batch_source,))
        version = store.add_version(
            case.id,
            NewVersion(
                payload=_payload("/b"), sources=(NewSource(m.SOURCE_IMPORT, "job-3/row-4"),)
            ),
        )
        sources = store.sources(case.id)
        assert [(s.source_type, s.case_version_id) for s in sources] == [
            (m.SOURCE_BATCH, case.current_version_id),
            (m.SOURCE_IMPORT, version.id),
        ]
        assert sources[0].metadata == {"attempt": 2, "batch_id": "batch-1", "issue_key": "PODIUM-1"}
        assert [
            s.case_id for s in store.find_sources(m.SOURCE_BATCH, batch_source.source_reference)
        ] == [case.id]

    def test_case_without_batch_source(self, store):
        case = _case(store)
        assert store.sources(case.id) == []
        source = store.add_source(case.id, NewSource(m.SOURCE_MANUAL, "ui"))
        assert source.case_version_id is None

    def test_duplicate_source_is_rejected(self, store):
        case = _case(store, sources=(NewSource(m.SOURCE_IMPORT, "job-1/row-1"),))
        with pytest.raises(IntegrityViolationError, match="UNIQUE"):
            store.add_source(case.id, NewSource(m.SOURCE_IMPORT, "job-1/row-1"))

    def test_source_version_must_belong_to_case(self, store):
        first, second = _case(store, "A"), _case(store, "B")
        with pytest.raises(IntegrityViolationError, match="FOREIGN KEY"):
            store.add_source(
                first.id,
                NewSource(m.SOURCE_MANUAL, "x"),
                case_version_id=second.current_version_id,
            )

    def test_source_type_is_extensible_in_the_database(self, store):
        """فهرستِ نوع‌ها فقط در Python است؛ نوعِ آینده بی‌migration ذخیره می‌شود."""
        case = _case(store)
        store._db.execute(
            "INSERT INTO box_case_sources (case_id, source_type, source_reference, "
            "source_metadata_json, created_at) VALUES (?, 'FUTURE_TYPE', 'r', '{}', 'now')",
            (case.id,),
        )
        assert [s.source_type for s in store.sources(case.id)] == ["FUTURE_TYPE"]

    def test_sources_are_append_only(self, store):
        case = _case(store, sources=(NewSource(m.SOURCE_MANUAL, "x"),))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("UPDATE box_case_sources SET source_reference = 'y'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("DELETE FROM box_case_sources WHERE case_id = ?", (case.id,))


def _sourced(payload=None, reference="batch/b/DEMO-1/execution_1/TC-001", **kwargs) -> NewCase:
    payload = payload or _payload()
    metadata = {"item_fingerprint": m.sha256_json(payload), **kwargs.pop("metadata", {})}
    sources = kwargs.pop("sources", (NewSource(m.SOURCE_BATCH, reference, metadata),))
    return NewCase(title="Imported", payload=payload, sources=sources, **kwargs)


class TestCreateCaseForSource:
    def test_creates_case_version_and_source_once(self, store):
        created = store.create_case_for_source(_sourced())
        assert created.outcome == "CREATED" and created.created
        (source,) = store.sources(created.case.id)
        assert source.case_version_id == created.case.current_version_id
        again = store.create_case_for_source(_sourced())
        assert again.outcome == "ALREADY_PRESENT" and again.case.id == created.case.id
        assert again.existing_sources == (source,)
        assert [
            _count(store, t) for t in ("box_cases", "box_case_versions", "box_case_sources")
        ] == [
            1,
            1,
            1,
        ]

    def test_identity_is_checked_before_the_fingerprint(self, store):
        store.create_case_for_source(_sourced())
        changed = store.create_case_for_source(_sourced(payload=_payload("/other")))
        assert changed.outcome == "SOURCE_CONFLICT" and changed.case is None
        assert _count(store, "box_cases") == 1 and _count(store, "box_case_versions") == 1

    def test_same_payload_under_another_identity_is_a_new_case(self, store):
        store.create_case_for_source(_sourced())
        other = store.create_case_for_source(
            _sourced(reference="batch/b/DEMO-1/execution_2/TC-001")
        )
        assert other.outcome == "CREATED"

    def test_archived_case_is_already_present_and_stays_archived(self, store):
        case = store.create_case_for_source(_sourced()).case
        store.archive_case(case.id)
        again = store.create_case_for_source(_sourced())
        assert again.outcome == "ALREADY_PRESENT" and again.case.archived
        assert store.get_case(case.id).archived

    def test_later_versions_do_not_change_the_identity_check(self, store):
        case = store.create_case_for_source(_sourced()).case
        store.add_version(case.id, NewVersion(payload=_payload("/edited")))
        assert store.create_case_for_source(_sourced()).outcome == "ALREADY_PRESENT"

    def test_unreadable_recorded_fingerprint_is_a_conflict(self, store):
        case = _case(
            store,
            payload=_payload(),
            sources=(NewSource(m.SOURCE_BATCH, "batch/b/DEMO-1/execution_1/TC-001"),),
        )
        result = store.create_case_for_source(_sourced())
        assert result.outcome == "SOURCE_CONFLICT"
        assert result.existing_sources[0].case_id == case.id

    def test_identity_on_two_cases_is_ambiguous(self, store):
        reference = "batch/b/DEMO-1/execution_1/TC-001"
        for title in ("A", "B"):
            _case(store, title, sources=(NewSource(m.SOURCE_BATCH, reference),))
        result = store.create_case_for_source(_sourced(reference=reference))
        assert result.outcome == "SOURCE_AMBIGUOUS"
        assert _count(store, "box_cases") == 2

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"sources": ()},
            {"sources": (NewSource(m.SOURCE_IMPORT, "job-1/row-1"),)},
            {
                "sources": (
                    NewSource(m.SOURCE_BATCH, "batch/a", {"item_fingerprint": "x"}),
                    NewSource(m.SOURCE_BATCH, "batch/b", {"item_fingerprint": "x"}),
                )
            },
            {
                "sources": (
                    NewSource(m.SOURCE_BATCH, "batch/a"),
                    NewSource(m.SOURCE_MANUAL, "ui"),
                )
            },
            {"metadata": {"item_fingerprint": "0" * 64}},
            {"fingerprint": "0" * 64},
        ],
        ids=["none", "not-batch", "two-batch", "extra-source", "bad-item-fp", "bad-version-fp"],
    )
    def test_contract_violations_write_nothing(self, store, kwargs):
        with pytest.raises(ValueError):
            store.create_case_for_source(_sourced(**kwargs))
        assert _count(store, "box_cases") == 0

    def test_links_are_not_accepted(self, store):
        target = _case(store)
        with pytest.raises(ValueError, match="links"):
            store.create_case_for_source(_sourced(links=(NewLink(target.id),)))
        assert _count(store, "box_cases") == 1

    def test_failure_rolls_back_everything(self, store, monkeypatch):
        def failing(*args, **kwargs):
            raise IntegrityViolationError("injected")

        monkeypatch.setattr(BoxStore, "_insert_source", failing)
        with pytest.raises(IntegrityViolationError):
            store.create_case_for_source(_sourced())
        assert [_count(store, t) for t in ("box_cases", "box_case_versions")] == [0, 0]
        assert not store._db.in_transaction

    def test_ordinary_create_case_is_unchanged(self, store):
        reference = "batch/b/DEMO-1/execution_1/TC-001"
        first = _case(store, sources=(NewSource(m.SOURCE_BATCH, reference),))
        second = _case(store, sources=(NewSource(m.SOURCE_BATCH, reference),))
        assert first.id != second.id  # مسیرهای معمولی همان رفتارِ M2 را دارند


# ── links ─────────────────────────────────────────────────────────────────────


class TestLinks:
    def test_create_with_links_and_list(self, store):
        producer = _case(store, "producer")
        consumer = _case(store, "consumer", links=(NewLink(producer.id, metadata={"var": "id"}),))
        links = store.links(consumer.id)
        assert [(lk.source_case_id, lk.target_case_id, lk.link_type) for lk in links] == [
            (consumer.id, producer.id, m.LINK_DEPENDS_ON)
        ]
        assert store.links(producer.id) == links
        assert links[0].metadata == {"var": "id"}

    def test_self_link_is_rejected(self, store):
        case = _case(store)
        with pytest.raises(IntegrityViolationError, match="cycle|CHECK"):
            store.add_link(case.id, NewLink(case.id))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            store._db.execute(
                "INSERT INTO box_case_links (source_case_id, target_case_id, link_type, "
                "metadata_json, created_at) VALUES (?, ?, 'RELATED', '{}', 'now')",
                (case.id, case.id),
            )

    def test_duplicate_link_is_rejected(self, store):
        a, b = _case(store, "a"), _case(store, "b")
        store.add_link(a.id, NewLink(b.id))
        with pytest.raises(IntegrityViolationError, match="UNIQUE"):
            store.add_link(a.id, NewLink(b.id))
        store.add_link(a.id, NewLink(b.id, m.LINK_RELATED))

    def test_dependency_cycle_is_rejected(self, store):
        a, b, c = _case(store, "a"), _case(store, "b"), _case(store, "c")
        store.add_link(a.id, NewLink(b.id))
        store.add_link(b.id, NewLink(c.id))
        with pytest.raises(IntegrityViolationError, match="cycle"):
            store.add_link(c.id, NewLink(a.id))
        store.add_link(c.id, NewLink(a.id, m.LINK_RELATED))

    def test_link_to_missing_case(self, store):
        case = _case(store)
        with pytest.raises(NotFoundError):
            store.add_link(case.id, NewLink(999))

    def test_remove_link(self, store):
        a, b = _case(store, "a"), _case(store, "b")
        link = store.add_link(a.id, NewLink(b.id))
        store.remove_link(link.id)
        assert store.links(a.id) == []
        with pytest.raises(NotFoundError):
            store.remove_link(link.id)


# ── import jobs ───────────────────────────────────────────────────────────────


class TestImportJobs:
    def test_lifecycle(self, store):
        job = store.create_import_job("POSTMAN", "collection.json")
        assert (job.status, job.completed_at, job.summary) == (m.IMPORT_PENDING, None, {})
        done = store.finish_import_job(job.id, m.IMPORT_COMPLETED, {"accepted": 3, "rejected": 1})
        assert done.status == m.IMPORT_COMPLETED
        assert done.completed_at is not None
        assert done.summary == {"accepted": 3, "rejected": 1}

    def test_final_job_is_immutable(self, store):
        job = store.finish_import_job(store.create_import_job("CSV", "a.csv").id, m.IMPORT_FAILED)
        with pytest.raises(InvalidTransitionError):
            store.finish_import_job(job.id, m.IMPORT_COMPLETED)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("UPDATE box_import_jobs SET filename = 'b.csv'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("DELETE FROM box_import_jobs")

    def test_import_job_links_cases_through_sources(self, store):
        job = store.create_import_job("JSON", "cases.json")
        case = _case(
            store,
            sources=(
                NewSource(m.SOURCE_IMPORT, f"job-{job.id}/test_cases/0", {"import_job_id": job.id}),
            ),
        )
        assert store.sources(case.id)[0].metadata["import_job_id"] == job.id


# ── script approvals ──────────────────────────────────────────────────────────


class TestScriptApprovals:
    def test_version_without_scripts_needs_no_approval(self, store):
        case = _case(store)
        assert store.script_approval_state(case.current_version_id) == m.SCRIPTS_NOT_REQUIRED
        with pytest.raises(IntegrityViolationError, match="no scripts"):
            store.record_script_decision(case.current_version_id, "a" * 64, m.APPROVAL_APPROVED)

    def test_script_fingerprint_is_computed_from_payload(self, store):
        case = _case(store, payload=_payload(script="pm.test('ok', () => {});"))
        version = store.current_version(case.id)
        assert version.has_scripts
        assert version.script_fingerprint == m.script_fingerprint(version.payload)

    def test_approval_tied_to_version_and_fingerprint(self, store):
        case = _case(store, payload=_payload(script="pm.test('a', () => {});"))
        v1 = store.current_version(case.id)
        assert store.script_approval_state(v1.id) == m.SCRIPTS_PENDING

        approval = store.record_script_decision(
            v1.id, v1.script_fingerprint, m.APPROVAL_APPROVED, [{"rule": "env-mutation"}]
        )
        assert approval.approved_at is not None
        assert approval.review_findings == [{"rule": "env-mutation"}]
        assert store.scripts_approved(v1.id)

        changed = store.add_version(case.id, NewVersion(payload=_payload(script="pm.test('b');")))
        assert changed.script_fingerprint != v1.script_fingerprint
        assert store.script_approval_state(changed.id) == m.SCRIPTS_PENDING
        assert store.scripts_approved(v1.id)  # historical version keeps its decision

    def test_approval_does_not_carry_over_even_with_identical_scripts(self, store):
        script = "pm.test('same', () => {});"
        case = _case(store, payload=_payload(script=script))
        v1 = store.current_version(case.id)
        store.record_script_decision(v1.id, v1.script_fingerprint, m.APPROVAL_APPROVED)
        v2 = store.add_version(case.id, NewVersion(payload=_payload("/other", script=script)))
        assert v2.script_fingerprint == v1.script_fingerprint
        assert store.script_approval_state(v2.id) == m.SCRIPTS_PENDING

    def test_wrong_fingerprint_is_rejected(self, store):
        case = _case(store, payload=_payload(script="x()"))
        version = store.current_version(case.id)
        with pytest.raises(IntegrityViolationError, match="do not match"):
            store.record_script_decision(version.id, "f" * 64, m.APPROVAL_APPROVED)
        with pytest.raises(sqlite3.IntegrityError, match="fingerprint"):
            store._db.execute(
                "INSERT INTO box_script_approvals (case_version_id, script_fingerprint, "
                "approval_status, review_findings_json, approved_at, created_at) "
                "VALUES (?, ?, 'APPROVED', '[]', 'now', 'now')",
                (version.id, "f" * 64),
            )
        assert store.script_approvals(version.id) == []

    def test_latest_decision_wins_and_history_is_kept(self, store):
        case = _case(store, payload=_payload(script="x()"))
        version = store.current_version(case.id)
        store.record_script_decision(version.id, version.script_fingerprint, m.APPROVAL_APPROVED)
        revoked = store.record_script_decision(
            version.id, version.script_fingerprint, m.APPROVAL_REVOKED
        )
        assert revoked.approved_at is None
        assert store.script_approval_state(version.id) == m.APPROVAL_REVOKED
        assert not store.scripts_approved(version.id)
        assert len(store.script_approvals(version.id)) == 2
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("UPDATE box_script_approvals SET approval_status = 'APPROVED'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("DELETE FROM box_script_approvals")


# ── runs ──────────────────────────────────────────────────────────────────────


class TestRuns:
    def test_run_unit_case_relationships(self, store):
        a, b, c = _case(store, "a"), _case(store, "b"), _case(store, "c")
        run = store.create_run(
            NewRun(
                units=(
                    NewRunUnit(2, (NewRunCase(c.id, c.current_version_id, 1),)),
                    NewRunUnit(
                        1,
                        (
                            NewRunCase(b.id, b.current_version_id, 2),
                            NewRunCase(a.id, a.current_version_id, 1),
                        ),
                        {"profile": "default"},
                    ),
                ),
                metadata={"trigger": "manual"},
            )
        )
        assert (run.status, run.metadata, run.started_at) == (
            m.RUN_QUEUED,
            {"trigger": "manual"},
            None,
        )
        units = store.run_units(run.id)
        assert [(u.unit_number, u.status) for u in units] == [
            (1, m.UNIT_QUEUED),
            (2, m.UNIT_QUEUED),
        ]
        assert units[0].metadata == {"profile": "default"}
        assert [rc.case_id for rc in store.run_cases(run.id)] == [a.id, b.id, c.id]
        assert [rc.case_id for rc in store.run_cases(run.id, unit_id=units[1].id)] == [c.id]
        assert [v.case_id for v in store.pinned_versions(run.id)] == [a.id, b.id, c.id]

    def test_pinned_versions_survive_new_versions_and_archive(self, store):
        case = _case(store)
        original = store.current_version(case.id)
        run = _single_run(store, case)
        store.add_version(case.id, NewVersion(payload=_payload("/v2")))
        store.archive_case(case.id)
        assert store.pinned_versions(run.id) == [original]

        rerun = store.create_run(
            NewRun(
                units=(NewRunUnit(1, (NewRunCase(case.id, original.id, 1),)),),
                retry_of_run_id=run.id,
            )
        )
        assert rerun.retry_of_run_id == run.id
        assert store.pinned_versions(rerun.id) == [original]

    def test_run_case_version_must_belong_to_case(self, store):
        a, b = _case(store, "a"), _case(store, "b")
        with pytest.raises(IntegrityViolationError, match="does not belong"):
            store.create_run(
                NewRun(units=(NewRunUnit(1, (NewRunCase(a.id, b.current_version_id, 1),)),))
            )
        run = _single_run(store, a)
        unit = store.run_units(run.id)[0]
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            store._db.execute(
                "INSERT INTO box_run_cases (run_id, run_unit_id, case_id, case_version_id, "
                "execution_order, status, result_metadata_json) "
                "VALUES (?, ?, ?, ?, 5, 'QUEUED', '{}')",
                (run.id, unit.id, b.id, a.current_version_id),
            )

    def test_run_case_unit_must_belong_to_run(self, store):
        a, b = _case(store, "a"), _case(store, "b")
        first, second = _single_run(store, a), _single_run(store, b)
        foreign_unit = store.run_units(second.id)[0]
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            store._db.execute(
                "INSERT INTO box_run_cases (run_id, run_unit_id, case_id, case_version_id, "
                "execution_order, status, result_metadata_json) "
                "VALUES (?, ?, ?, ?, 9, 'QUEUED', '{}')",
                (first.id, foreign_unit.id, b.id, b.current_version_id),
            )

    def test_pinned_columns_cannot_change(self, store):
        case = _case(store)
        version_2 = store.add_version(case.id, NewVersion(payload=_payload("/v2")))
        run = store.create_run(
            NewRun(units=(NewRunUnit(1, (NewRunCase(case.id, store.versions(case.id)[0].id, 1),)),))
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute(
                "UPDATE box_run_cases SET case_version_id = ? WHERE run_id = ?",
                (version_2.id, run.id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("UPDATE box_run_units SET unit_number = 7")

    def test_missing_retry_source(self, store):
        case = _case(store)
        with pytest.raises(NotFoundError):
            store.create_run(
                NewRun(
                    units=(NewRunUnit(1, (NewRunCase(case.id, case.current_version_id, 1),)),),
                    retry_of_run_id=999,
                )
            )
        assert _count(store, "box_runs") == 0

    def test_not_executable_case_is_final_from_creation(self, store):
        case = _case(store)
        run = store.create_run(
            NewRun(
                units=(
                    NewRunUnit(
                        1,
                        (
                            NewRunCase(
                                case.id,
                                case.current_version_id,
                                1,
                                status=m.CASE_RUN_NOT_EXECUTABLE,
                                result_metadata={"reason": "SCRIPTS_NOT_APPROVED"},
                            ),
                        ),
                    ),
                )
            )
        )
        run_case = store.run_cases(run.id)[0]
        assert run_case.result_metadata == {"reason": "SCRIPTS_NOT_APPROVED"}
        with pytest.raises(InvalidTransitionError):
            store.set_run_case_status(run_case.id, m.CASE_RUN_PASS)


class TestStatusTransitions:
    def test_full_happy_path(self, store):
        case = _case(store)
        run = _single_run(store, case)
        unit = store.run_units(run.id)[0]
        run_case = store.run_cases(run.id)[0]

        running = store.set_run_status(run.id, m.RUN_RUNNING)
        assert running.started_at is not None and running.finished_at is None
        assert store.set_unit_status(unit.id, m.UNIT_RUNNING).started_at is not None
        passed = store.set_run_case_status(
            run_case.id, m.CASE_RUN_PASS, result_metadata={"status_code": 200}
        )
        assert passed.result_metadata == {"status_code": 200}
        assert store.set_unit_status(unit.id, m.UNIT_PASS).finished_at is not None
        done = store.set_run_status(run.id, m.RUN_COMPLETED, metadata={"passed": 1})
        assert done.final and done.finished_at is not None
        assert done.started_at == running.started_at
        assert done.metadata == {"passed": 1}

    @pytest.mark.parametrize(
        ("current", "target"),
        [(None, m.RUN_COMPLETED), (None, m.RUN_QUEUED), (m.RUN_RUNNING, m.RUN_QUEUED)],
    )
    def test_invalid_run_transitions(self, store, current, target):
        run = _single_run(store, _case(store))
        if current:
            store.set_run_status(run.id, current)
        with pytest.raises(InvalidTransitionError):
            store.set_run_status(run.id, target)

    def test_invalid_unit_and_case_transitions(self, store):
        run = _single_run(store, _case(store))
        unit = store.run_units(run.id)[0]
        run_case = store.run_cases(run.id)[0]
        with pytest.raises(InvalidTransitionError):
            store.set_unit_status(unit.id, m.UNIT_PASS)
        with pytest.raises(InvalidTransitionError):
            store.set_run_case_status(run_case.id, m.CASE_RUN_QUEUED)
        with pytest.raises(InvalidTransitionError):
            store.set_run_case_status(run_case.id, m.CASE_RUN_NOT_EXECUTABLE)

    def test_final_records_cannot_change(self, store):
        run = _single_run(store, _case(store))
        unit = store.run_units(run.id)[0]
        run_case = store.run_cases(run.id)[0]
        store.set_run_case_status(run_case.id, m.CASE_RUN_NOT_RUN)
        store.set_unit_status(unit.id, m.UNIT_NOT_RUN)
        store.set_run_status(run.id, m.RUN_INTERRUPTED)

        with pytest.raises(InvalidTransitionError):
            store.set_run_status(run.id, m.RUN_RUNNING)
        with pytest.raises(ImmutableRecordError):
            store.set_unit_status(unit.id, m.UNIT_RUNNING)
        with pytest.raises(ImmutableRecordError):
            store.set_run_case_status(run_case.id, m.CASE_RUN_PASS)
        for table in ("box_runs", "box_run_units", "box_run_cases"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                store._db.execute(f"UPDATE {table} SET status = status")
        with pytest.raises(ImmutableRecordError):
            store._write("UPDATE box_runs SET metadata_json = '{}'")

    def test_database_enforces_transitions_directly(self, store):
        run = _single_run(store, _case(store))
        store._db.execute("UPDATE box_runs SET status = 'RUNNING' WHERE id = ?", (run.id,))
        with pytest.raises(sqlite3.IntegrityError, match="invalid run status transition"):
            store._db.execute("UPDATE box_runs SET status = 'QUEUED' WHERE id = ?", (run.id,))
        with pytest.raises(sqlite3.IntegrityError, match="invalid run case status transition"):
            store._db.execute("UPDATE box_run_cases SET status = 'NOT_EXECUTABLE'")
        # caseِ بسته تا فقط triggerِ گذار (نه triggerِ v3 «unit با caseِ باز») درگیر شود
        store._db.execute("UPDATE box_run_cases SET status = 'NOT_RUN'")
        with pytest.raises(sqlite3.IntegrityError, match="invalid run unit status transition"):
            store._db.execute("UPDATE box_run_units SET status = 'PASS'")

    def test_run_history_cannot_be_deleted(self, store):
        _single_run(store, _case(store))
        for table in ("box_run_cases", "box_run_units", "box_runs"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                store._db.execute(f"DELETE FROM {table}")


# ── transactions and JSON ─────────────────────────────────────────────────────


class TestTransactions:
    def test_failed_create_case_leaves_nothing(self, store):
        with pytest.raises(NotFoundError):
            _case(store, sources=(NewSource(m.SOURCE_MANUAL, "x"),), links=(NewLink(999),))
        for table in ("box_cases", "box_case_versions", "box_case_sources", "box_case_links"):
            assert _count(store, table) == 0

    def test_failed_create_run_leaves_nothing(self, store):
        a, b = _case(store, "a"), _case(store, "b")
        with pytest.raises(IntegrityViolationError):
            store.create_run(
                NewRun(
                    units=(
                        NewRunUnit(1, (NewRunCase(a.id, a.current_version_id, 1),)),
                        NewRunUnit(2, (NewRunCase(b.id, a.current_version_id, 1),)),
                    )
                )
            )
        for table in ("box_runs", "box_run_units", "box_run_cases"):
            assert _count(store, table) == 0

    def test_failed_add_version_keeps_current_version(self, store):
        case = _case(store)
        with pytest.raises(IntegrityViolationError):
            store.add_version(
                case.id,
                NewVersion(
                    payload=_payload("/b"),
                    sources=(NewSource(m.SOURCE_MANUAL, "dup"), NewSource(m.SOURCE_MANUAL, "dup")),
                ),
            )
        assert store.get_case(case.id).current_version_id == case.current_version_id
        assert len(store.versions(case.id)) == 1

    def test_exception_inside_transaction_rolls_back(self, store):
        with pytest.raises(RuntimeError):
            with store.transaction():
                store._write(
                    "INSERT INTO box_runs (status, metadata_json, created_at) "
                    "VALUES ('QUEUED', '{}', 'now')"
                )
                raise RuntimeError("boom")
        assert _count(store, "box_runs") == 0


class TestJsonFields:
    def test_round_trip_of_structured_fields(self, store):
        payload = {"request": {"body": {"raw": "سلام", "n": [1, 2.5, None, True, False]}}}
        case = _case(store, payload=payload)
        assert store.current_version(case.id).payload == payload
        raw = store._db.execute(
            "SELECT payload_json FROM box_case_versions WHERE id = ?", (case.current_version_id,)
        ).fetchone()[0]
        assert raw == m.to_json(payload)

    def test_unserializable_values_are_rejected_before_writing(self, store):
        case = _case(store)
        run = _single_run(store, case)
        with pytest.raises(ValueError):
            store.set_run_status(run.id, m.RUN_RUNNING, metadata={"x": float("nan")})
        assert store.get_run(run.id).status == m.RUN_QUEUED
        with pytest.raises(ValueError):
            store.create_import_job("CSV", "a.csv", {"x": object()})
        assert _count(store, "box_import_jobs") == 0

    def test_corrupt_stored_json_is_reported(self, store):
        from src.testcase_box.db import StorageError

        case = _case(store)
        # فقط برای شبیه‌سازیِ فایلِ خراب: محافظ‌ها در همین پایگاهِ موقت برداشته می‌شوند
        store._db.execute("DROP TRIGGER tr_box_case_versions_no_update")
        store._db.execute("PRAGMA ignore_check_constraints = ON")
        store._db.execute("UPDATE box_case_versions SET payload_json = '[1]'")
        with pytest.raises(StorageError, match="Corrupt JSON"):
            store.current_version(case.id)


# ── M2.1: script detection through the store ──────────────────────────────────


class TestDetectedScriptsNeedReview:
    @pytest.mark.parametrize(
        "event",
        [
            {"listen": "test", "script": {"exec": ["x()"]}},
            [{"listen": "test", "script": {"exec": ["x()"]}}],
            [{"listen": "test", "script": "x()"}],
            [{"listen": "test", "script": ["x()"]}],
            [{"listen": "test", "script": {"src": "https://example.test/s.js"}}],
            [{"listen": "test", "script": 5}],
            "x()",
        ],
        ids=["single", "list", "string", "array", "object-src", "malformed", "scalar"],
    )
    def test_detected_scripts_are_never_not_required(self, store, event):
        case = _case(store, payload={**_payload(), "event": event})
        version = store.current_version(case.id)
        assert version.has_scripts
        assert store.script_approval_state(version.id) == m.SCRIPTS_PENDING
        store.record_script_decision(version.id, version.script_fingerprint, m.APPROVAL_APPROVED)
        assert store.scripts_approved(version.id)

    def test_reviewed_fingerprint_keyword(self, store):
        case = _case(store, payload=_payload(script="x()"))
        version = store.current_version(case.id)
        approval = store.record_script_decision(
            version.id, reviewed_fingerprint=version.script_fingerprint, status=m.APPROVAL_REJECTED
        )
        assert approval.script_fingerprint == version.script_fingerprint


# ── M2.1: run finalization ────────────────────────────────────────────────────


def _two_unit_run(store: BoxStore) -> m.BoxRun:
    a, b, c = _case(store, "a"), _case(store, "b"), _case(store, "c")
    return store.create_run(
        NewRun(
            units=(
                NewRunUnit(
                    1,
                    (
                        NewRunCase(a.id, a.current_version_id, 1),
                        NewRunCase(b.id, b.current_version_id, 2, status=m.CASE_RUN_NOT_EXECUTABLE),
                    ),
                ),
                NewRunUnit(2, (NewRunCase(c.id, c.current_version_id, 1),)),
            )
        )
    )


def _close_single_run(store: BoxStore, run: m.BoxRun) -> tuple[m.RunUnit, m.RunCase]:
    unit, run_case = store.run_units(run.id)[0], store.run_cases(run.id)[0]
    store.set_run_case_status(run_case.id, m.CASE_RUN_NOT_RUN)
    store.set_unit_status(unit.id, m.UNIT_NOT_RUN)
    store.set_run_status(run.id, m.RUN_INTERRUPTED)
    return unit, run_case


class TestRunFinalization:
    def test_finalizing_with_unfinished_children_is_rejected(self, store):
        run = _two_unit_run(store)
        store.set_run_status(run.id, m.RUN_RUNNING)
        with pytest.raises(InvalidTransitionError, match=r"2 unit\(s\) and 2 case\(s\)"):
            store.set_run_status(run.id, m.RUN_COMPLETED)

        first_unit = store.run_units(run.id)[0]
        first_case = store.run_cases(run.id, unit_id=first_unit.id)[0]
        store.set_run_case_status(first_case.id, m.CASE_RUN_PASS)
        store.set_unit_status(first_unit.id, m.UNIT_RUNNING)
        store.set_unit_status(first_unit.id, m.UNIT_PASS)
        with pytest.raises(InvalidTransitionError, match=r"1 unit\(s\) and 1 case\(s\)"):
            store.set_run_status(run.id, m.RUN_INTERRUPTED)
        assert store.get_run(run.id).status == m.RUN_RUNNING

    def test_database_rejects_finalizing_with_unfinished_children(self, store):
        run = _two_unit_run(store)
        store.set_run_status(run.id, m.RUN_RUNNING)
        for status in ("COMPLETED", "CANCELLED", "INTERRUPTED"):
            with pytest.raises(sqlite3.IntegrityError, match="unfinished"):
                store._db.execute("UPDATE box_runs SET status = ? WHERE id = ?", (status, run.id))
        assert store.get_run(run.id).status == m.RUN_RUNNING

    def test_queued_run_needs_explicit_child_cancellation(self, store):
        run = _single_run(store, _case(store))
        with pytest.raises(InvalidTransitionError):
            store.set_run_status(run.id, m.RUN_CANCELLED)
        store.set_run_case_status(store.run_cases(run.id)[0].id, m.CASE_RUN_CANCELLED)
        store.set_unit_status(store.run_units(run.id)[0].id, m.UNIT_CANCELLED)
        assert store.set_run_status(run.id, m.RUN_CANCELLED).status == m.RUN_CANCELLED

    def test_valid_finalization_keeps_every_result(self, store):
        run = _two_unit_run(store)
        store.set_run_status(run.id, m.RUN_RUNNING)
        first, second = store.run_units(run.id)
        a, _ = store.run_cases(run.id, unit_id=first.id)
        (c,) = store.run_cases(run.id, unit_id=second.id)
        store.set_unit_status(first.id, m.UNIT_RUNNING)
        store.set_run_case_status(a.id, m.CASE_RUN_FAIL, result_metadata={"failed": 1})
        store.set_unit_status(first.id, m.UNIT_FAIL)
        store.set_run_case_status(c.id, m.CASE_RUN_NOT_RUN, result_metadata={"reason": "x"})
        store.set_unit_status(second.id, m.UNIT_NOT_RUN)

        done = store.set_run_status(run.id, m.RUN_COMPLETED)
        assert done.final
        assert [(rc.status, rc.result_metadata) for rc in store.run_cases(run.id)] == [
            (m.CASE_RUN_FAIL, {"failed": 1}),
            (m.CASE_RUN_NOT_EXECUTABLE, {}),
            (m.CASE_RUN_NOT_RUN, {"reason": "x"}),
        ]
        assert [u.status for u in store.run_units(run.id)] == [m.UNIT_FAIL, m.UNIT_NOT_RUN]

    def test_children_of_a_final_run_cannot_change(self, store):
        unit, run_case = _close_single_run(store, _single_run(store, _case(store)))
        with pytest.raises(ImmutableRecordError, match="is final"):
            store.set_unit_status(unit.id, m.UNIT_RUNNING)
        with pytest.raises(ImmutableRecordError, match="is final"):
            store.set_run_case_status(run_case.id, m.CASE_RUN_PASS)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("UPDATE box_run_units SET metadata_json = json_object('x', 1)")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("UPDATE box_run_cases SET result_metadata_json = json_object('x', 1)")

    def test_no_children_can_be_added_to_a_final_run(self, store):
        run = _single_run(store, _case(store))
        unit, _ = _close_single_run(store, run)
        other = _case(store, "other")
        with pytest.raises(sqlite3.IntegrityError, match="no units can be added"):
            store._db.execute(
                "INSERT INTO box_run_units (run_id, unit_number, status, metadata_json) "
                "VALUES (?, 2, 'QUEUED', '{}')",
                (run.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="no run cases can be added"):
            store._db.execute(
                "INSERT INTO box_run_cases (run_id, run_unit_id, case_id, case_version_id, "
                "execution_order, status, result_metadata_json) "
                "VALUES (?, ?, ?, ?, 2, 'QUEUED', '{}')",
                (run.id, unit.id, other.id, other.current_version_id),
            )


# ── M2.1: initial statuses (direct SQL, bypassing BoxStore) ───────────────────


def _raw_queued_run(store: BoxStore) -> int:
    return store._db.execute(
        "INSERT INTO box_runs (status, metadata_json, created_at) VALUES ('QUEUED', '{}', 'x')"
    ).lastrowid


def _expect_initial(store: BoxStore, allowed: bool, what: str, sql: str, params: tuple) -> None:
    if allowed:
        store._db.execute(sql, params)
    else:
        with pytest.raises(sqlite3.IntegrityError, match=f"invalid initial {what} status"):
            store._db.execute(sql, params)


class TestInitialStatuses:
    @pytest.mark.parametrize("status", m.RUN_STATUSES)
    def test_runs(self, store, status):
        _expect_initial(
            store,
            status in m.RUN_INITIAL,
            "run",
            "INSERT INTO box_runs (status, metadata_json, created_at) VALUES (?, '{}', 'x')",
            (status,),
        )

    @pytest.mark.parametrize("status", m.UNIT_STATUSES)
    def test_units(self, store, status):
        _expect_initial(
            store,
            status in m.UNIT_INITIAL,
            "run unit",
            "INSERT INTO box_run_units (run_id, unit_number, status, metadata_json) "
            "VALUES (?, 1, ?, '{}')",
            (_raw_queued_run(store), status),
        )

    @pytest.mark.parametrize("status", m.RUN_CASE_STATUSES)
    def test_run_cases(self, store, status):
        case = _case(store)
        run_id = _raw_queued_run(store)
        unit_id = store._db.execute(
            "INSERT INTO box_run_units (run_id, unit_number, status, metadata_json) "
            "VALUES (?, 1, 'QUEUED', '{}')",
            (run_id,),
        ).lastrowid
        _expect_initial(
            store,
            status in m.RUN_CASE_INITIAL,
            "run case",
            "INSERT INTO box_run_cases (run_id, run_unit_id, case_id, case_version_id, "
            "execution_order, status, result_metadata_json) VALUES (?, ?, ?, ?, 1, ?, '{}')",
            (run_id, unit_id, case.id, case.current_version_id, status),
        )

    @pytest.mark.parametrize("status", m.IMPORT_STATUSES)
    def test_import_jobs(self, store, status):
        _expect_initial(
            store,
            status in m.IMPORT_INITIAL,
            "import job",
            "INSERT INTO box_import_jobs (source_type, filename, status, summary_json, "
            "created_at, completed_at) VALUES ('CSV', 'a.csv', ?, '{}', 'x', ?)",
            (status, None if status == m.IMPORT_PENDING else "x"),
        )

    def test_python_initial_statuses_match_the_database(self):
        for status in m.RUN_CASE_STATUSES:
            if status in m.RUN_CASE_INITIAL:
                NewRunCase(1, 1, 1, status=status)
            else:
                with pytest.raises(ValueError):
                    NewRunCase(1, 1, 1, status=status)
        assert {m.RUN_QUEUED} == set(m.RUN_INITIAL)
        assert {m.UNIT_QUEUED} == set(m.UNIT_INITIAL)
        assert {m.IMPORT_PENDING} == set(m.IMPORT_INITIAL)

    def test_import_job_transitions_in_the_database(self, store):
        job = store.create_import_job("CSV", "a.csv")
        store._db.execute(
            "UPDATE box_import_jobs SET status = 'CANCELLED', completed_at = 'x' WHERE id = ?",
            (job.id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="invalid import job status transition"):
            store._db.execute(
                "UPDATE box_import_jobs SET status = 'COMPLETED' WHERE id = ?", (job.id,)
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("UPDATE box_import_jobs SET filename = 'b' WHERE id = ?", (job.id,))


# ── M2.1: historical columns, connection guard, rollback ──────────────────────


class TestHistoryColumns:
    def test_run_history_columns(self, store):
        case = _case(store)
        first = _single_run(store, case)
        rerun = store.create_run(
            NewRun(
                units=(NewRunUnit(1, (NewRunCase(case.id, case.current_version_id, 1),)),),
                retry_of_run_id=first.id,
            )
        )
        for assignment in ("created_at = 'y'", "retry_of_run_id = NULL", "id = 99"):
            with pytest.raises(sqlite3.IntegrityError, match="run history columns"):
                store._db.execute(f"UPDATE box_runs SET {assignment} WHERE id = ?", (rerun.id,))
        store._db.execute(
            "UPDATE box_runs SET metadata_json = json_object('note', 1) WHERE id = ?", (rerun.id,)
        )

    def test_case_history_columns(self, store):
        case = _case(store)
        for assignment in ("created_at = 'y'", "id = 99"):
            with pytest.raises(sqlite3.IntegrityError, match="case history columns"):
                store._db.execute(f"UPDATE box_cases SET {assignment} WHERE id = ?", (case.id,))
        assert store.update_case_details(case.id, title="still editable").title == "still editable"


class TestConnectionGuards:
    def test_store_requires_foreign_keys(self, tmp_path):
        path = tmp_path / "box.sqlite3"
        BoxStore.open(path).close()
        with closing(sqlite3.connect(path)) as connection:
            with pytest.raises(StorageError, match="foreign keys"):
                BoxStore(connection, path)

    def test_original_error_survives_an_automatic_rollback(self, store):
        """SQLite گاهی خودش rollback می‌کند؛ ROLLBACKِ دوم نباید خطای اصلی را بپوشاند."""
        with pytest.raises(RuntimeError, match="original"):
            with store.transaction():
                store._db.execute("ROLLBACK")  # شبیه‌سازیِ rollbackِ خودکارِ SQLite
                raise RuntimeError("original")
        assert not store._db.in_transaction
        assert _case(store).id == 1


# ── M2.2 / R2: unit finalization ──────────────────────────────────────────────


def _open_unit(store: BoxStore, unit_status: str) -> tuple[m.RunUnit, m.RunCase]:
    """unitی با یک caseِ باز، آماده‌ی رفتن به unit_status (از QUEUED یا RUNNING)."""
    run = _single_run(store, _case(store))
    store.set_run_status(run.id, m.RUN_RUNNING)
    unit, run_case = store.run_units(run.id)[0], store.run_cases(run.id)[0]
    if unit_status not in m.UNIT_TRANSITIONS[m.UNIT_QUEUED]:
        unit = store.set_unit_status(unit.id, m.UNIT_RUNNING)
    return unit, run_case


class TestUnitFinalization:
    @pytest.mark.parametrize("status", sorted(m.UNIT_FINAL))
    def test_unit_cannot_finish_with_an_open_case(self, store, status):
        unit, run_case = _open_unit(store, status)
        with pytest.raises(InvalidTransitionError, match=r"1 case\(s\) are not final"):
            store.set_unit_status(unit.id, status)
        with pytest.raises(sqlite3.IntegrityError, match="its cases are unfinished"):
            store._db.execute("UPDATE box_run_units SET status = ? WHERE id = ?", (status, unit.id))
        assert store.get_run_unit(unit.id).status == unit.status
        assert store.get_run_case(run_case.id).status == m.CASE_RUN_QUEUED  # no rewrite

        store.set_run_case_status(run_case.id, m.CASE_RUN_NOT_RUN)
        assert store.set_unit_status(unit.id, status).status == status
        assert store.get_run_case(run_case.id).status == m.CASE_RUN_NOT_RUN

    def test_unit_with_only_not_executable_cases_can_finish(self, store):
        case = _case(store)
        run = store.create_run(
            NewRun(
                units=(
                    NewRunUnit(
                        1,
                        (
                            NewRunCase(
                                case.id,
                                case.current_version_id,
                                1,
                                status=m.CASE_RUN_NOT_EXECUTABLE,
                            ),
                        ),
                    ),
                )
            )
        )
        unit = store.run_units(run.id)[0]
        assert store.set_unit_status(unit.id, m.UNIT_NOT_RUN).status == m.UNIT_NOT_RUN

    def test_cases_of_a_final_unit_cannot_change(self, store):
        unit, run_case = _open_unit(store, m.UNIT_PASS)
        store.set_run_case_status(run_case.id, m.CASE_RUN_PASS)
        store.set_unit_status(unit.id, m.UNIT_PASS)
        assert not store.get_run(unit.run_id).final
        with pytest.raises(ImmutableRecordError, match=f"Unit {unit.id} is final"):
            store.set_run_case_status(run_case.id, m.CASE_RUN_FAIL)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._db.execute("UPDATE box_run_cases SET result_metadata_json = json_object('x', 1)")

    def test_no_case_can_be_added_to_a_final_unit(self, store):
        unit, run_case = _open_unit(store, m.UNIT_PASS)
        store.set_run_case_status(run_case.id, m.CASE_RUN_PASS)
        store.set_unit_status(unit.id, m.UNIT_PASS)
        other = _case(store, "other")
        with pytest.raises(sqlite3.IntegrityError, match="the unit is final; no run cases"):
            store._db.execute(
                "INSERT INTO box_run_cases (run_id, run_unit_id, case_id, case_version_id, "
                "execution_order, status, result_metadata_json) "
                "VALUES (?, ?, ?, ?, 2, 'QUEUED', '{}')",
                (unit.run_id, unit.id, other.id, other.current_version_id),
            )

    def test_cases_of_an_open_unit_still_move(self, store):
        unit, run_case = _open_unit(store, m.UNIT_PASS)
        moved = store.set_run_case_status(
            run_case.id, m.CASE_RUN_SKIPPED, result_metadata={"skip": "precondition"}
        )
        assert moved.status == m.CASE_RUN_SKIPPED
        assert store.get_run_unit(unit.id).status == m.UNIT_RUNNING


# ── M2.2 / L-e: COMMIT failure and rollback failure ───────────────────────────


class TestCommitFailure:
    def test_failed_commit_rolls_back_and_keeps_the_original_error(self, store):
        case = _case(store)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            with store.transaction() as connection:
                connection.execute("PRAGMA defer_foreign_keys = ON")
                connection.execute(
                    "INSERT INTO box_case_sources (case_id, source_type, source_reference, "
                    "source_metadata_json, created_at) VALUES (999, 'MANUAL', 'x', '{}', 'x')"
                )
                connection.execute(
                    "UPDATE box_cases SET title = 'changed' WHERE id = ?", (case.id,)
                )
        assert not store._db.in_transaction
        assert _count(store, "box_case_sources") == 0
        assert store.get_case(case.id).title == case.title
        assert _case(store, "still usable").id == case.id + 1

    def test_rollback_failure_is_attached_as_a_note(self):
        class BrokenRollback:
            in_transaction = True

            def execute(self, sql, *args):
                if sql == "ROLLBACK":
                    raise sqlite3.OperationalError("disk I/O error")

        store = object.__new__(BoxStore)
        store._db = BrokenRollback()
        with pytest.raises(RuntimeError, match="original") as caught:
            with store.transaction():
                raise RuntimeError("original")
        assert any("rollback also failed (OperationalError)" in n for n in caught.value.__notes__)

    def test_abort_without_open_transaction_is_a_no_op(self, store):
        from src.testcase_box.db import abort

        error = RuntimeError("x")
        abort(store._db, error)
        assert not getattr(error, "__notes__", [])
