"""
تست‌های مدلِ نمایشیِ کتابخانه (M6.1): ردیف‌ها و فیلترها، جزئیات و نسخه‌ها، پوشاندنِ
نمایشی، خلاصه‌ی اسکریپت بی‌متن، هم‌خوانیِ readiness با M3 و فقط‌خواندنی بودن.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from src.testcase_box import library as lib
from src.testcase_box import models as m
from src.testcase_box.batch_import import ImportSelection, import_cases
from src.testcase_box.batch_source import AttemptRef, load_attempt
from src.testcase_box.contract import (
    STATUS_NEEDS_SCRIPT_REVIEW,
    STATUS_NOT_EXECUTABLE,
    STATUS_READY,
)
from src.testcase_box.models import NewCase, NewSource, NewVersion
from src.testcase_box.readiness import assess_version
from src.testcase_box.storage import BoxStore
from tests.unit.testcase_box.batch_fixtures import BATCH_ID, ISSUE, write_attempt

SECRETS = (
    "s3cr3t-cookie",
    "s3cr3t-apikey",
    "s3cr3t-disabled",
    "s3cr3t-query",
    "s3cr3t-pass",
    "s3cr3t-form",
    "s3cr3t-auth",
    "s3cr3t-userinfo",
    "s3cr3t-response",
    "hidden_script_marker",
)


def plain_item(path: str = "/a", **request) -> dict:
    return {
        "name": "item",
        "request": {
            "method": "GET",
            "header": [{"key": "Authorization", "value": "{{token}}"}],
            "url": {
                "raw": "{{base_url}}" + path,
                "host": ["{{base_url}}"],
                "path": [part for part in path.split("/") if part],
            },
            **request,
        },
    }


def secret_item() -> dict:
    return {
        "name": "secrets",
        "request": {
            "method": "POST",
            "header": [
                {"key": "Authorization", "value": "{{token}}"},
                {"key": "Cookie", "value": "session=s3cr3t-cookie"},
                {"key": "X-Api-Key", "value": "s3cr3t-apikey"},
                {"key": "X-Trace", "value": "visible-trace"},
                {"key": "Authorization", "value": "Bearer s3cr3t-disabled", "disabled": True},
            ],
            "url": {
                "raw": "{{base_url}}/a?apiKey=s3cr3t-query&page=1&token={{token}}",
                "host": ["{{base_url}}"],
                "path": ["a"],
                "query": [
                    {"key": "apiKey", "value": "s3cr3t-query"},
                    {"key": "page", "value": "1"},
                    {"key": "token", "value": "{{token}}"},
                ],
            },
            "body": {
                "mode": "raw",
                "raw": '{"password": "s3cr3t-pass", "user": "alice", "otp": "{{otp}}"}',
            },
        },
        "event": [
            {
                "listen": "test",
                "script": {"exec": ["const hidden_script_marker = 1;", "pm.test('a');"]},
            }
        ],
        "response": [{"name": "example", "body": "s3cr3t-response"}],
    }


@pytest.fixture
def store(tmp_path: Path):
    with BoxStore.open(tmp_path / "box.sqlite3") as opened:
        yield opened


@pytest.fixture
def library(store: BoxStore, tmp_path: Path) -> dict[str, int]:
    """سه caseِ Batch (واقعاً از M5 وارد شده)، یک IMPORT، یک MANUAL، یک بی‌منشأ، یک بایگانی."""
    root = tmp_path / "artifacts"
    write_attempt(root)
    ref = AttemptRef(BATCH_ID, ISSUE, 1)
    attempt = load_attempt(root, ref)
    report = import_cases(
        store,
        root,
        ref,
        [ImportSelection(c.case_id, c.item_fingerprint) for c in attempt.cases],
        expected_snapshot_fingerprint=attempt.snapshot_fingerprint,
    )
    ids = {o.case_id: o.box_case_id for o in report.outcomes}
    ids["import"] = store.create_case(
        NewCase(
            title="From a spreadsheet",
            payload=plain_item("/imported"),
            sources=(NewSource(m.SOURCE_IMPORT, "job-7/row-2"),),
        )
    ).id
    ids["manual"] = store.create_case(
        NewCase(
            title="Hand written secrets",
            payload=secret_item(),
            sources=(NewSource(m.SOURCE_MANUAL, "ui", {"api_key": "s3cr3t-apikey", "n": 1}),),
        )
    ).id
    ids["none"] = store.create_case(NewCase(title="No provenance", payload=plain_item("/none"))).id
    ids["archived"] = store.create_case(
        NewCase(title="Old archived case", payload=plain_item("/old"))
    ).id
    store.archive_case(ids["archived"])
    return ids


def _by_id(rows) -> dict[int, lib.LibraryRow]:
    return {row.case_id: row for row in rows}


class TestRows:
    def test_every_case_is_listed_newest_first(self, store, library):
        rows = lib.library_rows(store)
        assert {row.case_id for row in rows} == set(library.values())
        assert list(rows) == sorted(rows, key=lambda r: (r.updated_at, r.case_id), reverse=True)

    def test_source_types_distinguish_batch_import_manual_and_none(self, store, library):
        rows = _by_id(lib.library_rows(store))
        assert rows[library["TC-001"]].source_types == (m.SOURCE_BATCH,)
        assert rows[library["import"]].source_types == (m.SOURCE_IMPORT,)
        assert rows[library["manual"]].source_types == (m.SOURCE_MANUAL,)
        assert rows[library["none"]].source_types == (lib.SOURCE_NONE,)

    def test_batch_row_fields(self, store, library):
        row = _by_id(lib.library_rows(store))[library["TC-002"]]
        assert row.key == m.case_key(library["TC-002"])
        assert row.issue_keys == (ISSUE,)
        assert row.batch_case_ids == ("TC-002",)
        assert row.source_references == (f"batch/{BATCH_ID}/{ISSUE}/execution_1/TC-002",)
        assert row.service == "/api · op1"
        assert (row.version_number, row.version_count) == (1, 1)
        assert row.approval_state == m.SCRIPTS_PENDING

    def test_non_batch_rows_have_no_service(self, store, library):
        rows = _by_id(lib.library_rows(store))
        assert rows[library["import"]].service == ""
        assert rows[library["import"]].issue_keys == ()

    def test_readiness_matches_m3(self, store, library):
        for row in lib.library_rows(store):
            assert row.readiness == assess_version(store, row.version_id).status
        rows = _by_id(lib.library_rows(store))
        assert rows[library["TC-001"]].readiness == STATUS_NEEDS_SCRIPT_REVIEW
        assert rows[library["none"]].readiness == STATUS_READY
        assert rows[library["manual"]].readiness == STATUS_NOT_EXECUTABLE

    def test_current_version_follows_new_versions(self, store, library):
        store.add_version(library["none"], NewVersion(payload=plain_item("/v2")))
        row = _by_id(lib.library_rows(store))[library["none"]]
        assert (row.version_number, row.version_count) == (2, 2)

    def test_empty_library(self, store):
        assert lib.library_rows(store) == ()


class TestFilters:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("spreadsheet", {"import"}),
            ("SPREADSHEET", {"import"}),
            ("demo-1", {"TC-001", "TC-002", "TC-003"}),
            ("tc-003", {"TC-003"}),
            ("job-7/row", {"import"}),
            (f"{BATCH_ID}/{ISSUE}/execution_1/TC-002", {"TC-002"}),
            ("  ", None),
        ],
    )
    def test_search(self, store, library, text, expected):
        rows = lib.library_rows(store)
        found = {row.case_id for row in lib.filter_rows(rows, text=text)}
        assert found == (
            {row.case_id for row in rows}
            if expected is None
            else {library[name] for name in expected}
        )

    def test_search_by_box_key(self, store, library):
        rows = lib.library_rows(store)
        key = m.case_key(library["manual"])
        assert [r.case_id for r in lib.filter_rows(rows, text=key.lower())] == [library["manual"]]

    def test_status_filter(self, store, library):
        rows = lib.library_rows(store)
        archived = lib.filter_rows(rows, statuses={m.CASE_ARCHIVED})
        assert [row.case_id for row in archived] == [library["archived"]]
        active = lib.filter_rows(rows, statuses={m.CASE_ACTIVE})
        assert library["archived"] not in {row.case_id for row in active}
        assert len(lib.filter_rows(rows)) == len(rows)

    def test_source_filter(self, store, library):
        rows = lib.library_rows(store)
        assert {r.case_id for r in lib.filter_rows(rows, sources={lib.SOURCE_NONE})} == {
            library["none"],
            library["archived"],
        }
        batch_or_import = lib.filter_rows(rows, sources={m.SOURCE_BATCH, m.SOURCE_IMPORT})
        assert {r.case_id for r in batch_or_import} == {
            library["TC-001"],
            library["TC-002"],
            library["TC-003"],
            library["import"],
        }

    def test_readiness_filter(self, store, library):
        rows = lib.library_rows(store)
        blocked = lib.filter_rows(rows, readiness={STATUS_NOT_EXECUTABLE})
        assert [row.case_id for row in blocked] == [library["manual"]]

    def test_combined_filters(self, store, library):
        rows = lib.library_rows(store)
        found = lib.filter_rows(
            rows,
            text="demo",
            statuses={m.CASE_ACTIVE},
            sources={m.SOURCE_BATCH},
            readiness={STATUS_NEEDS_SCRIPT_REVIEW},
        )
        assert {r.case_id for r in found} == {library[c] for c in ("TC-001", "TC-002", "TC-003")}
        assert lib.filter_rows(rows, text="demo", sources={m.SOURCE_IMPORT}) == ()


class TestDetail:
    def test_current_version_by_default(self, store, library):
        detail = lib.case_detail(store, library["TC-002"])
        assert detail.key == m.case_key(library["TC-002"])
        assert detail.version.current and detail.version.number == 1
        assert detail.readiness == assess_version(store, detail.version.id)
        assert detail.approval_state == m.SCRIPTS_PENDING
        assert detail.configuration_note == (
            "Configuration: not evaluated. Requires an execution profile."
        )

    def test_versions_are_ordered_and_an_earlier_version_can_be_shown(self, store, library):
        case_id = library["none"]
        first = store.current_version(case_id)
        second = store.add_version(case_id, NewVersion(payload=plain_item("/v2")))
        detail = lib.case_detail(store, case_id)
        assert [(v.number, v.current) for v in detail.versions] == [(1, False), (2, True)]
        assert detail.version.id == second.id
        old = lib.case_detail(store, case_id, first.id)
        assert old.version.number == 1 and not old.version.current
        assert old.payload["request"]["url"]["raw"] == "{{base_url}}/none"

    def test_a_version_of_another_case_is_refused(self, store, library):
        other = store.current_version(library["import"]).id
        with pytest.raises(ValueError):
            lib.case_detail(store, library["none"], other)

    def test_batch_provenance(self, store, library):
        detail = lib.case_detail(store, library["TC-002"])
        (source,) = detail.sources
        assert source.source_type == m.SOURCE_BATCH
        assert source.version_number == 1
        assert source.metadata["executor_reported_status"] == "PASS"
        assert source.metadata["source_title"] is None
        assert source.metadata["title_origin"] == "GENERATED"
        assert source.metadata["depends_on"] == ["TC-001"]

    def test_provenance_metadata_is_masked(self, store, library):
        (source,) = lib.case_detail(store, library["manual"]).sources
        assert source.metadata == {"api_key": lib.MASK, "n": 1}
        assert store.sources(library["manual"])[0].metadata["api_key"] == "s3cr3t-apikey"

    def test_no_source(self, store, library):
        assert lib.case_detail(store, library["none"]).sources == ()


class TestMasking:
    def test_sensitive_values_are_masked_and_references_kept(self):
        item = secret_item()
        before = copy.deepcopy(item)
        masked, count = lib.masked_payload(item)
        assert item == before  # ورودی تغییر نمی‌کند
        text = json.dumps(masked)
        for secret in SECRETS:
            assert secret not in text
        headers = masked["request"]["header"]
        assert headers[0]["value"] == "{{token}}"
        assert headers[1]["value"] == lib.MASK
        assert headers[2]["value"] == lib.MASK
        assert headers[3]["value"] == "visible-trace"
        assert headers[4]["value"] == lib.MASK
        url = masked["request"]["url"]
        assert url["raw"] == "{{base_url}}/a?apiKey=[masked]&page=1&token={{token}}"
        assert [q["value"] for q in url["query"]] == [lib.MASK, "1", "{{token}}"]
        body = masked["request"]["body"]["raw"]
        assert body == '{"password": "[masked]", "user": "alice", "otp": "{{otp}}"}'
        assert masked["event"] == [{"listen": "test", "script": "[script text hidden]"}]
        assert masked["response"] == "[1 saved response example(s) hidden]"
        assert count == 6

    def test_bearer_reference_stays_visible(self):
        item = plain_item(header=[{"key": "Authorization", "value": "Bearer {{token}}"}])
        masked, count = lib.masked_payload(item)
        assert masked["request"]["header"][0]["value"] == "Bearer {{token}}"
        assert count == 0

    def test_form_auth_block_and_userinfo(self):
        item = plain_item(
            body={
                "mode": "urlencoded",
                "urlencoded": [
                    {"key": "client_secret", "value": "s3cr3t-form"},
                    {"key": "name", "value": "plain"},
                ],
            },
            auth={"type": "basic", "basic": [{"key": "password", "value": "s3cr3t-auth"}]},
        )
        item["request"]["url"]["raw"] = "https://user:s3cr3t-userinfo@host/a"
        item["request"]["url"]["auth"] = {"user": "s3cr3t-userinfo"}
        masked, count = lib.masked_payload(item)
        text = json.dumps(masked)
        assert all(secret not in text for secret in SECRETS)
        assert masked["request"]["body"]["urlencoded"][1]["value"] == "plain"
        assert masked["request"]["url"]["raw"] == "https://[masked]@host/a"
        assert count == 4

    def test_templated_json_body_is_masked_without_reformatting(self):
        raw = '{\n  "id": {{id}},\n  "accessKey": "s3cr3t-pass"\n}'
        masked, _ = lib.masked_payload(plain_item(body={"mode": "raw", "raw": raw}))
        assert (
            masked["request"]["body"]["raw"] == '{\n  "id": {{id}},\n  "accessKey": "[masked]"\n}'
        )

    def test_stored_payload_is_unchanged(self, store, library):
        detail = lib.case_detail(store, library["manual"])
        assert detail.masked_values == 6
        assert store.current_version(library["manual"]).payload == secret_item()


class TestScripts:
    def test_summary_without_script_text(self, store, library):
        detail = lib.case_detail(store, library["manual"])
        assert [(s.location, s.listen, s.lines) for s in detail.scripts] == [
            ("$.event[0]", "test", 2)
        ]
        assert "hidden_script_marker" not in repr(detail)

    def test_batch_scripts(self, store, library):
        scripts = lib.case_detail(store, library["TC-002"]).scripts
        assert {s.listen for s in scripts} == {"prerequest", "test"}
        assert all(s.lines > 0 for s in scripts)

    def test_no_scripts(self, store, library):
        assert lib.case_detail(store, library["none"]).scripts == ()


class TestReadOnly:
    def test_reads_change_nothing(self, store, library):
        changes = store._db.total_changes
        rows = lib.library_rows(store)
        lib.filter_rows(rows, text="x", statuses={m.CASE_ACTIVE})
        for row in rows:
            lib.case_detail(store, row.case_id)
        assert store._db.total_changes == changes

    def test_performance_with_500_cases(self, store):
        """D5: APIهای موجود کافی‌اند تا وقتی ۵۰۰ case در بودجه ساخته شوند."""
        with store.transaction():
            for index in range(500):
                store._insert_case(
                    NewCase(title=f"Case {index}", payload=plain_item(f"/c{index}")), "t"
                )
        started = time.perf_counter()
        rows = lib.library_rows(store)
        elapsed = time.perf_counter() - started
        assert len(rows) == 500
        assert elapsed < 5.0, f"library_rows took {elapsed:.2f}s for 500 cases"
