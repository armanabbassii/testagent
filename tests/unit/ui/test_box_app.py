"""
تست‌های صفحه‌ی TestCase Box (ui/box_app.py) روی runtimeِ واقعیِ Streamlit — M6.1.

پایگاهِ موقت؛ هیچ Jira، Batch، Newman یا پایگاهِ پیش‌فرضی لمس نمی‌شود. نکته‌های اصلی:
دیدنِ صفحه پایگاه را نمی‌سازد، هیچ مقدارِ حساس یا متنِ اسکریپتی رندر نمی‌شود، کلیدهای
session فقط «box_»/«_box_» هستند و ویزارد صفحه‌ی پیش‌فرض می‌ماند.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

streamlit = pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from src.testcase_box import models as m  # noqa: E402
from src.testcase_box.models import NewCase, NewSource, NewVersion  # noqa: E402
from src.testcase_box.storage import BoxStore  # noqa: E402
from tests.unit.testcase_box.test_library import (  # noqa: E402
    SECRETS,
    plain_item,
    secret_item,
)
from ui import box_app  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PAGE = "from ui import box_app\nbox_app.render()\n"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(box_app, "DB_PATH", tmp_path / "library" / "box.sqlite3")


@pytest.fixture
def db() -> Path:
    return box_app.DB_PATH


@pytest.fixture
def cases(db: Path) -> dict[str, int]:
    with BoxStore.open(db) as store:
        ids = {
            "secret": store.create_case(
                NewCase(
                    title="Hand written secrets",
                    payload=secret_item(),
                    sources=(NewSource(m.SOURCE_MANUAL, "ui"),),
                )
            ).id,
            "batch": store.create_case(
                NewCase(
                    title="DEMO-1 · TC-002",
                    payload=plain_item("/voucher/{{voucherId}}"),
                    sources=(
                        NewSource(
                            m.SOURCE_BATCH,
                            "batch/b/DEMO-1/execution_1/TC-002",
                            {
                                "issue_key": "DEMO-1",
                                "executor_reported_status": "FAIL",
                                "depends_on": ["TC-001"],
                                "title_origin": "GENERATED",
                                "source_title": None,
                            },
                        ),
                    ),
                )
            ).id,
            "archived": store.create_case(NewCase(title="Old case", payload=plain_item("/o"))).id,
        }
        ids["batch_v1"] = store.current_version(ids["batch"]).id
        store.add_version(ids["batch"], NewVersion(payload=plain_item("/voucher/v2")))
        store.archive_case(ids["archived"])
    return ids


def _page(selected: int | None = None) -> AppTest:
    app = AppTest.from_string(_PAGE, default_timeout=60)
    if selected is not None:
        app.session_state["box_selected_case_id"] = selected
    return app.run()


def _texts(node) -> list[str]:
    """متنِ همه‌ی عنصرهای رندرشده (proto ِ کامل، از جمله داده‌ی جدول‌ها و JSON)."""
    found = []
    proto = getattr(node, "proto", None)
    if proto is not None:
        found.append(str(proto))
    children = getattr(node, "children", None) or {}
    for child in children.values():
        found.extend(_texts(child))
    return found


def _rendered(app: AppTest) -> str:
    return "\n".join(_texts(app.main) + _texts(app.sidebar))


def _keys(app: AppTest) -> set[str]:
    return set(app.session_state.keys())


class TestNavigation:
    def test_box_is_the_third_page_and_the_wizard_stays_default(self):
        tree = ast.parse((_REPO_ROOT / "ui" / "app.py").read_text(encoding="utf-8"))
        pages = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Page"
        ]
        described = [
            {kw.arg: ast.literal_eval(kw.value) for kw in page.keywords if kw.arg != "icon"}
            for page in pages
        ]
        assert [page["title"] for page in described] == [
            "Single issue",
            "Batch testing",
            "TestCase Box",
        ]
        assert described[2]["url_path"] == "testcase-box"
        assert [page.get("default", False) for page in described] == [True, False, False]

    def test_the_app_still_opens_on_the_wizard(self):
        app = AppTest.from_file(str(_REPO_ROOT / "ui" / "app.py"), default_timeout=120).run()
        assert not app.exception
        assert app.title[0].value == "Test case generator"
        assert "workflow_state" in app.session_state


class TestEmptyLibrary:
    def test_visiting_the_page_never_creates_the_database(self, db):
        app = _page()
        assert not app.exception
        assert app.title[0].value == "TestCase Box"
        assert "no TestCase Box database exists yet" in app.info[0].value
        assert not db.exists() and not db.parent.exists()

    def test_an_existing_empty_library(self, db):
        BoxStore.open(db).close()
        app = _page()
        assert not app.exception
        assert app.info[0].value.startswith("The library has no test cases yet")

    def test_an_unreadable_database_is_reported_without_details(self, db):
        db.parent.mkdir(parents=True)
        db.write_bytes(b"not a database, just some bytes " * 8)
        app = _page()
        assert not app.exception
        assert "could not be opened" in app.error[0].value
        assert db.read_bytes() == b"not a database, just some bytes " * 8


class TestLibrary:
    def test_active_cases_are_listed_by_default(self, cases):
        app = _page()
        assert not app.exception
        table = app.dataframe[0].value
        assert set(table["Title"]) == {"Hand written secrets", "DEMO-1 · TC-002"}
        assert set(table["Source"]) == {"Manual", "Batch"}
        assert set(table["Readiness"]) == {"Not executable", "Ready"}
        assert "Showing 2 of 3 test cases." in [c.value for c in app.caption]

    def test_filters(self, cases):
        app = AppTest.from_string(_PAGE, default_timeout=60)
        app.session_state["box_filter_status"] = [m.CASE_ARCHIVED]
        app.run()
        assert list(app.dataframe[0].value["Title"]) == ["Old case"]
        app.session_state["box_filter_status"] = []
        app.session_state["box_search"] = "tc-002"
        app.run()
        assert list(app.dataframe[0].value["Title"]) == ["DEMO-1 · TC-002"]
        app.session_state["box_search"] = "nothing matches"
        app.run()
        assert "No test case matches these filters." in [i.value for i in app.info]

    def test_reading_writes_nothing(self, db, cases):
        with BoxStore.open(db):
            pass
        before = db.read_bytes()
        app = _page(cases["batch"])
        assert not app.exception
        assert db.read_bytes() == before


class TestDetails:
    def test_tabs_readiness_and_configuration_note(self, cases):
        app = _page(cases["batch"])
        assert not app.exception
        assert [tab.label for tab in app.tabs] == [
            "Overview",
            "Request",
            "Readiness",
            "Provenance",
            "Versions",
        ]
        rendered = _rendered(app)
        assert "Configuration: not evaluated. Requires an execution profile." in rendered
        assert "NEEDS_CONFIGURATION" not in rendered
        assert "as reported by the Batch executor, not the final Batch database status" in (
            rendered
        )
        assert "Not available" in rendered
        assert "DEPENDENCIES_NOT_LINKED" in rendered

    def test_versions_and_earlier_version_selection(self, cases):
        app = _page(cases["batch"])
        versions = app.dataframe[-1].value
        assert list(versions["Version"]) == ["v1", "v2"]
        assert list(versions["Current"]) == ["", "Yes"]
        key = f"box_version_{cases['batch']}"
        assert app.selectbox(key=key).options == ["v1", "v2 (current)"]
        assert "/voucher/v2" in _rendered(app)
        app.selectbox(key=key).set_value(cases["batch_v1"]).run()
        assert not app.exception
        assert "/voucher/{{voucherId}}" in _rendered(app)

    def test_secret_values_and_script_text_are_never_rendered(self, cases):
        app = _page(cases["secret"])
        assert not app.exception
        rendered = _rendered(app)
        for secret in SECRETS:
            assert secret not in rendered, secret
        assert "{{token}}" in rendered
        assert "[masked]" in rendered
        assert "Script text is hidden" in rendered
        assert "Not executable" in rendered
        findings = next(frame.value for frame in app.dataframe if "Severity" in frame.value.columns)
        assert "LITERAL_CREDENTIAL" in set(findings["Code"])
        assert "Error — blocks execution" in set(findings["Severity"])

    def test_a_deleted_selection_is_cleared(self, cases):
        app = _page(999)
        assert not app.exception
        assert "box_selected_case_id" not in app.session_state


class TestSessionIsolation:
    def test_only_box_keys_are_used(self, cases):
        app = AppTest.from_string(_PAGE, default_timeout=60)
        app.session_state["box_selected_case_id"] = cases["batch"]
        app.session_state["box_search"] = "demo"
        app.run()
        assert not app.exception
        assert {"box_filter_status", "box_search", f"box_version_{cases['batch']}"} <= _keys(app)
        assert all(key.startswith(("box_", "_box_")) for key in _keys(app)), _keys(app)


# ── M6.2: مدیریتِ case ──────────────────────────────────────────────────────


def _title_input(app: AppTest, case_id: int):
    return app.text_input(key=f"box_title_input_{case_id}")


def _save_title(app: AppTest, case_id: int, title: str) -> AppTest:
    _title_input(app, case_id).input(title)
    return app.button(key=f"box_title_save_{case_id}").click().run()


def _archive(app: AppTest, case_id: int) -> AppTest:
    app.button(key=f"box_archive_{case_id}").click().run()
    return app.button(key=f"box_archive_confirm_{case_id}").click().run()


def _history(db: Path, case_id: int):
    """رکوردهایی که مدیریتِ case نباید به آن‌ها دست بزند، به‌همراهِ خودِ case."""
    with BoxStore.open(db) as store:
        return store.versions(case_id), store.sources(case_id), store.get_case(case_id)


def _messages(elements) -> list[str]:
    return [element.value for element in elements]


class TestTitleEditing:
    def test_the_form_is_prefilled_with_the_current_title(self, cases):
        app = _page(cases["batch"])
        assert not app.exception
        assert _title_input(app, cases["batch"]).value == "DEMO-1 · TC-002"
        assert "The description cannot be edited in this version of the page." in _rendered(app)

    def test_a_valid_title_is_saved_and_shown(self, db, cases):
        versions, sources, before = _history(db, cases["batch"])
        app = _save_title(_page(cases["batch"]), cases["batch"], "  Voucher lookup fails  ")
        assert not app.exception
        assert _messages(app.success) == ["Title saved."]
        assert app.subheader[0].value.endswith("· Voucher lookup fails")
        assert _title_input(app, cases["batch"]).value == "Voucher lookup fails"
        assert "Voucher lookup fails" in set(app.dataframe[0].value["Title"])
        after_versions, after_sources, after = _history(db, cases["batch"])
        assert after.title == "Voucher lookup fails"
        assert (after.description, after.status, after.current_version_id) == (
            before.description,
            before.status,
            before.current_version_id,
        )
        assert after_versions == versions and after_sources == sources

    def test_the_success_message_is_shown_once(self, cases):
        app = _save_title(_page(cases["batch"]), cases["batch"], "Renamed")
        assert _messages(app.success) == ["Title saved."]
        app.run()
        assert not app.success

    @pytest.mark.parametrize(
        ("title", "message"),
        [
            ("   ", "Title not saved: Title must be a non-empty string."),
            ("x" * 301, "Title not saved: Title must be at most 300 characters."),
        ],
    )
    def test_an_invalid_title_is_rejected_without_writing(self, db, cases, title, message):
        before = db.read_bytes()
        app = _save_title(_page(cases["batch"]), cases["batch"], title)
        assert not app.exception
        assert _messages(app.error) == [message]
        assert not app.success
        assert db.read_bytes() == before
        assert _history(db, cases["batch"])[2].title == "DEMO-1 · TC-002"

    def test_an_unchanged_title_writes_nothing(self, db, cases):
        before = db.read_bytes()
        app = _save_title(_page(cases["batch"]), cases["batch"], " DEMO-1 · TC-002 ")
        assert not app.exception
        assert "The title is unchanged. Nothing was saved." in _messages(app.info)
        assert not app.success and not app.error
        assert db.read_bytes() == before

    def test_a_stale_form_never_overwrites_a_newer_title(self, db, cases):
        app = _page(cases["batch"])
        _title_input(app, cases["batch"]).input("My stale edit")
        with BoxStore.open(db) as store:
            store.update_case_details(cases["batch"], title="Renamed elsewhere")
        app.button(key=f"box_title_save_{cases['batch']}").click().run()
        assert not app.exception
        assert not app.success
        assert "changed somewhere else" in app.warning[0].value
        assert _history(db, cases["batch"])[2].title == "Renamed elsewhere"
        assert _title_input(app, cases["batch"]).value == "Renamed elsewhere"

    def test_the_form_follows_a_title_changed_elsewhere(self, db, cases):
        app = _page(cases["batch"])
        with BoxStore.open(db) as store:
            store.update_case_details(cases["batch"], title="Renamed elsewhere")
        app.run()
        assert _title_input(app, cases["batch"]).value == "Renamed elsewhere"

    def test_a_storage_failure_is_reported_without_success(self, db, cases, monkeypatch):
        def broken(self, case_id, **changes):
            raise box_app.StorageError("simulated failure")

        monkeypatch.setattr(BoxStore, "update_case_details", broken)
        before = db.read_bytes()
        app = _save_title(_page(cases["batch"]), cases["batch"], "Never saved")
        assert not app.exception
        assert not app.success
        assert app.error[0].value.startswith("Title not saved: the library could not be updated")
        assert "simulated failure" not in _rendered(app)
        assert db.read_bytes() == before

    def test_editing_never_exposes_secrets_or_script_text(self, cases):
        app = _save_title(_page(cases["secret"]), cases["secret"], "Renamed secrets")
        assert _messages(app.success) == ["Title saved."]
        rendered = _rendered(app)
        for secret in SECRETS:
            assert secret not in rendered, secret
        assert "Script text is hidden" in rendered


class TestArchiveRestore:
    def test_archive_requires_confirmation(self, db, cases):
        app = _page(cases["batch"])
        app.button(key=f"box_archive_{cases['batch']}").click().run()
        assert not app.exception
        assert app.warning[0].value.startswith("Archive BX-")
        assert _history(db, cases["batch"])[2].status == m.CASE_ACTIVE
        app.button(key=f"box_archive_cancel_{cases['batch']}").click().run()
        assert not app.exception
        assert "_box_confirm_archive" not in app.session_state
        assert _history(db, cases["batch"])[2].status == m.CASE_ACTIVE

    def test_archive_keeps_history_and_the_selection(self, db, cases):
        versions, sources, _ = _history(db, cases["batch"])
        app = _archive(_page(cases["batch"]), cases["batch"])
        assert not app.exception
        after_versions, after_sources, case = _history(db, cases["batch"])
        assert case.status == m.CASE_ARCHIVED
        assert after_versions == versions and after_sources == sources
        success = _messages(app.success)
        assert len(success) == 1 and " archived." in success[0]
        assert "hidden from the list by the Status filter (Active)" in success[0]
        # فیلترِ پیش‌فرض Active می‌ماند؛ case از جدول بیرون می‌رود ولی انتخاب‌شده می‌ماند
        assert app.session_state["box_filter_status"] == [m.CASE_ACTIVE]
        assert set(app.dataframe[0].value["Title"]) == {"Hand written secrets"}
        assert app.session_state["box_selected_case_id"] == cases["batch"]
        assert "hidden from the list by the current filters" in _rendered(app)
        assert app.button(key=f"box_restore_{cases['batch']}")
        assert "_box_confirm_archive" not in app.session_state

    def test_restore_reactivates_and_keeps_history(self, db, cases):
        versions, sources, _ = _history(db, cases["archived"])
        app = _page(cases["archived"])
        app.button(key=f"box_restore_{cases['archived']}").click().run()
        assert not app.exception
        after_versions, after_sources, case = _history(db, cases["archived"])
        assert case.status == m.CASE_ACTIVE
        assert after_versions == versions and after_sources == sources
        success = _messages(app.success)
        assert len(success) == 1 and " restored." in success[0]
        assert "Old case" in set(app.dataframe[0].value["Title"])
        assert app.session_state["box_filter_status"] == [m.CASE_ACTIVE]
        assert app.button(key=f"box_archive_{cases['archived']}")

    def test_archive_then_restore_round_trip(self, db, cases):
        versions, sources, _ = _history(db, cases["batch"])
        app = _archive(_page(cases["batch"]), cases["batch"])
        app.button(key=f"box_restore_{cases['batch']}").click().run()
        assert not app.exception
        assert _history(db, cases["batch"])[:2] == (versions, sources)
        assert _history(db, cases["batch"])[2].status == m.CASE_ACTIVE
        assert "DEMO-1 · TC-002" in set(app.dataframe[0].value["Title"])

    def test_a_failed_archive_is_reported_without_success(self, db, cases, monkeypatch):
        def locked(self, case_id):
            raise box_app.sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(BoxStore, "archive_case", locked)
        app = _archive(_page(cases["batch"]), cases["batch"])
        assert not app.exception
        assert not app.success
        assert "could not be archived (OperationalError). Nothing was changed." in (
            app.error[0].value
        )
        assert _history(db, cases["batch"])[2].status == m.CASE_ACTIVE

    def test_a_failed_restore_is_reported_without_success(self, db, cases, monkeypatch):
        def broken(self, case_id):
            raise box_app.StorageError("simulated failure")

        monkeypatch.setattr(BoxStore, "restore_case", broken)
        app = _page(cases["archived"])
        app.button(key=f"box_restore_{cases['archived']}").click().run()
        assert not app.exception
        assert not app.success
        assert "could not be restored (StorageError)" in app.error[0].value
        assert _history(db, cases["archived"])[2].status == m.CASE_ARCHIVED

    def test_a_pending_confirmation_is_dropped_when_another_case_is_selected(self, cases):
        app = _page(cases["batch"])
        app.button(key=f"box_archive_{cases['batch']}").click().run()
        assert app.session_state["_box_confirm_archive"] == cases["batch"]
        app.session_state["box_selected_case_id"] = cases["secret"]
        app.run()
        assert not app.exception
        assert "_box_confirm_archive" not in app.session_state
        app.session_state["box_selected_case_id"] = cases["batch"]
        app.run()
        assert app.button(key=f"box_archive_{cases['batch']}")
        assert not [w for w in _messages(app.warning) if w.startswith("Archive ")]

    def test_management_uses_only_box_keys(self, cases):
        app = _save_title(_page(cases["batch"]), cases["batch"], "Renamed")
        app = _archive(app, cases["batch"])
        assert not app.exception
        assert all(key.startswith(("box_", "_box_")) for key in _keys(app)), _keys(app)
