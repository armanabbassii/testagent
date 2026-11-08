"""
src/testcase_box/scripts.py — بازبینیِ ایستای اسکریپت‌های payload (تابعِ خالص)

    report = inspect_scripts(version.payload, version.script_fingerprint)

اسکریپت‌ها هرگز اجرا نمی‌شوند. این ماژول فقط متن و شکلِ آن‌ها را می‌خواند:
  * شکل‌های نامعتبر، eventِ ناشناخته، src و packages ِ غیرِ خالی → خطای مسدودکننده؛
  * هر ارجاعِ مستقیم به sendRequest (pm.sendRequest، pm["sendRequest"]، …) در V1 قطعاً
    رد می‌شود و تأیید هم آن را پاک نمی‌کند؛
  * اثرانگشتِ ذخیره‌شده باید با اثرانگشتِ بازمحاسبه‌شده بخواند (خطای یکپارچگی)؛
  * خواندن/نوشتنِ متغیر با نامِ literal استخراج می‌شود؛ نامِ محاسبه‌شده فقط هشدار است.

محدودیت: بررسیِ متنی است. فراخوانیِ پنهان یا محاسبه‌شده (مثلاً pm['send' + 'Request'] یا
eval) ممکن است دیده نشود — نه نبودنِ یافته و نه تأییدِ بازبین، اجرا را امن نمی‌کند.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.testcase_box.contract import (
    SCRIPT_EVENTS,
    SEVERITY_ERROR,
    Finding,
    finding,
)
from src.testcase_box.models import payload_scripts, script_fingerprint

_SEND_REQUEST_RE = re.compile(r"\bsendRequest\b")
_TOKEN_ACCESS_RE = re.compile(
    r"""(['"`])(?:token|authorization)\1|\bpm\s*\.\s*request\s*\.\s*headers\b""", re.IGNORECASE
)
_SCOPE = r"(?:environment|globals|collectionVariables|variables|iterationData)"
_CALL_RE = re.compile(r"\bpm\s*\.\s*" + _SCOPE + r"\s*\.\s*(get|has|set|unset)\s*\(\s*")
_LITERAL_NAME_RE = re.compile(r"""(['"`])([^'"`\\]*)\1""")
_READS = frozenset({"get", "has"})
_INERT_KEYS = frozenset({"id", "type", "name"})


@dataclass(frozen=True)
class ScriptVariable:
    name: str
    location: str
    listen: str


@dataclass(frozen=True)
class ScriptReport:
    findings: tuple[Finding, ...]
    computed_fingerprint: str | None
    reads: tuple[ScriptVariable, ...]
    writes: tuple[ScriptVariable, ...]

    @property
    def hard_rejections(self) -> tuple[str, ...]:
        """کدهایی که هیچ تأییدی پاکشان نمی‌کند."""
        return tuple(
            sorted({item.code for item in self.findings if item.severity == SEVERITY_ERROR})
        )


@dataclass(frozen=True)
class ScriptText:
    """متنِ یک eventِ خوش‌شکل — فقط برای بررسیِ ایستا؛ هرگز در پیامِ یافته نمی‌آید."""

    location: str
    listen: str
    text: str


def script_texts(payload: Any) -> tuple[ScriptText, ...]:
    """متنِ همان eventهایی که inspect_scripts بررسی می‌کند (شکلِ نامعتبر حذف می‌شود؛
    یافته‌هایش را inspect_scripts گزارش می‌کند)."""
    return tuple(
        ScriptText(location, str(listen), text) for location, listen, text in _texts(payload, [])
    )


def inspect_scripts(payload: Any, stored_fingerprint: str | None) -> ScriptReport:
    findings: list[Finding] = []
    reads: list[ScriptVariable] = []
    writes: list[ScriptVariable] = []
    computed = script_fingerprint(payload) if isinstance(payload, dict) else None
    if computed != stored_fingerprint:
        findings.append(finding("SCRIPT_FINGERPRINT_MISMATCH", "$"))
    for location, listen, text in _texts(payload, findings):
        if _SEND_REQUEST_RE.search(text):
            findings.append(finding("SCRIPT_SEND_REQUEST", location))
        if _TOKEN_ACCESS_RE.search(text):
            findings.append(finding("SCRIPT_TOKEN_ACCESS", location))
        for call in _CALL_RE.finditer(text):
            name = _LITERAL_NAME_RE.match(text, call.end())
            if name is None:
                findings.append(finding("SCRIPT_DYNAMIC_VARIABLE", location))
                continue
            target = reads if call.group(1) in _READS else writes
            target.append(ScriptVariable(name.group(2), location, str(listen)))
    return ScriptReport(tuple(findings), computed, tuple(reads), tuple(writes))


def _texts(payload: Any, findings: list[Finding]):
    """(location، listen، متن) برای هر event که متنش قابلِ اعتماد است؛ یافته‌های شکل و
    eventِ ناشناخته به همان ترتیبِ قبلی در findings ثبت می‌شوند."""
    for entry in payload_scripts(payload) if isinstance(payload, dict) else []:
        location, event = entry["path"], entry["event"]
        lines = _script_lines(event, location, findings)
        listen = event.get("listen") if isinstance(event, dict) else None
        if isinstance(event, dict) and listen not in SCRIPT_EVENTS:
            findings.append(finding("SCRIPT_EVENT_UNSUPPORTED", location))
        if lines is None:
            continue
        yield location, listen, "\n".join(lines)


def _script_lines(event: Any, location: str, findings: list[Finding]) -> list[str] | None:
    """خطوطِ متنِ اسکریپت به همان شکل‌هایی که postman-collection می‌پذیرد؛ None یعنی
    شکل قابلِ اعتماد نیست (یافته‌اش ثبت شده است)."""
    if not isinstance(event, dict):
        findings.append(finding("SCRIPT_MALFORMED", location))
        return None
    script = event.get("script")
    if script is None:
        return []
    if isinstance(script, str):
        return [script]
    if isinstance(script, list):
        if all(isinstance(line, str) for line in script):
            return script
        findings.append(finding("SCRIPT_MALFORMED", location))
        return None
    if not isinstance(script, dict):
        findings.append(finding("SCRIPT_MALFORMED", location))
        return None
    lines: list[str] | None = []
    for key in sorted(script):
        value = script[key]
        if key == "exec":
            if isinstance(value, str):
                lines = [value] if lines is not None else None
            elif isinstance(value, list) and all(isinstance(line, str) for line in value):
                lines = value if lines is not None else None
            elif value is not None:
                findings.append(finding("SCRIPT_MALFORMED", location))
                lines = None
        elif key == "src":
            if value not in (None, ""):
                findings.append(finding("SCRIPT_EXTERNAL_SOURCE", location))
        elif key == "packages":
            if value not in (None, {}, []):
                findings.append(finding("SCRIPT_PACKAGES_UNSUPPORTED", location))
        elif key not in _INERT_KEYS and value not in (None, "", {}, []):
            findings.append(finding("SCRIPT_MALFORMED", location))
    return lines
