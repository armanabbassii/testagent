"""
تست‌های ورود به Jira (ADFS SAML و PAT) و کلاینتِ REST — روی سرورِ ساختگی.

هیچ‌کدام به Jiraِ واقعی، اعتبارنامه‌ی واقعی یا شبکه دست نمی‌زنند.
"""

from __future__ import annotations

import pytest
import requests

from src.task_sources.attachments import AttachmentTooLarge
from src.task_sources.jira import (
    JiraAuthenticationError,
    JiraClient,
    JiraConnectionError,
    JiraMfaRequiredError,
    JiraNotFoundError,
    JiraPermissionError,
    JiraRateLimitError,
    JiraResponseError,
    JiraServerError,
)
from tests.unit.task_sources.fake_jira import (
    ASSERTION,
    JIRA,
    PASSWORD,
    TOKEN,
    USER,
    FakeJira,
    adfs_settings,
    comment,
    issue_body,
    pat_settings,
)


def _client(server: FakeJira, settings=None) -> JiraClient:
    return JiraClient(settings or adfs_settings(), session=server.session())


@pytest.fixture
def server() -> FakeJira:
    return FakeJira(issues={"DEMO-7": issue_body()})


class TestAdfsSignIn:
    def test_the_verified_flow_signs_in_and_confirms_the_identity(self, server):
        client = _client(server)

        identity = client.authenticate()

        assert identity["name"] == USER
        assert server.sign_ins == 1
        assert client.identity == identity

    def test_the_password_is_sent_only_to_the_sso_host_and_only_once(self, server):
        _client(server).authenticate()

        with_password = [call for call in server.calls if PASSWORD in call.data.values()]
        assert len(with_password) == 1
        assert with_password[0].url.startswith("https://sso.test/adfs/ls/")
        assert all(PASSWORD not in call.data.values() for call in server.calls_to("jira.test"))

    def test_the_login_form_fields_are_used_and_kmsi_is_not_requested(self, server):
        _client(server).authenticate()

        credentials = next(call for call in server.calls if "Password" in call.data)
        assert credentials.data == {
            "UserName": USER,
            "Password": PASSWORD,
            "AuthMethod": "FormsAuthentication",
        }
        # action نسبیِ فرم روی همان میزبانِ ADFS حل می‌شود.
        assert credentials.url == "https://sso.test/adfs/ls/?client-request-id=1"

    def test_the_saml_response_is_forwarded_only_to_jira(self, server):
        _client(server).authenticate()

        forwarded = [call for call in server.calls if call.data.get("SAMLResponse") == ASSERTION]
        assert [call.url for call in forwarded] == [f"{JIRA}/plugins/servlet/samlsso"]

    def test_rejected_credentials_report_the_adfs_message(self, server):
        server.adfs_mode = "wrong"

        with pytest.raises(JiraAuthenticationError) as error:
            _client(server).authenticate()

        assert not isinstance(error.value, JiraMfaRequiredError)
        assert "Incorrect user ID or password." in str(error.value)
        assert PASSWORD not in str(error.value)

    def test_an_mfa_prompt_is_detected_and_not_bypassed(self, server):
        server.adfs_mode = "mfa"

        with pytest.raises(JiraMfaRequiredError, match="MFA"):
            _client(server).authenticate()

        assert server.sign_ins == 0
        assert not any("Context" in call.data for call in server.calls)

    def test_an_unknown_adfs_page_is_an_explicit_error(self, server):
        server.adfs_mode = "unknown"

        with pytest.raises(JiraAuthenticationError, match="Unexpected response"):
            _client(server).authenticate()

    def test_credentials_are_never_sent_when_jira_redirects_to_another_idp(self, server):
        server.saml_request_action = "https://evil.test/adfs/ls/"

        with pytest.raises(JiraAuthenticationError, match="no credentials were sent"):
            _client(server).authenticate()

        assert server.calls_to("evil.test") == []
        assert not any("Password" in call.data for call in server.calls)

    def test_credentials_are_never_sent_when_the_login_form_posts_elsewhere(self, server):
        server.login_action = "https://evil.test/collect"

        with pytest.raises(JiraAuthenticationError, match="no credentials were sent"):
            _client(server).authenticate()

        assert not any("Password" in call.data for call in server.calls)

    def test_credentials_are_never_sent_over_plain_http(self, server):
        server.login_action = "http://sso.test/adfs/ls/"

        with pytest.raises(JiraAuthenticationError):
            _client(server).authenticate()

        assert not any("Password" in call.data for call in server.calls)

    def test_the_saml_response_is_not_forwarded_to_another_host(self, server):
        server.acs_action = "https://evil.test/acs"

        with pytest.raises(JiraAuthenticationError, match="was not forwarded"):
            _client(server).authenticate()

        assert server.calls_to("evil.test") == []

    def test_adfs_without_forms_authentication_is_rejected(self, server):
        server.login_auth_method = "WindowsAuthentication"

        with pytest.raises(JiraAuthenticationError, match="forms authentication"):
            _client(server).authenticate()

        assert not any("Password" in call.data for call in server.calls)

    def test_jira_that_does_not_start_saml_is_reported(self, server):
        server.login_starts_saml = False

        with pytest.raises(JiraAuthenticationError, match="SAML sign-in"):
            _client(server).authenticate()

    def test_an_assertion_jira_does_not_accept_is_an_authentication_error(self, server):
        server.acs_accepts = False

        with pytest.raises(JiraAuthenticationError, match="myself returned HTTP 401"):
            _client(server).authenticate()


class TestSessionLifecycle:
    def test_the_session_is_reused_across_requests(self, server):
        client = _client(server)

        client.issue("DEMO-7")
        client.comments("DEMO-7")
        client.myself()

        assert server.sign_ins == 1

    def test_an_expired_session_is_restored_with_one_new_sign_in(self, server):
        client = _client(server)
        client.issue("DEMO-7")
        server.expire_sessions()

        assert client.issue("DEMO-7")["key"] == "DEMO-7"
        assert server.sign_ins == 2

    def test_a_redirect_to_the_login_page_counts_as_an_expired_session(self, server):
        server.redirect_when_anonymous = True
        client = _client(server)
        client.issue("DEMO-7")
        server.expire_sessions()

        assert client.issue("DEMO-7")["key"] == "DEMO-7"
        assert server.sign_ins == 2

    def test_a_session_that_cannot_be_restored_fails_without_a_loop(self, server):
        client = _client(server)
        client.issue("DEMO-7")
        server.reject_rest = True

        with pytest.raises(JiraAuthenticationError):
            client.issue("DEMO-7")

        # یک ورودِ اولیه + فقط یک تلاشِ دوباره؛ آن تلاش هم در /myself رد می‌شود.
        assert server.sign_ins == 2

    def test_old_cookies_are_dropped_before_signing_in_again(self, server):
        client = _client(server)
        client.authenticate()
        client.session.cookies.set("stale", "1", domain="jira.test")

        client.authenticate()

        assert client.session.cookies.get("stale", domain="jira.test") is None


class TestPersonalAccessToken:
    def test_the_token_is_sent_as_a_bearer_header(self, server):
        client = _client(server, pat_settings())

        assert client.issue("DEMO-7")["key"] == "DEMO-7"
        assert all(call.headers.get("Authorization") == f"Bearer {TOKEN}" for call in server.calls)
        assert server.calls_to("sso.test") == []

    def test_a_rejected_token_is_not_retried(self, server):
        client = _client(server, pat_settings(token="expired"))

        with pytest.raises(JiraAuthenticationError, match="myself returned HTTP 401"):
            client.issue("DEMO-7")

        assert len(server.calls) == 1

    def test_a_token_rejected_later_names_jira_token(self, server):
        client = _client(server, pat_settings())
        client.myself()
        server.reject_rest = True

        with pytest.raises(JiraAuthenticationError, match="JIRA_TOKEN"):
            client.issue("DEMO-7")


class TestHttpErrors:
    @pytest.mark.parametrize(
        ("status", "error"),
        [
            (403, JiraPermissionError),
            (404, JiraNotFoundError),
            (429, JiraRateLimitError),
            (500, JiraServerError),
            (503, JiraServerError),
            (418, JiraResponseError),
        ],
    )
    def test_each_status_has_its_own_error(self, server, status, error):
        server.issue_status["DEMO-7"] = status

        with pytest.raises(error):
            _client(server).issue("DEMO-7")

    def test_a_permission_error_says_the_sign_in_worked(self, server):
        server.issue_status["DEMO-7"] = 403

        with pytest.raises(JiraPermissionError, match="Signed in.*You do not have permission"):
            _client(server).issue("DEMO-7")

    def test_rate_limit_reports_retry_after(self, server):
        server.issue_status["DEMO-7"] = 429
        server.retry_after = "30"

        with pytest.raises(JiraRateLimitError, match="30 seconds"):
            _client(server).issue("DEMO-7")

    def test_a_non_json_body_is_a_response_error(self, server):
        with pytest.raises(JiraResponseError, match="non-JSON"):
            _client(server)._json("/rest/api/2/text")

    @pytest.mark.parametrize(
        ("exception", "message"),
        [
            (requests.exceptions.SSLError("bad chain"), "JIRA_CA_BUNDLE"),
            (requests.exceptions.ConnectTimeout("slow"), "did not answer in time"),
            (requests.exceptions.ConnectionError("refused"), "Could not connect"),
        ],
    )
    def test_network_and_tls_failures_are_connection_errors(self, server, exception, message):
        server.errors["/login.jsp"] = exception

        with pytest.raises(JiraConnectionError, match=message):
            _client(server).authenticate()

    def test_a_configured_ca_bundle_is_named_when_it_does_not_verify(self, server, tmp_path):
        bundle = tmp_path / "ca.pem"
        bundle.write_text("x")
        server.errors["/login.jsp"] = requests.exceptions.SSLError("bad chain")

        with pytest.raises(JiraConnectionError, match="does not verify it"):
            _client(server, adfs_settings(ca_bundle=str(bundle))).authenticate()


class TestTlsAndTimeouts:
    def test_every_request_verifies_certificates_with_the_default_store(self, server):
        _client(server).issue("DEMO-7")

        assert {call.verify for call in server.calls} == {True}

    def test_a_ca_bundle_is_used_for_every_request(self, server, tmp_path):
        bundle = tmp_path / "ca.pem"
        bundle.write_text("x")

        _client(server, adfs_settings(ca_bundle=str(bundle))).issue("DEMO-7")

        assert {call.verify for call in server.calls} == {str(bundle)}

    def test_every_request_has_a_timeout(self, server):
        _client(server, adfs_settings(connect_timeout=3, read_timeout=9)).issue("DEMO-7")

        assert {call.timeout for call in server.calls} == {(3, 9)}


class TestComments:
    def test_all_pages_are_collected_in_order(self, server):
        server.comments["DEMO-7"] = [comment(number) for number in range(1, 6)]

        result = _client(server).comments("DEMO-7", page_size=2)

        assert [item["id"] for item in result] == ["1", "2", "3", "4", "5"]
        pages = [call for call in server.calls if call.url.endswith("/comment")]
        assert len(pages) == 3

    def test_no_comments_is_one_request(self, server):
        assert _client(server).comments("DEMO-7") == []

    def test_a_list_that_ends_before_total_is_an_error(self, server):
        server.comments["DEMO-7"] = [comment(number) for number in range(1, 6)]
        server.missing_comments = 1

        with pytest.raises(JiraResponseError, match="reported 6 comments.*returned only 5"):
            _client(server).comments("DEMO-7", page_size=2)


class TestAttachments:
    def test_metadata_by_id(self, server):
        assert _client(server).attachment("42")["id"] == "42"

    def test_a_file_is_downloaded_with_the_session(self, server):
        server.files["notes.txt"] = b"hello"

        data = _client(server).download(f"{JIRA}/secure/attachment/1/notes.txt", 100)

        assert data == b"hello"

    def test_a_download_over_the_limit_stops(self, server):
        server.files["big.txt"] = b"x" * 50

        with pytest.raises(AttachmentTooLarge):
            _client(server).download(f"{JIRA}/secure/attachment/1/big.txt", 10)

    def test_a_declared_length_over_the_limit_stops_before_reading(self, server):
        server.files["big.txt"] = b"x"
        server.file_headers["big.txt"] = {"Content-Length": "999"}

        with pytest.raises(AttachmentTooLarge):
            _client(server).download(f"{JIRA}/secure/attachment/1/big.txt", 10)

    def test_the_session_is_never_sent_to_another_host(self, server):
        client = _client(server)
        client.authenticate()
        before = len(server.calls)

        with pytest.raises(JiraResponseError, match="another host"):
            client.download("https://files.test/secure/attachment/1/a.txt", 100)

        assert len(server.calls) == before

    def test_a_missing_file_is_an_error(self, server):
        with pytest.raises(JiraNotFoundError):
            _client(server).download(f"{JIRA}/secure/attachment/9/gone.txt", 100)
