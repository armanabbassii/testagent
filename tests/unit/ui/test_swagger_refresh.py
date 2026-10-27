"""
تست‌های وضعیتِ دکمه‌های Update قدم دوم — ui/swagger_refresh.py

وضعیت در یک dict (به‌جای st.session_state) نگه داشته می‌شود و تابعِ
تازه‌سازی تزریق می‌شود؛ هیچ شبکه و هیچ Streamlitی درگیر نیست.
"""

from __future__ import annotations

from pathlib import Path

from src.agents.test_case_generator.swagger_snapshots import (
    RefreshResult,
    SwaggerRefreshError,
)
from ui.swagger_refresh import (
    is_refreshing,
    refresh_status,
    run_pending_refresh,
    start_refresh,
)
from ui.workflow import INPUT_SWAGGER_SNAPSHOTS, WorkflowState


def _result(key: str = "admin", label: str = "Admin") -> RefreshResult:
    return RefreshResult(
        key=key,
        label=label,
        path=Path(f"data/swagger/{key}.json"),
        url=f"http://podium-back.devpod.ir/api/v3/api-docs/{label}",
        operations=601,
        size_bytes=516_674,
    )


def _fails(reason: str = "Request timed out."):
    def refresh(_key):
        raise SwaggerRefreshError("Admin", reason, "Please check the connection and try again.")

    return refresh


class TestLoadingState:
    def test_controls_start_idle(self):
        state: dict = {}
        assert not is_refreshing(state, "admin")
        assert refresh_status(state, "admin") is None

    def test_a_click_marks_only_that_source_as_loading(self):
        state: dict = {}
        assert start_refresh(state, "admin") is True
        assert is_refreshing(state, "admin")
        assert not is_refreshing(state, "customer")

    def test_a_duplicate_click_while_loading_is_ignored(self):
        state: dict = {}
        start_refresh(state, "admin")
        assert start_refresh(state, "admin") is False

    def test_nothing_is_fetched_unless_a_click_is_pending(self):
        calls: list[str] = []
        assert run_pending_refresh({}, "admin", lambda key: calls.append(key)) is None
        assert calls == []

    def test_loading_is_reset_after_success(self):
        state: dict = {}
        start_refresh(state, "admin")
        status = run_pending_refresh(state, "admin", lambda _key: _result())

        assert not is_refreshing(state, "admin")
        assert status.ok
        assert "data/swagger/admin.json was updated" in status.message
        assert "601 operations" in status.message
        assert refresh_status(state, "admin") == status

    def test_loading_is_reset_after_failure(self):
        state: dict = {}
        start_refresh(state, "admin")
        status = run_pending_refresh(state, "admin", _fails())

        assert not is_refreshing(state, "admin")
        assert not status.ok
        assert status.message.startswith(
            "Failed to refresh Admin Swagger: Request timed out."
        )
        assert "Please check the connection and try again." in status.message

    def test_loading_is_reset_after_an_unexpected_exception(self):
        def boom(_key):
            raise ValueError("bug")

        state: dict = {}
        start_refresh(state, "admin")
        status = run_pending_refresh(state, "admin", boom)

        assert not is_refreshing(state, "admin")
        assert not status.ok
        assert "unexpected error (ValueError: bug)" in status.message
        assert "existing file was kept" in status.message


class TestRetry:
    def test_the_control_can_be_clicked_again_after_a_failure(self):
        state: dict = {}
        start_refresh(state, "admin")
        run_pending_refresh(state, "admin", _fails())

        assert start_refresh(state, "admin") is True
        # پیامِ خطای قبلی هنگامِ تلاشِ تازه پاک می‌شود
        assert refresh_status(state, "admin") is None
        status = run_pending_refresh(state, "admin", lambda _key: _result())
        assert status.ok

    def test_one_source_failing_does_not_touch_the_other(self):
        state: dict = {}
        start_refresh(state, "admin")
        start_refresh(state, "customer")
        run_pending_refresh(state, "admin", _fails())

        assert is_refreshing(state, "customer")
        assert refresh_status(state, "customer") is None
        status = run_pending_refresh(
            state, "customer", lambda _key: _result("customer", "Customer")
        )
        assert status.ok
        assert not refresh_status(state, "admin").ok


class TestStep2ResultAfterRefresh:
    """تازه‌سازی نگاشت را اجرا نمی‌کند، ولی نتیجه‌ی قبلی را «قدیمی» گزارش می‌کند."""

    def test_a_changed_snapshot_is_reported_as_a_pending_input_change(self):
        state = WorkflowState()
        state.set_input(INPUT_SWAGGER_SNAPSHOTS, "admin=aaaaaaaaaaaa")
        state.record_result(2, {"services": [], "mappings": []})
        assert state.pending_input_changes(2) == []

        state.set_input(INPUT_SWAGGER_SNAPSHOTS, "admin=bbbbbbbbbbbb")
        assert state.pending_input_changes(2) == ["Swagger snapshot contents"]
        # نتیجه نه پاک می‌شود و نه کهنه — فقط هشدار
        assert state.has_result(2) and not state.is_stale(2)
