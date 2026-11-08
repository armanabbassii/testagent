"""
تست‌های پیش‌نمایش و واردکردنِ caseهای Batch (M5): انتخابِ جزئی، تکرار و dedup، تغییرِ
منبع میانِ پیش‌نمایش و واردکردن، عنوان، سیاستِ readiness، وابستگی‌ها، اتمی بودن و
فقط‌خواندنی ماندنِ Batch.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from src.testcase_box import batch_import as bi
from src.testcase_box import models as m
from src.testcase_box import storage as st
from src.testcase_box.batch_import import ImportSelection, import_cases, preview_cases
from src.testcase_box.batch_source import AttemptRef, load_attempt
from src.testcase_box.contract import STATUS_NEEDS_SCRIPT_REVIEW, STATUS_NOT_EXECUTABLE
from src.testcase_box.migrations import MIGRATIONS
from src.testcase_box.models import sha256_json
from src.testcase_box.preparation import STATUS_NEEDS_CONFIGURATION, prepare_case
from src.testcase_box.profiles import ExecutionProfile, HostIntent
from src.testcase_box.storage import BoxStore
from tests.unit.testcase_box.batch_fixtures import (
    BATCH_ID,
    ISSUE,
    build_collection,
    default_cases,
    generator_case,
    tree_state,
    write_attempt,
)

REF = AttemptRef(BATCH_ID, ISSUE, 1)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "artifacts"
    write_attempt(path)
    return path


@pytest.fixture
def store(tmp_path: Path):
    with BoxStore.open(tmp_path / "box.sqlite3") as opened:
        yield opened


def _select(attempt, *case_ids, **titles):
    return [
        ImportSelection(case_id, attempt.case(case_id).item_fingerprint, titles.get(case_id))
        for case_id in case_ids
    ]


def _import(store, root, *case_ids, attempt=None, snapshot=None, **titles):
    attempt = attempt or load_attempt(root, REF)
    return import_cases(
        store,
        root,
        REF,
        _select(attempt, *case_ids, **titles),
        expected_snapshot_fingerprint=snapshot or attempt.snapshot_fingerprint,
    )


def _rows(store) -> tuple[int, int, int]:
    return tuple(
        store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("box_cases", "box_case_versions", "box_case_sources")
    )


class TestPreview:
    def test_preview_writes_nothing_and_reports_readiness(self, store, root):
        before = _rows(store)
        previews = preview_cases(store, load_attempt(root, REF))
        assert _rows(store) == before
        assert [p.source.case_id for p in previews] == ["TC-001", "TC-002", "TC-003"]
        assert all(p.importable for p in previews)
        assert {p.readiness.status for p in previews} == {STATUS_NEEDS_SCRIPT_REVIEW}

    def test_preview_of_an_ineligible_attempt_is_empty(self, store, tmp_path):
        other = tmp_path / "other"
        write_attempt(other, summary=None)
        assert preview_cases(store, load_attempt(other, REF)) == ()

    def test_dependency_warning_even_when_the_producer_is_selected(self, store, root):
        attempt = load_attempt(root, REF)
        both = {p.source.case_id: p for p in preview_cases(store, attempt, ["TC-001", "TC-002"])}
        consumer = both["TC-002"]
        assert consumer.warnings == (bi.DEPENDENCIES_NOT_LINKED,)
        assert consumer.dependencies == (bi.DependencyNote("TC-001", True, False),)
        assert both["TC-001"].warnings == ()
        alone = preview_cases(store, attempt, ["TC-002"])[0]
        assert alone.dependencies == (bi.DependencyNote("TC-001", False, False),)

    def test_preview_reports_existing_library_cases(self, store, root):
        report = _import(store, root, "TC-001")
        previews = preview_cases(store, load_attempt(root, REF), ["TC-001", "TC-002"])
        assert previews[0].existing_case_ids == (report.outcomes[0].box_case_id,)
        assert previews[1].dependencies[0].in_library is True

    def test_case_ids_must_not_be_a_string(self, store, root):
        with pytest.raises(TypeError):
            preview_cases(store, load_attempt(root, REF), "TC-001")


class TestImport:
    def test_selected_cases_only(self, store, root):
        report = _import(store, root, "TC-001", "TC-003")
        assert report.counts == {"ADDED": 2, "ALREADY_PRESENT": 0, "REJECTED": 0, "FAILED": 0}
        assert _rows(store) == (2, 2, 2)
        assert {s.source_reference for c in store.list_cases() for s in store.sources(c.id)} == {
            REF.case_reference("TC-001"),
            REF.case_reference("TC-003"),
        }

    def test_stored_payload_and_provenance(self, store, root):
        attempt = load_attempt(root, REF)
        source = attempt.case("TC-002")
        outcome = _import(store, root, "TC-002", attempt=attempt).outcomes[0]
        version = store.get_version(outcome.box_version_id)
        assert version.payload == source.item  # برابرِ معنایی؛ بایت‌ها canonical JSON‌اند
        assert version.fingerprint == source.item_fingerprint == sha256_json(source.item)
        (provenance,) = store.sources(outcome.box_case_id)
        assert provenance.source_type == m.SOURCE_BATCH
        assert provenance.case_version_id == version.id
        metadata = provenance.metadata
        assert metadata["item_fingerprint"] == source.item_fingerprint
        assert metadata["snapshot_fingerprint"] == attempt.snapshot_fingerprint
        assert metadata["executor_reported_status"] == "PASS"
        assert metadata["db_status"] == "unavailable"
        assert metadata["readiness_status_at_import"] == STATUS_NEEDS_SCRIPT_REVIEW
        assert metadata["depends_on"] == ["TC-001"]

    def test_imported_case_waits_for_script_review_and_is_never_approved(self, store, root):
        outcome = _import(store, root, "TC-001").outcomes[0]
        assert outcome.readiness_status == STATUS_NEEDS_SCRIPT_REVIEW
        assert store.script_approvals(outcome.box_version_id) == []
        assert store.script_approval_state(outcome.box_version_id) == m.SCRIPTS_PENDING

    def test_repeat_import_is_already_present(self, store, root):
        first = _import(store, root, "TC-001", "TC-002")
        before = _rows(store)
        second = _import(store, root, "TC-001", "TC-002")
        assert second.counts["ALREADY_PRESENT"] == 2
        assert _rows(store) == before
        assert [o.box_case_id for o in second.outcomes] == [o.box_case_id for o in first.outcomes]

    def test_archived_case_is_reported_and_not_restored(self, store, root):
        case_id = _import(store, root, "TC-001").outcomes[0].box_case_id
        store.archive_case(case_id)
        outcome = _import(store, root, "TC-001").outcomes[0]
        assert (outcome.outcome, outcome.archived) == ("ALREADY_PRESENT", True)
        assert store.get_case(case_id).archived

    def test_new_library_version_does_not_break_identity(self, store, root):
        outcome = _import(store, root, "TC-003").outcomes[0]
        edited = dict(store.current_version(outcome.box_case_id).payload, name="edited")
        store.add_version(outcome.box_case_id, m.NewVersion(payload=edited))
        assert _import(store, root, "TC-003").outcomes[0].outcome == "ALREADY_PRESENT"

    def test_same_identity_with_changed_payload_is_a_conflict(self, store, root):
        _import(store, root, "TC-001")
        before = _rows(store)
        path = root / BATCH_ID / ISSUE / "execution_1" / "collection.json"
        collection = json.loads(path.read_text("utf-8"))
        collection["item"][0]["item"][0]["request"]["method"] = "PUT"
        path.write_text(json.dumps(collection), encoding="utf-8")
        outcome = _import(store, root, "TC-001").outcomes[0]  # پیش‌نمایشِ تازه
        assert (outcome.outcome, outcome.codes) == ("REJECTED", (st.SOURCED_CONFLICT,))
        assert _rows(store) == before

    def test_edited_title_is_independent_of_source_metadata(self, store, root):
        outcome = _import(store, root, "TC-001", **{"TC-001": "My voucher creation"}).outcomes[0]
        case = store.get_case(outcome.box_case_id)
        assert case.title == "My voucher creation"
        metadata = store.sources(case.id)[0].metadata
        assert metadata["title_origin"] == bi.TITLE_USER
        assert metadata["default_title"] == "DEMO-1 · TC-001"
        assert metadata["source_title"] is None and metadata["source_title_available"] is False
        store.update_case_details(case.id, title="Renamed")
        assert store.sources(case.id)[0].metadata == metadata

    def test_generated_title_is_not_the_source_title(self, store, root):
        outcome = _import(store, root, "TC-002").outcomes[0]
        case = store.get_case(outcome.box_case_id)
        metadata = store.sources(case.id)[0].metadata
        assert case.title == metadata["default_title"] == "DEMO-1 · TC-002"
        assert metadata["title_origin"] == bi.TITLE_GENERATED
        assert metadata["source_title"] is None

    @pytest.mark.parametrize("title", ["", "   ", "x" * 301])
    def test_invalid_title_is_rejected(self, store, root, title):
        outcome = _import(store, root, "TC-001", **{"TC-001": title}).outcomes[0]
        assert (outcome.outcome, outcome.codes) == ("REJECTED", (bi.TITLE_INVALID,))
        assert _rows(store) == (0, 0, 0)

    def test_dependency_warning_survives_import_and_no_links_are_created(self, store, root):
        report = _import(store, root, "TC-001", "TC-002")
        warnings = {o.case_id: o.warnings for o in report.outcomes}
        assert warnings == {"TC-001": (), "TC-002": (bi.DEPENDENCIES_NOT_LINKED,)}
        assert all(store.links(o.box_case_id) == [] for o in report.outcomes)
        consumer = store.current_version(report.outcomes[1].box_case_id)
        store.record_script_decision(consumer.id, consumer.script_fingerprint, m.APPROVAL_APPROVED)
        prepared = prepare_case(
            consumer,
            host_intent=HostIntent.NORMAL,
            profile=ExecutionProfile("p", resolved_variables={"base_url"}),
            approval_state=m.APPROVAL_APPROVED,
        )
        assert prepared.status == STATUS_NEEDS_CONFIGURATION

    def test_import_job_records_counts_and_outcomes(self, store, root):
        report = _import(store, root, "TC-001", "TC-001")
        job = store.get_import_job(report.job_id)
        assert (job.status, job.source_type, job.filename) == (
            m.IMPORT_COMPLETED,
            m.SOURCE_BATCH,
            REF.reference,
        )
        assert job.summary["counts"] == {
            "ADDED": 1,
            "ALREADY_PRESENT": 0,
            "REJECTED": 1,
            "FAILED": 0,
        }
        assert job.summary["outcomes"][1]["codes"] == [bi.SELECTION_DUPLICATE]
        assert store.sources(report.outcomes[0].box_case_id)[0].metadata["import_job_id"] == job.id


class TestSourceChanges:
    def test_snapshot_change_rejects_the_whole_selection(self, store, root):
        attempt = load_attempt(root, REF)
        step3 = root / BATCH_ID / ISSUE / "execution_1" / "step3.json"
        step3.write_bytes(step3.read_bytes() + b"\n")
        report = _import(store, root, "TC-001", "TC-003", attempt=attempt)
        assert {(o.outcome, o.codes) for o in report.outcomes} == {
            ("REJECTED", (bi.SOURCE_CHANGED,))
        }
        assert _rows(store) == (0, 0, 0)

    def test_item_fingerprint_mismatch(self, store, root):
        attempt = load_attempt(root, REF)
        report = import_cases(
            store,
            root,
            REF,
            [ImportSelection("TC-001", "0" * 64)],
            expected_snapshot_fingerprint=attempt.snapshot_fingerprint,
        )
        assert report.outcomes[0].codes == (bi.SOURCE_CHANGED,)
        assert _rows(store) == (0, 0, 0)

    def test_attempt_that_became_ineligible(self, store, root):
        attempt = load_attempt(root, REF)
        (root / BATCH_ID / ISSUE / "execution_1" / ".collection.json.tmp").write_text("{}")
        report = _import(store, root, "TC-001", attempt=attempt)
        assert report.outcomes[0].codes == ("INCOMPLETE_WRITE",)

    def test_unknown_and_invalid_selections(self, store, root):
        attempt = load_attempt(root, REF)
        report = import_cases(
            store,
            root,
            REF,
            [ImportSelection("TC-999", "0" * 64), "TC-001", ImportSelection(7, "x")],
            expected_snapshot_fingerprint=attempt.snapshot_fingerprint,
        )
        assert [o.codes for o in report.outcomes] == [
            (bi.SOURCE_CASE_NOT_FOUND,),
            (bi.SELECTION_INVALID,),
            (bi.SELECTION_INVALID,),
        ]


class TestPolicy:
    def _attempt_with(self, tmp_path, header_value):
        path = tmp_path / "policy"
        collection = build_collection([generator_case("TC-001", path="/health")])
        collection["item"][0]["item"][0]["request"]["header"] = [
            {"key": "Authorization", "value": header_value}
        ]
        write_attempt(path, cases=[generator_case("TC-001", path="/health")], collection=collection)
        return path

    def test_not_executable_case_is_preserved_without_repair(self, store, tmp_path):
        path = self._attempt_with(tmp_path, "Bearer {{token}}")
        outcome = _import(store, path, "TC-001").outcomes[0]
        assert (outcome.outcome, outcome.readiness_status) == ("ADDED", STATUS_NOT_EXECUTABLE)
        stored = store.get_version(outcome.box_version_id).payload
        assert stored["request"]["header"][0]["value"] == "Bearer {{token}}"

    def test_literal_credential_is_never_stored(self, store, tmp_path):
        path = self._attempt_with(tmp_path, "s3cr3t-literal")
        attempt = load_attempt(path, REF)
        preview = preview_cases(store, attempt)[0]
        assert not preview.importable
        assert set(preview.rejection_codes) == {bi.PAYLOAD_REJECTED, "LITERAL_CREDENTIAL"}
        report = _import(store, path, "TC-001", attempt=attempt)
        assert report.outcomes[0].outcome == "REJECTED"
        assert _rows(store) == (0, 0, 0)
        job_text = json.dumps(store.get_import_job(report.job_id).summary)
        assert "s3cr3t" not in job_text

    def test_source_problem_rejects_the_case(self, store, tmp_path):
        path = tmp_path / "unnamed"
        cases = default_cases()
        collection = build_collection(cases)
        collection["item"][0]["item"][2]["name"] = "TC-001"
        write_attempt(path, cases=cases, collection=collection)
        attempt = load_attempt(path, REF)
        report = import_cases(
            store,
            path,
            REF,
            [ImportSelection("TC-001", attempt.cases[0].item_fingerprint)],
            expected_snapshot_fingerprint=attempt.snapshot_fingerprint,
        )
        assert report.outcomes[0].codes == (bi.SOURCE_CASE_NOT_FOUND,)


class TestAtomicity:
    def test_failure_on_one_case_rolls_it_back_and_continues(self, store, root, monkeypatch):
        real = BoxStore._insert_source
        calls = {"n": 0}

        def failing(self, case_id, version_id, source, stamp):
            calls["n"] += 1
            if calls["n"] == 2:
                raise st.IntegrityViolationError("injected")
            return real(self, case_id, version_id, source, stamp)

        monkeypatch.setattr(BoxStore, "_insert_source", failing)
        report = _import(store, root, "TC-001", "TC-002", "TC-003")
        assert [o.outcome for o in report.outcomes] == ["ADDED", "FAILED", "ADDED"]
        assert report.outcomes[1].codes == ("IntegrityViolationError",)
        assert _rows(store) == (2, 2, 2)  # بی case یا نسخه‌ی یتیم
        assert store.get_import_job(report.job_id).status == m.IMPORT_COMPLETED

    def test_unexpected_error_closes_the_job_as_failed(self, store, root, monkeypatch):
        attempt = load_attempt(root, REF)

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(bi, "load_attempt", boom)
        with pytest.raises(RuntimeError):
            _import(store, root, "TC-001", attempt=attempt)
        (job,) = [store.get_import_job(1)]
        assert job.status == m.IMPORT_FAILED and job.summary["interrupted"] is True
        assert _rows(store) == (0, 0, 0)

    def test_concurrent_imports_create_one_case(self, tmp_path, root):
        attempt = load_attempt(root, REF)
        selection = _select(attempt, "TC-001")
        barrier = threading.Barrier(2)
        results = []

        def worker():
            with BoxStore.open(tmp_path / "shared.sqlite3") as store:
                barrier.wait()
                report = import_cases(
                    store,
                    root,
                    REF,
                    selection,
                    expected_snapshot_fingerprint=attempt.snapshot_fingerprint,
                )
                results.append(report.outcomes[0].outcome)

        with BoxStore.open(tmp_path / "shared.sqlite3"):
            pass  # migrate پیش از رقابت
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == ["ADDED", "ALREADY_PRESENT"]
        with BoxStore.open(tmp_path / "shared.sqlite3") as store:
            assert _rows(store) == (1, 1, 1)


class TestIsolation:
    def test_batch_tree_and_box_schema_are_unchanged(self, store, root):
        before = tree_state(root)
        schema_before = store._db.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        attempt = load_attempt(root, REF)
        preview_cases(store, attempt)
        _import(store, root, "TC-001", "TC-002", "TC-003", attempt=attempt)
        _import(store, root, "TC-001", attempt=attempt)
        assert tree_state(root) == before
        schema_after = store._db.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        assert [tuple(row) for row in schema_after] == [tuple(row) for row in schema_before]
        assert store.schema_version == MIGRATIONS[-1][0]
