"""
models.py — قراردادِ ورودی و خروجیِ موتورِ اجرای Newman

همه‌ی ساختارها قابلِ سریال‌سازی به JSON اند (to_dict) و هیچ مقدارِ محرمانه‌ای
ندارند: URLها redact شده‌اند، هدر و بدنه اصلاً نگه داشته نمی‌شوند و از متغیرهای
میزبان فقط «منبع» و «خالی/پر بودن» ثبت می‌شود، نه مقدار.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_ERROR = "ERROR"

# error_kind — فقط وقتی status=ERROR است پر می‌شود.
ERROR_NODE_NOT_FOUND = "NODE_NOT_FOUND"
ERROR_NEWMAN_NOT_FOUND = "NEWMAN_NOT_FOUND"
ERROR_REPORTER_NOT_FOUND = "REPORTER_NOT_FOUND"
ERROR_INVALID_OPTIONS = "INVALID_OPTIONS"
ERROR_INVALID_COLLECTION = "INVALID_COLLECTION"
ERROR_INVALID_ENVIRONMENT = "INVALID_ENVIRONMENT"
ERROR_INVALID_GLOBALS = "INVALID_GLOBALS"
ERROR_HOST_VARIABLE_IN_COLLECTION = "HOST_VARIABLE_IN_COLLECTION"
ERROR_HOST_VARIABLE_EMPTY = "HOST_VARIABLE_EMPTY"
ERROR_OUTPUT_DIRECTORY = "OUTPUT_DIRECTORY"
ERROR_PROCESS_START_FAILED = "PROCESS_START_FAILED"
ERROR_PROCESS_TIMEOUT = "PROCESS_TIMEOUT"
ERROR_PROCESS_CANCELLED = "PROCESS_CANCELLED"
ERROR_PROCESS_FAILED = "PROCESS_FAILED"
ERROR_NEWMAN_RUNTIME = "NEWMAN_RUNTIME_ERROR"
ERROR_NETWORK = "NETWORK_ERROR"
ERROR_SCRIPT = "SCRIPT_ERROR"
ERROR_REPORT_MISSING = "REPORT_MISSING"
ERROR_REPORT_MALFORMED = "REPORT_MALFORMED"
ERROR_REPORT_INCONSISTENT = "REPORT_INCONSISTENT"
ERROR_SANITIZATION_FAILED = "SANITIZATION_FAILED"
ERROR_INPUT_FILE_MODIFIED = "INPUT_FILE_MODIFIED"
ERROR_INTERNAL = "INTERNAL_ERROR"
ERROR_PRIVATE_DIRECTORY = "PRIVATE_DIRECTORY"
ERROR_REQUEST_NOT_EXECUTED = "REQUEST_NOT_EXECUTED"
ERROR_ACCESS_TOKEN_INVALID = "ACCESS_TOKEN_INVALID"

# متغیرِ هدرِ «Authorization: {{token}}» در کالکشن (postman_builder._TOKEN_VAR)
TOKEN_VARIABLE = "token"

# چرا درخواستی فرستاده نشد
SKIP_FAILED_PRECONDITION = "failed_precondition"  # یک بررسیِ پیش از درخواست شکست خورد
SKIP_UNEXPLAINED = "unexplained"  # هیچ بررسیِ شکست‌خورده‌ای دلیلش را نمی‌گوید

NO_ASSERTIONS_WARNING = "No assertions were executed."

DEFAULT_REQUEST_TIMEOUT_MS = 30_000
DEFAULT_PROCESS_TIMEOUT_S = 15 * 60

# فایل‌های موجودِ پروژه — فقط خوانده می‌شوند.
_POSTMAN_ENV_DIR = (
    Path(__file__).resolve().parents[2] / "agents" / "test_case_generator" / "postmanEnv"
)
DEV_ENVIRONMENT_PATH = _POSTMAN_ENV_DIR / "dev.postman_environment.json"
WORKSPACE_GLOBALS_PATH = _POSTMAN_ENV_DIR / "workspace.postman_globals.json"


@dataclass(frozen=True)
class NewmanOptions:
    request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS
    process_timeout_s: float = DEFAULT_PROCESS_TIMEOUT_S
    delay_request_ms: int = 0
    bail: bool = False
    # TLS همیشه تأیید می‌شود مگر صراحتاً خاموش شود.
    insecure: bool = False


@dataclass(frozen=True)
class NewmanRunRequest:
    collection_path: Path
    environment_path: Path
    globals_path: Path
    output_dir: Path
    options: NewmanOptions = field(default_factory=NewmanOptions)
    # توکنِ دسترسیِ خام، فقط در حافظه — در زمانِ اجرا به‌صورتِ «Bearer <token>» جای
    # متغیرِ token ِ Environment می‌نشیند؛ فایل‌های Environment/Globals دست نمی‌خورند.
    # None = بدونِ تزریق (رفتارِ قبلی).
    access_token: str | None = field(default=None, repr=False)

    @classmethod
    def for_dev(
        cls,
        collection_path: Path | str,
        output_dir: Path | str,
        options: NewmanOptions | None = None,
    ) -> NewmanRunRequest:
        """همان دو فایلِ موجودِ dev و globals — هیچ انتخاب‌گرِ محیطِ دیگری نیست."""
        return cls(
            collection_path=Path(collection_path),
            environment_path=DEV_ENVIRONMENT_PATH,
            globals_path=WORKSPACE_GLOBALS_PATH,
            output_dir=Path(output_dir),
            options=options or NewmanOptions(),
        )


@dataclass
class AssertionResult:
    name: str
    passed: bool
    skipped: bool = False
    message: str = ""


@dataclass
class RequestResult:
    index: int
    name: str
    method: str = ""
    url: str = ""
    status_code: int | None = None
    status_text: str = ""
    response_time_ms: int | None = None
    assertions: list[AssertionResult] = field(default_factory=list)
    # {"kind": "network"|"script", "code": ..., "message": ...}
    errors: list[dict[str, str]] = field(default_factory=list)
    executed: bool = False
    skipped: bool = False
    skip_reason: str = ""
    # SKIP_FAILED_PRECONDITION (مثلاً guardِ وابستگی → FAIL) یا SKIP_UNEXPLAINED (→ ERROR)
    skip_kind: str = ""
    completed: bool = False


@dataclass
class HostVariableCheck:
    variable: str
    source: str  # environment | collection | globals | none
    defined: bool
    non_empty: bool


@dataclass
class InputFile:
    path: str
    sha256_before: str = ""
    sha256_after: str = ""


@dataclass
class RunStats:
    """شمارش‌های اجرا. None یعنی «نامعلوم» (مثلاً پاک‌سازی شکست خورد و رخدادها
    قابلِ اتکا نبودند) — هرگز به‌جای صفرِ واقعی."""

    items_total: int | None = 0
    requests_executed: int | None = 0
    requests_skipped: int | None = 0
    unexplained_skips: int | None = 0
    # درخواست‌های کالکشن که اصلاً شروع نشدند (مثلاً Newman زودتر متوقف شد)
    items_not_started: int | None = 0
    network_errors: int | None = 0
    script_errors: int | None = 0
    assertions_total: int | None = 0
    assertions_failed: int | None = 0

    @classmethod
    def unknown(cls) -> RunStats:
        return cls(**{name: None for name in cls.__dataclass_fields__})


@dataclass
class Artifacts:
    run_dir: str = ""
    json_report: str = ""
    html_report: str = ""
    stdout_log: str = ""
    stderr_log: str = ""
    result_json: str = ""


@dataclass
class NewmanRunResult:
    run_id: str
    status: str = STATUS_ERROR
    error_kind: str = ""
    error_message: str = ""
    no_assertions: bool = False
    warnings: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    exit_code: int | None = None
    timed_out: bool = False
    # لغوِ همکارانه (cancel_event) — status=ERROR و error_kind=PROCESS_CANCELLED
    cancelled: bool = False
    process_id: int | None = None
    node_version: str = ""
    newman_version: str = ""
    htmlextra_version: str = ""
    command: list[str] = field(default_factory=list)
    collection: InputFile = field(default_factory=lambda: InputFile(""))
    environment: InputFile = field(default_factory=lambda: InputFile(""))
    globals: InputFile = field(default_factory=lambda: InputFile(""))
    host_variables: list[HostVariableCheck] = field(default_factory=list)
    stats: RunStats = field(default_factory=RunStats)
    requests: list[RequestResult] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    artifacts: Artifacts = field(default_factory=Artifacts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "ERROR_ACCESS_TOKEN_INVALID",
    "TOKEN_VARIABLE",
    "ERROR_PROCESS_CANCELLED",
    "SKIP_UNEXPLAINED",
    "SKIP_FAILED_PRECONDITION",
    "ERROR_REQUEST_NOT_EXECUTED",
    "ERROR_PRIVATE_DIRECTORY",
    "Artifacts",
    "AssertionResult",
    "DEFAULT_PROCESS_TIMEOUT_S",
    "DEFAULT_REQUEST_TIMEOUT_MS",
    "DEV_ENVIRONMENT_PATH",
    "ERROR_HOST_VARIABLE_EMPTY",
    "ERROR_HOST_VARIABLE_IN_COLLECTION",
    "ERROR_INPUT_FILE_MODIFIED",
    "ERROR_INTERNAL",
    "ERROR_INVALID_COLLECTION",
    "ERROR_INVALID_ENVIRONMENT",
    "ERROR_INVALID_GLOBALS",
    "ERROR_INVALID_OPTIONS",
    "ERROR_NETWORK",
    "ERROR_NEWMAN_NOT_FOUND",
    "ERROR_NEWMAN_RUNTIME",
    "ERROR_NODE_NOT_FOUND",
    "ERROR_OUTPUT_DIRECTORY",
    "ERROR_PROCESS_FAILED",
    "ERROR_PROCESS_START_FAILED",
    "ERROR_PROCESS_TIMEOUT",
    "ERROR_REPORTER_NOT_FOUND",
    "ERROR_REPORT_INCONSISTENT",
    "ERROR_REPORT_MALFORMED",
    "ERROR_REPORT_MISSING",
    "ERROR_SANITIZATION_FAILED",
    "ERROR_SCRIPT",
    "HostVariableCheck",
    "InputFile",
    "NO_ASSERTIONS_WARNING",
    "NewmanOptions",
    "NewmanRunRequest",
    "NewmanRunResult",
    "RequestResult",
    "RunStats",
    "STATUS_ERROR",
    "STATUS_FAIL",
    "STATUS_PASS",
    "WORKSPACE_GLOBALS_PATH",
]
