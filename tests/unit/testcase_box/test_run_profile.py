"""
tests/unit/testcase_box/test_run_profile.py — پروفایلِ اجرای run (M7.4) — بی‌شبکه، بی‌اجرا
"""

from __future__ import annotations

import pytest

from src.test_execution.newman import models as newman_models
from src.testcase_box import run_profile as rp
from src.testcase_box.profiles import HostIntent
from src.testcase_box.run_profile import RunProfile, RunProfileError

SECRET = "s3cr3t-token-value-never-shown"


def test_the_default_profile_uses_the_approved_newman_defaults():
    profile = RunProfile(HostIntent.NORMAL)
    assert profile.to_dict() == {
        "schema": 1,
        "host_intent": "NORMAL",
        "request_timeout_ms": 30_000,
        "process_timeout_s": 900,
        "bail": False,
        "insecure": False,
    }


def test_the_copied_defaults_match_the_newman_engine():
    assert rp.DEFAULT_REQUEST_TIMEOUT_MS == newman_models.DEFAULT_REQUEST_TIMEOUT_MS
    assert rp.DEFAULT_PROCESS_TIMEOUT_S == newman_models.DEFAULT_PROCESS_TIMEOUT_S
    engine_defaults = newman_models.NewmanOptions()
    assert (engine_defaults.bail, engine_defaults.insecure) == (False, False)


def test_valid_custom_values_are_accepted():
    profile = RunProfile.from_dict(
        {
            "schema": 1,
            "host_intent": "VDI",
            "request_timeout_ms": 5_000,
            "process_timeout_s": 120,
        }
    )
    assert profile.host_intent is HostIntent.VDI
    assert (profile.request_timeout_ms, profile.process_timeout_s) == (5_000, 120)
    assert RunProfile.from_dict(profile.to_dict()) == profile


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_timeout_ms", 0),
        ("request_timeout_ms", 999),
        ("request_timeout_ms", 30_001),
        ("request_timeout_ms", 1.5),
        ("request_timeout_ms", "30000"),
        ("request_timeout_ms", True),
        ("process_timeout_s", 59),
        ("process_timeout_s", 901),
        ("process_timeout_s", None),
    ],
)
def test_invalid_timeouts_are_rejected(field, value):
    with pytest.raises(RunProfileError, match=field):
        RunProfile.from_dict({"schema": 1, "host_intent": "NORMAL", field: value})


def test_tls_verification_cannot_be_disabled():
    for value in (True, 1, "true"):
        with pytest.raises(RunProfileError, match="TLS"):
            RunProfile(HostIntent.NORMAL, insecure=value)
    with pytest.raises(RunProfileError, match="TLS"):
        RunProfile.from_dict({"schema": 1, "host_intent": "NORMAL", "insecure": True})


def test_bail_cannot_be_enabled():
    with pytest.raises(RunProfileError, match="bail"):
        RunProfile.from_dict({"schema": 1, "host_intent": "NORMAL", "bail": True})


@pytest.mark.parametrize(
    "extra",
    [
        {"newman_args": ["--insecure"]},
        {"delay_request_ms": 0},  # گزینه‌ی موتور، ولی جزوِ پروفایلِ تأییدشده نیست
        {"delay_request_ms": 250},
        {"executable": "C:/evil/node.exe"},
        {"script": "pm.sendRequest()"},
        {"token": SECRET},
        {"environment": {"token": SECRET}},
    ],
)
def test_unsupported_fields_are_rejected_without_echoing_values(extra):
    with pytest.raises(RunProfileError, match="Unsupported profile field") as caught:
        RunProfile.from_dict({"schema": 1, "host_intent": "NORMAL", **extra})
    assert SECRET not in str(caught.value) and "evil" not in str(caught.value)


@pytest.mark.parametrize(
    "data",
    [
        {"host_intent": "NORMAL"},
        {"schema": 2, "host_intent": "NORMAL"},
        {"schema": True, "host_intent": "NORMAL"},
        {"schema": 1},
        {"schema": 1, "host_intent": "normal"},
        {"schema": 1, "host_intent": "VDI-2"},
        ["not", "a", "dict"],
    ],
)
def test_schema_and_host_intent_are_explicit(data):
    with pytest.raises(RunProfileError):
        RunProfile.from_dict(data)


def test_serialization_is_stable_and_contains_no_secrets():
    first = RunProfile.from_dict({"schema": 1, "request_timeout_ms": 10_000, "host_intent": "VDI"})
    second = RunProfile.from_dict({"host_intent": "VDI", "schema": 1, "request_timeout_ms": 10_000})
    assert first.to_json() == second.to_json()
    assert first.fingerprint == second.fingerprint
    assert first.to_json() == (
        '{"bail":false,"host_intent":"VDI","insecure":false,'
        '"process_timeout_s":900,"request_timeout_ms":10000,"schema":1}'
    )
    assert first.fingerprint != RunProfile(HostIntent.VDI).fingerprint
    for text in (first.to_json(), repr(first)):
        assert "token" not in text.lower() and "password" not in text.lower()


def test_the_option_values_build_the_engine_options_without_running_anything():
    values = RunProfile(HostIntent.NORMAL, request_timeout_ms=7_000).newman_option_values()
    options = newman_models.NewmanOptions(**values)  # همان نام‌های فیلد — هیچ کلیدِ اضافه
    engine_fields = set(newman_models.NewmanOptions.__dataclass_fields__)
    assert set(values) == engine_fields - {"delay_request_ms"}
    assert (options.request_timeout_ms, options.process_timeout_s) == (7_000, 900.0)
    assert (options.bail, options.insecure) == (False, False)
    # Box به پیش‌فرضِ خودِ موتور تکیه می‌کند — هرگز تأخیری تنظیم نمی‌کند
    assert options.delay_request_ms == newman_models.NewmanOptions().delay_request_ms == 0


def test_the_profile_has_no_delay_field():
    assert "delay_request_ms" not in RunProfile.__dataclass_fields__
    assert "delay_request_ms" not in RunProfile(HostIntent.NORMAL).to_dict()
    with pytest.raises(TypeError):
        RunProfile(HostIntent.NORMAL, delay_request_ms=0)
