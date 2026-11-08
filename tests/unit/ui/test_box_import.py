"""
تست‌های بخشِ «Import from Batch» در صفحه‌ی TestCase Box (ui/box_app.py) — M6.3.

با APIهای واقعیِ M5 و artifactهای Batch ساخته‌شده با batch_fixtures (همان نویسنده‌ی Batch)
داخلِ tmp_path. هیچ Batch، Newman یا پایگاهِ پیش‌فرضی لمس نمی‌شود. mock فقط برای شکستِ
عمدی و گزارشِ ساختگیِ انتخابِ تکراری است.
"""

from __future__ import annotations

import json
from contextlib import closing
import sqlite3
from pathlib import Path

import pytest

streamlit = pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from src.testcase_box import batch_import as bi  # noqa: E402
from src.testcase_box.batch_source import AttemptRef  # noqa: E402
from src.testcase_box.storage import BoxStore  # noqa: E402
from tests.unit.testcase_box.batch_fixtures import (  # noqa: E402
    BATCH_ID,
    ISSUE,
    build_collection,
    generator_case,
    tree_state,
    write_attempt,
)
from ui import box_app  # noqa: E402

_PAGE = "from ui import box_app\nbox_app.render()\n"
RUN_1 = AttemptRef(BATCH_ID, ISSUE, 1).reference
RUN_2 = AttemptRef(BATCH_ID, ISSUE, 2).reference
SECRET = "s3cr3t-literal-header"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(box_app, "DB_PATH", tmp_path / "library" / "box.sqlite3")
    monkeypatch.setattr(box_app, "BATCH_ROOT", tmp_path / "artifacts")


@pytest.fixture
def db() -> Path:
    return box_app.DB_PATH


@pytest.fixture
def root() -> Path:
    return box_app.BATCH_ROOT


@pytest.fixture
def run(root) -> Path:
    return write_attempt(root)


def _page(run_reference: str | None = None) -> AppTest:
    app = AppTest.from_string(_PAGE, default_timeout=60)
    app.session_state["box_section"] = "Import from Batch"
    if run_reference is not None:
        app.session_state["box_import_run"] = run_reference
    return app.run()


def _digest(app: AppTest) -> str:
    return app.session_state["_box_import_preview"]["digest"]


def _cases(app: AppTest):
    return app.multiselect(key=f"box_import_cases_{_digest(app)}")


def _token(app: AppTest) -> int:
    state = app.session_state
    return state["_box_import_token"] if "_box_import_token" in state else 0


def _go(app: AppTest):
    """دکمه‌ی Import ِ رسم‌شده — کلیدش digest ِ پیش‌نمایش و token ِ یک‌بارمصرف را دارد."""
    return app.button(key=f"box_import_go_{_digest(app)}_{_token(app)}")


def _import(app: AppTest) -> AppTest:
    return _go(app).click().run()


def _texts(node) -> list[str]:
    found = []
    proto = getattr(node, "proto", None)
    if proto is not None:
        found.append(str(proto))
    for child in (getattr(node, "children", None) or {}).values():
        found.extend(_texts(child))
    return found


def _rendered(app: AppTest) -> str:
    return "\n".join(_texts(app.main) + _texts(app.sidebar))


def _messages(elements) -> list[str]:
    return [element.value for element in elements]


def _library(db: Path) -> list[tuple[str, str, str]]:
    """(عنوان، مرجعِ منبع، title_origin) برای هر case — از راهِ API ِ ذخیره‌سازی."""
    with BoxStore.open(db) as store:
        rows = []
        for case in store.list_cases():
            (source,) = store.sources(case.id)
            rows.append((case.title, source.source_reference, source.metadata["title_origin"]))
        return sorted(rows, key=lambda row: row[1])


def _jobs(db: Path) -> int:
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as connection:
        return connection.execute("SELECT COUNT(*) FROM box_import_jobs").fetchone()[0]


class TestDiscovery:
    def test_the_library_stays_the_default_section(self, run):
        app = AppTest.from_string(_PAGE, default_timeout=60).run()
        assert not app.exception
        assert app.radio(key="box_section").value == "Library"
        assert "box_import_run" not in app.session_state

    def test_no_runs(self, db):
        app = _page()
        assert not app.exception
        assert "No Batch runs were found" in app.info[0].value
        assert not db.exists() and not db.parent.exists()

    def test_eligible_and_ineligible_runs_with_reasons(self, root, db):
        write_attempt(root)
        write_attempt(root, attempt=2, summary=None)
        write_attempt(root, attempt=3, summary="minimal")
        directory = write_attempt(root, attempt=4)
        (directory / "collection.json").write_bytes(b"{ not json")
        app = _page()
        assert not app.exception
        runs = app.dataframe[0].value
        assert list(runs["Attempt"]) == [1, 2, 3, 4]
        assert list(runs["Can import"]) == ["Yes", "No", "No", "No"]
        reasons = list(runs["Reason"])
        assert reasons[0] == "—"
        assert reasons[1].startswith("IN_PROGRESS_OR_INTERRUPTED — ")
        assert reasons[2].startswith("MINIMAL_SUMMARY — ")
        assert reasons[3].startswith("ARTIFACT_MALFORMED — ")
        assert "(collection.json)" in reasons[3]
        assert "4 run(s) found · 1 can be imported · 3 cannot." in _messages(app.caption)
        assert app.selectbox(key="box_import_run").options == [
            f"{ISSUE} · attempt 1 · batch {BATCH_ID[:8]} · PASS"
        ]
        assert not db.exists()

    def test_only_ineligible_runs(self, root):
        write_attempt(root, summary=None)
        app = _page()
        assert not app.exception
        assert "None of these runs can be imported." in _messages(app.info)
        assert not app.selectbox


class TestPreview:
    def test_preview_creates_nothing_and_writes_nothing(self, run, root, db):
        before = tree_state(root)
        app = _page(RUN_1)
        assert not app.exception
        preview = app.dataframe[1].value
        assert list(preview["Case"]) == ["TC-001", "TC-002", "TC-003"]
        assert list(preview["Can import"]) == ["Yes", "Yes", "Yes"]
        assert list(preview["In library"]) == ["—", "—", "—"]
        assert "DEPENDENCIES_NOT_LINKED" in preview["Notes"][1]
        assert "needs TC-001 (in this run)" in preview["Notes"][1]
        assert _cases(app).value == ["TC-001", "TC-002", "TC-003"]
        assert "Nothing has been written yet." in _rendered(app)
        # انتخاب و عنوان عوض می‌شوند؛ هنوز هیچ نوشتنی نیست
        _cases(app).set_value(["TC-003"]).run()
        app.text_input(key=f"box_import_title_{_digest(app)}_TC-003").input("Health").run()
        assert not app.exception
        assert not db.exists() and not db.parent.exists()
        assert tree_state(root) == before

    def test_no_writes_to_an_existing_library_before_import(self, run, db):
        BoxStore.open(db).close()
        before = db.read_bytes()
        app = _page(RUN_1)
        _cases(app).set_value(["TC-001"]).run()
        assert not app.exception
        assert db.read_bytes() == before

    def test_already_present_and_rejected_cases(self, root, db):
        collection = build_collection(
            [generator_case("TC-001", path="/health"), generator_case("TC-002", path="/x")]
        )
        collection["item"][0]["item"][1]["request"]["header"] = [
            {"key": "Authorization", "value": SECRET}
        ]
        write_attempt(
            root,
            cases=[generator_case("TC-001", path="/health"), generator_case("TC-002", path="/x")],
            collection=collection,
        )
        app = _import(_page(RUN_1))
        assert _library(db) == [(f"{ISSUE} · TC-001", f"{RUN_1}/TC-001", "GENERATED")]
        app = _page(RUN_1)
        assert not app.exception
        preview = app.dataframe[1].value
        assert list(preview["In library"]) == ["BX-000001", "—"]
        assert list(preview["Can import"]) == ["Yes", "No"]
        assert "LITERAL_CREDENTIAL — " in preview["Reason"][1]
        assert "SOURCE_PAYLOAD_REJECTED — " in preview["Reason"][1]
        # فقط caseهای قابلِ واردکردن قابلِ انتخاب‌اند و caseِ موجود پیش‌فرض انتخاب نیست
        assert _cases(app).options == ["TC-001"]
        assert _cases(app).value == []
        assert _go(app).disabled
        rendered = _rendered(app)
        assert SECRET not in rendered
        assert "pm.test" not in rendered and "pm.response" not in rendered

    def test_an_unreadable_library_blocks_the_preview(self, run, db):
        db.parent.mkdir(parents=True)
        db.write_bytes(b"not a database " * 8)
        app = _page(RUN_1)
        assert not app.exception
        assert "could not be opened" in app.error[0].value
        assert "_box_import_preview" not in app.session_state
        assert db.read_bytes() == b"not a database " * 8


class TestImport:
    def test_only_the_selected_cases_are_imported(self, run, db):
        app = _page(RUN_1)
        _cases(app).set_value(["TC-001", "TC-003"]).run()
        app = _import(app)
        assert not app.exception
        assert _library(db) == [
            (f"{ISSUE} · TC-001", f"{RUN_1}/TC-001", "GENERATED"),
            (f"{ISSUE} · TC-003", f"{RUN_1}/TC-003", "GENERATED"),
        ]
        assert len(app.success) == 1
        assert app.success[0].value.startswith("Imported 2 test case(s). Added 2 ·")
        report = next(f.value for f in app.dataframe if "Outcome" in f.value.columns)
        assert list(report["Case"]) == ["TC-001", "TC-003"]
        assert list(report["Outcome"]) == ["Added", "Added"]
        assert list(report["Library key"]) == ["BX-000001", "BX-000002"]
        assert _jobs(db) == 1

    def test_the_preview_fingerprints_reach_the_existing_api(self, run, monkeypatch):
        calls = []
        real = box_app.import_cases

        def spy(store, root, ref, selections, **kwargs):
            calls.append((root, ref, tuple(selections), kwargs))
            return real(store, root, ref, selections, **kwargs)

        monkeypatch.setattr(box_app, "import_cases", spy)
        app = _page(RUN_1)
        preview = app.session_state["_box_import_preview"]
        _cases(app).set_value(["TC-002"]).run()
        title_key = f"box_import_title_{_digest(app)}_TC-002"
        app.text_input(key=title_key).input("  Voucher lookup  ").run()
        _import(app)
        ((root, ref, selections, kwargs),) = calls
        assert root == box_app.BATCH_ROOT
        assert ref == AttemptRef(BATCH_ID, ISSUE, 1)
        assert selections == (
            bi.ImportSelection("TC-002", preview["fingerprints"]["TC-002"], "Voucher lookup"),
        )
        assert kwargs == {"expected_snapshot_fingerprint": preview["snapshot"]}

    def test_a_title_override_is_stored_as_a_user_title(self, run, db):
        app = _page(RUN_1)
        _cases(app).set_value(["TC-003"]).run()
        app.text_input(key=f"box_import_title_{_digest(app)}_TC-003").input("Health check").run()
        app = _import(app)
        assert not app.exception
        assert _library(db) == [("Health check", f"{RUN_1}/TC-003", "USER")]

    def test_an_invalid_title_stops_before_any_write(self, run, db):
        app = _page(RUN_1)
        _cases(app).set_value(["TC-003"]).run()
        app.text_input(key=f"box_import_title_{_digest(app)}_TC-003").input("x" * 301).run()
        app = _import(app)
        assert not app.exception
        assert _messages(app.error) == [
            "Nothing was imported. TC-003: Title must be at most 300 characters. Fix the "
            "title and click Import again."
        ]
        assert not app.success
        assert not db.exists()

    def test_a_source_change_after_the_preview_is_rejected(self, run, db):
        app = _page(RUN_1)
        old_digest = _digest(app)
        collection = run / "collection.json"
        collection.write_text(
            json.dumps(json.loads(collection.read_text(encoding="utf-8")), indent=1),
            encoding="utf-8",
        )
        app = _import(app)
        assert not app.exception
        assert not app.success
        assert app.error[0].value.startswith("No test case was imported. Added 0 ·")
        assert any("changed after it was previewed" in w for w in _messages(app.warning))
        report = next(f.value for f in app.dataframe if "Outcome" in f.value.columns)
        assert set(report["Outcome"]) == {"Rejected"}
        assert all(r.startswith("SOURCE_CHANGED — ") for r in report["Reason"])
        with BoxStore.open(db) as store:
            assert store.list_cases() == []
        # پیش‌نمایش دوباره خوانده شده است و Import ِ تازه با اثرانگشتِ تازه کار می‌کند
        assert _digest(app) != old_digest
        app = _import(app)
        assert len(app.success) == 1
        assert len(_library(db)) == 3

    def test_a_partial_import_is_reported_as_partial(self, run, db, monkeypatch):
        real = BoxStore.create_case_for_source

        def flaky(self, new):
            if new.sources[0].source_reference.endswith("/TC-002"):
                raise sqlite3.OperationalError("database is locked")
            return real(self, new)

        monkeypatch.setattr(BoxStore, "create_case_for_source", flaky)
        app = _import(_page(RUN_1))
        assert not app.exception
        assert not app.success
        assert app.warning[0].value.startswith("Partly imported: 2 of 3 case(s) were added")
        report = next(f.value for f in app.dataframe if "Outcome" in f.value.columns)
        assert list(report["Outcome"]) == ["Added", "Failed", "Added"]
        assert report["Reason"][1] == "OperationalError"
        assert [row[1] for row in _library(db)] == [f"{RUN_1}/TC-001", f"{RUN_1}/TC-003"]

    def test_an_entirely_failed_import_never_reports_success(self, run, db, monkeypatch):
        def broken(self, new):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(BoxStore, "create_case_for_source", broken)
        app = _import(_page(RUN_1))
        assert not app.exception
        assert not app.success
        assert app.error[0].value.startswith("No test case was imported.")
        assert "Failed 3" in app.error[0].value
        with BoxStore.open(db) as store:
            assert store.list_cases() == []

    def test_already_present_cases_are_reported_without_success(self, run, db):
        _import(_page(RUN_1))
        app = _page(RUN_1)
        _cases(app).set_value(["TC-001", "TC-002"]).run()
        app = _import(app)
        assert not app.exception
        assert not app.success
        assert app.info[0].value.startswith(
            "Nothing new was imported: all selected cases are already in the library."
        )
        report = next(f.value for f in app.dataframe if "Outcome" in f.value.columns)
        assert list(report["Outcome"]) == ["Already in library", "Already in library"]
        assert len(_library(db)) == 3

    def test_a_duplicate_selection_is_labelled_from_the_report(self, run, monkeypatch):
        def fake(store, root, ref, selections, **kwargs):
            outcomes = (
                bi.CaseOutcome("TC-001", f"{RUN_1}/TC-001", bi.OUTCOME_ADDED, box_case_id=1),
                bi.CaseOutcome("TC-001", None, bi.OUTCOME_REJECTED, (bi.SELECTION_DUPLICATE,)),
            )
            return bi.ImportReport(
                7, ref.reference, kwargs["expected_snapshot_fingerprint"], outcomes
            )

        monkeypatch.setattr(box_app, "import_cases", fake)
        app = _import(_page(RUN_1))
        assert not app.exception
        assert not app.success
        assert app.warning[0].value.startswith("Partly imported: 1 of 2")
        report = next(f.value for f in app.dataframe if "Outcome" in f.value.columns)
        assert report["Reason"][1] == ("SELECTION_DUPLICATE — the case was selected more than once")

    def test_an_unexpected_error_shows_only_its_class(self, run, monkeypatch):
        def crash(*args, **kwargs):
            raise RuntimeError(f"boom {SECRET}")

        monkeypatch.setattr(box_app, "import_cases", crash)
        app = _import(_page(RUN_1))
        assert not app.exception
        assert not app.success
        assert _messages(app.error) == [
            "The import stopped before an import job was created (RuntimeError). Nothing was "
            "imported; you can click Import again."
        ]
        assert SECRET not in _rendered(app)

    def test_reruns_never_repeat_the_import(self, run, db):
        app = _import(_page(RUN_1))
        assert len(app.success) == 1
        app.run()
        app.run()
        assert not app.exception
        assert _jobs(db) == 1
        assert len(_library(db)) == 3
        assert _cases(app).value == []
        assert _go(app).disabled

    def test_switching_runs_drops_the_report_and_preview(self, root, db):
        write_attempt(root)
        write_attempt(root, attempt=2)
        app = _import(_page(RUN_1))
        assert len(app.success) == 1
        app.selectbox(key="box_import_run").set_value(RUN_2).run()
        assert not app.exception
        assert not app.success
        assert "_box_import_report" not in app.session_state
        assert app.session_state["_box_import_preview"]["reference"] == RUN_2
        assert _cases(app).value == ["TC-001", "TC-002", "TC-003"]
        app.selectbox(key="box_import_run").set_value(None).run()
        assert "_box_import_preview" not in app.session_state
        assert _jobs(db) == 1

    def test_imported_cases_appear_in_the_library_with_provenance(self, run, db):
        app = _import(_page(RUN_1))
        app.session_state["box_section"] = "Library"
        with BoxStore.open(db) as store:
            batch_case = next(c for c in store.list_cases() if c.title.endswith("TC-002"))
        app.session_state["box_selected_case_id"] = batch_case.id
        app.run()
        assert not app.exception
        assert set(app.dataframe[0].value["Source"]) == {"Batch"}
        rendered = _rendered(app)
        assert "DEPENDENCIES_NOT_LINKED" in rendered
        assert "as reported by the Batch executor, not the final Batch database status" in (
            rendered
        )

    def test_import_uses_only_box_keys(self, run):
        app = _import(_page(RUN_1))
        assert not app.exception
        keys = set(app.session_state.keys())
        assert all(key.startswith(("box_", "_box_")) for key in keys), keys


# ── اصلاح‌های پایداریِ M6.3 (F1–F4) ─────────────────────────────────────────


def _job(db: Path, job_id: int):
    with BoxStore.open(db) as store:
        return store.get_import_job(job_id)


def _fail_on_second_case(monkeypatch) -> None:
    """خطایی داخلِ پردازشِ import_cases، پس از نوشته‌شدنِ caseِ اول (بیرون از try ِ هر case)."""
    real = bi._readiness
    calls = []

    def readiness(source):
        calls.append(source.case_id)
        if len(calls) == 2:
            raise RuntimeError(f"boom {SECRET}")
        return real(source)

    monkeypatch.setattr(bi, "_readiness", readiness)


class TestImportReliability:
    # F1 — دوبار-کلیک
    def test_the_second_click_of_a_double_click_imports_nothing(self, run, db):
        """AppTest نمی‌تواند دو کلیکِ مرورگر روی یک دکمه‌ی رسم‌شده را بفرستد؛ پس کلیکِ دوم
        شبیه‌سازی می‌شود: token ِ همان دکمه پیش از کلیک مصرف‌شده است (کاری که کلیکِ اول
        می‌کند) و انتخابِ قدیمی هنوز در session است (همان چیزی که مرورگر دوباره می‌فرستد)."""
        app = _page(RUN_1)
        button = _go(app)
        app.session_state["_box_import_token"] = _token(app) + 1
        button.click().run()
        assert not app.exception
        assert "This Import click was already handled. Nothing more was imported." in (
            _messages(app.info)
        )
        assert not app.success
        assert not db.exists()

    def test_a_handled_click_retires_its_button(self, run, db):
        app = _page(RUN_1)
        old_key = _go(app).key
        app = _import(app)
        assert len(app.success) == 1
        assert _go(app).key != old_key
        assert not [b for b in app.button if b.key == old_key]
        # انتخابِ قدیمی دوباره فرستاده می‌شود و کلیک‌ها روی دکمه‌ی تازه — هر کلیک فقط یک کار
        _cases(app).set_value(["TC-001"]).run()
        app = _import(app)
        assert app.info[0].value.startswith("Nothing new was imported")
        assert _jobs(db) == 2
        assert len(_library(db)) == 3

    def test_a_concurrent_import_is_refused(self, run, db):
        app = _page(RUN_1)
        assert box_app._IMPORT_LOCK.acquire(blocking=False)
        try:
            app = _import(app)
        finally:
            box_app._IMPORT_LOCK.release()
        assert not app.exception
        assert app.warning[0].value.startswith("Another import is running in this app.")
        assert not db.exists()
        assert _cases(app).value == ["TC-001", "TC-002", "TC-003"]
        app = _import(app)
        assert len(app.success) == 1

    # F2 — پیامِ خطا فقط چیزی را می‌گوید که معلوم است
    def test_the_library_could_not_be_opened(self, run, db, monkeypatch):
        def broken(cls, path):
            raise box_app.StorageError(f"boom {SECRET}")

        monkeypatch.setattr(box_app._ObservedStore, "open", classmethod(broken))
        app = _import(_page(RUN_1))
        assert not app.exception
        assert _messages(app.error) == [
            "The TestCase Box library could not be opened (StorageError). No import job was "
            "created. Nothing was imported; you can click Import again."
        ]
        assert _cases(app).value == ["TC-001", "TC-002", "TC-003"]
        assert SECRET not in _rendered(app)

    def test_the_import_job_could_not_be_created(self, run, db, monkeypatch):
        def broken(self, *args, **kwargs):
            raise sqlite3.OperationalError(f"boom {SECRET}")

        monkeypatch.setattr(BoxStore, "create_import_job", broken)
        app = _import(_page(RUN_1))
        assert not app.exception
        assert _messages(app.error) == [
            "The import job could not be created or confirmed (OperationalError). No test case "
            "was imported; you can click Import again."
        ]
        assert _cases(app).value == ["TC-001", "TC-002", "TC-003"]
        assert _jobs(db) == 0
        with BoxStore.open(db) as store:
            assert store.list_cases() == []

    def test_processing_failed_after_the_job_was_created(self, run, db, monkeypatch):
        app = _page(RUN_1)
        _fail_on_second_case(monkeypatch)
        app = _import(app)
        assert not app.exception
        assert not app.success
        (error,) = _messages(app.error)
        assert error.startswith(
            "The import stopped with an error (RuntimeError). Import job #1 was recorded as "
            "failed. 1 test case(s) were added before the error and stay in the library."
        )
        assert _job(db, 1).status == box_app.IMPORT_FAILED
        assert [row[1] for row in _library(db)] == [f"{RUN_1}/TC-001"]
        # ممکن است caseهایی نوشته شده باشند — انتخاب پاک می‌شود و دکمه غیرفعال است
        assert _cases(app).value == []
        assert _go(app).disabled
        assert SECRET not in _rendered(app)

    def test_the_failed_job_could_not_be_closed(self, run, db, monkeypatch):
        app = _page(RUN_1)
        _fail_on_second_case(monkeypatch)

        def broken(self, job_id, status, summary=None):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(BoxStore, "finish_import_job", broken)
        app = _import(app)
        assert not app.exception
        (error,) = _messages(app.error)
        assert error.startswith(
            "The import stopped with an error (RuntimeError) and import job #1 could not be "
            "closed; its final state is unknown. 1 test case(s) were added"
        )
        assert "recorded as failed" not in error
        assert _job(db, 1).status == "PENDING"
        assert _cases(app).value == []

    def test_final_job_completion_failed(self, run, db, monkeypatch):
        real = BoxStore.finish_import_job

        def no_completion(self, job_id, status, summary=None):
            if status == box_app.IMPORT_COMPLETED:
                raise sqlite3.OperationalError(f"boom {SECRET}")
            return real(self, job_id, status, summary)

        monkeypatch.setattr(BoxStore, "finish_import_job", no_completion)
        app = _import(_page(RUN_1))
        assert not app.exception
        assert not app.success
        (error,) = _messages(app.error)
        assert error.startswith(
            "All selected cases were processed, but import job #1 could not be marked "
            "completed (OperationalError); its final state is unknown. 3 test case(s) were "
            "added before the error and stay in the library."
        )
        assert "recorded as failed" not in error
        assert _job(db, 1).status == "PENDING"
        assert len(_library(db)) == 3
        assert _cases(app).value == []
        assert SECRET not in _rendered(app)

    # F3 — عنوانِ نامعتبر: انتخاب می‌ماند و هر تلاش فقط همان خطا را می‌دهد
    def test_an_invalid_title_keeps_the_selection_for_a_fix(self, run, db):
        app = _page(RUN_1)
        _cases(app).set_value(["TC-003"]).run()
        title_key = f"box_import_title_{_digest(app)}_TC-003"
        app.text_input(key=title_key).input("x" * 301).run()
        for _ in range(2):
            app = _import(app)
            assert not app.exception
            assert _messages(app.error) == [
                "Nothing was imported. TC-003: Title must be at most 300 characters. Fix the "
                "title and click Import again."
            ]
            assert _cases(app).value == ["TC-003"]
            assert not db.exists()
        app.text_input(key=title_key).input("Health check").run()
        app = _import(app)
        assert len(app.success) == 1
        assert _library(db) == [("Health check", f"{RUN_1}/TC-003", "USER")]
        assert _jobs(db) == 1

    # F4 — خروج‌های ایمنی با پیام
    def test_a_click_on_an_outdated_preview_explains_itself(self, run, db):
        app = _page(RUN_1)
        button = _go(app)
        preview = dict(app.session_state["_box_import_preview"], digest="another-preview")
        app.session_state["_box_import_preview"] = preview
        button.click().run()
        assert not app.exception
        assert app.warning[0].value.startswith("The preview changed before your click was handled")
        assert not app.success and not app.error
        assert not db.exists()

    def test_a_click_without_valid_cases_explains_itself(self, run, db):
        app = _page(RUN_1)
        button = _go(app)
        preview = dict(app.session_state["_box_import_preview"], fingerprints={})
        app.session_state["_box_import_preview"] = preview
        button.click().run()
        assert not app.exception
        assert app.warning[0].value.startswith(
            "None of the selected cases can be imported from this preview. Nothing was imported."
        )
        assert not app.success
        assert not db.exists()

    def test_a_notice_does_not_replace_the_last_report(self, run, db):
        app = _import(_page(RUN_1))
        button = _go(app)
        _cases(app).set_value(["TC-001"]).run()
        button = _go(app)
        app.session_state["_box_import_token"] = _token(app) + 1
        button.click().run()
        assert "This Import click was already handled. Nothing more was imported." in (
            _messages(app.info)
        )
        assert len(app.success) == 1
        assert _jobs(db) == 1
