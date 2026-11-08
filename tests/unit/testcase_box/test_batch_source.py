"""
تست‌های خواننده‌ی فقط‌خواندنیِ artifactهای Batch (M5): قراردادِ واجدِ شرایط بودن،
یکپارچگیِ مسیر و JSON، هویت و اثرانگشت، فراداده‌ی منشأ و هم‌خوانیِ ثابت‌های آینه‌شده.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from src.testcase_box import batch_source as bs
from src.testcase_box.batch_source import AttemptRef, discover_attempts, load_attempt
from src.testcase_box.models import sha256_json
from tests.unit.testcase_box.batch_fixtures import (
    BATCH_ID,
    ISSUE,
    build_collection,
    default_cases,
    generator_case,
    step_results,
    tree_state,
    write_attempt,
)

REF = AttemptRef(BATCH_ID, ISSUE, 1)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "artifacts"
    path.mkdir()
    return path


def _codes(attempt) -> set[str]:
    return {problem.code for problem in attempt.problems}


def _rewrite(path: Path, document) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


class TestEligibleAttempts:
    def test_valid_attempt_with_three_cases(self, root):
        write_attempt(root)
        attempt = load_attempt(root, REF)
        assert attempt.eligible and attempt.problems == ()
        assert [case.case_id for case in attempt.cases] == ["TC-001", "TC-002", "TC-003"]
        assert all(case.importable for case in attempt.cases)
        assert attempt.executor_reported_status == "PASS"
        assert attempt.execution_id == "exec-1"

    @pytest.mark.parametrize(
        ("status", "phase", "newman_executed"),
        [("FAIL", "done", True), ("ERROR", "newman", True), ("CANCELLED", "newman", False)],
    )
    def test_non_pass_final_outcomes_with_a_valid_collection_are_eligible(
        self, root, status, phase, newman_executed
    ):
        write_attempt(root, status=status, phase=phase, newman_executed=newman_executed)
        attempt = load_attempt(root, REF)
        assert attempt.eligible
        assert attempt.executor_reported_status == status
        assert attempt.cases[0].metadata["executor_reported_status"] == status
        assert attempt.cases[0].metadata["db_status"] == "unavailable"

    def test_optional_steps_may_be_absent(self, root):
        write_attempt(root, skip_steps=(1, 2, 3, 5))
        attempt = load_attempt(root, REF)
        assert attempt.eligible
        metadata = attempt.case("TC-002").metadata
        assert metadata["operation_id"] is None
        assert metadata["source_title_sha256"] is None
        assert metadata["depends_on"] == [] and metadata["consumes"] == []

    def test_discovery_lists_eligible_and_ineligible_attempts(self, root):
        write_attempt(root)
        write_attempt(root, attempt=2, summary=None)
        write_attempt(root, issue="DEMO-2")
        (root / "not a batch").mkdir()
        (root / BATCH_ID / "DEMO-1" / "notes").mkdir()
        listings = {
            (item.ref.issue_key, item.ref.attempt): item for item in discover_attempts(root)
        }
        assert set(listings) == {("DEMO-1", 1), ("DEMO-1", 2), ("DEMO-2", 1)}
        assert listings[("DEMO-1", 1)].eligible
        assert listings[("DEMO-1", 1)].case_ids == ("TC-001", "TC-002", "TC-003")
        assert not listings[("DEMO-1", 2)].eligible
        assert {p.code for p in listings[("DEMO-1", 2)].problems} == {bs.IN_PROGRESS_OR_INTERRUPTED}

    def test_discovery_of_a_missing_root_is_empty(self, tmp_path):
        assert discover_attempts(tmp_path / "missing") == ()


class TestIneligibleAttempts:
    def test_missing_summary_means_in_progress_or_interrupted(self, root):
        write_attempt(root, summary=None)
        assert _codes(load_attempt(root, REF)) == {bs.IN_PROGRESS_OR_INTERRUPTED}

    def test_minimal_summary(self, root):
        write_attempt(root, summary="minimal")
        assert _codes(load_attempt(root, REF)) == {bs.MINIMAL_SUMMARY}

    @pytest.mark.parametrize(
        ("update", "code"),
        [
            ({"extra": 1}, bs.SUMMARY_MALFORMED),
            ({"batch_id": "f" * 32}, bs.SUMMARY_IDENTITY_MISMATCH),
            ({"issue_key": "DEMO-9"}, bs.SUMMARY_IDENTITY_MISMATCH),
            ({"attempt": 2}, bs.SUMMARY_IDENTITY_MISMATCH),
            ({"attempt": True}, bs.SUMMARY_IDENTITY_MISMATCH),
            ({"attempt": 1.0}, bs.SUMMARY_IDENTITY_MISMATCH),
            ({"execution_id": ""}, bs.SUMMARY_MALFORMED),
            ({"status": "RUNNING"}, bs.SUMMARY_MALFORMED),
            ({"phase": "nowhere"}, bs.SUMMARY_MALFORMED),
            ({"status": "INTERRUPTED"}, bs.STATUS_NOT_ELIGIBLE),
            ({"status": "NOT_EXECUTED"}, bs.STATUS_NOT_ELIGIBLE),
            ({"newman_executed": False}, bs.SUMMARY_INCONSISTENT),
            ({"phase": "step4"}, bs.SUMMARY_INCONSISTENT),
            ({"collection_path": ""}, bs.SUMMARY_INCONSISTENT),
            (
                {"collection_path": "/x/" + BATCH_ID + "/DEMO-1/execution_2/collection.json"},
                bs.SUMMARY_INCONSISTENT,
            ),
            ({"artifact_dir": "/elsewhere/execution_1"}, bs.SUMMARY_INCONSISTENT),
            ({"step_artifacts": []}, bs.SUMMARY_MALFORMED),
        ],
        ids=lambda value: str(value)[:40],
    )
    def test_summary_checks(self, root, update, code):
        write_attempt(root, summary_update=update)
        attempt = load_attempt(root, REF)
        assert not attempt.eligible
        assert _codes(attempt) == {code}
        assert attempt.cases == () and attempt.snapshot_fingerprint is None

    def test_missing_summary_key(self, root):
        directory = write_attempt(root)
        summary = json.loads((directory / bs.SUMMARY_FILE).read_text(encoding="utf-8"))
        del summary["newman_run_id"]
        _rewrite(directory / bs.SUMMARY_FILE, summary)
        assert _codes(load_attempt(root, REF)) == {bs.SUMMARY_MALFORMED}

    def test_cancelled_before_the_collection_is_not_eligible(self, root):
        write_attempt(root, status="CANCELLED", phase="step3", write_collection=False)
        assert _codes(load_attempt(root, REF)) == {bs.SUMMARY_INCONSISTENT}

    def test_missing_collection_file(self, root):
        directory = write_attempt(root)
        (directory / bs.COLLECTION_FILE).unlink()
        assert _codes(load_attempt(root, REF)) == {bs.COLLECTION_MISSING}

    def test_step4_is_required(self, root):
        write_attempt(root, skip_steps=(4,))
        assert _codes(load_attempt(root, REF)) == {bs.STEP4_MISSING}

    def test_listed_step_file_must_exist(self, root):
        directory = write_attempt(root)
        (directory / "step5.json").unlink()
        assert _codes(load_attempt(root, REF)) == {bs.STEP_ARTIFACT_INCONSISTENT}

    def test_unlisted_step_file_is_inconsistent(self, root):
        directory = write_attempt(root, skip_steps=(5,))
        _rewrite(directory / "step5.json", {"review": {}})
        assert _codes(load_attempt(root, REF)) == {bs.STEP_ARTIFACT_INCONSISTENT}

    def test_step_artifact_pointing_elsewhere(self, root):
        write_attempt(root, summary_update={"step_artifacts": {"step4.json": "/x/step4.json"}})
        assert _codes(load_attempt(root, REF)) == {bs.STEP_ARTIFACT_INCONSISTENT}

    @pytest.mark.parametrize("name", [".collection.json.tmp", ".execution_summary.json.tmp"])
    def test_leftover_temporary_file_means_incomplete_write(self, root, name):
        directory = write_attempt(root)
        (directory / name).write_text("{}", encoding="utf-8")
        assert _codes(load_attempt(root, REF)) == {bs.INCOMPLETE_WRITE}

    def test_missing_attempt_directory(self, root):
        assert _codes(load_attempt(root, REF)) == {bs.PATH_INVALID}

    def test_invalid_root(self, tmp_path):
        assert _codes(load_attempt(tmp_path / "missing", REF)) == {bs.ROOT_INVALID}
        file_root = tmp_path / "file"
        file_root.write_text("x", encoding="utf-8")
        assert _codes(load_attempt(file_root, REF)) == {bs.ROOT_INVALID}


class TestJsonIntegrity:
    @pytest.mark.parametrize(
        "content",
        [
            b'{"a": 1, "a": 2}',
            b'{"a": NaN}',
            b'{"a": Infinity}',
            b"\xef\xbb\xbf{}",
            b'{"a": "\xff"}',
            b"[]",
            b"{",
        ],
        ids=["duplicate-key", "nan", "infinity", "bom", "bad-utf8", "array", "truncated"],
    )
    @pytest.mark.parametrize("name", ["collection.json", "step4.json", "execution_summary.json"])
    def test_malformed_json_is_rejected(self, root, content, name):
        directory = write_attempt(root)
        (directory / name).write_bytes(content)
        assert _codes(load_attempt(root, REF)) == {bs.ARTIFACT_MALFORMED}

    def test_oversized_file(self, root, monkeypatch):
        write_attempt(root)
        monkeypatch.setitem(bs.MAX_BYTES, bs.COLLECTION_FILE, 10)
        assert _codes(load_attempt(root, REF)) == {bs.ARTIFACT_TOO_LARGE}

    def test_file_swapped_between_lstat_and_open(self, root, monkeypatch):
        write_attempt(root)
        real = os.fstat

        def swapped(descriptor):
            values = list(real(descriptor))[:10]
            values[1] += 1  # st_ino
            return os.stat_result(values)

        monkeypatch.setattr(bs.os, "fstat", swapped)
        assert _codes(load_attempt(root, REF)) == {bs.ARTIFACT_CHANGED_WHILE_READING}

    def test_hashes_come_from_the_exact_bytes(self, root):
        directory = write_attempt(root)
        attempt = load_attempt(root, REF)
        files = dict(attempt.files)
        for name in (bs.SUMMARY_FILE, bs.COLLECTION_FILE, "step1.json", "step5.json"):
            assert files[name] == hashlib.sha256((directory / name).read_bytes()).hexdigest()
        assert set(files) == {
            bs.SUMMARY_FILE,
            bs.COLLECTION_FILE,
            *(f"step{n}.json" for n in range(1, 6)),
        }
        assert attempt.snapshot_fingerprint == sha256_json(
            {"attempt": REF.reference, "files": [[n, files[n]] for n in sorted(files)]}
        )
        metadata = attempt.cases[0].metadata
        assert metadata["collection_sha256"] == files[bs.COLLECTION_FILE]
        assert metadata["summary_sha256"] == files[bs.SUMMARY_FILE]

    def test_unrelated_newman_output_is_not_part_of_the_snapshot(self, root):
        directory = write_attempt(root)
        before = load_attempt(root, REF).snapshot_fingerprint
        (directory / "newman").mkdir()
        (directory / "newman" / "result.json").write_text("{}", encoding="utf-8")
        assert load_attempt(root, REF).snapshot_fingerprint == before

    @pytest.mark.parametrize("name", ["collection.json", "execution_summary.json", "step3.json"])
    def test_snapshot_changes_with_any_consumed_artifact(self, root, name):
        directory = write_attempt(root)
        before = load_attempt(root, REF).snapshot_fingerprint
        path = directory / name
        path.write_bytes(path.read_bytes() + b"\n")
        assert load_attempt(root, REF).snapshot_fingerprint != before


class TestPathSafety:
    @pytest.mark.parametrize(
        "args",
        [
            ("..", ISSUE, 1),
            (BATCH_ID, "../x", 1),
            (BATCH_ID, "a/b", 1),
            (BATCH_ID, "DEMO.", 1),
            (BATCH_ID, "CON", 1),
            (BATCH_ID, "nul.txt", 1),
            (BATCH_ID, "C:", 1),
            (BATCH_ID, ISSUE, 0),
            (BATCH_ID, ISSUE, True),
            (BATCH_ID, ISSUE, "1"),
            ("", ISSUE, 1),
        ],
    )
    def test_unsafe_references_are_refused(self, args):
        with pytest.raises(ValueError):
            AttemptRef(*args)

    def test_symlinked_attempt_directory_is_rejected(self, root, tmp_path):
        real = write_attempt(tmp_path / "elsewhere")
        target = root / BATCH_ID / ISSUE
        target.mkdir(parents=True)
        try:
            os.symlink(real, target / "execution_1", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("Creating symbolic links is not permitted on this machine.")
        assert _codes(load_attempt(root, REF)) == {bs.PATH_LINK}

    def test_symlinked_file_is_rejected(self, root, tmp_path):
        directory = write_attempt(root)
        outside = tmp_path / "outside.json"
        outside.write_bytes((directory / "step5.json").read_bytes())
        (directory / "step5.json").unlink()
        try:
            os.symlink(outside, directory / "step5.json")
        except (OSError, NotImplementedError):
            pytest.skip("Creating symbolic links is not permitted on this machine.")
        assert _codes(load_attempt(root, REF)) == {bs.PATH_LINK}

    @pytest.mark.skipif(sys.platform != "win32", reason="junctions exist only on Windows")
    def test_junctioned_batch_directory_is_rejected(self, root, tmp_path):
        import _winapi

        write_attempt(tmp_path / "elsewhere")
        _winapi.CreateJunction(str(tmp_path / "elsewhere" / BATCH_ID), str(root / BATCH_ID))
        assert _codes(load_attempt(root, REF)) == {bs.PATH_LINK}
        listing = discover_attempts(root)
        assert len(listing) == 1 and not listing[0].eligible

    def test_symlinked_root_is_rejected(self, root, tmp_path):
        write_attempt(root)
        link = tmp_path / "linked-root"
        try:
            os.symlink(root, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("Creating symbolic links is not permitted on this machine.")
        assert _codes(load_attempt(link, REF)) == {bs.ROOT_INVALID}


class TestCases:
    def test_identity_title_and_payload(self, root):
        write_attempt(root)
        case = load_attempt(root, REF).case("TC-002")
        assert case.source_reference == f"batch/{BATCH_ID}/{ISSUE}/execution_1/TC-002"
        assert case.default_title == "DEMO-1 · TC-002"
        assert case.metadata["source_title"] is None
        assert case.metadata["source_title_available"] is False
        assert (
            case.metadata["source_title_sha256"]
            == hashlib.sha256(b"Original title TC-002").hexdigest()
        )
        assert case.item["name"] == "TC-002"  # Batch عنوانِ خوانا را حذف کرده است
        assert "readable" not in json.dumps(case.item)
        collection = json.loads(
            (root / BATCH_ID / ISSUE / "execution_1" / "collection.json").read_text("utf-8")
        )
        original = collection["item"][0]["item"][1]
        assert case.item == original
        assert case.item is not original
        assert case.item_fingerprint == sha256_json(original)

    def test_dependency_and_step_metadata(self, root):
        write_attempt(root)
        attempt = load_attempt(root, REF)
        producer, consumer, vdi = attempt.cases
        assert consumer.metadata["depends_on"] == ["TC-001"]
        assert consumer.metadata["consumes"] == [{"variable": "voucherId", "producers": ["TC-001"]}]
        assert producer.metadata["produces"] == ["voucherId"]
        assert vdi.metadata["host_variable"] == "base_url_vdi"
        assert consumer.metadata["host_basis"] == "default"
        assert consumer.metadata["base_path"] == "/api"
        assert consumer.metadata["operation_id"] == "op1"
        assert consumer.metadata["collection_variable_names"] == ["token"]

    def test_metadata_has_no_absolute_paths_or_script_text(self, root):
        write_attempt(root)
        attempt = load_attempt(root, REF)
        text = json.dumps([case.metadata for case in attempt.cases])
        assert str(root) not in text and str(root).replace("\\", "\\\\") not in text
        assert "pm." not in text and "readable" not in text

    def test_item_without_case_id(self, root):
        cases = default_cases()
        collection = build_collection(cases)
        collection["item"][0]["item"][2]["name"] = "Request 3"
        write_attempt(root, cases=cases, collection=collection)
        attempt = load_attempt(root, REF)
        assert attempt.eligible
        unnamed = attempt.cases[2]
        assert unnamed.case_id is None and unnamed.source_reference is None
        assert [p.code for p in unnamed.problems] == [bs.CASE_ID_UNAVAILABLE]

    def test_duplicate_case_ids(self, root):
        cases = default_cases()
        collection = build_collection(cases)
        collection["item"][0]["item"][2]["name"] = "TC-001"
        write_attempt(root, cases=cases, collection=collection)
        attempt = load_attempt(root, REF)
        duplicates = [c for c in attempt.cases if c.case_id == "TC-001"]
        assert len(duplicates) == 2
        assert all(bs.CASE_ID_DUPLICATE in {p.code for p in c.problems} for c in duplicates)
        assert attempt.case("TC-001") is None

    def test_folder_event_or_auth_is_unsupported(self, root):
        cases = default_cases()
        collection = build_collection(cases)
        collection["item"][0]["auth"] = {"type": "noauth"}
        write_attempt(root, cases=cases, collection=collection)
        attempt = load_attempt(root, REF)
        assert all(
            bs.FOLDER_CONTEXT_UNSUPPORTED in {p.code for p in c.problems} for c in attempt.cases
        )

    @pytest.mark.parametrize("key", ["event", "auth"])
    def test_collection_event_or_auth_makes_the_attempt_ineligible(self, root, key):
        cases = default_cases()
        collection = build_collection(cases)
        collection[key] = [{"listen": "prerequest"}] if key == "event" else {"type": "bearer"}
        write_attempt(root, cases=cases, collection=collection)
        assert _codes(load_attempt(root, REF)) == {bs.COLLECTION_CONTEXT_UNSUPPORTED}

    def test_unresolved_and_host_inconsistent_cases(self, root):
        cases = default_cases()
        steps = step_results(cases)
        steps[4]["unresolved"] = [{"test_case_id": "TC-001", "host_status": "CONFLICT"}]
        steps[4]["requests"][2]["host_variable"] = "base_url"
        write_attempt(root, cases=cases, steps=steps)
        attempt = load_attempt(root, REF)
        assert {p.code for p in attempt.case("TC-001").problems} == {bs.CASE_UNRESOLVED}
        assert {p.code for p in attempt.case("TC-003").problems} == {bs.HOST_INCONSISTENT}
        assert attempt.case("TC-002").importable

    def test_ambiguous_step4_rows(self, root):
        cases = default_cases()
        steps = step_results(cases)
        steps[4]["requests"].append(dict(steps[4]["requests"][0]))
        write_attempt(root, cases=cases, steps=steps)
        assert {p.code for p in load_attempt(root, REF).case("TC-001").problems} == {
            bs.STEP4_AMBIGUOUS
        }

    def test_malformed_collection_structure(self, root):
        collection = build_collection(default_cases())
        collection["item"].append("not an item")
        write_attempt(root, collection=collection)
        assert _codes(load_attempt(root, REF)) == {bs.ARTIFACT_MALFORMED}

    def test_unsafe_step_values_are_not_copied(self, root):
        cases = [generator_case("TC-001")]
        steps = step_results(cases)
        steps[4]["requests"][0]["host_basis"] = "anything"
        steps[4]["requests"][0]["method"] = "TRACE"
        write_attempt(root, cases=cases, steps=steps)
        metadata = load_attempt(root, REF).case("TC-001").metadata
        assert metadata["host_basis"] is None and metadata["method"] is None


class TestReadOnly:
    def test_reading_never_changes_the_batch_tree(self, root):
        write_attempt(root)
        write_attempt(root, attempt=2, summary=None)
        before = tree_state(root)
        discover_attempts(root)
        load_attempt(root, REF)
        assert tree_state(root) == before


class TestDrift:
    """ثابت‌های آینه‌شده باید با کدِ Batch برابر بمانند."""

    def test_constants_match_batch(self):
        from src.batch_testing import executor
        from src.batch_testing import models as batch_models
        from src.batch_testing.artifacts import ALLOWED_NAMES
        from src.batch_testing.storage import DEFAULT_ARTIFACT_ROOT

        assert bs.DEFAULT_BATCH_ARTIFACT_ROOT == DEFAULT_ARTIFACT_ROOT
        assert bs.SAFE_COMPONENT_RE.pattern == executor._SAFE_COMPONENT.pattern
        assert executor._CASE_ID_PREFIX.pattern == r"^([A-Za-z]+-\d+)\b"
        assert bs.OUTCOME_FIELDS == {f.name for f in dataclasses.fields(executor.ExecutionOutcome)}
        assert bs.EXECUTION_FINAL == batch_models.EXECUTION_FINAL
        assert bs.ELIGIBLE_STATUSES < batch_models.EXECUTION_FINAL
        assert bs.PHASES == set(batch_models.PHASES)
        assert bs.REVIEW_STATUSES == set(batch_models.REVIEW_STATUSES)
        assert bs.FINAL_REVIEW_STATUSES == set(batch_models.FINAL_REVIEW_STATUSES)
        assert {bs.SUMMARY_FILE, bs.COLLECTION_FILE, *bs.STEP_FILES} <= ALLOWED_NAMES

    def test_case_id_pattern_matches_sanitized_names(self):
        from src.batch_testing.executor import _CASE_ID_PREFIX

        for name in ("TC-001 — title", "ABC-12", "Request 1", "tc_1"):
            match = _CASE_ID_PREFIX.match(name)
            expected = match.group(1) if match else None
            got = name.split(" ")[0] if bs.CASE_ID_RE.fullmatch(name.split(" ")[0]) else None
            assert got == expected
