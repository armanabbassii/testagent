"""
تست‌های تازه‌سازیِ snapshotهای Swagger — دکمه‌های Update Admin/Customer قدم دوم

هیچ تستی به شبکه نمی‌رود: نشستِ HTTP با یک FakeSession جایگزین می‌شود و
فایل‌ها در tmp_path نوشته می‌شوند (نه در data/swagger/).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.agents.test_case_generator import swagger_snapshots
from src.agents.test_case_generator.api_mapping import Step2ApiMappingGenerator
from src.agents.test_case_generator.swagger_snapshots import (
    SELECTION_ADMIN,
    SELECTION_CUSTOMER,
    SwaggerRefreshError,
    refresh_snapshot,
    snapshot_fingerprint,
    source_for,
)

ADMIN_URL = "http://podium-back.devpod.ir/api/v3/api-docs/Admin"
CUSTOMER_URL = "http://podium-back.devpod.ir/api/v3/api-docs/Customer"


def _spec(operation_id: str, path: str) -> dict:
    return {
        "openapi": "3.0.1",
        "info": {"title": "Podium", "version": "1.0"},
        "servers": [{"url": "http://podium-back.devpod.ir/api"}],
        "paths": {
            path: {
                "get": {
                    "operationId": operation_id,
                    "summary": operation_id,
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }


def _bytes(document) -> bytes:
    return document if isinstance(document, bytes) else json.dumps(document).encode()


class _Response:
    def __init__(self, status: int = 200, content: bytes = b"", reason: str = "OK"):
        self.status_code = status
        self.content = content
        self.reason = reason


class FakeSession:
    """هر get یک پاسخ یا استثنا از صف برمی‌گرداند و درخواست را ثبت می‌کند."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append({"url": url, "timeout": timeout, "headers": headers})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _ok(document) -> _Response:
    return _Response(200, _bytes(document))


@pytest.fixture
def snapshots(tmp_path):
    """هر دو snapshot با محتوای «قدیمی» در یک پوشه‌ی موقت."""
    (tmp_path / "admin.json").write_bytes(_bytes(_spec("oldAdmin", "/admin/old")))
    (tmp_path / "customer.json").write_bytes(_bytes(_spec("oldCustomer", "/customer/old")))
    return tmp_path


def _read(directory, name: str) -> bytes:
    return (directory / name).read_bytes()


def _refresh(key: str, session: FakeSession, directory):
    return refresh_snapshot(key, session=session, directory=directory, timeout=(1, 2))


# ── ۱ تا ۳: هر دکمه نشانیِ خودش را می‌گیرد و فقط فایلِ خودش را عوض می‌کند ────

class TestSuccessfulRefresh:
    def test_the_configured_source_urls_are_used(self):
        assert source_for(SELECTION_ADMIN).remote_url == ADMIN_URL
        assert source_for(SELECTION_CUSTOMER).remote_url == CUSTOMER_URL

    def test_admin_fetches_the_admin_url_and_updates_only_admin_json(self, snapshots):
        customer_before = _read(snapshots, "customer.json")
        new = _bytes(_spec("newAdmin", "/admin/new"))
        session = FakeSession(_Response(200, new))

        result = _refresh(SELECTION_ADMIN, session, snapshots)

        assert [call["url"] for call in session.calls] == [ADMIN_URL]
        assert _read(snapshots, "admin.json") == new
        assert _read(snapshots, "customer.json") == customer_before
        assert result.path == snapshots / "admin.json"
        assert result.operations == 1

    def test_customer_fetches_the_customer_url_and_updates_only_customer_json(
        self, snapshots
    ):
        admin_before = _read(snapshots, "admin.json")
        new = _bytes(_spec("newCustomer", "/customer/new"))
        session = FakeSession(_Response(200, new))

        _refresh(SELECTION_CUSTOMER, session, snapshots)

        assert [call["url"] for call in session.calls] == [CUSTOMER_URL]
        assert _read(snapshots, "customer.json") == new
        assert _read(snapshots, "admin.json") == admin_before

    def test_the_file_holds_exactly_the_retrieved_bytes(self, snapshots):
        new = b'{"openapi":"3.0.1","info":{},"paths":{"/x":{"get":{"responses":{}}}}}'
        _refresh(SELECTION_ADMIN, FakeSession(_Response(200, new)), snapshots)
        assert _read(snapshots, "admin.json") == new

    def test_every_call_sends_a_fresh_request_with_both_timeouts(self, snapshots):
        session = FakeSession(
            _ok(_spec("a", "/admin/a")), _ok(_spec("b", "/admin/b"))
        )
        _refresh(SELECTION_ADMIN, session, snapshots)
        _refresh(SELECTION_ADMIN, session, snapshots)

        assert len(session.calls) == 2
        assert session.calls[0]["timeout"] == (1, 2)
        assert b"/admin/b" in _read(snapshots, "admin.json")

    def test_default_timeouts_come_from_the_project_config(self, snapshots, monkeypatch):
        monkeypatch.setattr(swagger_snapshots, "SWAGGER_CONNECT_TIMEOUT_SECONDS", 3.0)
        monkeypatch.setattr(swagger_snapshots, "SWAGGER_READ_TIMEOUT_SECONDS", 45.0)
        session = FakeSession(_ok(_spec("a", "/admin/a")))
        refresh_snapshot(SELECTION_ADMIN, session=session, directory=snapshots)
        assert session.calls[0]["timeout"] == (3.0, 45.0)

    def test_a_missing_target_file_is_created(self, tmp_path):
        _refresh(SELECTION_ADMIN, FakeSession(_ok(_spec("a", "/admin/a"))), tmp_path)
        assert (tmp_path / "admin.json").is_file()

    def test_no_temporary_file_is_left_behind(self, snapshots):
        _refresh(SELECTION_ADMIN, FakeSession(_ok(_spec("a", "/admin/a"))), snapshots)
        assert sorted(p.name for p in snapshots.iterdir()) == ["admin.json", "customer.json"]


# ── ۴ تا ۸: هر شکست فایلِ قبلی را دست‌نخورده نگه می‌دارد ─────────────────────

_FAILURES = [
    pytest.param(
        requests.exceptions.ConnectTimeout("connect"),
        "Connection timed out after 1s",
        id="connect-timeout",
    ),
    pytest.param(
        requests.exceptions.ReadTimeout("read"),
        "Request timed out",
        id="read-timeout",
    ),
    pytest.param(
        requests.exceptions.ConnectionError("refused"),
        "connection refused or network failure",
        id="connection-error",
    ),
    pytest.param(_Response(404, b"Not Found", "Not Found"), "HTTP 404 Not Found", id="http-404"),
    pytest.param(
        _Response(500, b'{"error":"boom"}', "Internal Server Error"),
        "HTTP 500",
        id="http-500",
    ),
    pytest.param(_Response(200, b"<html>login</html>"), "not valid JSON", id="html"),
    pytest.param(_Response(200, b"{\"openapi\": "), "not valid JSON", id="truncated-json"),
    pytest.param(_Response(200, b""), "empty response", id="empty"),
    pytest.param(_Response(200, b"[]"), "not a Swagger/OpenAPI document", id="json-array"),
    pytest.param(
        _Response(200, b'{"error": "x"}'), "not a Swagger/OpenAPI document", id="no-version"
    ),
    pytest.param(
        _Response(200, b'{"openapi": "3.0.1", "paths": {}}'), "not usable", id="no-paths"
    ),
    pytest.param(
        _Response(200, b'{"openapi": "3.0.1", "paths": {"/x": {"parameters": []}}}'),
        "no API operation",
        id="no-operations",
    ),
]


class TestFailuresPreserveTheFile:
    @pytest.mark.parametrize("outcome, reason", _FAILURES)
    def test_failure_is_reported_and_the_previous_file_is_kept(
        self, snapshots, outcome, reason
    ):
        before = _read(snapshots, "admin.json")

        with pytest.raises(SwaggerRefreshError) as caught:
            _refresh(SELECTION_ADMIN, FakeSession(outcome), snapshots)

        assert str(caught.value).startswith("Failed to refresh Admin Swagger:")
        assert reason in str(caught.value)
        assert _read(snapshots, "admin.json") == before
        assert sorted(p.name for p in snapshots.iterdir()) == ["admin.json", "customer.json"]

    def test_network_failures_ask_the_user_to_check_the_connection(self, snapshots):
        with pytest.raises(SwaggerRefreshError) as caught:
            _refresh(
                SELECTION_CUSTOMER,
                FakeSession(requests.exceptions.ReadTimeout("slow")),
                snapshots,
            )
        assert caught.value.label == "Customer"
        assert caught.value.hint == "Please check the connection and try again."

    def test_a_permission_error_while_writing_keeps_the_file(self, snapshots, monkeypatch):
        before = _read(snapshots, "admin.json")

        def deny(*_args, **_kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(swagger_snapshots.os, "replace", deny)
        with pytest.raises(SwaggerRefreshError, match="Permission denied while writing"):
            _refresh(SELECTION_ADMIN, FakeSession(_ok(_spec("a", "/admin/a"))), snapshots)

        assert _read(snapshots, "admin.json") == before
        assert sorted(p.name for p in snapshots.iterdir()) == ["admin.json", "customer.json"]

    def test_any_other_write_error_is_reported(self, snapshots, monkeypatch):
        def full(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(swagger_snapshots.os, "replace", full)
        with pytest.raises(SwaggerRefreshError, match="No space left on device"):
            _refresh(SELECTION_ADMIN, FakeSession(_ok(_spec("a", "/admin/a"))), snapshots)
        assert not [p for p in snapshots.iterdir() if p.suffix == ".tmp"]


# ── ۹: تلاشِ دوباره بعد از شکست ──────────────────────────────────────────────

class TestRetry:
    def test_a_later_call_succeeds_after_a_timeout(self, snapshots):
        new = _bytes(_spec("newAdmin", "/admin/new"))
        session = FakeSession(requests.exceptions.ReadTimeout("slow"), _Response(200, new))

        with pytest.raises(SwaggerRefreshError):
            _refresh(SELECTION_ADMIN, session, snapshots)
        _refresh(SELECTION_ADMIN, session, snapshots)

        assert _read(snapshots, "admin.json") == new
        assert len(session.calls) == 2


# ── ۱۱: اجرای بعدیِ نگاشت از سندِ تازه می‌خواند ─────────────────────────────

class TestMappingUsesTheRefreshedDocument:
    def test_step2_maps_against_the_refreshed_snapshot(self, snapshots, monkeypatch):
        monkeypatch.setattr(swagger_snapshots, "SNAPSHOT_DIR", snapshots)
        _refresh(SELECTION_ADMIN, FakeSession(_ok(_spec("newAdmin", "/admin/new"))), snapshots)

        step1 = {"test_cases": [{"id": "TC-001", "title": "Read the new admin resource"}]}
        payload = {
            "mappings": [
                {
                    "test_case_id": "TC-001",
                    "api": {"method": "GET", "path": "/admin/new", "operation_id": "newAdmin"},
                    "confidence": "high",
                    "reason": "Only operation.",
                    "clarification": "",
                }
            ],
            "clarifications": [],
        }
        with patch("src.agents.test_case_generator.api_mapping.LLMClient") as mock_cls:
            llm = MagicMock()
            llm.chat.return_value = json.dumps(payload)
            mock_cls.return_value = llm
            result = Step2ApiMappingGenerator().generate(
                step1_result=step1,
                service_sources=[],
                user_id="test",
                snapshot_keys=[SELECTION_ADMIN],
            )

        paths = [api["path"] for api in result["services"][0]["apis"]]
        assert paths == ["/admin/new"]
        assert result["mappings"][0]["api"]["path"] == "/admin/new"
        assert "newAdmin" in llm.chat.call_args.kwargs["user_message"]
        assert "oldAdmin" not in llm.chat.call_args.kwargs["user_message"]


class TestSnapshotFingerprint:
    def test_the_fingerprint_changes_only_when_the_file_changes(self, snapshots):
        before = snapshot_fingerprint([SELECTION_ADMIN], directory=snapshots)
        assert before == snapshot_fingerprint([SELECTION_ADMIN], directory=snapshots)

        _refresh(SELECTION_ADMIN, FakeSession(_ok(_spec("new", "/admin/new"))), snapshots)
        assert snapshot_fingerprint([SELECTION_ADMIN], directory=snapshots) != before

    def test_refreshing_customer_does_not_change_the_admin_fingerprint(self, snapshots):
        before = snapshot_fingerprint([SELECTION_ADMIN], directory=snapshots)
        _refresh(SELECTION_CUSTOMER, FakeSession(_ok(_spec("c", "/customer/c"))), snapshots)
        assert snapshot_fingerprint([SELECTION_ADMIN], directory=snapshots) == before

    def test_a_missing_file_is_reported_not_raised(self, tmp_path):
        assert snapshot_fingerprint([SELECTION_ADMIN], directory=tmp_path) == "admin=missing"
