"""
src/testcase_box/models.py — وضعیت‌ها، گذارهای مجاز و رکوردهای TestCase Box

  * case: یک تست‌کیسِ منطقی در کتابخانه (عنوان/توضیح/وضعیت قابلِ ویرایش‌اند)؛
  * version: محتوای اجرایی (payload) — پس از ساخت تغییرناپذیر؛ هر تغییرِ محتوا
    نسخه‌ی تازه می‌سازد؛
  * source / link / import job / script approval: منشأ، رابطه‌ها، سابقه‌ی import
    و تأییدِ صریحِ اسکریپت؛
  * run → unit → run case: نسخه‌ی دقیقِ هر case در هر اجرا سنجاق (pin) می‌شود.

اعتبارسنجیِ ورودی همین‌جا (__post_init__) انجام می‌شود؛ پایگاهِ داده همان قاعده‌ها
را دوباره با CHECK/trigger/foreign key اعمال می‌کند.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

# ── case ─────────────────────────────────────────────────────────────────────
CASE_ACTIVE = "ACTIVE"
CASE_ARCHIVED = "ARCHIVED"
CASE_STATUSES = (CASE_ACTIVE, CASE_ARCHIVED)
CASE_KEY_PREFIX = "BX-"

# ── source — نوعِ منشأ در پایگاهِ داده محدود نیست (بدونِ migration گسترش می‌یابد)؛
# فهرستِ نوع‌های شناخته‌شده فقط در Python اعمال می‌شود.
SOURCE_BATCH = "BATCH"
SOURCE_IMPORT = "IMPORT"
SOURCE_MANUAL = "MANUAL"
KNOWN_SOURCE_TYPES = frozenset({SOURCE_BATCH, SOURCE_IMPORT, SOURCE_MANUAL})

# ── link ─────────────────────────────────────────────────────────────────────
LINK_DEPENDS_ON = "DEPENDS_ON"
LINK_RELATED = "RELATED"
LINK_TYPES = (LINK_DEPENDS_ON, LINK_RELATED)

# ── import job ───────────────────────────────────────────────────────────────
IMPORT_PENDING = "PENDING"
IMPORT_COMPLETED = "COMPLETED"
IMPORT_FAILED = "FAILED"
IMPORT_CANCELLED = "CANCELLED"
IMPORT_STATUSES = (IMPORT_PENDING, IMPORT_COMPLETED, IMPORT_FAILED, IMPORT_CANCELLED)
IMPORT_INITIAL = frozenset({IMPORT_PENDING})
IMPORT_FINAL = frozenset({IMPORT_COMPLETED, IMPORT_FAILED, IMPORT_CANCELLED})
IMPORT_TRANSITIONS: dict[str, frozenset[str]] = {IMPORT_PENDING: IMPORT_FINAL}

# ── script approval ──────────────────────────────────────────────────────────
APPROVAL_APPROVED = "APPROVED"
APPROVAL_REJECTED = "REJECTED"
APPROVAL_REVOKED = "REVOKED"
APPROVAL_STATUSES = (APPROVAL_APPROVED, APPROVAL_REJECTED, APPROVAL_REVOKED)
# وضعیتِ محاسبه‌شده برای یک نسخه (در جدول ذخیره نمی‌شوند)
SCRIPTS_NOT_REQUIRED = "NOT_REQUIRED"
SCRIPTS_PENDING = "PENDING"

# ── run ──────────────────────────────────────────────────────────────────────
RUN_QUEUED = "QUEUED"
RUN_RUNNING = "RUNNING"
RUN_COMPLETED = "COMPLETED"
RUN_CANCELLED = "CANCELLED"
# runner بی‌آنکه اجرا را ببندد از بین رفت؛ در V1 ادامه ندارد — rerun با نسخه‌های سنجاق‌شده
RUN_INTERRUPTED = "INTERRUPTED"
RUN_STATUSES = (RUN_QUEUED, RUN_RUNNING, RUN_COMPLETED, RUN_CANCELLED, RUN_INTERRUPTED)
RUN_INITIAL = frozenset({RUN_QUEUED})
RUN_FINAL = frozenset({RUN_COMPLETED, RUN_CANCELLED, RUN_INTERRUPTED})
RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    RUN_QUEUED: frozenset({RUN_RUNNING, RUN_CANCELLED, RUN_INTERRUPTED}),
    RUN_RUNNING: frozenset({RUN_COMPLETED, RUN_CANCELLED, RUN_INTERRUPTED}),
}
# (v4) درخواستِ کنترلیِ run — هر session ثبت می‌کند، مالکِ اجرا اجرا می‌کند
RUN_ACTION_NONE = "NONE"
RUN_ACTION_CANCEL = "CANCEL"
RUN_ACTIONS = (RUN_ACTION_NONE, RUN_ACTION_CANCEL)

# ── unit ─────────────────────────────────────────────────────────────────────
UNIT_QUEUED = "QUEUED"
UNIT_RUNNING = "RUNNING"
UNIT_PASS = "PASS"
UNIT_FAIL = "FAIL"
UNIT_ERROR = "ERROR"
UNIT_CANCELLED = "CANCELLED"
UNIT_NOT_RUN = "NOT_RUN"
UNIT_STATUSES = (
    UNIT_QUEUED,
    UNIT_RUNNING,
    UNIT_PASS,
    UNIT_FAIL,
    UNIT_ERROR,
    UNIT_CANCELLED,
    UNIT_NOT_RUN,
)
UNIT_INITIAL = frozenset({UNIT_QUEUED})
UNIT_FINAL = frozenset({UNIT_PASS, UNIT_FAIL, UNIT_ERROR, UNIT_CANCELLED, UNIT_NOT_RUN})
UNIT_TRANSITIONS: dict[str, frozenset[str]] = {
    UNIT_QUEUED: frozenset({UNIT_RUNNING, UNIT_CANCELLED, UNIT_NOT_RUN}),
    UNIT_RUNNING: frozenset({UNIT_PASS, UNIT_FAIL, UNIT_ERROR, UNIT_CANCELLED, UNIT_NOT_RUN}),
}

# ── run case ─────────────────────────────────────────────────────────────────
CASE_RUN_QUEUED = "QUEUED"
CASE_RUN_PASS = "PASS"
CASE_RUN_FAIL = "FAIL"
CASE_RUN_ERROR = "ERROR"
CASE_RUN_SKIPPED = "SKIPPED"
# از ابتدا قابلِ اجرا نبود (مثلاً اسکریپتِ تأییدنشده) — پایانی از لحظه‌ی ساخت
CASE_RUN_NOT_EXECUTABLE = "NOT_EXECUTABLE"
CASE_RUN_CANCELLED = "CANCELLED"
CASE_RUN_NOT_RUN = "NOT_RUN"
RUN_CASE_STATUSES = (
    CASE_RUN_QUEUED,
    CASE_RUN_PASS,
    CASE_RUN_FAIL,
    CASE_RUN_ERROR,
    CASE_RUN_SKIPPED,
    CASE_RUN_NOT_EXECUTABLE,
    CASE_RUN_CANCELLED,
    CASE_RUN_NOT_RUN,
)
RUN_CASE_INITIAL = frozenset({CASE_RUN_QUEUED, CASE_RUN_NOT_EXECUTABLE})
RUN_CASE_FINAL = frozenset(RUN_CASE_STATUSES) - {CASE_RUN_QUEUED}
RUN_CASE_TRANSITIONS: dict[str, frozenset[str]] = {
    CASE_RUN_QUEUED: frozenset(
        {
            CASE_RUN_PASS,
            CASE_RUN_FAIL,
            CASE_RUN_ERROR,
            CASE_RUN_SKIPPED,
            CASE_RUN_CANCELLED,
            CASE_RUN_NOT_RUN,
        }
    ),
}

TITLE_MAX_CHARS = 300
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidTransitionError(ValueError):
    """گذارِ وضعیتِ غیرمجاز — هیچ چیزی ذخیره نشد."""


def _check(kind: str, transitions: dict[str, frozenset[str]], current: str, target: str) -> None:
    if target not in transitions.get(current, frozenset()):
        raise InvalidTransitionError(f"A {kind} cannot go from {current} to {target}.")


def check_run_transition(current: str, target: str) -> None:
    _check("run", RUN_TRANSITIONS, current, target)


def check_unit_transition(current: str, target: str) -> None:
    _check("run unit", UNIT_TRANSITIONS, current, target)


def check_run_case_transition(current: str, target: str) -> None:
    _check("run case", RUN_CASE_TRANSITIONS, current, target)


def check_import_transition(current: str, target: str) -> None:
    _check("import job", IMPORT_TRANSITIONS, current, target)


# ── JSON و اثرانگشت ─────────────────────────────────────────────────────────


def to_json(value: Any) -> str:
    """JSONِ قطعی (کلیدهای مرتب) — هر مقداری که بی‌کم‌وکاست برنمی‌گردد رد می‌شود.

    tuple، کلیدِ غیرِ رشته‌ای، NaN/Infinity و انواعِ ناشناخته ValueError می‌دهند تا
    آنچه خوانده می‌شود دقیقاً همان باشد که نوشته شد.
    """
    try:
        text = json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Value is not JSON-serializable: {type(exc).__name__}.") from exc
    if json.loads(text) != value:
        raise ValueError("Value does not survive a JSON round trip (tuple or non-string key?).")
    return text


def from_json(text: str, kind: type = dict) -> Any:
    """خواندن و بررسیِ نوعِ ریشه؛ ValueError اگر JSON نامعتبر یا از نوعِ دیگری باشد."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("Stored value is not valid JSON.") from exc
    if not isinstance(value, kind):
        raise ValueError(f"Stored JSON is a {type(value).__name__}, expected {kind.__name__}.")
    return value


def sha256_json(value: Any) -> str:
    return hashlib.sha256(to_json(value).encode("utf-8")).hexdigest()


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.match(value))


# کلیدهای Script که خودشان هیچ کدی اجرا نمی‌کنند؛ هر کلیدِ دیگرِ غیرِ خالی (exec، src،
# packages، کلیدِ ناشناخته‌ی نسخه‌های بعدی) «ممکن است اجرا شود» حساب می‌شود.
_INERT_SCRIPT_KEYS = frozenset({"id", "type", "name"})


def _blank(value: Any) -> bool:
    """فقط None، رشته‌ی سفید و فهرستی از رشته‌های سفید «خالی»اند؛ هر چیزِ دیگر محتواست."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return all(isinstance(line, str) and not line.strip() for line in value)
    return False


def _event_may_run(event: Any) -> bool:
    """همان شکل‌هایی که postman-collection (Event.update) می‌پذیرد، به‌علاوه‌ی fail-closed:

    * script رشته یا آرایه → exec؛
    * script شیء → هر کلیدِ غیرِ خنثیِ غیرِ خالی؛ فقط «packages: {}» (خروجیِ Postmanِ
      جدید برای اسکریپتِ خالی) فراداده‌ی بی‌اثر حساب می‌شود؛
    * script نبود/خالی → چیزی اجرا نمی‌شود؛
    * هر شکلِ دیگر (عدد، بولی، entryِ غیرِ شیء) قابلِ دسته‌بندیِ مطمئن نیست → بازبینی.
    """
    if not isinstance(event, dict):
        return not _blank(event)
    script = event.get("script")
    if script is None or isinstance(script, str | list):
        return not _blank(script)
    if isinstance(script, dict):
        return any(
            not _blank(value)
            for key, value in script.items()
            if key not in _INERT_SCRIPT_KEYS and not (key == "packages" and value == {})
        )
    return True


def payload_scripts(payload: Any, path: str = "$") -> list[dict[str, Any]]:
    """همه‌ی eventهایی که ممکن است اسکریپت اجرا کنند — در هر عمقی از payload.

    «event» مثلِ PropertyListِ SDK خوانده می‌شود: آرایه، یا یک شیء/مقدارِ تک که یک entry
    حساب می‌شود. هر entry خام و با مسیرش برمی‌گردد تا اثرانگشت دقیقاً به همان محتوا و
    همان جایگاه بسته باشد. فقط استخراجِ ساختاری است — هیچ تحلیلِ امنیتی‌ای اینجا نیست.
    """
    found: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for key in sorted(payload):
            value = payload[key]
            if key == "event":
                entries = (
                    [(f"{path}.event[{index}]", entry) for index, entry in enumerate(value)]
                    if isinstance(value, list)
                    else [(f"{path}.event", value)]
                )
                found.extend(
                    {"path": entry_path, "event": entry}
                    for entry_path, entry in entries
                    if _event_may_run(entry)
                )
            found.extend(payload_scripts(value, f"{path}.{key}"))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(payload_scripts(value, f"{path}[{index}]"))
    return found


def script_fingerprint(payload: dict[str, Any]) -> str | None:
    """SHA-256 ِ همه‌ی اسکریپت‌ها؛ None یعنی payload هیچ اسکریپتی ندارد."""
    scripts = payload_scripts(payload)
    return sha256_json(scripts) if scripts else None


def case_key(case_id: int) -> str:
    return f"{CASE_KEY_PREFIX}{case_id:06d}"


def _require_text(name: str, value: Any, max_chars: int | None = None) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    if max_chars is not None and len(value) > max_chars:
        raise ValueError(f"{name} must be at most {max_chars} characters.")


def _require_json(name: str, value: Any, kind: type = dict) -> None:
    if not isinstance(value, kind):
        raise ValueError(f"{name} must be a JSON {kind.__name__}.")
    try:
        to_json(value)
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from exc


def validate_title(title: Any) -> None:
    _require_text("title", title, TITLE_MAX_CHARS)


def validate_json(name: str, value: Any, kind: type = dict) -> None:
    _require_json(name, value, kind)


def _require_id(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")


# ── ورودی‌ها ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NewSource:
    source_type: str
    source_reference: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source_type not in KNOWN_SOURCE_TYPES:
            raise ValueError(f"Unknown source type {self.source_type!r}.")
        _require_text("source_reference", self.source_reference)
        _require_json("source metadata", self.metadata)


@dataclass(frozen=True)
class NewLink:
    """رابطه‌ی خروجی از caseِ در حالِ ساخت (یا caseِ مبدأ) به target_case_id."""

    target_case_id: int
    link_type: str = LINK_DEPENDS_ON
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id("target_case_id", self.target_case_id)
        if self.link_type not in LINK_TYPES:
            raise ValueError(f"Unknown link type {self.link_type!r}.")
        _require_json("link metadata", self.metadata)


def _check_version_fields(payload: Any, fingerprint: Any, created_by: Any) -> None:
    _require_json("payload", payload)
    if fingerprint is not None and not is_sha256(fingerprint):
        raise ValueError("fingerprint must be a lowercase SHA-256 hex digest.")
    if created_by is not None:
        _require_text("created_by", created_by)


@dataclass(frozen=True)
class NewVersion:
    """محتوای تازه برای یک caseِ موجود. fingerprint ِ خالی = SHA-256 ِ JSONِ قطعیِ payload؛
    اثرانگشتِ اسکریپت همیشه از خودِ payload محاسبه می‌شود (از بیرون پذیرفته نمی‌شود)."""

    payload: dict[str, Any]
    fingerprint: str | None = None
    created_by: str | None = None
    sources: tuple[NewSource, ...] = ()

    def __post_init__(self) -> None:
        _check_version_fields(self.payload, self.fingerprint, self.created_by)


@dataclass(frozen=True)
class NewCase:
    title: str
    payload: dict[str, Any]
    description: str = ""
    sources: tuple[NewSource, ...] = ()
    links: tuple[NewLink, ...] = ()
    fingerprint: str | None = None
    created_by: str | None = None

    def __post_init__(self) -> None:
        validate_title(self.title)
        if not isinstance(self.description, str):
            raise ValueError("description must be a string.")
        _check_version_fields(self.payload, self.fingerprint, self.created_by)


@dataclass(frozen=True)
class NewRunCase:
    case_id: int
    case_version_id: int
    execution_order: int
    status: str = CASE_RUN_QUEUED
    result_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id("case_id", self.case_id)
        _require_id("case_version_id", self.case_version_id)
        _require_id("execution_order", self.execution_order)
        if self.status not in RUN_CASE_INITIAL:
            raise ValueError(f"A run case cannot start as {self.status!r}.")
        _require_json("result metadata", self.result_metadata)


@dataclass(frozen=True)
class NewRunUnit:
    unit_number: int
    cases: tuple[NewRunCase, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id("unit_number", self.unit_number)
        if not self.cases:
            raise ValueError("A run unit needs at least one case.")
        orders = [case.execution_order for case in self.cases]
        if len(set(orders)) != len(orders):
            raise ValueError("execution_order must be unique within a unit.")
        _require_json("unit metadata", self.metadata)


@dataclass(frozen=True)
class NewRun:
    units: tuple[NewRunUnit, ...]
    retry_of_run_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.units:
            raise ValueError("A run needs at least one unit.")
        numbers = [unit.unit_number for unit in self.units]
        if len(set(numbers)) != len(numbers):
            raise ValueError("unit_number must be unique within a run.")
        case_ids = [case.case_id for unit in self.units for case in unit.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("A case can appear only once in a run.")
        if self.retry_of_run_id is not None:
            _require_id("retry_of_run_id", self.retry_of_run_id)
        _require_json("run metadata", self.metadata)


# ── رکوردهای ذخیره‌شده ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class BoxCase:
    id: int
    title: str
    description: str
    status: str
    current_version_id: int
    created_at: str
    updated_at: str

    @property
    def key(self) -> str:
        return case_key(self.id)

    @property
    def archived(self) -> bool:
        return self.status == CASE_ARCHIVED


@dataclass(frozen=True)
class CaseVersion:
    id: int
    case_id: int
    version_number: int
    payload: dict[str, Any]
    fingerprint: str
    script_fingerprint: str | None
    created_at: str
    created_by: str | None

    @property
    def has_scripts(self) -> bool:
        return self.script_fingerprint is not None


@dataclass(frozen=True)
class CaseSource:
    id: int
    case_id: int
    case_version_id: int | None
    source_type: str
    source_reference: str
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class CaseLink:
    id: int
    source_case_id: int
    target_case_id: int
    link_type: str
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class ImportJob:
    id: int
    source_type: str
    filename: str
    status: str
    summary: dict[str, Any]
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class ScriptApproval:
    id: int
    case_version_id: int
    script_fingerprint: str
    approval_status: str
    review_findings: list[Any]
    approved_at: str | None
    created_at: str


@dataclass(frozen=True)
class BoxRun:
    id: int
    status: str
    retry_of_run_id: int | None
    metadata: dict[str, Any]
    created_at: str
    started_at: str | None
    finished_at: str | None

    @property
    def final(self) -> bool:
        return self.status in RUN_FINAL


@dataclass(frozen=True)
class RunUnit:
    id: int
    run_id: int
    unit_number: int
    status: str
    started_at: str | None
    finished_at: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class RunCase:
    id: int
    run_id: int
    run_unit_id: int
    case_id: int
    case_version_id: int
    execution_order: int
    status: str
    result_metadata: dict[str, Any]


@dataclass(frozen=True)
class RunLease:
    """مالکیتِ اجرای یک run — فقط به گیرنده‌ی آن داده می‌شود. token (مبهم) در repr و هیچ
    پیامی نمی‌آید؛ generation مقدارِ fencing است."""

    run_id: int
    generation: int
    token: str = field(repr=False)


@dataclass(frozen=True)
class RunOwnership:
    """وضعیتِ مالکیتِ یک run برای نمایش و بازیابی — هرگز توکن ندارد."""

    run_id: int
    status: str
    leased: bool
    owner_pid: int | None
    owner_host: str | None
    heartbeat_at: str | None
    lease_generation: int
    requested_action: str
