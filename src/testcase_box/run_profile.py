"""
src/testcase_box/run_profile.py — پروفایلِ اجرای run (Execution Profile، M7.4)

    profile = RunProfile(host_intent=HostIntent.NORMAL)          # پیش‌فرض‌های تأییدشده
    profile = RunProfile.from_dict({"schema": 1, "host_intent": "VDI", "request_timeout_ms": 10_000})
    profile.to_json()          # JSONِ قطعی (کلیدهای مرتب) — snapshotِ ذخیره‌شدنی
    profile.fingerprint        # SHA-256 ِ همان JSON

نام عمداً RunProfile است: profiles.ExecutionProfile (M4) فقط نامِ متغیرهای حل‌شده را دارد و
این پروفایل پارامترهای مجازِ اجرای Newman را. این دو جدا و مکمل‌اند.

فقط پارامترهای فهرست‌شده پذیرفته می‌شوند — هیچ گزینه‌ی دلبخواهِ Newman، آرگومانِ خطِ فرمان،
مسیرِ اجرایی یا اسکریپتی. پیش‌فرض‌ها همان مقادیرِ Batch (D10) و سقف‌اند، نه کف:

  * request_timeout_ms  پیش‌فرض و سقف ۳۰٬۰۰۰ (کف ۱٬۰۰۰)
  * process_timeout_s   پیش‌فرض و سقف ۹۰۰ = ۱۵ دقیقه (کف ۶۰)
  * bail               همیشه false — true رد می‌شود
  * insecure            همیشه false (TLS همیشه تأیید می‌شود) — true رد می‌شود
  * host_intent         NORMAL یا VDI، صریح و بی‌پیش‌فرض (هرگز حدس زده نمی‌شود — M4)

schema (نسخه‌ی قالب) صریح است: from_dict قالبِ ناشناخته یا بی‌نسخه را رد می‌کند، پس snapshotِ
ذخیره‌شده با تغییرِ قالب بی‌صدا معنای دیگری پیدا نمی‌کند. پروفایل هیچ توکن، مقدارِ متغیر یا
اعتبارنامه‌ای ندارد و پیام‌های خطا هرگز مقدارِ ورودی را تکرار نمی‌کنند.

پیش‌فرض‌ها از src.test_execution.newman.models کپی شده‌اند (بسته‌ی Box هیچ import ای از موتور
ندارد — همان قراردادِ contract.py و test_box_modules_import_only_stdlib_and_box)؛ تست‌ها
هم‌خوانی را بررسی می‌کنند. newman_option_values() فقط آرگومان‌های NewmanOptions را می‌دهد.

delay_request_ms ِ موتور عمداً جزوِ پروفایل نیست (جزوِ گزینه‌های تأییدشده‌ی D10 نیست و با یک
درخواست در هر unit هیچ کاربردی ندارد) — پیش‌فرضِ ۰ ِ خودِ NewmanOptions به کار می‌رود.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.testcase_box.contract import safe_name
from src.testcase_box.models import sha256_json
from src.testcase_box.profiles import HostIntent

PROFILE_SCHEMA_VERSION = 1

# هم‌خوان با newman.models.DEFAULT_REQUEST_TIMEOUT_MS / DEFAULT_PROCESS_TIMEOUT_S (تست)
DEFAULT_REQUEST_TIMEOUT_MS = 30_000
DEFAULT_PROCESS_TIMEOUT_S = 15 * 60

REQUEST_TIMEOUT_RANGE_MS = (1_000, DEFAULT_REQUEST_TIMEOUT_MS)
PROCESS_TIMEOUT_RANGE_S = (60, DEFAULT_PROCESS_TIMEOUT_S)

_FIELDS = (
    "schema",
    "host_intent",
    "request_timeout_ms",
    "process_timeout_s",
    "bail",
    "insecure",
)


class RunProfileError(ValueError):
    """پروفایلِ نامعتبر — پیام فقط نامِ فیلد را دارد، هرگز مقدار را."""


def _integer(name: str, value: Any, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RunProfileError(f"{name} must be an integer.")
    low, high = bounds
    if not low <= value <= high:
        raise RunProfileError(f"{name} must be between {low} and {high}.")
    return value


@dataclass(frozen=True)
class RunProfile:
    host_intent: HostIntent
    request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS
    process_timeout_s: int = DEFAULT_PROCESS_TIMEOUT_S
    bail: bool = False
    insecure: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.host_intent, str) or self.host_intent not in tuple(HostIntent):
            raise RunProfileError("host_intent must be NORMAL or VDI.")
        object.__setattr__(self, "host_intent", HostIntent(self.host_intent))
        _integer("request_timeout_ms", self.request_timeout_ms, REQUEST_TIMEOUT_RANGE_MS)
        _integer("process_timeout_s", self.process_timeout_s, PROCESS_TIMEOUT_RANGE_S)
        if self.bail is not False:
            raise RunProfileError("bail cannot be enabled; every case runs to completion.")
        if self.insecure is not False:
            raise RunProfileError("TLS verification cannot be disabled (insecure must be false).")

    @classmethod
    def from_dict(cls, data: Any) -> RunProfile:
        if not isinstance(data, dict):
            raise RunProfileError("The profile must be a JSON object.")
        unknown = sorted(key for key in data if key not in _FIELDS)
        if unknown:
            names = ", ".join(safe_name(key) for key in unknown)
            raise RunProfileError(f"Unsupported profile field(s): {names}.")
        if data.get("schema") != PROFILE_SCHEMA_VERSION or isinstance(data.get("schema"), bool):
            raise RunProfileError(
                f"The profile schema must be {PROFILE_SCHEMA_VERSION}; other versions are not "
                "converted automatically."
            )
        if "host_intent" not in data:
            raise RunProfileError("host_intent is required.")
        values = {key: value for key, value in data.items() if key != "schema"}
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROFILE_SCHEMA_VERSION,
            "host_intent": self.host_intent.value,
            "request_timeout_ms": self.request_timeout_ms,
            "process_timeout_s": self.process_timeout_s,
            "bail": False,
            "insecure": False,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def fingerprint(self) -> str:
        return sha256_json(self.to_dict())

    def newman_option_values(self) -> dict[str, Any]:
        """آرگومان‌های NewmanOptions ِ موتورِ موجود (همان نام‌ها) — runnerِ M7.5 با
        NewmanOptions(**values) می‌سازد. بسته‌ی Box به موتور import ندارد (مرزِ test_contract).
        delay_request_ms عمداً نیست: پیش‌فرضِ ۰ ِ خودِ NewmanOptions به کار می‌رود."""
        return {
            "request_timeout_ms": self.request_timeout_ms,
            "process_timeout_s": float(self.process_timeout_s),
            "bail": False,
            "insecure": False,
        }


__all__ = [
    "DEFAULT_PROCESS_TIMEOUT_S",
    "DEFAULT_REQUEST_TIMEOUT_MS",
    "PROFILE_SCHEMA_VERSION",
    "RunProfile",
    "RunProfileError",
]
