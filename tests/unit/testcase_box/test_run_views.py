"""
tests/unit/testcase_box/test_run_views.py — مدلِ نمایشیِ runها و دسترسیِ امن به artifactها (M7.7)
"""

from __future__ import annotations

import pytest

from src.testcase_box import models as m
from src.testcase_box import runner as runner_module
from src.testcase_box.run_views import VERIFICATION_TEXT, run_artifacts, run_summary, unit_rows
from tests.unit.testcase_box.runner_fakes import FakeEngine, outcome
from tests.unit.testcase_box.test_run_planning import event, item
from src.testcase_box.storage import BoxStore
from tests.unit.testcase_box.runner_fakes import write_configuration
from tests.unit.testcase_box.test_runner import cases, plan, runner


@pytest.fixture(autouse=True)
def _no_real_defaults(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("No real gate, engine or token provider in unit tests.")

    for name in ("_default_gate", "_default_engine", "_default_token_provider"):
        monkeypatch.setattr(runner_module, name, forbidden)


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


def _completed(world, behaviours=None, payloads=None):
    selected = cases(world, *(payloads or (item(path="a"), item(path="b"))))
    result = runner(world, FakeEngine(behaviours or [])).run(plan(world, selected))
    return world["store"].get_run(result.run_id)


def test_summary_counts_and_unverified_are_separate(world):
    run = _completed(
        world,
        [outcome(no_assertions=False), outcome()],
        payloads=(item(path="a"), item(path="b"), item(event())),
    )
    summary = run_summary(world["store"], run)
    assert (summary.total, summary.done, summary.remaining) == (3, 3, 0)
    assert summary.counts[m.UNIT_PASS] == 2 and summary.counts[m.UNIT_NOT_RUN] == 1
    assert (summary.verified, summary.unverified, summary.blocked) == (1, 1, 1)
    assert summary.progress == 1.0 and summary.final
    rows = unit_rows(world["store"], run.id)
    assert [r.verification for r in rows] == ["VERIFIED", "UNVERIFIED", ""]
    assert rows[0].version_number == 1 and rows[0].http_status == (200,)
    assert "not verified by assertions" in VERIFICATION_TEXT["UNVERIFIED"]


def test_available_artifacts_are_only_published_files_inside_the_run(world):
    run = _completed(world)
    entries = run_artifacts(world["runs"], world["store"], run)
    names = sorted({e.name for e in entries if e.available})
    assert names == ["collection.json", "result.json"]
    for entry in entries:
        assert entry.path is None or (world["runs"].resolve() in entry.path.parents)


def test_missing_and_forged_artifacts_are_never_offered(world):
    run = _completed(world, payloads=(item(path="a"),))
    unit = world["store"].run_units(run.id)[0]
    store = world["store"]
    store._db.execute("DROP TRIGGER IF EXISTS tr_box_runs_final_immutable")  # فقط پایگاهِ موقت
    store._db.execute("DROP TRIGGER IF EXISTS tr_box_run_units_final_immutable")
    store._db.execute("DROP TRIGGER IF EXISTS tr_box_run_units_run_final_update")
    forged = {
        **unit.metadata,
        "artifacts": {
            "result_json": "unit_001/../../../secret.txt",
            "html_report": "unit_001/x/../../unit_001/collection.json",
            "stdout_log": "unit_001/missing/stdout.log",
            "json_report": "/etc/passwd",
        },
    }
    store._db.execute(
        "UPDATE box_run_units SET metadata_json = ? WHERE id = ?", (m.to_json(forged), unit.id)
    )
    (world["tmp"] / "secret.txt").write_text("top secret")
    entries = run_artifacts(world["runs"], store, store.get_run(run.id))
    offered = [e for e in entries if e.available]
    assert all(e.name == "collection.json" for e in offered)
    reasons = {e.reason for e in entries if not e.available}
    assert "The file is missing." in reasons and "The file is outside the run directory." in reasons


def test_a_forged_run_directory_name_is_ignored(world):
    run = _completed(world, payloads=(item(path="a"),))
    forged = m.BoxRun(
        **{**run.__dict__, "metadata": {**run.metadata, "artifact_dir": "../outside"}}
    )
    assert run_artifacts(world["runs"], world["store"], forged) == []


def test_a_deleted_run_directory_is_reported_as_missing(world, tmp_path):
    run = _completed(world, payloads=(item(path="a"),))
    entries = run_artifacts(tmp_path / "elsewhere", world["store"], run)
    assert len(entries) == 1 and not entries[0].available and "missing" in entries[0].reason


# ── شمارش‌ها و ردیف‌ها یک طبقه‌بندی دارند (R1) ───────────────────────────────


def _mixed(world):
    """PASS ِ تأییدشده، PASS ِ بی‌assertion، FAIL، ERROR و NOT_RUN (caseِ مسدود) در یک run."""
    return _completed(
        world,
        [
            outcome(no_assertions=False),
            outcome(),
            outcome(status="FAIL", no_assertions=False),
            outcome(status="ERROR", exit_code=1),
        ],
        payloads=(item(path="a"), item(path="b"), item(path="c"), item(path="d"), item(event())),
    )


def _drop_final_triggers(store):  # فقط پایگاهِ موقتِ تست — فراداده‌ی runِ نهایی را جعل می‌کنیم
    names = [
        row[0] for row in store._db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    ]
    for name in names:
        store._db.execute(f'DROP TRIGGER IF EXISTS "{name}"')


def _strip_verification(store, run_id, unit_number):
    _drop_final_triggers(store)
    unit = next(u for u in store.run_units(run_id) if u.unit_number == unit_number)
    metadata = {k: v for k, v in unit.metadata.items() if k != "verification"}
    store._db.execute(
        "UPDATE box_run_units SET metadata_json = ? WHERE id = ?", (m.to_json(metadata), unit.id)
    )
    for case in store.run_cases(run_id, unit_id=unit.id):
        result = {k: v for k, v in case.result_metadata.items() if k != "verification"}
        store._db.execute(
            "UPDATE box_run_cases SET result_metadata_json = ? WHERE id = ?",
            (m.to_json(result), case.id),
        )


@pytest.mark.parametrize(
    ("status", "verification", "verified", "unverified", "result"),
    [
        (m.UNIT_PASS, "VERIFIED", True, False, "VERIFIED_PASS"),
        (m.UNIT_PASS, "UNVERIFIED", False, True, "UNVERIFIED"),
        (m.UNIT_PASS, "", False, True, "UNVERIFIED"),  # بی‌فراداده‌ی verification
        (m.UNIT_FAIL, "FAILED", False, False, m.UNIT_FAIL),
        (m.UNIT_ERROR, "NOT_VERIFIED", False, False, m.UNIT_ERROR),
        (m.UNIT_ERROR, "VERIFIED", False, False, m.UNIT_ERROR),  # ناسازگار: هرگز PASS نیست
        (m.UNIT_NOT_RUN, "", False, False, m.UNIT_NOT_RUN),
    ],
)
def test_the_row_classification_table(status, verification, verified, unverified, result):
    from src.testcase_box.run_views import UnitRow

    row = UnitRow(1, 1, "TC-1", 1, 1, status, "", "", verification, ())
    assert (row.verified_pass, row.unverified_pass, row.result) == (verified, unverified, result)


def test_a_mixed_run_counts_every_state_once(world):
    run = _mixed(world)
    summary = run_summary(world["store"], run)
    rows = unit_rows(world["store"], run.id)
    assert [r.result for r in rows] == [
        "VERIFIED_PASS",
        "UNVERIFIED",
        m.UNIT_FAIL,
        m.UNIT_ERROR,
        m.UNIT_NOT_RUN,
    ]
    assert (summary.verified, summary.unverified) == (1, 1)
    assert summary.counts == {
        m.UNIT_PASS: 2,
        m.UNIT_FAIL: 1,
        m.UNIT_ERROR: 1,
        m.UNIT_NOT_RUN: 1,
        m.UNIT_CANCELLED: 0,
    }


def test_a_pass_with_missing_verification_metadata_is_counted_as_unverified(world):
    run = _completed(world, [outcome(no_assertions=False)], payloads=(item(path="a"),))
    store = world["store"]
    _strip_verification(store, run.id, 1)
    (row,) = unit_rows(store, run.id)
    assert row.status == m.UNIT_PASS and row.verification == ""  # وضعیتِ ذخیره‌شده دست نخورد
    summary = run_summary(store, store.get_run(run.id))
    assert row.result == "UNVERIFIED"
    assert (summary.verified, summary.unverified) == (0, 1)


def test_summary_counts_equal_the_row_level_states(world):
    run = _mixed(world)
    store = world["store"]
    _strip_verification(store, run.id, 1)  # PASS ِ تأییدشده بی‌فراداده می‌شود → UNVERIFIED
    rows = unit_rows(store, run.id)
    summary = run_summary(store, store.get_run(run.id))
    assert summary.verified == sum(1 for r in rows if r.result == "VERIFIED_PASS") == 0
    assert summary.unverified == sum(1 for r in rows if r.result == "UNVERIFIED") == 2
    assert summary.verified + summary.unverified == summary.counts[m.UNIT_PASS]
    assert sum(summary.counts.values()) == summary.total == len(rows)
