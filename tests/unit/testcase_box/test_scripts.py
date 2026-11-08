"""
تست‌های بازبینیِ ایستای اسکریپت (inspect_scripts) — هیچ اسکریپتی اجرا نمی‌شود.
"""

from __future__ import annotations

import pytest

from src.testcase_box.models import script_fingerprint
from src.testcase_box.scripts import inspect_scripts, script_texts

SECRET_SCRIPT = "const leaked = 's3cr3t-script-body';"


def _payload(*events) -> dict:
    return {"request": {"method": "GET"}, "event": list(events)}


def _event(script, listen: str = "test") -> dict:
    return {"listen": listen, "script": script}


def _report(payload, stored="match"):
    stored_fingerprint = script_fingerprint(payload) if stored == "match" else stored
    return inspect_scripts(payload, stored_fingerprint)


def _codes(payload, stored="match") -> set[str]:
    return {item.code for item in _report(payload, stored).findings}


class TestShapes:
    @pytest.mark.parametrize(
        "script",
        [
            "pm.test('ok', () => {});",
            ["pm.test('ok', () => {});"],
            {"exec": ["pm.test('ok', () => {});"], "type": "text/javascript"},
            {"exec": "pm.test('ok', () => {});", "id": "1", "name": "n"},
            {"exec": [""], "type": "text/javascript", "packages": {}},
        ],
        ids=["string", "array", "object", "object-exec-string", "empty-export"],
    )
    def test_supported_shapes(self, script):
        assert _codes(_payload(_event(script))) == set()

    def test_single_object_event(self):
        payload = {"request": {}, "event": _event("x();")}
        report = _report(payload)
        assert report.findings == ()
        assert report.computed_fingerprint is not None

    @pytest.mark.parametrize(
        ("event", "code"),
        [
            (7, "SCRIPT_MALFORMED"),
            (_event(5), "SCRIPT_MALFORMED"),
            (_event(["ok", 5]), "SCRIPT_MALFORMED"),
            (_event({"exec": 5}), "SCRIPT_MALFORMED"),
            (_event({"exec": ["x"], "future": "y"}), "SCRIPT_MALFORMED"),
            (_event({"src": "https://example.test/s.js"}), "SCRIPT_EXTERNAL_SOURCE"),
            (
                _event({"exec": ["x"], "packages": {"lib": {"id": "1"}}}),
                "SCRIPT_PACKAGES_UNSUPPORTED",
            ),
            (_event({"exec": ["x"], "packages": "lib"}), "SCRIPT_PACKAGES_UNSUPPORTED"),
            (_event("x();", listen="beforeQuery"), "SCRIPT_EVENT_UNSUPPORTED"),
            ({"script": "x();"}, "SCRIPT_EVENT_UNSUPPORTED"),
        ],
    )
    def test_blocking_shapes(self, event, code):
        report = _report(_payload(event))
        assert code in {item.code for item in report.findings}
        assert code in report.hard_rejections

    def test_nested_events_are_inspected(self):
        payload = {
            "request": {},
            "profile": {"folders": [{"event": [_event("pm.sendRequest('x')")]}]},
        }
        report = _report(payload)
        assert [(f.code, f.location) for f in report.findings] == [
            ("SCRIPT_SEND_REQUEST", "$.profile.folders[0].event[0]")
        ]


class TestHardRejection:
    @pytest.mark.parametrize(
        "text",
        [
            "pm.sendRequest('https://x', cb);",
            "pm .sendRequest({url: 'x'});",
            'pm["sendRequest"]("x");',
            "const send = pm.sendRequest; send('x');",
            "/* sendRequest */",
        ],
    )
    def test_send_request_forms(self, text):
        report = _report(_payload(_event(text, listen="prerequest")))
        assert "SCRIPT_SEND_REQUEST" in {f.code for f in report.findings}
        assert "SCRIPT_SEND_REQUEST" in report.hard_rejections

    def test_obfuscated_call_is_a_known_limit(self):
        """بررسیِ متنی است؛ این شکل دیده نمی‌شود — به همین دلیل تأیید ≠ امنیت."""
        report = _report(_payload(_event("pm['send' + 'Request']('x');")))
        assert "SCRIPT_SEND_REQUEST" not in {f.code for f in report.findings}

    def test_names_containing_send_request_are_not_matched(self):
        assert _codes(_payload(_event("mySendRequestHelper();"))) == set()


class TestFingerprint:
    def test_matching_fingerprint(self):
        payload = _payload(_event("x();"))
        assert "SCRIPT_FINGERPRINT_MISMATCH" not in _codes(payload)

    @pytest.mark.parametrize("stored", [None, "0" * 64])
    def test_mismatch_is_blocking(self, stored):
        report = _report(_payload(_event("x();")), stored=stored)
        assert "SCRIPT_FINGERPRINT_MISMATCH" in report.hard_rejections

    def test_stored_fingerprint_without_scripts_is_a_mismatch(self):
        assert _codes({"request": {}}, stored="a" * 64) == {"SCRIPT_FINGERPRINT_MISMATCH"}
        assert _codes({"request": {}}, stored=None) == set()


class TestVariablesAndAccess:
    def test_literal_reads_and_writes(self):
        pre = _event(
            "pm.environment.set('a', 1); pm.collectionVariables.get(\"b\"); pm.variables.has(`c`);",
            listen="prerequest",
        )
        test = _event("pm.globals.unset('d'); pm.iterationData.get('e');")
        report = _report(_payload(pre, test))
        assert sorted((v.name, v.listen) for v in report.writes) == [
            ("a", "prerequest"),
            ("d", "test"),
        ]
        assert sorted((v.name, v.listen) for v in report.reads) == [
            ("b", "prerequest"),
            ("c", "prerequest"),
            ("e", "test"),
        ]
        assert report.findings == ()

    def test_computed_names_are_warnings(self):
        report = _report(_payload(_event("const k = 'x'; pm.environment.get(k);")))
        assert [(f.code, f.severity) for f in report.findings] == [
            ("SCRIPT_DYNAMIC_VARIABLE", "WARNING")
        ]
        assert report.hard_rejections == ()

    @pytest.mark.parametrize(
        "text",
        [
            "pm.environment.get('token')",
            'pm.variables.get("Authorization")',
            "pm.request.headers.get('x')",
        ],
    )
    def test_token_access_is_flagged(self, text):
        assert "SCRIPT_TOKEN_ACCESS" in _codes(_payload(_event(text)))


def test_script_texts_returns_only_trusted_texts_in_order():
    payload = _payload(
        _event(["a();", "b();"], listen="prerequest"),
        _event(5),
        _event({"exec": "c();", "type": "text/javascript"}),
        {"script": "d();"},
    )
    assert [(s.location, s.listen, s.text) for s in script_texts(payload)] == [
        ("$.event[0]", "prerequest", "a();\nb();"),
        ("$.event[2]", "test", "c();"),
        ("$.event[3]", "None", "d();"),
    ]
    assert script_texts("not a payload") == ()


def test_script_text_never_appears_in_findings():
    payload = _payload(_event(SECRET_SCRIPT + " pm.sendRequest('x'); pm.environment.get(k);"))
    for item in _report(payload, stored=None).findings:
        assert "s3cr3t" not in item.message and "s3cr3t" not in item.location
