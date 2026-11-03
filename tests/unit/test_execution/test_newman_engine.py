"""
تست‌های واحدِ موتورِ Newman — با runnerِ ساختگی، بدونِ Node و شبکه.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import pytest

from src.test_execution.newman import (
    DEV_ENVIRONMENT_PATH,
    NO_ASSERTIONS_WARNING,
    WORKSPACE_GLOBALS_PATH,
    NewmanExecutionEngine,
    NewmanOptions,
    NewmanRunRequest,
)
from src.test_execution.newman import engine as engine_module
from src.test_execution.newman.locator import NewmanInstallation, locate
from src.test_execution.newman.preflight import run_preflight, sha256_of
from src.test_execution.newman.scratch import (
    MARKER,
    PrivateDirectoryError,
    acl_problems,
    create_private_dir,
    remove_private_dir,
    sweep_stale,
)
from tests.unit.test_execution.newman_fakes import (
    ALL_SECRETS,
    COOKIE,
    INSTALLATION,
    PASSWORD,
    QUERY_SECRET,
    TOKEN,
    FakeRunner,
    item_events,
    request_item,
    secret_execution,
    summary_for,
    write_inputs,
)

PASSING = item_events(0, "TC-001 — Read", assertions=[("Status code is 200", None)])


def _run(tmp_path: Path, runner: FakeRunner, *, options: NewmanOptions | None = None, **inputs):
    collection, environment, globals_path = write_inputs(tmp_path, **inputs)
    request = NewmanRunRequest(
        collection, environment, globals_path, tmp_path / "out", options or NewmanOptions()
    )
    return NewmanExecutionEngine(
        process_runner=runner, installation=INSTALLATION, scratch_root=tmp_path / "scratch"
    ).run(request)


def _whole_token(text: str, secret: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])", text) is not None


def _artifact_texts(result) -> dict[str, str]:
    run_dir = Path(result.artifacts.run_dir)
    return {path.name: path.read_text(encoding="utf-8") for path in run_dir.iterdir()}


class TestClassification:
    def test_all_assertions_pass(self, tmp_path):
        result = _run(tmp_path, FakeRunner(events=PASSING))
        assert (result.status, result.error_kind, result.no_assertions) == ("PASS", "", False)
        assert result.stats.assertions_total == 1

    def test_an_assertion_failure_is_fail(self, tmp_path):
        events = item_events(
            0, "TC-001", status=500, assertions=[("Status code is 200", "expected 200 but got 500")]
        )
        result = _run(tmp_path, FakeRunner(events=events))
        assert result.status == "FAIL"
        assert result.error_kind == ""
        assert result.requests[0].assertions[0].message == "expected 200 but got 500"

    @pytest.mark.parametrize(
        ("status", "failure", "expected"),
        [
            (401, None, "PASS"),
            (403, "expected response to have status code 200 but got 403", "FAIL"),
            (500, None, "PASS"),
        ],
    )
    def test_http_status_codes_are_judged_only_by_assertions(
        self, tmp_path, status, failure, expected
    ):
        name = f"Status code is {200 if failure else status}"
        events = item_events(0, "TC-001", status=status, assertions=[(name, failure)])
        result = _run(tmp_path, FakeRunner(events=events))
        assert result.status == expected
        assert result.requests[0].status_code == status

    def test_a_network_error_with_assertion_failures_is_error_and_keeps_the_failures(
        self, tmp_path
    ):
        events = item_events(
            0, "TC-001", status=403, assertions=[("Status code is 200", "got 403")]
        ) + item_events(
            1,
            "TC-002",
            network_error="connect ECONNREFUSED 127.0.0.1:9",
            assertions=[("Status code is 200", "no code")],
        )
        result = _run(tmp_path, FakeRunner(events=events))
        assert (result.status, result.error_kind) == ("ERROR", "NETWORK_ERROR")
        assert result.stats.assertions_failed == 2
        assert result.requests[0].assertions[0].message == "got 403"
        assert result.requests[1].errors == [
            {"kind": "network", "message": "connect ECONNREFUSED 127.0.0.1:9"}
        ]
        assert result.requests[1].status_code is None

    def test_a_script_error_is_error(self, tmp_path):
        events = PASSING[:-1] + [
            {
                "type": "script_error",
                "item_id": "id-0",
                "item_name": "TC-001 — Read",
                "index": 0,
                "event": "test",
                "error": {"name": "TypeError", "message": "x is undefined"},
            },
            PASSING[-1],
        ]
        result = _run(tmp_path, FakeRunner(events=events))
        assert (result.status, result.error_kind) == ("ERROR", "SCRIPT_ERROR")

    def test_a_newman_runtime_error_is_error(self, tmp_path):
        result = _run(
            tmp_path, FakeRunner(events=PASSING, done_error={"name": "Error", "message": "boom"})
        )
        assert (result.status, result.error_kind) == ("ERROR", "NEWMAN_RUNTIME_ERROR")

    def test_no_assertions_is_a_flagged_pass(self, tmp_path):
        result = _run(tmp_path, FakeRunner(events=item_events(0, "TC-001")))
        assert (result.status, result.no_assertions) == ("PASS", True)
        assert NO_ASSERTIONS_WARNING in result.warnings
        texts = _artifact_texts(result)
        assert NO_ASSERTIONS_WARNING in texts["newman-report.html"]
        assert json.loads(texts["newman-report.json"])["engine_warnings"] == [NO_ASSERTIONS_WARNING]
        assert json.loads(texts["result.json"])["no_assertions"] is True

    def test_a_skipped_dependency_is_fail_with_the_reason(self, tmp_path):
        events = item_events(
            0,
            "TC-002",
            sent=False,
            assertions=[
                ("Required collection variable 'voucherId' is available", "voucherId is not set")
            ],
        )
        result = _run(tmp_path, FakeRunner(events=events))
        assert result.status == "FAIL"
        assert result.requests[0].skipped is True
        assert result.requests[0].skip_reason == "voucherId is not set"
        assert result.requests[0].skip_kind == "failed_precondition"
        assert result.requests[0].executed is False


class TestStartupAndInputs:
    def test_node_missing(self, tmp_path):
        missing = NewmanInstallation(
            error_kind="NODE_NOT_FOUND", error_message="Node.js was not found on PATH."
        )
        collection, environment, globals_path = write_inputs(tmp_path)
        result = NewmanExecutionEngine(process_runner=FakeRunner(), installation=missing).run(
            NewmanRunRequest(collection, environment, globals_path, tmp_path / "out")
        )
        assert (result.status, result.error_kind) == ("ERROR", "NODE_NOT_FOUND")
        assert result.artifacts.run_dir == ""

    def test_newman_and_reporter_missing_are_detected(self, tmp_path):
        assert locate(node_modules=tmp_path).error_kind == "NEWMAN_NOT_FOUND"
        (tmp_path / "newman").mkdir()
        (tmp_path / "newman" / "index.js").write_text("", encoding="utf-8")
        (tmp_path / "newman" / "package.json").write_text('{"version": "6.2.3"}', encoding="utf-8")
        assert locate(node_modules=tmp_path).error_kind == "REPORTER_NOT_FOUND"

    def test_the_process_cannot_start(self, tmp_path):
        result = _run(tmp_path, FakeRunner(started=False))
        assert (result.status, result.error_kind) == ("ERROR", "PROCESS_START_FAILED")

    def test_the_wrapper_fails(self, tmp_path):
        result = _run(
            tmp_path, FakeRunner(exit_code=4, finish=False, write_events=False, summary=None)
        )
        assert (result.status, result.error_kind) == ("ERROR", "PROCESS_FAILED")

    @pytest.mark.parametrize(
        ("target", "content", "kind"),
        [
            ("collection.json", "{not json", "INVALID_COLLECTION"),
            ("collection.json", json.dumps({"item": []}), "INVALID_COLLECTION"),
            (
                "dev.json",
                json.dumps({"_postman_variable_scope": "globals", "values": []}),
                "INVALID_ENVIRONMENT",
            ),
            ("dev.json", "[]", "INVALID_ENVIRONMENT"),
            (
                "globals.json",
                json.dumps({"_postman_variable_scope": "environment", "values": []}),
                "INVALID_GLOBALS",
            ),
            ("globals.json", "", "INVALID_GLOBALS"),
        ],
    )
    def test_invalid_input_files(self, tmp_path, target, content, kind):
        runner = FakeRunner(events=PASSING)
        collection, environment, globals_path = write_inputs(tmp_path)
        (tmp_path / target).write_text(content, encoding="utf-8")
        result = NewmanExecutionEngine(process_runner=runner, installation=INSTALLATION).run(
            NewmanRunRequest(collection, environment, globals_path, tmp_path / "out")
        )
        assert (result.status, result.error_kind) == ("ERROR", kind)
        assert runner.calls == []

    def test_a_missing_input_file(self, tmp_path):
        collection, environment, globals_path = write_inputs(tmp_path)
        result = NewmanExecutionEngine(process_runner=FakeRunner(), installation=INSTALLATION).run(
            NewmanRunRequest(collection, tmp_path / "nope.json", globals_path, tmp_path / "out")
        )
        assert result.error_kind == "INVALID_ENVIRONMENT"

    def test_an_old_baseurl_collection_is_rejected(self, tmp_path):
        result = _run(tmp_path, FakeRunner(), items=[request_item("TC-001", host="{{baseUrl}}")])
        assert result.error_kind == "INVALID_COLLECTION"

    def test_a_collection_level_host_variable_is_forbidden(self, tmp_path):
        result = _run(
            tmp_path, FakeRunner(), collection_variables=[{"key": "base_url", "value": "http://x"}]
        )
        assert result.error_kind == "HOST_VARIABLE_IN_COLLECTION"

    def test_invalid_options(self, tmp_path):
        result = _run(tmp_path, FakeRunner(), options=NewmanOptions(process_timeout_s=0))
        assert result.error_kind == "INVALID_OPTIONS"


class TestHostVariablePrecedence:
    @staticmethod
    def _check(tmp_path, env_values, globals_values):
        paths = write_inputs(tmp_path, env_values=env_values, globals_values=globals_values)
        return run_preflight(*paths)

    def test_an_empty_environment_value_wins_over_globals(self, tmp_path):
        result = self._check(
            tmp_path,
            [{"key": "base_url", "value": "", "enabled": True}],
            [{"key": "base_url", "value": "http://global.test", "enabled": True}],
        )
        assert result.error_kind == "HOST_VARIABLE_EMPTY"
        assert result.host_variables[0].source == "environment"

    def test_globals_supply_a_variable_missing_from_the_environment(self, tmp_path):
        result = self._check(tmp_path, [], [{"key": "base_url", "value": "http://global.test"}])
        assert result.ok
        assert (result.host_variables[0].source, result.host_variables[0].non_empty) == (
            "globals",
            True,
        )

    def test_a_disabled_environment_entry_falls_back_to_globals(self, tmp_path):
        result = self._check(
            tmp_path,
            [{"key": "base_url", "value": "", "enabled": False}],
            [{"key": "base_url", "value": "http://global.test"}],
        )
        assert result.ok and result.host_variables[0].source == "globals"

    def test_an_undefined_variable_is_empty(self, tmp_path):
        result = self._check(tmp_path, [], [])
        assert result.error_kind == "HOST_VARIABLE_EMPTY"
        assert result.host_variables[0].defined is False

    def test_only_used_host_variables_are_checked(self, tmp_path):
        result = self._check(tmp_path, [{"key": "base_url", "value": "http://a.test"}], [])
        assert [check.variable for check in result.host_variables] == ["base_url"]

    def test_values_are_never_reported(self, tmp_path):
        result = self._check(
            tmp_path, [{"key": "base_url", "value": "http://secret-host.test"}], []
        )
        assert "secret-host" not in json.dumps([vars(check) for check in result.host_variables])


class TestReports:
    def test_a_missing_json_report_is_error(self, tmp_path):
        result = _run(tmp_path, FakeRunner(events=PASSING, summary=None))
        assert (result.status, result.error_kind) == ("ERROR", "REPORT_MISSING")

    def test_a_malformed_json_report_is_error(self, tmp_path):
        result = _run(tmp_path, FakeRunner(events=PASSING, summary="{truncated"))
        assert (result.status, result.error_kind) == ("ERROR", "REPORT_MALFORMED")

    def test_a_missing_event_log_is_error(self, tmp_path):
        result = _run(tmp_path, FakeRunner(events=PASSING, write_events=False))
        assert (result.status, result.error_kind) == ("ERROR", "REPORT_MISSING")

    def test_a_report_that_disagrees_with_the_run_is_error(self, tmp_path):
        summary = summary_for(PASSING)
        summary["run"]["stats"]["requests"]["total"] = 5
        result = _run(tmp_path, FakeRunner(events=PASSING, summary=summary))
        assert (result.status, result.error_kind) == ("ERROR", "REPORT_INCONSISTENT")

    def test_an_exit_code_that_contradicts_the_report_is_error(self, tmp_path):
        result = _run(tmp_path, FakeRunner(events=PASSING, exit_code=1))
        assert result.error_kind == "REPORT_INCONSISTENT"

    def test_request_level_results_are_extracted(self, tmp_path):
        events = item_events(
            0,
            "TC-001 — Read voucher",
            status=200,
            assertions=[("Status code is 200", None), ("Response has id", "expected id")],
            url=f"http://podium.test/api/voucher/7?api_key={QUERY_SECRET}&page=2",
        )
        result = _run(tmp_path, FakeRunner(events=events))
        (request,) = result.requests
        assert request.name == "TC-001 — Read voucher"
        assert (request.method, request.status_code, request.status_text) == ("GET", 200, "OK")
        assert request.response_time_ms == 12
        assert request.url == "http://podium.test/api/voucher/7?api_key=***&page=2"
        assert [(a.name, a.passed, a.message) for a in request.assertions] == [
            ("Status code is 200", True, ""),
            ("Response has id", False, "expected id"),
        ]
        assert (request.executed, request.completed, request.skipped) == (True, True, False)


class TestTimeout:
    def test_partial_results_survive_a_timeout(self, tmp_path):
        events = PASSING + item_events(1, "TC-002", sent=False, done=False)
        runner = FakeRunner(events=events, timed_out=True, finish=False, summary=None)
        result = _run(tmp_path, runner, options=NewmanOptions(process_timeout_s=5))
        assert (result.status, result.error_kind, result.timed_out) == (
            "ERROR",
            "PROCESS_TIMEOUT",
            True,
        )
        assert runner.calls[0]["timeout_s"] == 5
        assert result.requests[0].status_code == 200 and result.requests[0].completed
        assert result.requests[1].completed is False
        assert result.stats.requests_executed == 1

    def test_the_default_timeouts(self, tmp_path):
        runner = FakeRunner(events=PASSING)
        _run(tmp_path, runner)
        assert runner.calls[0]["timeout_s"] == 900
        assert runner.calls[0]["config"]["timeout_request_ms"] == 30_000


class TestSecretProtection:
    @staticmethod
    def _leaky_runner() -> FakeRunner:
        events = item_events(
            0,
            "TC-001 — Read",
            assertions=[("Status code is 200", f"expected {TOKEN} to equal x")],
            url=f"http://podium.test/api/voucher?api_key={QUERY_SECRET}&token={TOKEN}",
        )
        return FakeRunner(
            events=events,
            summary=summary_for(events, executions=[secret_execution()]),
            html=f"<html><body><a href='http://podium.test/api/voucher?api_key={QUERY_SECRET}'>x</a>"
            f"<pre>Authorization: {TOKEN}</pre><pre>Set-Cookie: SESSION={COOKIE}</pre>"
            f"<p>{PASSWORD}</p></body></html>",
            stdout=f"debug token={TOKEN}\n",
            stderr=f"warning: cookie {COOKIE} password {PASSWORD}\n",
        )

    def test_no_secret_appears_in_any_persisted_artifact(self, tmp_path):
        result = _run(tmp_path, self._leaky_runner())
        texts = _artifact_texts(result)
        assert set(texts) == {
            "newman-report.json",
            "newman-report.html",
            "stdout.log",
            "stderr.log",
            "result.json",
        }
        for name, text in texts.items():
            for secret in ALL_SECRETS:
                assert secret not in text, (name, secret[:8])
        assert result.status == "FAIL"

    def test_sensitive_structures_are_removed_from_the_json_report(self, tmp_path):
        result = _run(tmp_path, self._leaky_runner())
        report = json.loads(_artifact_texts(result)["newman-report.json"])
        execution = report["run"]["executions"][0]
        headers = {h["key"]: h["value"] for h in execution["request"]["header"]}
        assert headers == {"Authorization": "***", "Cookie": "***", "Accept": "application/json"}
        assert execution["request"]["body"] == {"redacted": True}
        assert execution["response"]["stream"] == {"redacted": True}
        assert execution["response"]["header"][0]["value"] == "***"
        assert execution["request"]["url"]["query"][0]["value"] == "***"
        assert {v["value"] for v in report["environment"]["values"]} == {"***"}

    def test_redacted_url_and_messages_in_the_result(self, tmp_path):
        result = _run(tmp_path, self._leaky_runner())
        assert result.requests[0].url == "http://podium.test/api/voucher?api_key=***&token=***"
        assert result.requests[0].assertions[0].message == "expected *** to equal x"
        assert TOKEN not in result.stdout_tail

    def test_a_sanitization_failure_publishes_no_report(self, tmp_path, monkeypatch):
        real = engine_module.scrub_text
        monkeypatch.setattr(
            engine_module,
            "scrub_text",
            lambda text, secrets: text if "<html" in text else real(text, secrets),
        )
        result = _run(tmp_path, self._leaky_runner())
        assert (result.status, result.error_kind) == ("ERROR", "SANITIZATION_FAILED")
        texts = _artifact_texts(result)
        assert set(texts) == {"result.json"}
        for secret in ALL_SECRETS:
            assert secret not in texts["result.json"]
        assert result.requests == []

    def test_the_failure_message_never_contains_a_secret(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine_module, "scrub_text", lambda text, secrets: text)
        result = _run(tmp_path, self._leaky_runner())
        assert result.error_kind == "SANITIZATION_FAILED"
        assert not any(secret in result.error_message for secret in ALL_SECRETS)

    def test_the_child_gets_no_python_credentials(self, monkeypatch):
        from src.test_execution.newman.process import child_environment

        monkeypatch.setenv("JIRA_PASSWORD", "x")
        monkeypatch.setenv("LLM_API_KEY", "y")
        environment = child_environment()
        assert "JIRA_PASSWORD" not in environment and "LLM_API_KEY" not in environment


class TestArtifactsAndInputs:
    def test_each_run_gets_its_own_directory_and_nothing_is_deleted(self, tmp_path):
        first = _run(tmp_path, FakeRunner(events=PASSING))
        marker = Path(first.artifacts.run_dir) / "keep.txt"
        marker.write_text("previous", encoding="utf-8")
        second = _run(tmp_path, FakeRunner(events=PASSING))
        assert first.artifacts.run_dir != second.artifacts.run_dir
        assert marker.read_text(encoding="utf-8") == "previous"
        assert Path(first.artifacts.result_json).is_file()

    def test_the_private_directory_is_removed(self, tmp_path):
        runner = FakeRunner(events=PASSING)
        _run(tmp_path, runner)
        assert not Path(runner.calls[0]["config"]["events_path"]).parent.exists()

    def test_raw_reports_are_written_only_to_the_private_directory(self, tmp_path):
        runner = FakeRunner(events=PASSING)
        result = _run(tmp_path, runner)
        config = runner.calls[0]["config"]
        for key in ("json_report", "html_report", "events_path"):
            assert Path(config[key]).parent != Path(result.artifacts.run_dir)

    def test_input_hashes_are_recorded_and_unchanged(self, tmp_path):
        result = _run(tmp_path, FakeRunner(events=PASSING))
        for record in (result.collection, result.environment, result.globals):
            assert record.sha256_before and record.sha256_before == record.sha256_after

    def test_a_modified_input_file_is_error(self, tmp_path):
        def touch(config):
            Path(config["environment"]).write_text(
                Path(config["environment"]).read_text(encoding="utf-8") + " ", encoding="utf-8"
            )

        result = _run(tmp_path, FakeRunner(events=PASSING, side_effect=touch))
        assert result.error_kind == "INPUT_FILE_MODIFIED"

    def test_the_result_is_serialisable(self, tmp_path):
        result = _run(tmp_path, FakeRunner(events=PASSING))
        saved = json.loads(Path(result.artifacts.result_json).read_text(encoding="utf-8"))
        assert saved == json.loads(json.dumps(result.to_dict()))
        assert saved["command"][-1] == "<private config>"


class TestRealProjectFiles:
    def test_the_dev_environment_and_globals_pass_preflight_without_changes(self, tmp_path):
        before = (sha256_of(DEV_ENVIRONMENT_PATH), sha256_of(WORKSPACE_GLOBALS_PATH))
        collection, _, _ = write_inputs(
            tmp_path, items=[request_item("a"), request_item("b", host="{{base_url_vdi}}")]
        )
        result = run_preflight(collection, DEV_ENVIRONMENT_PATH, WORKSPACE_GLOBALS_PATH)
        assert result.ok, result.error_message
        assert [(c.variable, c.source, c.non_empty) for c in result.host_variables] == [
            ("base_url", "environment", True),
            ("base_url_vdi", "environment", True),
        ]
        assert (sha256_of(DEV_ENVIRONMENT_PATH), sha256_of(WORKSPACE_GLOBALS_PATH)) == before

    def test_for_dev_uses_the_existing_files(self, tmp_path):
        request = NewmanRunRequest.for_dev(tmp_path / "c.json", tmp_path)
        assert request.environment_path == DEV_ENVIRONMENT_PATH
        assert request.globals_path == WORKSPACE_GLOBALS_PATH
        assert DEV_ENVIRONMENT_PATH.name == "dev.postman_environment.json"
        assert WORKSPACE_GLOBALS_PATH.name == "workspace.postman_globals.json"


class TestShortSecrets:
    """هیچ مقدارِ محرمانه‌ی شناخته‌شده‌ای، با هر طولی، در خروجیِ منتشرشده نمی‌ماند."""

    SHORT = ["Q", "x9", "k7Z"]

    @staticmethod
    def _env(secret: str) -> list[dict]:
        return [
            {"key": "base_url", "value": "http://podium.test", "enabled": True},
            {"key": "token", "value": secret, "enabled": True},
        ]

    @pytest.mark.parametrize("secret", SHORT)
    def test_short_secrets_in_sensitive_contexts_are_masked(self, tmp_path, secret):
        events = item_events(
            0,
            "TC-001",
            assertions=[("Status code is 200", None)],
            url=f"http://podium.test/api/voucher?token={secret}&page=2",
        )
        execution = {
            "request": {
                "header": [{"key": "Authorization", "value": secret}],
                "url": {"query": []},
            },
            "response": {"header": []},
        }
        runner = FakeRunner(
            events=events,
            summary=summary_for(events, executions=[execution]),
            html=f"<html><body><a href='http://podium.test/api/voucher?token={secret}&amp;page=2'>r</a>"
            "</body></html>",
            stdout=f"request sent with token={secret}\n",
            stderr=f"Authorization: Bearer {secret}\n",
        )
        result = _run(tmp_path, runner, env_values=self._env(secret))
        assert result.status == "PASS", result.error_message
        assert result.requests[0].url == "http://podium.test/api/voucher?token=***&page=2"
        for name, text in _artifact_texts(result).items():
            if name.endswith(".json"):
                strings = json.dumps(json.loads(text), ensure_ascii=False)
                assert not _whole_token(strings, secret), name
            else:
                assert not _whole_token(text, secret), name

    @pytest.mark.parametrize("secret", SHORT)
    @pytest.mark.parametrize("where", ["stderr", "stdout", "assertion", "network"])
    def test_a_short_secret_in_free_text_withholds_the_artifacts(self, tmp_path, secret, where):
        assertion = f"expected {secret} to equal x" if where == "assertion" else None
        events = item_events(
            0,
            "TC-001",
            assertions=[("Status code is 200", assertion)],
            network_error=f"socket closed near {secret}" if where == "network" else None,
        )
        runner = FakeRunner(
            events=events,
            stdout=f"value {secret} leaked\n" if where == "stdout" else "",
            stderr=f"value {secret} leaked\n" if where == "stderr" else "",
        )
        result = _run(tmp_path, runner, env_values=self._env(secret))
        assert (result.status, result.error_kind) == ("ERROR", "SANITIZATION_FAILED")
        texts = _artifact_texts(result)
        assert set(texts) == {"result.json"}
        saved = json.loads(texts["result.json"])
        assert saved["requests"] == [] and saved["stdout_tail"] == saved["stderr_tail"] == ""
        strings = json.dumps(saved, ensure_ascii=False)
        assert not _whole_token(strings, secret)

    @pytest.mark.parametrize("secret", SHORT)
    def test_htmlextra_entity_escaped_urls_are_masked(self, tmp_path, secret):
        """htmlextra (Handlebars) writes «=» as «&#x3D;» — found against the real reporter."""
        html = (
            f'<a href="http://podium.test/api/ok?token&#x3D;{secret}">'
            f"http://podium.test/api/ok?page&#x3D;1&amp;token&#x3D;{secret}</a>"
        )
        result = _run(tmp_path, FakeRunner(events=PASSING, html=html), env_values=self._env(secret))
        assert result.status == "PASS", result.error_message
        published = _artifact_texts(result)["newman-report.html"]
        assert "token&#x3D;***" in published and "page&#x3D;1" in published
        assert not _whole_token(published, secret)

    def test_a_long_secret_in_handlebars_escaped_form_is_masked(self, tmp_path):
        secret = "LONG=SECRET`VALUE-123"
        escaped = "LONG&#x3D;SECRET&#x60;VALUE-123"
        result = _run(
            tmp_path,
            FakeRunner(events=PASSING, html=f"<p>{escaped}</p>"),
            env_values=self._env(secret),
        )
        assert result.status == "PASS", result.error_message
        assert escaped not in _artifact_texts(result)["newman-report.html"]

    def test_a_short_secret_inside_a_longer_word_is_not_a_leak(self, tmp_path):
        runner = FakeRunner(events=PASSING, stdout="the about page\n")
        result = _run(tmp_path, runner, env_values=self._env("ab"))
        assert result.status == "PASS"
        assert "the about page" in _artifact_texts(result)["stdout.log"]


class TestCollectionVariables:
    """متغیرهای حساسِ سطحِ کالکشن مثلِ environment و globals محافظت می‌شوند."""

    LONG = "COLL-SECRET-TOKEN-5566aa"

    def _runner(self) -> FakeRunner:
        events = item_events(0, "TC-001", assertions=[("Status code is 200", f"saw {self.LONG}")])
        summary = summary_for(events)
        summary["collection"]["variable"] = [
            {"key": "apiToken", "value": self.LONG},
            {"key": "password", "value": "p9"},
            {"key": "voucherId", "value": "123"},
        ]
        return FakeRunner(
            events=events,
            summary=summary,
            html=f"<html><body>{self.LONG}</body></html>",
            stdout=f"using {self.LONG} and password=p9\n",
            stderr=f"error with {self.LONG}\n",
        )

    def test_collection_variable_secrets_never_reach_artifacts(self, tmp_path):
        result = _run(
            tmp_path,
            self._runner(),
            collection_variables=[
                {"key": "apiToken", "value": self.LONG},
                {"key": "password", "value": "p9"},
            ],
        )
        assert result.status == "FAIL"
        texts = _artifact_texts(result)
        for name, text in texts.items():
            assert self.LONG not in text, name
            assert not _whole_token(text, "p9"), name
        report = json.loads(texts["newman-report.json"])
        variables = {v["key"]: v["value"] for v in report["collection"]["variable"]}
        assert variables == {"apiToken": "***", "password": "***", "voucherId": "123"}
        assert result.requests[0].assertions[0].message == "saw ***"

    def test_host_variable_rules_still_apply_to_collection_variables(self, tmp_path):
        result = _run(
            tmp_path,
            self._runner(),
            collection_variables=[{"key": "base_url_vdi", "value": "http://x"}],
        )
        assert result.error_kind == "HOST_VARIABLE_IN_COLLECTION"


class TestSkippedRequests:
    def test_an_unexplained_skip_is_error_not_pass(self, tmp_path):
        events = PASSING + item_events(1, "TC-002", sent=False)
        result = _run(
            tmp_path,
            FakeRunner(events=events),
            items=[request_item("TC-001"), request_item("TC-002")],
        )
        assert (result.status, result.error_kind) == ("ERROR", "REQUEST_NOT_EXECUTED")
        skipped = result.requests[1]
        assert (skipped.skipped, skipped.skip_kind) == (True, "unexplained")
        assert result.stats.unexplained_skips == 1

    def test_an_explained_and_an_unexplained_skip_are_distinguished(self, tmp_path):
        events = item_events(
            0,
            "TC-001",
            sent=False,
            assertions=[("Required collection variable 'v' is available", "v is not set")],
        ) + item_events(1, "TC-002", sent=False)
        result = _run(
            tmp_path,
            FakeRunner(events=events),
            items=[request_item("TC-001"), request_item("TC-002")],
        )
        assert [r.skip_kind for r in result.requests] == ["failed_precondition", "unexplained"]
        assert result.status == "ERROR"

    def test_a_request_that_never_started_is_error(self, tmp_path):
        result = _run(
            tmp_path,
            FakeRunner(events=PASSING),
            items=[request_item("TC-001"), request_item("TC-002")],
        )
        assert (result.status, result.error_kind) == ("ERROR", "REQUEST_NOT_EXECUTED")
        assert result.stats.items_not_started == 1

    def test_bail_after_a_failure_is_still_fail(self, tmp_path):
        failing = item_events(
            0, "TC-001", status=500, assertions=[("Status code is 200", "got 500")]
        )
        result = _run(
            tmp_path,
            FakeRunner(events=failing),
            items=[request_item("TC-001"), request_item("TC-002")],
            options=NewmanOptions(bail=True),
        )
        assert result.status == "FAIL"
        assert result.stats.items_not_started == 1

    def test_errors_and_failures_keep_their_classification(self, tmp_path):
        for name in ("a", "b", "c"):
            (tmp_path / name).mkdir()
        network = item_events(0, "TC-001", network_error="connect ECONNREFUSED")
        assert _run(tmp_path / "a", FakeRunner(events=network)).error_kind == "NETWORK_ERROR"
        failing = item_events(0, "TC-001", assertions=[("Status code is 200", "got 500")])
        assert _run(tmp_path / "b", FakeRunner(events=failing)).status == "FAIL"
        assert (
            _run(tmp_path / "c", FakeRunner(events=item_events(0, "TC-001"))).no_assertions is True
        )


class TestPrivateDirectory:
    def test_the_private_directory_is_marked_and_restricted(self, tmp_path):
        directory = create_private_dir(tmp_path / "scratch")
        assert (directory / MARKER).is_file()
        assert acl_problems(directory) == []
        assert remove_private_dir(directory) and not directory.exists()

    def test_a_failed_permission_check_refuses_to_run(self, tmp_path, monkeypatch):
        from src.test_execution.newman import scratch

        monkeypatch.setattr(scratch, "acl_problems", lambda path: ["inherited entry for Users"])
        with pytest.raises(PrivateDirectoryError, match="not restricted"):
            create_private_dir(tmp_path / "scratch")
        assert list((tmp_path / "scratch").iterdir()) == []

    def test_the_engine_stops_when_no_private_directory_can_be_made(self, tmp_path, monkeypatch):
        def refuse(root):
            raise PrivateDirectoryError(
                "the private directory is not restricted to the current user: x"
            )

        monkeypatch.setattr(engine_module, "create_private_dir", refuse)
        runner = FakeRunner(events=PASSING)
        result = _run(tmp_path, runner)
        assert (result.status, result.error_kind) == ("ERROR", "PRIVATE_DIRECTORY")
        assert runner.calls == []

    def test_only_stale_marked_engine_directories_are_swept(self, tmp_path):
        root = tmp_path / "scratch"
        root.mkdir()
        old, fresh = create_private_dir(root), create_private_dir(root)
        unmarked = root / "run-unmarked1"
        unmarked.mkdir()
        foreign = root / "someone-else"
        foreign.mkdir()
        (foreign / MARKER).write_text("x", encoding="utf-8")
        (root / "run-file00000").write_text("x", encoding="utf-8")
        past = time.time() - 7 * 3600
        for path in (old, unmarked, foreign):
            os.utime(path, (past, past))

        assert sweep_stale(root) == 1
        assert not old.exists()
        assert fresh.exists() and unmarked.exists() and foreign.exists()
        assert (root / "run-file00000").exists()

    def test_remove_refuses_anything_that_is_not_an_engine_directory(self, tmp_path):
        other = tmp_path / "run-notmine01"
        other.mkdir()
        assert remove_private_dir(other) is False
        assert other.exists()

    def test_each_run_uses_and_removes_its_own_private_directory(self, tmp_path):
        runner = FakeRunner(events=PASSING)
        _run(tmp_path, runner)
        private = Path(runner.calls[0]["config"]["events_path"]).parent
        assert private.parent == tmp_path / "scratch"
        assert not private.exists()
        assert runner.calls[0]["config"]["parent_pid"] == os.getpid()


class TestSanitizationFailureStatistics:
    """پاک‌سازی شکست خورد: فقط result.json، وضعیت ERROR/SANITIZATION_FAILED، و
    شمارش‌ها فقط وقتی رخدادهای ساختاریافته قابلِ اتکا باشند — وگرنه null."""

    @staticmethod
    def _break_html(monkeypatch) -> None:
        real = engine_module.scrub_text
        monkeypatch.setattr(
            engine_module,
            "scrub_text",
            lambda text, secrets: text if "<html" in text else real(text, secrets),
        )

    @staticmethod
    def _leaky_html() -> str:
        return f"<html><body><pre>Authorization: {TOKEN}</pre></body></html>"

    def _assert_failed_closed(self, result) -> dict:
        assert (result.status, result.error_kind) == ("ERROR", "SANITIZATION_FAILED")
        assert result.no_assertions is False and result.warnings == []
        assert result.requests == []
        texts = _artifact_texts(result)
        assert set(texts) == {"result.json"}  # هیچ گزارشِ خامی منتشر نشد
        for secret in ALL_SECRETS:
            assert secret not in texts["result.json"]
        return json.loads(texts["result.json"])["stats"]

    def test_reliable_counts_are_kept_and_no_verdict_is_guessed(self, tmp_path, monkeypatch):
        self._break_html(monkeypatch)
        events = item_events(
            0,
            "TC-001 — Read",
            status=401,
            assertions=[("Status code is 200", "expected 401 to equal 200")],
        ) + item_events(1, "TC-002 — Read", assertions=[("Status code is 200", None)])
        runner = FakeRunner(events=events, html=self._leaky_html())
        result = _run(
            tmp_path, runner, items=[request_item("TC-001 — Read"), request_item("TC-002 — Read")]
        )
        stats = self._assert_failed_closed(result)  # نه FAIL، نه PASS
        assert (stats["requests_executed"], stats["assertions_total"]) == (2, 2)
        assert (stats["assertions_failed"], stats["items_not_started"]) == (1, 0)
        assert result.stats.requests_executed == 2

    def test_all_passing_assertions_still_give_error_not_pass(self, tmp_path, monkeypatch):
        self._break_html(monkeypatch)
        result = _run(tmp_path, FakeRunner(events=PASSING, html=self._leaky_html()))
        stats = self._assert_failed_closed(result)
        assert (stats["requests_executed"], stats["assertions_failed"]) == (1, 0)

    def test_a_report_that_disagrees_with_the_events_gives_unknown_counts(
        self, tmp_path, monkeypatch
    ):
        self._break_html(monkeypatch)
        summary = summary_for(PASSING)
        summary["run"]["stats"]["requests"]["total"] = 5
        result = _run(
            tmp_path, FakeRunner(events=PASSING, summary=summary, html=self._leaky_html())
        )
        stats = self._assert_failed_closed(result)
        assert set(stats.values()) == {None}  # نامعلوم، نه صفر

    @pytest.mark.parametrize(
        "runner_options",
        [
            {"finish": False},  # رخدادها ناقص‌اند
            {"write_events": False},  # فایلِ رخداد نیست
            {"summary": None},  # گزارشِ JSON نیست
        ],
    )
    def test_incomplete_structured_data_gives_unknown_counts(
        self, tmp_path, monkeypatch, runner_options
    ):
        self._break_html(monkeypatch)
        runner = FakeRunner(events=PASSING, html=self._leaky_html(), **runner_options)
        stats = self._assert_failed_closed(_run(tmp_path, runner))
        assert set(stats.values()) == {None}

    def test_a_timeout_with_a_sanitization_failure_has_unknown_counts(self, tmp_path, monkeypatch):
        self._break_html(monkeypatch)
        runner = FakeRunner(
            events=PASSING, timed_out=True, finish=False, summary=None, html=self._leaky_html()
        )
        result = _run(tmp_path, runner, options=NewmanOptions(process_timeout_s=5))
        stats = self._assert_failed_closed(result)
        assert result.timed_out is True and set(stats.values()) == {None}

    def test_a_cancellation_with_a_sanitization_failure_has_unknown_counts(
        self, tmp_path, monkeypatch
    ):
        self._break_html(monkeypatch)
        runner = FakeRunner(events=PASSING, cancelled=True, html=self._leaky_html())
        result = _run(tmp_path, runner)
        stats = self._assert_failed_closed(result)
        assert result.cancelled is True and set(stats.values()) == {None}

    def test_successful_runs_keep_their_normal_counts(self, tmp_path):
        result = _run(tmp_path, FakeRunner(events=PASSING))
        assert result.status == "PASS"
        assert (result.stats.requests_executed, result.stats.assertions_total) == (1, 1)


class TestCorsAndLiteralRegression:
    """PODIUM-3386: «Access-Control-Allow-Credentials: true» دیگر گزارش را خراب نمی‌کند."""

    @staticmethod
    def _execution(headers: list[dict]) -> dict:
        return {
            "item": {"name": "TC-001 — Read"},
            "cursor": {"bof": True, "eof": True, "empty": False, "length": 1},
            "request": {
                "method": "GET",
                "header": [{"key": "Authorization", "value": TOKEN}],
                "url": {"host": ["podium", "test"], "path": ["api", "voucher"]},
            },
            "response": {"code": 200, "responseTime": 1234, "header": headers},
            "assertions": [{"assertion": "Status code is 200", "skipped": False}],
        }

    def test_a_cors_credentials_header_keeps_the_report_valid_and_the_verdict(self, tmp_path):
        execution = self._execution(
            [
                {"key": "Access-Control-Allow-Credentials", "value": "true"},
                {"key": "Access-Control-Allow-Origin", "value": "http://podium.test"},
            ]
        )
        runner = FakeRunner(events=PASSING, summary=summary_for(PASSING, executions=[execution]))
        result = _run(tmp_path, runner)
        assert (result.status, result.error_kind) == ("PASS", "")
        report = json.loads(_artifact_texts(result)["newman-report.json"])
        published = report["run"]["executions"][0]
        assert published["cursor"]["bof"] is True and published["cursor"]["eof"] is True
        assert published["response"]["header"][0] == {
            "key": "Access-Control-Allow-Credentials",
            "value": "true",
        }
        assert published["request"]["header"][0]["value"] == "***"

    def test_a_numeric_sensitive_header_keeps_report_numbers(self, tmp_path):
        execution = self._execution([{"key": "X-Session-Id", "value": "1234"}])
        runner = FakeRunner(events=PASSING, summary=summary_for(PASSING, executions=[execution]))
        result = _run(tmp_path, runner)
        assert result.status == "PASS"
        report = json.loads(_artifact_texts(result)["newman-report.json"])
        published = report["run"]["executions"][0]
        assert published["response"]["responseTime"] == 1234
        assert published["cursor"]["length"] == 1
        assert published["response"]["header"][0]["value"] == "***"


ACCESS_TOKEN = "SYNTHETIC-RUNTIME-ACCESS-9e21"  # ساختگی
EMPTY_TOKEN_ENV = [
    {"key": "base_url", "value": "http://podium.test", "enabled": True},
    {"key": "token", "value": "", "enabled": True},
]
EMPTY_TOKEN_GLOBALS = [{"key": "token", "value": "", "enabled": True}]


def _run_with_token(tmp_path: Path, runner: FakeRunner, token: str | None, cancel_event=None):
    collection, environment, globals_path = write_inputs(
        tmp_path, env_values=EMPTY_TOKEN_ENV, globals_values=EMPTY_TOKEN_GLOBALS
    )
    before = {path: path.read_bytes() for path in (collection, environment, globals_path)}
    request = NewmanRunRequest(
        collection, environment, globals_path, tmp_path / "out", access_token=token
    )
    engine = NewmanExecutionEngine(
        process_runner=runner, installation=INSTALLATION, scratch_root=tmp_path / "scratch"
    )
    result = engine.run(request, cancel_event) if cancel_event else engine.run(request)
    for path, content in before.items():
        assert path.read_bytes() == content  # فایل‌های ورودی بایت‌به‌بایت دست‌نخورده
    return result


def _all_output_text(tmp_path: Path) -> str:
    return "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (tmp_path / "out").rglob("*")
        if path.is_file()
    )


class TestRuntimeAccessToken:
    def test_the_token_reaches_newman_only_through_the_process_environment(self, tmp_path):
        runner = FakeRunner(events=PASSING)
        result = _run_with_token(tmp_path, runner, ACCESS_TOKEN)
        assert result.status == "PASS"
        [call] = runner.calls
        runtime = json.loads(call["kwargs"]["extra_env"][engine_module.RUNTIME_ENV_VARS])
        assert runtime == [{"key": "token", "value": f"Bearer {ACCESS_TOKEN}"}]
        assert ACCESS_TOKEN not in json.dumps(call["config"])  # نه در config.json
        assert ACCESS_TOKEN not in " ".join(call["argv"] + result.command)
        assert ACCESS_TOKEN not in repr(
            NewmanRunRequest(Path("c"), Path("e"), Path("g"), Path("o"), access_token=ACCESS_TOKEN)
        )

    def test_the_bearer_prefix_is_added_exactly_once(self, tmp_path):
        runner = FakeRunner(events=PASSING)
        _run_with_token(tmp_path, runner, ACCESS_TOKEN)
        value = json.loads(runner.calls[0]["kwargs"]["extra_env"][engine_module.RUNTIME_ENV_VARS])[
            0
        ]["value"]
        assert value.count("Bearer") == 1 and value == "Bearer " + ACCESS_TOKEN

    def test_without_a_token_nothing_is_injected(self, tmp_path):
        runner = FakeRunner(events=PASSING)
        _run_with_token(tmp_path, runner, None)
        assert "extra_env" not in runner.calls[0]["kwargs"]

    @pytest.mark.parametrize("token", ["", "   ", "has space", "tab\there", "new\nline"])
    def test_an_empty_or_invalid_token_never_starts_newman(self, tmp_path, token):
        runner = FakeRunner(events=PASSING)
        result = _run_with_token(tmp_path, runner, token)
        assert (result.status, result.error_kind) == ("ERROR", "ACCESS_TOKEN_INVALID")
        assert runner.calls == []
        assert not (tmp_path / "out").exists()

    def test_the_token_is_scrubbed_from_every_output(self, tmp_path):
        bearer = f"Bearer {ACCESS_TOKEN}"
        runner = FakeRunner(
            events=PASSING,
            stdout=f"Authorization: {bearer}\n",
            stderr=f"token={ACCESS_TOKEN}\n",
            html=f"<html><body><p>{bearer}</p><p>{ACCESS_TOKEN}</p></body></html>",
            side_effect=lambda config: Path(config["json_report"]).write_text(
                json.dumps(
                    {
                        **summary_for(PASSING),
                        "environment": {"values": [{"key": "token", "value": bearer}]},
                        "note": f"echo {ACCESS_TOKEN}",
                    }
                ),
                encoding="utf-8",
            ),
        )
        result = _run_with_token(tmp_path, runner, ACCESS_TOKEN)
        assert result.status == "PASS"
        assert result.artifacts.json_report and result.artifacts.html_report
        text = _all_output_text(tmp_path)
        assert ACCESS_TOKEN not in text
        assert list((tmp_path / "scratch").iterdir()) == []

    def test_timeout_keeps_the_token_out_and_cleans_the_scratch_directory(self, tmp_path):
        runner = FakeRunner(
            events=PASSING, timed_out=True, finish=False, stdout=f"Bearer {ACCESS_TOKEN}"
        )
        result = _run_with_token(tmp_path, runner, ACCESS_TOKEN)
        assert result.timed_out and result.error_kind == "PROCESS_TIMEOUT"
        assert ACCESS_TOKEN not in _all_output_text(tmp_path)
        assert list((tmp_path / "scratch").iterdir()) == []

    def test_cancellation_before_start_never_runs_newman_and_cleans_up(self, tmp_path):
        event = threading.Event()
        event.set()
        runner = FakeRunner(events=PASSING)
        result = _run_with_token(tmp_path, runner, ACCESS_TOKEN, cancel_event=event)
        assert (result.cancelled, result.error_kind) == (True, "PROCESS_CANCELLED")
        assert runner.calls == []
        assert ACCESS_TOKEN not in _all_output_text(tmp_path)
        assert list((tmp_path / "scratch").iterdir()) == []

    def test_cancellation_during_the_run_keeps_the_token_out(self, tmp_path):
        runner = FakeRunner(
            events=PASSING, cancelled=True, finish=False, stderr=f"Bearer {ACCESS_TOKEN}"
        )
        result = _run_with_token(tmp_path, runner, ACCESS_TOKEN, cancel_event=threading.Event())
        assert result.cancelled
        assert ACCESS_TOKEN not in _all_output_text(tmp_path)
        assert list((tmp_path / "scratch").iterdir()) == []


def test_the_process_runner_passes_extra_env_to_the_child_only(monkeypatch):
    from src.test_execution.newman import process as process_module

    monkeypatch.setenv("TESTAGENT_SHOULD_NOT_LEAK", "x")
    env = process_module.child_environment({engine_module.RUNTIME_ENV_VARS: "[]"})
    assert env[engine_module.RUNTIME_ENV_VARS] == "[]"
    assert "TESTAGENT_SHOULD_NOT_LEAK" not in env
    assert engine_module.RUNTIME_ENV_VARS not in os.environ
