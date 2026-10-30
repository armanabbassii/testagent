"""تست‌های import_issue: issue → TaskInput، با گزارشِ جدای هر مرحله."""

from __future__ import annotations

import pytest

from src.task_sources import (
    ATTACHMENT_FAILED,
    ATTACHMENT_PROCESSED,
    ATTACHMENT_TOO_LARGE,
    ATTACHMENT_UNSUPPORTED,
)
from src.task_sources.jira import JiraClient, JiraSettings, import_issue
from src.task_sources.jira.importer import (
    STAGE_AUTHENTICATION,
    STAGE_COMMENTS,
    STAGE_CONFIGURATION,
    STAGE_ISSUE,
    STAGE_REFERENCE,
    format_timestamp,
)
from tests.unit.task_sources.fake_jira import (
    JIRA,
    FakeJira,
    adfs_settings,
    attachment_meta,
    comment,
    issue_body,
)


@pytest.fixture
def server() -> FakeJira:
    files = {
        "curl.txt": b"curl -X GET https://api.test/admin/types\r\n",
        "sample.json": b'{"types": ["XLSX"]}',
        "shot.png": b"\x89PNG",
        "ExcelType.txt": b"EPFS\x00\x00\x00\x0f",
    }
    attachments = [
        attachment_meta("curl.txt", len(files["curl.txt"]), number=1),
        attachment_meta("sample.json", len(files["sample.json"]), "application/json", 2),
        attachment_meta("shot.png", 4, "image/png", 3),
        attachment_meta("ExcelType.txt", 8, number=4),
        attachment_meta("huge.pdf", 50_000_000, "application/pdf", 5),
    ]
    return FakeJira(
        issues={
            "DEMO-7": issue_body(
                description="Add two APIs for types.\r\n{code}GET /api/admin/types{code}",
                attachments=attachments,
            )
        },
        comments={
            "DEMO-7": [
                comment(1, "Endpoints:\r\n/api/admin/types", author="Dev One"),
                comment(2, "Please log time.", author="Lead", updated="2026-01-03T08:30:00.000+0330"),
                comment(3, "Retry raised to 15s.", author="Lead"),
            ]
        },
        files=files,
    )


def _import(server: FakeJira, reference: str = "DEMO-7", settings=None):
    settings = settings or adfs_settings()
    return import_issue(
        reference, settings=settings, client=JiraClient(settings, session=server.session())
    )


class TestSuccessfulImport:
    def test_every_stage_is_reported(self, server):
        result = _import(server, f"{JIRA}/browse/demo-7")

        assert result.ok
        assert (result.authenticated, result.issue_found, result.comments_retrieved) == (
            True,
            True,
            True,
        )
        assert result.identity == "Test User"
        assert result.issue_key == "DEMO-7"
        assert result.auth_method.startswith("ADFS")

    def test_the_task_input_is_provider_independent_and_normalised(self, server):
        task = _import(server).task_input

        assert task.title == "Add an endpoint for voucher types"
        assert task.description == "Add two APIs for types.\n\n~~~~\nGET /api/admin/types\n~~~~"
        assert (task.source, task.source_reference) == ("jira", "DEMO-7")
        assert task.source_url == f"{JIRA}/browse/DEMO-7"
        assert task.metadata["issue_type"] == "Story"
        assert task.metadata["status"] == "In Progress"
        assert task.metadata["retrieved_at"]

    def test_all_comments_keep_order_author_date_and_content(self, server):
        comments = _import(server).task_input.comments

        assert [item.author for item in comments] == ["Dev One", "Lead", "Lead"]
        assert comments[0].body == "Endpoints:\n/api/admin/types"
        assert comments[0].created == "2026-01-01 10:00 +03:30"
        assert comments[1].created == "2026-01-02 10:00 +03:30 (edited 2026-01-03 08:30 +03:30)"

    def test_comment_pagination_is_complete(self, server):
        server.comments["DEMO-7"] = [comment(number) for number in range(1, 121)]

        comments = _import(server).task_input.comments

        assert len(comments) == 120
        assert comments[-1].body == "comment 120"

    def test_each_attachment_gets_an_explicit_status(self, server):
        attachments = {item.filename: item for item in _import(server).task_input.attachments}

        assert attachments["curl.txt"].status == ATTACHMENT_PROCESSED
        assert attachments["curl.txt"].content == "curl -X GET https://api.test/admin/types"
        assert attachments["sample.json"].status == ATTACHMENT_PROCESSED
        assert attachments["shot.png"].status == ATTACHMENT_UNSUPPORTED
        assert attachments["ExcelType.txt"].status == ATTACHMENT_FAILED
        assert attachments["huge.pdf"].status == ATTACHMENT_TOO_LARGE

    def test_skipped_attachments_are_not_downloaded(self, server):
        _import(server)

        downloaded = {call.url.rsplit("/", 1)[1] for call in server.calls if "/secure/attachment/" in call.url}
        assert downloaded == {"curl.txt", "sample.json", "ExcelType.txt"}

    def test_attachment_problems_become_warnings(self, server):
        warnings = _import(server).warnings

        assert any(item.startswith("Attachment shot.png: unsupported") for item in warnings)
        assert any(item.startswith("Attachment ExcelType.txt: failed") for item in warnings)
        assert any(item.startswith("Attachment huge.pdf: too large") for item in warnings)

    def test_a_failed_download_does_not_abort_the_import(self, server):
        server.file_status["curl.txt"] = 500

        result = _import(server)

        assert result.ok
        curl = result.task_input.attachments[0]
        assert curl.status == ATTACHMENT_FAILED
        assert "download failed" in curl.detail

    def test_the_task_description_keeps_the_origin_of_each_part(self, server):
        text = _import(server).task_input.to_task_description()

        assert text.startswith("# Task DEMO-7: Add an endpoint for voucher types\n")
        assert "## Comments (3, oldest first)" in text
        assert "### Attachment: curl.txt" in text
        assert "- ExcelType.txt: failed" in text


class TestFailedStages:
    def test_missing_configuration(self, server):
        result = import_issue("DEMO-7", settings=JiraSettings(base_url=""))

        assert result.error_stage == STAGE_CONFIGURATION
        assert "JIRA_BASE_URL" in result.error
        assert not result.ok

    def test_an_invalid_reference_makes_no_request(self, server):
        result = _import(server, "not a key")

        assert result.error_stage == STAGE_REFERENCE
        assert server.calls == []

    def test_an_authentication_failure(self, server):
        server.adfs_mode = "wrong"

        result = _import(server)

        assert result.error_stage == STAGE_AUTHENTICATION
        assert not result.authenticated
        assert result.task_input is None

    def test_a_permission_failure_is_distinct_from_authentication(self, server):
        server.issue_status["DEMO-7"] = 403

        result = _import(server)

        assert result.authenticated
        assert result.identity == "Test User"
        assert result.error_stage == STAGE_ISSUE
        assert "HTTP 403" in result.error

    def test_a_missing_issue(self, server):
        result = _import(server, "DEMO-404")

        assert result.error_stage == STAGE_ISSUE
        assert "HTTP 404" in result.error

    def test_incomplete_comments_fail_the_import(self, server):
        server.missing_comments = 1

        result = _import(server)

        assert result.error_stage == STAGE_COMMENTS
        assert result.task_input is None


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("2026-10-14T18:09:41.000+0330", "2026-10-14 18:09 +03:30"),
        ("2026-01-01T00:00:00.000+0000", "2026-01-01 00:00 +00:00"),
        ("yesterday", "yesterday"),
        ("", ""),
    ],
)
def test_format_timestamp(raw, shown):
    assert format_timestamp(raw) == shown
