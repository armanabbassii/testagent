"""
تست‌های واحدِ redaction.py — پاک‌سازیِ ساختاریِ گزارشِ JSON و تشخیصِ هدرِ حساس.

همه‌ی مقدارها ساختگی‌اند؛ هیچ Newman، Node یا شبکه‌ای لازم نیست.
"""

from __future__ import annotations

import json

import pytest

from src.test_execution.newman.redaction import (
    CORS_HEADERS,
    MASK,
    MIN_SECRET_LENGTH,
    SanitizationError,
    is_sensitive_header,
    sanitize_summary,
    summary_secrets,
    verify_clean_json,
)

SECRET = "SYNTHETIC-SECRET-VALUE-91ab"
QUERY_SECRET = "SYNTHETIC-QUERY-KEY-77cd"
COOKIE = "SYNTHETIC-COOKIE-55ef"


def _summary(*, response_headers=(), request_headers=(), cursor=None, extra=None) -> dict:
    execution = {
        "item": {"name": "TC-001"},
        "cursor": cursor if cursor is not None else {"bof": True, "eof": True, "empty": False},
        "request": {
            "method": "GET",
            "header": [dict(item) for item in request_headers],
            "url": {"raw": "http://podium.test/api/voucher", "host": ["podium", "test"]},
        },
        "response": {
            "code": 200,
            "responseTime": 1234,
            "header": [dict(item) for item in response_headers],
        },
        "assertions": [{"assertion": "Status code is 200", "skipped": False}],
    }
    summary = {
        "collection": {"info": {"name": "c"}, "item": [], "variable": []},
        "environment": {"values": []},
        "globals": {"values": []},
        "run": {
            "stats": {"requests": {"total": 1, "failed": 0}, "assertions": {"total": 1}},
            "timings": {"started": 1730000000000, "responseAverage": 1234},
            "executions": [execution],
            "failures": [],
            "error": None,
        },
    }
    summary.update(extra or {})
    return summary


def _sanitize(summary: dict) -> dict:
    secrets = summary_secrets(summary)
    cleaned = sanitize_summary(summary, secrets)
    verify_clean_json(cleaned, secrets, "the JSON report")
    json.loads(json.dumps(cleaned))  # همیشه JSONِ معتبر
    return cleaned


def _execution(report: dict) -> dict:
    return report["run"]["executions"][0]


class TestCorsHeaders:
    def test_allow_credentials_true_keeps_every_json_true(self):
        summary = _summary(
            response_headers=[{"key": "Access-Control-Allow-Credentials", "value": "true"}]
        )
        assert summary_secrets(summary) == set()
        report = _sanitize(summary)
        execution = _execution(report)
        assert execution["cursor"] == {"bof": True, "eof": True, "empty": False}
        assert execution["response"]["header"] == [
            {"key": "Access-Control-Allow-Credentials", "value": "true"}
        ]
        assert execution["assertions"][0]["skipped"] is False

    @pytest.mark.parametrize("name", sorted(CORS_HEADERS))
    def test_standard_cors_headers_are_not_sensitive(self, name):
        assert not is_sensitive_header(name)
        assert not is_sensitive_header(name.title())
        assert not is_sensitive_header(f"  {name.upper()} ")

    @pytest.mark.parametrize(
        "name",
        [
            "Authorization",
            "Proxy-Authorization",
            "Cookie",
            "Set-Cookie",
            "X-Api-Key",
            "X-Auth-Token",
            "X-Session-Id",
            "X-CSRF-Token",
            "Access-Token",
            "X-Access-Control-Token",  # فقط نام‌های دقیقِ CORS مستثنا‌اند
            "Access-Control-Allow-Credentials-Token",
        ],
    )
    def test_other_sensitive_headers_stay_protected(self, name):
        assert is_sensitive_header(name)


class TestStructuralRedaction:
    def test_a_numeric_secret_equal_to_a_report_number_keeps_the_number(self):
        summary = _summary(response_headers=[{"key": "X-Session-Id", "value": "1234"}])
        assert summary_secrets(summary) == {"1234"}
        report = _sanitize(summary)
        execution = _execution(report)
        assert execution["response"]["responseTime"] == 1234  # عدد، نه رشته
        assert report["run"]["timings"]["responseAverage"] == 1234
        assert execution["response"]["header"][0]["value"] == MASK

    def test_json_literals_are_never_masked_even_when_they_are_secrets(self):
        summary = _summary(
            extra={"flags": {"ok": True, "off": False, "none": None, "n": 1234, "f": 1.5}}
        )
        cleaned = sanitize_summary(summary, {"true", "false", "null", "1234", "1.5"})
        assert cleaned["flags"] == {"ok": True, "off": False, "none": None, "n": 1234, "f": 1.5}
        assert _execution(cleaned)["cursor"]["bof"] is True

    def test_a_secret_in_nested_strings_and_keys_is_masked(self):
        summary = _summary(
            extra={
                "deep": {
                    "level": [
                        {"message": f"failed with {SECRET} inside", "list": [f"x{SECRET}y"]},
                        {SECRET: "value keyed by the secret"},
                    ]
                }
            }
        )
        cleaned = sanitize_summary(summary, {SECRET})
        level = cleaned["deep"]["level"]
        assert level[0] == {"message": f"failed with {MASK} inside", "list": [f"x{MASK}y"]}
        assert level[1] == {MASK: "value keyed by the secret"}
        assert SECRET not in json.dumps(cleaned)

    def test_encoded_forms_inside_strings_are_masked(self):
        secret = "SYNTHETIC/VALUE&with=chars"
        summary = _summary(
            extra={
                "texts": [
                    "SYNTHETIC%2FVALUE%26with%3Dchars",
                    "SYNTHETIC/VALUE&amp;with=chars",
                    "SYNTHETIC/VALUE&amp;with&#x3D;chars",
                ]
            }
        )
        cleaned = sanitize_summary(summary, {secret})
        assert cleaned["texts"] == [MASK, MASK, MASK]

    def test_keys_that_collide_after_masking_fail_closed(self):
        summary = _summary(extra={"keys": {f"a{SECRET}": 1, f"a{SECRET[::-1]}": 2}})
        with pytest.raises(SanitizationError, match="identical after redaction"):
            sanitize_summary(summary, {SECRET, SECRET[::-1]})

    def test_the_input_report_is_not_modified(self):
        summary = _summary(extra={"message": f"has {SECRET}"})
        before = json.dumps(summary, sort_keys=True)
        sanitize_summary(summary, {SECRET})
        assert json.dumps(summary, sort_keys=True) == before


class TestSecretLocations:
    def test_header_body_url_and_query_secrets_are_removed(self):
        summary = _summary(
            request_headers=[
                {"key": "Authorization", "value": f"Bearer {SECRET}"},
                {"key": "Cookie", "value": f"SESSION={COOKIE}"},
                {"key": "Accept", "value": "application/json"},
            ],
            response_headers=[{"key": "Set-Cookie", "value": f"SESSION={COOKIE}; Path=/"}],
        )
        execution = summary["run"]["executions"][0]
        execution["request"]["url"] = {
            "raw": f"http://podium.test/api/voucher?api_key={QUERY_SECRET}",
            "host": ["podium", "test"],
            "query": [{"key": "api_key", "value": QUERY_SECRET}],
        }
        execution["request"]["body"] = {"mode": "raw", "raw": json.dumps({"password": SECRET})}
        execution["response"]["stream"] = {"type": "Buffer", "data": [1, 2, 3]}
        summary["run"]["failures"] = [{"error": {"message": f"expected {SECRET} to be x"}}]

        report = _sanitize(summary)
        text = json.dumps(report)
        for secret in (SECRET, COOKIE, QUERY_SECRET):
            assert secret not in text
        execution = _execution(report)
        headers = {h["key"]: h["value"] for h in execution["request"]["header"]}
        assert headers == {"Authorization": MASK, "Cookie": MASK, "Accept": "application/json"}
        assert execution["request"]["body"] == {"redacted": True}
        assert execution["response"]["stream"] == {"redacted": True}
        assert execution["request"]["url"]["query"][0]["value"] == MASK
        assert execution["request"]["url"]["raw"].endswith(f"api_key={MASK}")
        assert report["run"]["failures"][0]["error"]["message"] == f"expected {MASK} to be x"

    @pytest.mark.parametrize("short", ["7", "ab", "x9z"])
    def test_a_short_secret_is_not_replaced_but_verification_fails_closed(self, short):
        assert len(short) < MIN_SECRET_LENGTH
        summary = _summary(extra={"note": f"value {short} remains"})
        cleaned = sanitize_summary(summary, {short})
        assert cleaned["note"] == f"value {short} remains"  # جایگزینیِ متنی نه
        with pytest.raises(SanitizationError, match="still present"):
            verify_clean_json(cleaned, {short}, "the JSON report")

    def test_a_short_secret_in_a_sensitive_header_is_masked_structurally(self):
        summary = _summary(request_headers=[{"key": "X-Auth-Token", "value": "ab"}])
        report = _sanitize(summary)
        assert _execution(report)["request"]["header"][0]["value"] == MASK


class TestReportShapes:
    @pytest.mark.parametrize(
        "summary",
        [
            {},
            {"run": None},
            {"run": {"executions": None}},
            {"run": {"executions": ["not-a-dict", 7, None]}},
            {"environment": None, "globals": "x", "collection": []},
            {"collection": {"variable": [{"key": "apiToken", "value": SECRET}, "junk"]}},
            {"run": {"executions": [{"request": "raw", "response": None}]}},
            {"unknown": [True, False, None, 0, -1, 2.5, "", {"nested": [None, True]}]},
        ],
    )
    def test_any_report_shape_stays_valid_json_with_types_preserved(self, summary):
        secrets = summary_secrets(summary) | {SECRET}
        cleaned = sanitize_summary(summary, secrets)
        assert json.loads(json.dumps(cleaned)) == cleaned
        verify_clean_json(cleaned, secrets, "the JSON report")
        assert SECRET not in json.dumps(cleaned)
        if "unknown" in summary:
            assert cleaned["unknown"] == summary["unknown"]
