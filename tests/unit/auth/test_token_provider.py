"""
تست‌های واحدِ src/auth/token_provider.py — بدونِ شبکه.

درخواست با یک requests.Sessionِ واقعی ساخته می‌شود ولی به‌جای شبکه به یک adapterِ
ساختگی می‌رسد؛ پس هدرها و بدنه‌ی واقعاً آماده‌شده (PreparedRequest) بررسی می‌شوند.
همه‌ی مقدارها ساختگی‌اند.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from src.auth import token_provider as tp
from src.auth.token_provider import AccessToken, AuthenticationError, TokenProvider, TokenSettings

REPO_ROOT = Path(__file__).resolve().parents[3]
URL = "https://podium.test/api/admin/oauth2/refresh"
REFRESH = "SYNTHETIC-REFRESH-TOKEN-0b7e41"
ACCESS = "SYNTHETIC-ACCESS-TOKEN-c93d10"
ROTATED = "SYNTHETIC-ROTATED-REFRESH-5aa2"


def _response(request, status: int = 200, body=None, *, raw: bytes | None = None, headers=None):
    response = requests.Response()
    response.status_code = status
    if raw is None:
        payload = {"result": {"token": ACCESS, "refreshToken": ROTATED}} if body is None else body
        raw = json.dumps(payload).encode()
    response._content = raw
    response._content_consumed = True
    response.headers = CaseInsensitiveDict({"Content-Type": "application/json", **(headers or {})})
    response.url = request.url
    response.request = request
    return response


class _Adapter(BaseAdapter):
    """جای شبکه — درخواستِ آماده‌شده و kwargs را نگه می‌دارد."""

    def __init__(self, respond) -> None:
        super().__init__()
        self.respond = respond
        self.sent: list[tuple[requests.PreparedRequest, dict]] = []

    def send(self, request, **kwargs):
        self.sent.append((request, kwargs))
        return self.respond(request)

    def close(self) -> None:
        pass


class _Endpoint:
    """session_factory: هر session یک adapter برای https و http دارد (http نباید برسد)."""

    def __init__(self, respond=None, *, preset_cookie: bool = False) -> None:
        self.adapter = _Adapter(respond or (lambda request: _response(request)))
        self.preset_cookie = preset_cookie

    def __call__(self) -> requests.Session:
        session = requests.Session()
        session.mount("https://", self.adapter)
        session.mount("http://", self.adapter)
        if self.preset_cookie:
            session.cookies.set("SESSION", "SYNTHETIC-COOKIE-77", domain="podium.test")
        return session

    @property
    def sent(self):
        return self.adapter.sent


def _provider(endpoint: _Endpoint, **settings) -> TokenProvider:
    values = {"refresh_url": URL, "refresh_token": REFRESH, **settings}
    return TokenProvider(lambda: TokenSettings(**values), endpoint)


def _failure(endpoint: _Endpoint, **settings) -> AuthenticationError:
    with pytest.raises(AuthenticationError) as caught:
        _provider(endpoint, **settings).fetch()
    return caught.value


def _assert_secret_free(error: AuthenticationError) -> None:
    text = f"{error} {error!r} {error.args}"
    for secret in (REFRESH, ACCESS, ROTATED):
        assert secret not in text
    assert error.__cause__ is None  # زنجیره‌ی استثنای requests بریده شده
    assert error.__suppress_context__ or error.__context__ is None


class TestConfiguration:
    @pytest.mark.parametrize("refresh_token", ["", "   "])
    def test_a_missing_or_empty_refresh_token_sends_nothing(self, refresh_token):
        endpoint = _Endpoint()
        error = _failure(endpoint, refresh_token=refresh_token.strip())
        assert error.kind == tp.KIND_CONFIGURATION
        assert "PODIUM_REFRESH_TOKEN is not set" in str(error)
        assert endpoint.sent == []

    def test_a_missing_endpoint_sends_nothing(self):
        endpoint = _Endpoint()
        error = _failure(endpoint, refresh_url="")
        assert error.kind == tp.KIND_CONFIGURATION
        assert "PODIUM_TOKEN_REFRESH_URL is not set" in str(error)
        assert endpoint.sent == []

    def test_an_http_endpoint_is_rejected_and_the_refresh_token_is_never_sent(self):
        endpoint = _Endpoint()
        error = _failure(endpoint, refresh_url="http://podium.test/api/admin/oauth2/refresh")
        assert error.kind == tp.KIND_INSECURE_TRANSPORT
        assert "https://" in str(error)
        assert endpoint.sent == []
        _assert_secret_free(error)

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://podium.test/refresh",
            "podium.test/api/admin/oauth2/refresh",
            "https:///no-host",
            "https://user:pass@podium.test/api/admin/oauth2/refresh",
        ],
    )
    def test_other_invalid_endpoints_are_rejected(self, url):
        endpoint = _Endpoint()
        assert _failure(endpoint, refresh_url=url).kind == tp.KIND_CONFIGURATION
        assert endpoint.sent == []

    def test_every_problem_is_reported_together(self):
        error = _failure(_Endpoint(), refresh_url="", refresh_token="", connect_timeout=0)
        assert "PODIUM_TOKEN_REFRESH_URL" in str(error)
        assert "PODIUM_REFRESH_TOKEN" in str(error)
        assert "TIMEOUT" in str(error)

    def test_a_missing_ca_bundle_file_is_rejected(self, tmp_path):
        error = _failure(_Endpoint(), ca_bundle=str(tmp_path / "missing.pem"))
        assert error.kind == tp.KIND_CONFIGURATION

    def test_settings_repr_never_shows_the_refresh_token(self):
        assert REFRESH not in repr(TokenSettings(URL, REFRESH))

    def test_from_config_reads_src_config(self, monkeypatch):
        monkeypatch.undo()  # fixtureِ سراسری from_config را خنثی کرده
        from src import config

        monkeypatch.setattr(config, "PODIUM_TOKEN_REFRESH_URL", f"  {URL} ")
        monkeypatch.setattr(config, "PODIUM_REFRESH_TOKEN", f" {REFRESH} ")
        monkeypatch.setattr(config, "PODIUM_TOKEN_CA_BUNDLE", "")
        monkeypatch.setattr(config, "PODIUM_TOKEN_CONNECT_TIMEOUT_SECONDS", 3.0)
        monkeypatch.setattr(config, "PODIUM_TOKEN_READ_TIMEOUT_SECONDS", 7.0)
        monkeypatch.setattr(config, "PODIUM_TOKEN_ALLOW_INSECURE_HTTP", "true")
        settings = TokenSettings.from_config()
        assert settings.allow_insecure_http == "true"
        assert (settings.refresh_url, settings.refresh_token) == (URL, REFRESH)
        assert settings.timeout == (3.0, 7.0)
        assert settings.verify is True

    def test_the_test_suite_never_sees_the_real_env_settings(self):
        # fixtureِ سراسری: providerِ بدونِ تنظیماتِ ساختگی فقط خطای پیکربندی می‌دهد
        with pytest.raises(AuthenticationError) as caught:
            TokenProvider(session_factory=_Endpoint()).fetch()
        assert caught.value.kind == tp.KIND_CONFIGURATION


HTTP_URL = "http://podium-dev.test/api/admin/oauth2/refresh"


class TestInsecureHttpOptIn:
    @pytest.mark.parametrize("opt_in", ["", "false", "FALSE", "0", "no", "  "])
    def test_https_works_without_the_opt_in_and_keeps_tls_verification(self, opt_in):
        endpoint = _Endpoint()
        assert _provider(endpoint, allow_insecure_http=opt_in).fetch().value == ACCESS
        [(request, kwargs)] = endpoint.sent
        assert request.url == URL and kwargs["verify"] is True

    def test_the_opt_in_never_disables_tls_verification_for_https(self, tmp_path):
        endpoint = _Endpoint()
        _provider(endpoint, allow_insecure_http="true").fetch()
        assert endpoint.sent[0][1]["verify"] is True
        bundle = tmp_path / "ca.pem"
        bundle.write_text("synthetic", encoding="utf-8")
        _provider(endpoint, allow_insecure_http="true", ca_bundle=str(bundle)).fetch()
        assert endpoint.sent[1][1]["verify"] == str(bundle)

    @pytest.mark.parametrize("opt_in", ["", "false", "False", "0", "no", "   "])
    def test_http_is_rejected_unless_explicitly_opted_in(self, opt_in):
        endpoint = _Endpoint()
        error = _failure(endpoint, refresh_url=HTTP_URL, allow_insecure_http=opt_in)
        assert error.kind == tp.KIND_INSECURE_TRANSPORT
        assert "PODIUM_TOKEN_ALLOW_INSECURE_HTTP=true" in str(error)
        assert endpoint.sent == []  # refresh token هرگز فرستاده نشد
        _assert_secret_free(error)

    def test_http_is_rejected_when_the_opt_in_is_missing_from_settings(self):
        endpoint = _Endpoint()
        error = _failure(endpoint, refresh_url=HTTP_URL)  # پیش‌فرض: بدونِ opt-in
        assert error.kind == tp.KIND_INSECURE_TRANSPORT and endpoint.sent == []

    @pytest.mark.parametrize("opt_in", ["true", "TRUE", " True "])
    def test_http_is_accepted_only_with_an_explicit_true(self, opt_in):
        endpoint = _Endpoint(preset_cookie=True)
        provider = _provider(endpoint, refresh_url=HTTP_URL, allow_insecure_http=opt_in)
        first, second = provider.fetch(), provider.fetch()
        assert first.value == second.value == ACCESS
        for request, kwargs in endpoint.sent:
            assert request.url == HTTP_URL
            assert json.loads(request.body) == {"refreshToken": REFRESH}  # ثابت
            assert "Cookie" not in request.headers
            assert kwargs["verify"] is True  # برای HTTP بی‌اثر، ولی هرگز False

    @pytest.mark.parametrize(
        "opt_in",
        ["yes", "1", "on", "enable", "truee", "true1", "maybe", "SYNTHETIC-LOOKS-SECRET-1f"],
    )
    @pytest.mark.parametrize("url", [URL, HTTP_URL])
    def test_an_invalid_opt_in_value_is_a_safe_configuration_error(self, opt_in, url):
        endpoint = _Endpoint()
        error = _failure(endpoint, refresh_url=url, allow_insecure_http=opt_in)
        assert error.kind == tp.KIND_CONFIGURATION
        assert "PODIUM_TOKEN_ALLOW_INSECURE_HTTP must be true or false" in str(error)
        assert opt_in.strip() not in str(error)  # مقدار تکرار نمی‌شود
        assert endpoint.sent == []
        _assert_secret_free(error)

    def test_an_http_redirect_target_is_never_followed_from_https(self):
        def respond(request):
            return _response(request, 307, raw=b"", headers={"Location": HTTP_URL})

        endpoint = _Endpoint(respond)
        assert _failure(endpoint, allow_insecure_http="true").kind == tp.KIND_HTTP_STATUS
        assert [request.url for request, _ in endpoint.sent] == [URL]

    def test_http_failures_keep_secrets_out_of_errors_and_logs(self, caplog):
        caplog.set_level(logging.DEBUG)
        echo = json.dumps({"refreshToken": REFRESH, "result": {"token": ACCESS}}).encode()
        error = _failure(
            _Endpoint(lambda request: _response(request, 401, raw=echo)),
            refresh_url=HTTP_URL,
            allow_insecure_http="true",
        )
        assert error.kind == tp.KIND_REJECTED
        _assert_secret_free(error)
        _provider(_Endpoint(), refresh_url=HTTP_URL, allow_insecure_http="true").fetch()
        for secret in (REFRESH, ACCESS, ROTATED):
            assert secret not in caplog.text

    def test_the_opt_in_is_read_only_by_the_token_provider_configuration(self):
        users = sorted(
            str(path.relative_to(REPO_ROOT)).replace("\\", "/")
            for root in ("src", "ui")
            for path in (REPO_ROOT / root).rglob("*.py")
            if "PODIUM_TOKEN_ALLOW_INSECURE_HTTP" in path.read_text(encoding="utf-8")
        )
        assert users == ["src/auth/token_provider.py", "src/config.py"]


class TestEnvFileLoading:
    def test_dotenv_never_overrides_an_already_set_process_variable(self, tmp_path):
        env_file = tmp_path / "test.env"
        env_file.write_text(
            "PODIUM_REFRESH_TOKEN=SYNTHETIC-FROM-FILE\n"
            "PODIUM_TOKEN_REFRESH_URL=https://file.test/api/admin/oauth2/refresh\n",
            encoding="utf-8",
        )
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP")
            if key in os.environ
        }
        environment.update(
            {
                name: "synthetic"
                for name in (
                    "LLM_BASE_URL",
                    "LLM_MODEL",
                    "LLM_API_KEY",
                    "EMBEDDING_BASE_URL",
                    "EMBEDDING_MODEL",
                    "EMBEDDING_API_KEY",
                    "CLIENT_ID",
                )
            }
        )
        environment["ENV_FILE"] = str(env_file)
        environment["PODIUM_REFRESH_TOKEN"] = "SYNTHETIC-FROM-PROCESS"
        script = (
            "from src import config as c; "
            "print(c.PODIUM_REFRESH_TOKEN == 'SYNTHETIC-FROM-PROCESS', "
            "c.PODIUM_TOKEN_REFRESH_URL == 'https://file.test/api/admin/oauth2/refresh')"
        )
        output = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        ).stdout
        assert output.split() == ["True", "True"]


class TestRequest:
    def test_success_returns_result_token(self):
        endpoint = _Endpoint()
        access = _provider(endpoint).fetch()
        assert isinstance(access, AccessToken)
        assert access.value == ACCESS
        assert access.obtained_at.tzinfo is not None

    def test_the_request_is_a_json_post_with_only_the_refresh_token(self):
        endpoint = _Endpoint()
        _provider(endpoint, connect_timeout=4, read_timeout=9).fetch()
        [(request, kwargs)] = endpoint.sent
        assert request.method == "POST"
        assert request.url == URL
        assert json.loads(request.body) == {"refreshToken": REFRESH}
        assert request.headers["Content-Type"] == "application/json"
        assert kwargs["timeout"] == (4, 9)
        assert kwargs["verify"] is True

    def test_a_ca_bundle_is_used_for_verification(self, tmp_path):
        bundle = tmp_path / "ca.pem"
        bundle.write_text("synthetic", encoding="utf-8")
        endpoint = _Endpoint()
        _provider(endpoint, ca_bundle=str(bundle)).fetch()
        assert endpoint.sent[0][1]["verify"] == str(bundle)

    def test_no_cookie_is_sent_even_if_the_session_had_one(self):
        endpoint = _Endpoint(preset_cookie=True)
        _provider(endpoint).fetch()
        [(request, _)] = endpoint.sent
        assert "Cookie" not in request.headers

    def test_cookies_from_one_response_are_not_sent_on_the_next_fetch(self):
        def respond(request):
            return _response(request, headers={"Set-Cookie": "SESSION=SYNTHETIC-77; Path=/"})

        endpoint = _Endpoint(respond)
        provider = _provider(endpoint)
        provider.fetch()
        provider.fetch()
        assert all("Cookie" not in request.headers for request, _ in endpoint.sent)

    def test_the_static_refresh_token_is_never_replaced_by_result_refresh_token(self):
        endpoint = _Endpoint()
        settings = TokenSettings(URL, REFRESH)
        provider = TokenProvider(lambda: settings, endpoint)
        first, second = provider.fetch(), provider.fetch()
        bodies = [json.loads(request.body) for request, _ in endpoint.sent]
        assert bodies == [{"refreshToken": REFRESH}, {"refreshToken": REFRESH}]
        assert settings.refresh_token == REFRESH
        assert ROTATED not in repr(first) + repr(second) + repr(provider.__dict__)

    def test_redirects_are_not_followed(self):
        def respond(request):
            return _response(request, 302, raw=b"", headers={"Location": "http://elsewhere.test/"})

        endpoint = _Endpoint(respond)
        error = _failure(endpoint)
        assert error.kind == tp.KIND_HTTP_STATUS
        assert len(endpoint.sent) == 1  # هیچ درخواستِ دومی (به http) نیست

    def test_there_is_no_automatic_retry(self):
        endpoint = _Endpoint(lambda request: _response(request, 503, raw=b"busy"))
        _failure(endpoint)
        assert len(endpoint.sent) == 1


class TestFailures:
    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (401, tp.KIND_REJECTED),
            (403, tp.KIND_REJECTED),
            (500, tp.KIND_SERVER),
            (502, tp.KIND_SERVER),
            (503, tp.KIND_SERVER),
            (404, tp.KIND_HTTP_STATUS),
            (204, tp.KIND_HTTP_STATUS),
        ],
    )
    def test_http_errors(self, status, kind):
        # بدنه عمداً همه‌ی رازها را پژواک می‌دهد — نباید به پیام برسد
        echo = json.dumps({"error": REFRESH, "result": {"token": ACCESS}}).encode()
        error = _failure(_Endpoint(lambda request: _response(request, status, raw=echo)))
        assert error.kind == kind
        assert f"HTTP {status}" in str(error)
        _assert_secret_free(error)

    @pytest.mark.parametrize(
        ("exception", "kind"),
        [
            (requests.exceptions.ConnectTimeout, tp.KIND_TIMEOUT),
            (requests.exceptions.ReadTimeout, tp.KIND_TIMEOUT),
            (requests.exceptions.ConnectionError, tp.KIND_CONNECTION),
            (requests.exceptions.SSLError, tp.KIND_TLS),
            (requests.exceptions.ChunkedEncodingError, tp.KIND_CONNECTION),
            (RuntimeError, tp.KIND_CONNECTION),
        ],
    )
    def test_transport_failures_have_fixed_messages(self, exception, kind):
        def respond(request):
            raise exception(f"failed while sending {REFRESH} and {request.body!r}")

        error = _failure(_Endpoint(respond))
        assert error.kind == kind
        _assert_secret_free(error)

    @pytest.mark.parametrize(
        "raw",
        [b"not json", b"<html>gateway</html>", b"", b'{"result": '],
    )
    def test_invalid_json(self, raw):
        error = _failure(_Endpoint(lambda request: _response(request, raw=raw)))
        assert error.kind == tp.KIND_MALFORMED

    @pytest.mark.parametrize(
        "body",
        [
            [],
            "token",
            {},
            {"result": None},
            {"result": "token"},
            {"result": []},
            {"token": ACCESS},
            {"result": {}},
            {"result": {"refreshToken": ROTATED}},
            {"result": {"token": None}},
            {"result": {"token": ""}},
            {"result": {"token": "   "}},
            {"result": {"token": 12345}},
            {"result": {"token": ["a"]}},
            {"result": {"token": {"value": ACCESS}}},
            {"result": {"token": f"Bearer {ACCESS}"}},
            {"result": {"token": f" {ACCESS}"}},
            {"result": {"token": f"{ACCESS}\n"}},
        ],
    )
    def test_malformed_responses(self, body):
        error = _failure(_Endpoint(lambda request: _response(request, body=body)))
        assert error.kind == tp.KIND_MALFORMED
        _assert_secret_free(error)


class TestSecrecy:
    def test_access_token_repr_hides_the_value(self):
        assert ACCESS not in repr(AccessToken(ACCESS))
        assert ACCESS not in str(AccessToken(ACCESS))

    def test_nothing_secret_is_logged(self, caplog):
        caplog.set_level(logging.DEBUG)
        _provider(_Endpoint()).fetch()
        with pytest.raises(AuthenticationError):
            _provider(_Endpoint(lambda request: _response(request, 401))).fetch()
        for secret in (REFRESH, ACCESS, ROTATED):
            assert secret not in caplog.text


def test_the_batch_runner_uses_the_shared_provider_by_default():
    from src.batch_testing import runner as runner_module

    assert isinstance(runner_module._default_token_provider(), TokenProvider)
