"""تست‌های کلیدِ issue، نشانیِ issue و تنظیماتِ Jira (متغیرهای محیطی)."""

from __future__ import annotations

import pytest

from src import config
from src.task_sources.jira import JiraConfigError, JiraReferenceError, JiraSettings
from src.task_sources.jira.keys import parse_issue_reference
from tests.unit.task_sources.fake_jira import PASSWORD, TOKEN, adfs_settings, pat_settings


class TestIssueReference:
    @pytest.mark.parametrize(
        "reference",
        [
            "DEMO-7",
            "demo-7",
            "  DEMO-7  ",
            "https://jira.test/browse/DEMO-7",
            "https://jira.test/browse/DEMO-7/",
            "https://jira.test/browse/DEMO-7?focusedCommentId=1#comment-1",
            "https://JIRA.test/browse/demo-7",
        ],
    )
    def test_keys_and_urls_become_the_issue_key(self, reference):
        assert parse_issue_reference(reference, "https://jira.test") == "DEMO-7"

    def test_project_keys_with_digits_and_underscores(self):
        assert parse_issue_reference("AB_2-10") == "AB_2-10"

    @pytest.mark.parametrize(
        "reference",
        ["", "   ", "DEMO", "DEMO-", "DEMO-0", "7-DEMO", "DEMO 7", "DEMO-7-8", "-7"],
    )
    def test_invalid_keys_are_rejected(self, reference):
        with pytest.raises(JiraReferenceError):
            parse_issue_reference(reference)

    def test_a_url_on_another_host_is_rejected(self):
        with pytest.raises(JiraReferenceError, match="not the configured Jira"):
            parse_issue_reference("https://other.test/browse/DEMO-7", "https://jira.test")

    @pytest.mark.parametrize(
        "reference",
        [
            "https://jira.test/projects/DEMO/issues",
            "https://jira.test/",
            "ftp://jira.test/browse/DEMO-7",
            "https:///browse/DEMO-7",
        ],
    )
    def test_urls_that_are_not_issue_urls_are_rejected(self, reference):
        with pytest.raises(JiraReferenceError):
            parse_issue_reference(reference, "https://jira.test")


class TestSettings:
    def test_adfs_needs_the_sso_url_and_credentials_but_no_token(self):
        assert adfs_settings().validate().auth_method == "adfs"

    def test_pat_needs_only_the_token(self):
        assert pat_settings().validate().auth_method == "pat"

    def test_every_missing_value_is_reported_at_once(self):
        with pytest.raises(JiraConfigError) as error:
            JiraSettings(base_url="").validate()

        message = str(error.value)
        for name in ("JIRA_BASE_URL", "JIRA_SSO_URL", "JIRA_USERNAME", "JIRA_PASSWORD"):
            assert name in message
        assert "JIRA_TOKEN" not in message

    def test_pat_without_a_token_names_jira_token(self):
        with pytest.raises(JiraConfigError, match="JIRA_TOKEN"):
            pat_settings(token="").validate()

    def test_an_unknown_method_is_rejected(self):
        with pytest.raises(JiraConfigError, match="JIRA_AUTH_METHOD"):
            adfs_settings(auth_method="basic").validate()

    @pytest.mark.parametrize("field", ["base_url", "sso_url"])
    def test_plain_http_is_rejected(self, field):
        with pytest.raises(JiraConfigError, match="https"):
            adfs_settings(**{field: "http://jira.test"}).validate()

    def test_a_missing_ca_bundle_file_is_an_error_not_a_silent_fallback(self, tmp_path):
        with pytest.raises(JiraConfigError, match="JIRA_CA_BUNDLE"):
            adfs_settings(ca_bundle=str(tmp_path / "missing.pem")).validate()

    def test_verification_is_never_disabled(self, tmp_path):
        bundle = tmp_path / "ca.pem"
        bundle.write_text("x")
        assert adfs_settings().verify is True
        assert adfs_settings(ca_bundle=str(bundle)).verify == str(bundle)

    def test_non_positive_limits_are_rejected(self):
        with pytest.raises(JiraConfigError, match="MAX_ATTACHMENT"):
            adfs_settings(max_attachment_bytes=0).validate()

    def test_secrets_are_not_in_the_repr(self):
        text = repr(adfs_settings()) + repr(pat_settings())
        assert PASSWORD not in text
        assert TOKEN not in text

    def test_values_come_from_src_config(self, monkeypatch):
        values = {
            "JIRA_BASE_URL": " https://jira.test/ ",
            "JIRA_SSO_URL": "https://sso.test/adfs/ls/",
            "JIRA_AUTH_METHOD": " PAT ",
            "JIRA_USERNAME": "u",
            "JIRA_PASSWORD": "p",
            "JIRA_TOKEN": " t ",
            "JIRA_CA_BUNDLE": "",
            "JIRA_CONNECT_TIMEOUT_SECONDS": 4.0,
            "JIRA_READ_TIMEOUT_SECONDS": 8.0,
            "JIRA_MAX_ATTACHMENT_BYTES": 100,
            "JIRA_MAX_ATTACHMENT_CHARS": 50,
        }
        for name, value in values.items():
            monkeypatch.setattr(config, name, value)

        settings = JiraSettings.from_config()

        assert settings.base_url == "https://jira.test"
        assert settings.auth_method == "pat"
        assert settings.token == "t"
        assert settings.timeout == (4.0, 8.0)
        assert (settings.max_attachment_bytes, settings.max_attachment_chars) == (100, 50)

    def test_the_default_method_is_adfs(self, monkeypatch):
        monkeypatch.setattr(config, "JIRA_AUTH_METHOD", "")
        assert JiraSettings.from_config().auth_method == "adfs"
