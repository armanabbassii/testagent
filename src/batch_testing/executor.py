"""
src/batch_testing/executor.py — اجرای یک issue: Jira → قدم‌های ۱ تا ۵ → Newman

این ماژول هیچ منطقِ قدم‌ها را دوباره پیاده نمی‌کند؛ همان اجزای موجود را کنارِ هم
می‌گذارد:

    load_jira_task(state, key)                  ← دریافتِ فقط‌خواندنیِ issue + منابعِ شاهد
    run_all(state, before_step=gate)           ← همان خطِ لوله‌ی تک‌issue (Run All)
    NewmanExecutionEngine.run(request, cancel)  ← موتورِ Milestone 3 و لغوِ 4.2

دروازه (before_step) پیش از قدمِ ۳: اگر هیچ تست‌کیسی «قابلِ اجرا» نباشد — یعنی
همان شرط‌هایی که قدمِ ۴ برای ساختنِ درخواست دارد — خطِ لوله پیش از قدم ۳ می‌ایستد و
نتیجه NOT_EXECUTED + NEEDS_REVIEW است؛ Newman اجرا نمی‌شود. needs_reviewِ قدمِ ۵
جلوی Newman را نمی‌گیرد. blockedِ قدمِ ۵ Newman را اجرا نمی‌کند (قراردادِ قدمِ ۵:
«کالکشن قابلِ اتکا نیست»)، ولی برچسبش از روی خودِ کالکشن تعیین می‌شود:
COLLECTION_INVALID فقط وقتی کالکشن غایب است یا اعتبارسنج‌های خودِ قدمِ ۴ ردش
می‌کنند؛ در غیرِ این صورت FINAL_REVIEW_BLOCKED با کدهای issueِ قدمِ ۵.

خروجی (ExecutionOutcome) همه‌ی چیزی است که runner در BatchStore.finish_execution
ذخیره می‌کند؛ این ماژول خودش به پایگاهِ داده دست نمی‌زند.

امنیتِ artifact — سیاستِ «فهرستِ مجاز»:
  * متنِ آزاد (خلاصه، عنوان، گام‌ها، نتیجه‌ی مورد انتظار، دلیل‌ها، ابهام‌ها، نامِ
    سناریو و کالکشن، توضیحِ درخواست) ممکن است متنِ Jira یا بازنویسیِ آن توسطِ مدل
    باشد؛ هیچ‌کدام نوشته نمی‌شود — به‌جایش {redacted, sha256, chars}.
  * فقط داده‌ی ساختاریافته می‌ماند: شناسه‌ها، enumها، شمارش‌ها، متد/مسیرِ
    عملیات‌هایی که قدمِ ۲ با کاتالوگِ Swagger تطبیق داده، و هش‌ها.
  * کالکشن همان چیزی است که Newman اجرا می‌کند: نام‌ها و توضیح‌ها حذف/خنثی می‌شوند
    (اثری در اجرا ندارند)؛ URL، متد، هدر، query، بدنه و اسکریپت‌ها دست نمی‌خورند و
    یکسان‌بودنشان پیش از نوشتن بررسی می‌شود.
  * مقدارهای محرمانه‌ی شناخته‌شده: environment/globals و «نامزدهای محرمانه‌ی Jira»
    (مقدارِ بعد از کلیدهای حساس، Authorization، userinfo و query parameterهای
    حساسِ URLها، و رشته‌های توکن‌مانند) — فقط در حافظه، هرگز ذخیره نمی‌شوند. هر
    artifact (از جمله مقدارهای اجراییِ کالکشن) با آن‌ها بررسی می‌شود؛ اگر یکی مانده
    باشد: آن artifact نوشته نمی‌شود، Newman اجرا نمی‌شود و نتیجه ERROR است.

توکنِ دسترسی: runner در آغازِ هر نشست یکی می‌گیرد (src/auth) و در ExecutionRequest
می‌دهد؛ فقط در حافظه است، هرگز ذخیره نمی‌شود و خام و Bearerش جزوِ مقادیرِ محرمانه‌اند.
بدونِ توکن هیچ کاری (Jira، قدم‌ها، Newman) انجام نمی‌شود: ERROR / AUTHENTICATION_FAILED.

Execution Gate: runner در request.before_newman حصاری می‌دهد که درست پیش از engine.run
صدا زده می‌شود (مالکیتِ Gate را تأیید می‌کند). اگر رد کند، Newman شروع نمی‌شود و نتیجه
CANCELLED است؛ runner (که همان لحظه cancel_event را ست کرده) آن را INTERRUPTED ثبت می‌کند.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from src.agents.test_case_generator.api_discovery import operation_key
from src.auth import AccessToken
from src.agents.test_case_generator.environment_decisions import NORMALIZATION_VERSIONS
from src.agents.test_case_generator.host_selection import (
    HOST_RESOLVED,
    host_resolution_of,
    selections_by_case,
)
from src.agents.test_case_generator.postman_generation import (
    check_dependency_wiring,
    check_variables_are_available,
    extract_services,
    mapping_for,
    operation_index,
    validate_collection,
)
from src.batch_testing.models import (
    EXEC_CANCELLED,
    EXEC_ERROR,
    EXEC_FAIL,
    EXEC_NOT_EXECUTED,
    EXEC_PASS,
    FINAL_REVIEW_STATUSES,
    PHASE_DONE,
    PHASE_JIRA_FETCH,
    PHASE_NEWMAN,
    PHASE_STEP1,
    PHASE_STEP2,
    PHASE_STEP3,
    PHASE_STEP4,
    PHASE_STEP5,
    REVIEW_NEEDS_REVIEW,
    REVIEW_NONE,
)
from src.batch_testing.storage import DEFAULT_ARTIFACT_ROOT
from src.test_execution.newman import (
    DEV_ENVIRONMENT_PATH,
    WORKSPACE_GLOBALS_PATH,
    NewmanExecutionEngine,
    NewmanOptions,
    NewmanRunRequest,
)
from src.test_execution.newman.models import ERROR_PROCESS_CANCELLED, STATUS_FAIL, STATUS_PASS
from src.test_execution.newman.redaction import (
    MASK,
    SanitizationError,
    is_secret_key,
    is_sensitive_header,
    url_secrets,
    variable_secrets,
    verify_clean,
    verify_clean_json,
)
from ui import pipeline
from ui.workflow import INPUT_TASK_DESCRIPTION, WorkflowState

# ── error_kindهای اجرا ──────────────────────────────────────────────────────
ERROR_JIRA_FETCH = "JIRA_FETCH_FAILED"
ERROR_PIPELINE_STEP = "PIPELINE_STEP_FAILED"
ERROR_PIPELINE_EXCEPTION = "PIPELINE_EXCEPTION"
ERROR_COLLECTION_INVALID = "COLLECTION_INVALID"
ERROR_FINAL_REVIEW_BLOCKED = "FINAL_REVIEW_BLOCKED"
ERROR_NEWMAN = "NEWMAN_ERROR"
ERROR_ARTIFACT = "ARTIFACT_WRITE_FAILED"
ERROR_ARTIFACT_SANITIZATION = "ARTIFACT_SANITIZATION_FAILED"
ERROR_INVALID_REQUEST = "INVALID_EXECUTION_REQUEST"
# شکستِ احرازِ هویت پیش از هر کاری — جدا از خطاهای Newman (NEWMAN_ERROR)
ERROR_AUTHENTICATION = "AUTHENTICATION_FAILED"
STAGE_AUTHENTICATION = "authentication"

NOT_EXECUTED_NO_RUNNABLE = "No test case is runnable after Step 2."

_STEP_PHASES = {1: PHASE_STEP1, 2: PHASE_STEP2, 3: PHASE_STEP3, 4: PHASE_STEP4, 5: PHASE_STEP5}
# اجزای مسیرِ artifact — فقط نویسه‌های امن (کلیدِ Jira مثل PODIUM-3386).
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
# نامِ متغیر/پارامتر/JSON pathی که ساختار است، نه متن (مثلاً voucherId، data.items[0].id)
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_.\-\[\]$]{0,63}$")
_CASE_ID_PREFIX = re.compile(r"^([A-Za-z]+-\d+)\b")
# تکه‌ی بلندِ حرف‌ورقم — اگر هم حرف و هم رقم دارد شکلِ کلید/توکن است، نه نام
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]{20,}")
# بخشِ مسیرِ API: متغیرِ Postman، پارامترِ Swagger یا نویسه‌های سادهٔ URL
_PATH_SEGMENT = re.compile(
    r"^(?:\{\{[A-Za-z0-9_.\-]+\}\}|\{[A-Za-z0-9_.\-]+\}|:?[A-Za-z0-9_.~\-]*)$"
)
_HOST_VARIABLE = re.compile(r"^\{\{[A-Za-z0-9_]+\}\}")
# رشته‌ی توکن‌مانند در متنِ Jira: بلند، حرف و رقم با هم، بدونِ فاصله و «/»
_TOKEN_LIKE = re.compile(r"(?<![A-Za-z0-9_\-.+=])[A-Za-z0-9_\-.+=]{24,}(?![A-Za-z0-9_\-.+=])")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_KEY_VALUE = re.compile(
    r"([A-Za-z0-9_.\-]+)[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic|token)\s+)?([^\s\"'&;,<>)\]}]+)",
    re.IGNORECASE,
)

# پیامِ ثابتِ هر نوعِ دلیلِ بازبینی — پیامِ اصلی ممکن است متنِ مدل داشته باشد.
_REVIEW_MESSAGES = {
    "TEST_CASE_REVIEW": "One or more test cases need an environment decision.",
    "UNMATCHED": "An environment statement could not be tied to any test case.",
    "REJECTED": "An environment statement was rejected and may affect host selection.",
    "CONFLICT": "Conflicting environment statements apply.",
}


# ── قرارداد ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExecutionRequest:
    batch_id: str
    issue_key: str
    execution_id: str
    attempt: int
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT
    environment_path: Path = DEV_ENVIRONMENT_PATH
    globals_path: Path = WORKSPACE_GLOBALS_PATH
    newman_options: NewmanOptions = field(default_factory=NewmanOptions)
    cancel_event: threading.Event | None = None
    # توکنِ نشستِ Batch — فقط در حافظه
    access_token: AccessToken | None = field(default=None, repr=False)
    # حصارِ runner درست پیش از شروعِ پروسه‌ی Newman (Execution Gate): هر استثنا یعنی Newman
    # شروع نمی‌شود. None = بدونِ حصار (رفتارِ قبلی).
    before_newman: Callable[[], None] | None = field(default=None, repr=False, compare=False)


@dataclass
class ExecutionOutcome:
    """همه‌ی فیلدهای لازم برای BatchStore.finish_execution + چند فیلدِ توضیحی."""

    status: str
    phase: str = PHASE_DONE
    review_status: str = REVIEW_NONE
    review_reasons: list[dict[str, Any]] = field(default_factory=list)
    final_review_status: str = ""
    error_stage: str = ""
    error_kind: str = ""
    error_message: str = ""
    artifact_dir: str = ""
    collection_path: str = ""
    task_source: dict[str, Any] = field(default_factory=dict)
    newman_run_id: str = ""
    newman_status: str = ""
    newman_error_kind: str = ""
    newman_no_assertions: bool = False
    newman_run_dir: str = ""
    newman_result_path: str = ""
    requests_executed: int | None = None
    assertions_total: int | None = None
    assertions_failed: int | None = None
    cases_total: int | None = None
    cases_executable: int | None = None
    cases_not_executed: int | None = None
    # توضیحی — در جدولِ executions ذخیره نمی‌شوند
    newman_executed: bool = False
    not_executed_reason: str = ""
    step_artifacts: dict[str, str] = field(default_factory=dict)

    _NON_STORAGE = ("status", "newman_executed", "not_executed_reason", "step_artifacts")

    def storage_fields(self) -> dict[str, Any]:
        """آرگومان‌های BatchStore.finish_execution (به‌جز status)."""
        values = asdict(self)
        return {key: value for key, value in values.items() if key not in self._NON_STORAGE}


# ── قابلِ اجرا بودنِ تست‌کیس‌ها (همان شرط‌های قدمِ ۴) ──────────────────────


@dataclass
class Runnability:
    runnable: list[str] = field(default_factory=list)
    blocked: list[dict[str, str]] = field(default_factory=list)
    issue_review: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.runnable) + len(self.blocked)


def assess_runnability(step1_result: Any, step2_result: Any) -> Runnability:
    """کدام تست‌کیس‌ها به درخواستِ Postman تبدیل می‌شوند — بدونِ تکرارِ منطقِ قدمِ ۴.

    از همان اجزای قدمِ ۴ (operation_index، mapping_for، انتخابِ ذخیره‌شده‌ی میزبان)
    استفاده می‌کند؛ هیچ قاعده‌ی تازه‌ای اضافه نمی‌کند.
    """
    resolution = host_resolution_of(step1_result, step2_result)
    selections = selections_by_case(resolution)
    mappings = [m for m in (step2_result or {}).get("mappings") or [] if isinstance(m, dict)]
    try:
        index = operation_index(extract_services(step2_result))
    except Exception:  # noqa: BLE001 — نتیجه‌ی قدم ۲ بدونِ سرویس: هیچ تست‌کیسی اجرا نمی‌شود
        index = {}
    result = Runnability(issue_review=resolution.get("issue_review") or {})
    for case in (step1_result or {}).get("test_cases") or []:
        case_id = str(case.get("id") or "").strip() if isinstance(case, dict) else ""
        if not case_id:
            continue
        selection = selections.get(case_id)
        mapping = mapping_for(mappings, case_id)
        api = mapping.get("api") if isinstance(mapping, dict) else None
        if selection is None or selection.get("host_status") != HOST_RESOLVED:
            status = (selection or {}).get("host_status") or "missing"
            reason = f"Its host selection is {status}."
        elif not isinstance(api, dict) or not api.get("method") or not api.get("path"):
            reason = "Step 2 did not resolve an API for it."
        else:
            found = index.get(operation_key(str(api.get("method")), str(api.get("path"))))
            if found is None:
                reason = "Its mapped operation is not among the discovered APIs."
            elif not str(found[0].get("base_url") or "").strip():
                reason = "Its service has no base URL."
            else:
                result.runnable.append(case_id)
                continue
        result.blocked.append({"test_case_id": case_id, "reason": reason})
    return result


def review_of(runnability: Runnability) -> tuple[str, list[dict[str, Any]]]:
    """بازبینیِ issue: بازبینیِ محیط (قدم ۲) + تست‌کیس‌های غیرقابلِ اجرا.

    پیام‌ها ثابت و ساخته‌ی کدند (پیامِ اصلی ممکن است نامِ سرویسِ ساخته‌ی مدل را داشته
    باشد). هشدارهای اطلاعاتیِ قدم ۱ (مثلاً «VDI ذکر شده») عمداً این‌جا نیستند.
    """
    reasons: list[dict[str, Any]] = []
    if runnability.issue_review.get("status") == REVIEW_NEEDS_REVIEW:
        for reason in runnability.issue_review.get("reasons") or []:
            if isinstance(reason, dict):
                kind = str(reason.get("kind") or "")
                kind = kind if _safe_identifier(kind) else "ENVIRONMENT"
                reasons.append(
                    {
                        "kind": kind,
                        "decision_ids": _ids(reason.get("decision_ids")),
                        "message": _REVIEW_MESSAGES.get(kind, "Environment review is needed."),
                    }
                )
    for item in runnability.blocked:
        reasons.append(
            {
                "kind": "NOT_RUNNABLE",
                "test_case_id": item["test_case_id"],
                "message": item["reason"],
            }
        )
    return (REVIEW_NEEDS_REVIEW if reasons else REVIEW_NONE), reasons


# ── نامزدهای محرمانه‌ی Jira (فقط در حافظه) ──────────────────────────────────


def jira_candidate_secrets(texts: list[str]) -> set[str]:
    """مقدارهایی از متنِ Jira که شکلِ اعتبارنامه دارند — هرگز ذخیره یا لاگ نمی‌شوند.

    مقدارِ بعد از کلیدهای حساس، userinfo و query parameterهای حساسِ URLها و رشته‌های
    توکن‌مانند؛ فقط آن‌هایی که شبیهِ اعتبارنامه‌اند (حداقل ۶ نویسه و رقم یا نماد، یا
    ۱۶+ نویسه) — مقدارِ کوتاه یا واژه‌ی ساده موجبِ ردِ اشتباهِ artifact می‌شد.
    """
    candidates: set[str] = set()
    for text in texts:
        candidates |= _keyed_secrets(text)
        for url in _URL.findall(text or ""):
            candidates |= url_secrets(url)
        for token in _TOKEN_LIKE.findall(text or ""):
            if re.search(r"[A-Za-z]", token) and re.search(r"\d", token):
                candidates.add(token)
    return {value for value in candidates if _credential_like(value)}


def _keyed_secrets(text: str) -> set[str]:
    """مقدارِ بعد از کلیدِ حساس («password=…»، «"token": "…"») — نه ارجاعِ «{{…}}»."""
    return {
        value.strip()
        for key, value in _KEY_VALUE.findall(text or "")
        if is_secret_key(key) and value and "{{" not in value
    }


def _credential_like(value: str) -> bool:
    """بس کوتاه یا کلمه‌ی ساده نیست — جلوی ردِ اشتباهِ واژه‌هایی مثلِ «percentage»."""
    if len(value) < 6:
        return False
    return bool(re.search(r"\d", value) or re.search(r"[^A-Za-z0-9]", value) or len(value) >= 16)


# ── سیاستِ «فهرستِ مجاز» برای نتیجه‌ی قدم‌ها ────────────────────────────────


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redacted(value: Any) -> Any:
    """متنِ آزاد → فقط هش و طول؛ رشته‌ی خالی همان خالی می‌ماند."""
    text = (
        value
        if isinstance(value, str)
        else ("" if value is None else json.dumps(value, ensure_ascii=False))
    )
    if not text:
        return ""
    return {"redacted": True, "sha256": _sha256(text), "chars": len(text)}


def _token_like(text: str) -> bool:
    """تکه‌ای ۲۰+ نویسه‌ای با حرف و رقم (مثلاً sk_live_4eC39…، بخشی از JWT)."""
    return any(
        re.search(r"[A-Za-z]", run) and re.search(r"\d", run) for run in _ALNUM_RUN.findall(text)
    )


def _safe_identifier(text: str) -> bool:
    """نامِ ساختاری که می‌شود نگه داشت — نه متن، نه چیزی به شکلِ کلید/توکن."""
    return bool(_IDENTIFIER.match(text)) and not _token_like(text)


def _ident(value: Any) -> Any:
    text = str(value or "")
    return text if _safe_identifier(text) else redacted(text)


def _reference(value: Any) -> Any:
    """شناسه‌ی کامنتِ Jira (عددی) یا "" — هر چیزِ دیگری هش می‌شود."""
    text = str(value or "")
    if re.fullmatch(r"[A-Za-z0-9_.\-]{0,64}", text) and not _token_like(text):
        return text
    return redacted(text)


def _sha256_field(value: Any) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


def safe_path(value: Any) -> str:
    """مسیرِ API برای ردیابی؛ هر بخشِ غیرساده یا توکن‌مانند → «***».

    query و fragment هم در یک بخشِ غیرساده می‌افتند و پوشانده می‌شوند.
    """
    text = str(value or "")
    return "/".join(
        segment if _PATH_SEGMENT.match(segment) and not _token_like(segment) else MASK
        for segment in text.split("/")
    )


def safe_url(value: Any) -> Any:
    """URLِ کاتالوگ برای ردیابی: scheme، host، port و مسیرِ امن.

    userinfo (user:password@) و fragment حذف می‌شوند و مقدارِ هر query parameter
    (حساس یا نه) «***» می‌شود — فقط نامِ ساده‌ی پارامتر می‌ماند. URLی که این‌طور
    تجزیه نشود کامل هش می‌شود.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    variable = _HOST_VARIABLE.match(text)
    if variable:  # «{{base_url}}/api» — میزبان متغیرِ environment است
        rest = text[variable.end() :]
        if "?" in rest or "#" in rest or "@" in rest:
            return redacted(text)
        return variable.group(0) + safe_path(rest)
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return redacted(text)
    host = parts.hostname or ""
    if (
        parts.scheme.lower() not in ("http", "https")
        or not re.fullmatch(r"[A-Za-z0-9.\-]+", host)
        or _token_like(host)
    ):
        return redacted(text)
    query = "&".join(
        (name if _safe_identifier(name) else MASK) + (f"={MASK}" if item else "")
        for name, item in parse_qsl(parts.query, keep_blank_values=True)
    )
    return (
        f"{parts.scheme.lower()}://{host}"
        + (f":{port}" if port else "")
        + safe_path(parts.path)
        + (f"?{query}" if query else "")
    )


def _enum(value: Any, allowed: tuple[str, ...] | frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else ""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ids(values: Any) -> list[str]:
    return [item for item in values or [] if isinstance(item, str) and _safe_identifier(item)]


def _dicts(values: Any) -> list[dict]:
    return [item for item in values or [] if isinstance(item, dict)]


def _method(value: Any) -> str:
    return str(value).upper() if str(value or "").upper() in _HTTP_METHODS else ""


def _evidence(decision: dict) -> dict[str, Any]:
    evidence = decision.get("evidence") if isinstance(decision.get("evidence"), dict) else {}
    original = str(evidence.get("original") or evidence.get("as_returned") or "")
    span = evidence.get("span")
    return {
        "redacted": True,
        "sha256": _sha256(original) if original else "",
        "span": [int(x) for x in span] if isinstance(span, list) and len(span) == 2 else None,
        "normalization": _enum(evidence.get("normalization"), NORMALIZATION_VERSIONS),
    }


def _source(decision: dict) -> dict[str, Any]:
    source = decision.get("source") if isinstance(decision.get("source"), dict) else {}
    return {
        "type": _enum(source.get("type"), ("description", "comment", "attachment")),
        "reference": redacted(source.get("reference"))
        if source.get("type") == "attachment"
        else _reference(source.get("reference")),
        "sha256": _sha256_field(source.get("sha256")),
    }


def _project_step1(result: dict) -> dict[str, Any]:
    return {
        "task_summary": redacted(result.get("task_summary")),
        "identified_requirements": len(result.get("identified_requirements") or []),
        "clarifications": len(result.get("clarifications") or []),
        "test_cases": [
            {
                "id": _ident(case.get("id")),
                "type": _enum(
                    case.get("type"), ("positive", "negative", "boundary", "state_transition")
                ),
                "priority": _enum(case.get("priority"), ("high", "medium", "low")),
                "auth_intent": _ident(case.get("auth_intent")),
                "title": redacted(case.get("title")),
                "preconditions": len(case.get("preconditions") or []),
                "steps": len(case.get("steps") or []),
                "expected_result": redacted(case.get("expected_result")),
                "related_service": {
                    "method": _method((case.get("related_service") or {}).get("method")),
                    "path": redacted((case.get("related_service") or {}).get("path")),
                },
            }
            for case in _dicts(result.get("test_cases"))
        ],
        "variables": [
            {
                "name": _ident(variable.get("name")),
                "scope": _enum(variable.get("scope"), ("global", "collection")),
                "source_test_case_id": _ident(variable.get("source_test_case_id")),
                "response_path": _ident(variable.get("response_path")),
                "required": bool(variable.get("required")),
                "consumers": len(variable.get("consumers") or []),
                "instruction": redacted(variable.get("instruction")),
            }
            for variable in _dicts(result.get("variables"))
        ],
        "environment_decisions": [
            {
                "id": _ident(decision.get("id")),
                "status": _enum(decision.get("status"), ("ACCEPTED",)),
                "scope": _enum(decision.get("scope"), ("task", "service")),
                "environment": _enum(decision.get("environment"), ("vdi", "normal")),
                "service": {
                    "method": _method((decision.get("service") or {}).get("method")),
                    "path": redacted((decision.get("service") or {}).get("path")),
                    "name": redacted((decision.get("service") or {}).get("name")),
                },
                "test_case_ids": _ids(decision.get("test_case_ids")),
                "evidence": _evidence(decision),
                "source": _source(decision),
                "warnings": len(decision.get("warnings") or []),
            }
            for decision in _dicts(result.get("environment_decisions"))
        ],
        "rejected_environment_decisions": [
            {
                "id": _ident(decision.get("id")),
                "status": "REJECTED",
                "scope": _enum(decision.get("scope"), ("task", "service")),
                "environment": _enum(decision.get("environment"), ("vdi", "normal")),
                "rejection_code": _ident((decision.get("rejection") or {}).get("code")),
                "evidence": _evidence(decision),
                "source": _source(decision),
            }
            for decision in _dicts(result.get("rejected_environment_decisions"))
        ],
        "environment_sources": [
            {
                "type": _enum(source.get("type"), ("description", "comment", "attachment")),
                "reference": redacted(source.get("reference"))
                if source.get("type") == "attachment"
                else _reference(source.get("reference")),
                "sha256": _sha256_field(source.get("sha256")),
                "chars": _int(source.get("chars")),
            }
            for source in _dicts(result.get("environment_sources"))
        ],
        "environment_warnings": len(result.get("environment_warnings") or []),
    }


def _project_step2(result: dict) -> dict[str, Any]:
    resolution = (
        result.get("host_resolution") if isinstance(result.get("host_resolution"), dict) else {}
    )
    review = (
        resolution.get("issue_review") if isinstance(resolution.get("issue_review"), dict) else {}
    )
    return {
        # کاتالوگ از snapshotِ Swagger می‌آید، نه از Jira — فقط خلاصه‌اش؛ URLها بی
        # userinfo، fragment و مقدارِ query
        "services": [
            {
                "name": _ident(service.get("name")),
                "source_url": safe_url(service.get("source_url")),
                "base_url": safe_url(service.get("base_url")),
                "operations": len(service.get("apis") or []),
            }
            for service in _dicts(result.get("services"))
        ],
        "mappings": [
            {
                "test_case_id": _ident(mapping.get("test_case_id")),
                # متد/مسیر را قدمِ ۲ با کاتالوگِ Swagger اعتبارسنجی کرده است
                "api": {
                    "method": _method(mapping["api"].get("method")),
                    "path": safe_path(mapping["api"].get("path")),
                    "operation_id": _ident(mapping["api"].get("operation_id")),
                }
                if isinstance(mapping.get("api"), dict)
                else None,
                "confidence": _enum(mapping.get("confidence"), ("high", "medium", "low")),
                "reason": redacted(mapping.get("reason")),
                "clarification": redacted(mapping.get("clarification")),
            }
            for mapping in _dicts(result.get("mappings"))
        ],
        "clarifications": len(result.get("clarifications") or []),
        "host_resolution": {
            "decisions": [
                {
                    "id": _ident(item.get("id")),
                    "status": _enum(item.get("status"), ("MATCHED", "UNMATCHED", "REJECTED")),
                    "scope": _enum(item.get("scope"), ("task", "service")),
                    "environment": _enum(item.get("environment"), ("vdi", "normal")),
                    "matched_test_case_ids": _ids(item.get("matched_test_case_ids")),
                }
                for item in _dicts(resolution.get("decisions"))
            ],
            "selections": [
                {
                    "test_case_id": _ident(item.get("test_case_id")),
                    "host_status": _enum(
                        item.get("host_status"), ("RESOLVED", "CONFLICT", "NEEDS_REVIEW")
                    ),
                    "variable": _enum(item.get("variable"), ("base_url", "base_url_vdi")),
                    "environment": _enum(item.get("environment"), ("vdi", "normal")),
                    "basis": _enum(item.get("basis"), ("default", "task", "service")),
                    "decision_ids": _ids(item.get("decision_ids")),
                    "review_causes": [
                        {
                            "kind": _ident(cause.get("kind")),
                            "decision_ids": _ids(cause.get("decision_ids")),
                        }
                        for cause in _dicts(item.get("review_causes"))
                    ],
                }
                for item in _dicts(resolution.get("selections"))
            ],
            "issue_review": {
                "status": _enum(review.get("status"), ("NONE", "NEEDS_REVIEW")),
                "reasons": [
                    {
                        "kind": _ident(reason.get("kind")),
                        "decision_ids": _ids(reason.get("decision_ids")),
                    }
                    for reason in _dicts(review.get("reasons"))
                ],
            },
            "clarifications": len(resolution.get("clarifications") or []),
            "warnings": len(resolution.get("warnings") or []),
        },
    }


def _project_step3(result: dict) -> dict[str, Any]:
    return {
        "scenarios": [
            {
                "id": _ident(item.get("id")),
                "title": redacted(item.get("title")),
                "reason": redacted(item.get("reason")),
                "test_case_ids": _ids(item.get("test_case_ids")),
            }
            for item in _dicts(result.get("scenarios"))
        ],
        "execution_order": [
            {
                "test_case_id": _ident(item.get("test_case_id")),
                "order": _int(item.get("order")),
                "depends_on": _ids(item.get("depends_on")),
            }
            for item in _dicts(result.get("execution_order"))
        ],
        "data_dependencies": [
            {
                "variable_name": _ident(item.get("variable_name")),
                "source": {
                    "test_case_id": _ident((item.get("source") or {}).get("test_case_id")),
                    "location": _ident((item.get("source") or {}).get("location")),
                    "path": _ident((item.get("source") or {}).get("path")),
                },
                "targets": [
                    {
                        "test_case_id": _ident(target.get("test_case_id")),
                        "location": _ident(target.get("location")),
                        "parameter": _ident(target.get("parameter")),
                    }
                    for target in _dicts(item.get("targets"))
                ],
                "confidence": _enum(item.get("confidence"), ("high", "medium", "low")),
                "reason": redacted(item.get("reason")),
            }
            for item in _dicts(result.get("data_dependencies"))
        ],
        "clarifications": [
            {
                "type": _ident(item.get("type")),
                "test_case_id": _ident(item.get("test_case_id")),
                "message": redacted(item.get("message")),
            }
            for item in _dicts(result.get("clarifications"))
        ],
    }


def _project_step4(result: dict) -> dict[str, Any]:
    return {
        "collection_name": redacted(result.get("collection_name")),
        "collection": {"see": "collection.json"},
        "scenarios": [
            {
                "id": _ident(item.get("id")),
                "title": redacted(item.get("title")),
                "request_count": _int(item.get("request_count")),
            }
            for item in _dicts(result.get("scenarios"))
        ],
        "requests": [
            {
                "test_case_id": _ident(item.get("test_case_id")),
                "method": _method(item.get("method")),
                "path": safe_path(item.get("path")),
                "host_variable": _enum(item.get("host_variable"), ("base_url", "base_url_vdi")),
                "host_basis": _enum(item.get("host_basis"), ("default", "task", "service")),
                "base_path": safe_path(item.get("base_path")),
            }
            for item in _dicts(result.get("requests"))
        ],
        "unresolved": [
            {
                "test_case_id": _ident(item.get("test_case_id")),
                "host_status": _enum(item.get("host_status"), ("CONFLICT", "NEEDS_REVIEW")),
                "reason": redacted(item.get("reason")),
            }
            for item in _dicts(result.get("unresolved"))
        ],
        "clarifications": len(result.get("clarifications") or []),
        "warnings": len(result.get("warnings") or []),
    }


def _project_step5(result: dict) -> dict[str, Any]:
    review = result.get("review") if isinstance(result.get("review"), dict) else {}
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    return {
        "review": {
            "status": _enum(review.get("status"), ("ready", "needs_review", "blocked")),
            "issues": [
                {
                    "code": _ident(item.get("code")),
                    "step": _int(item.get("step")),
                    "test_case_id": _ident(item.get("test_case_id"))
                    if item.get("test_case_id")
                    else "",
                }
                for item in _dicts(review.get("issues"))
            ],
            "warnings": len(review.get("warnings") or []),
            "unresolved_clarifications": [
                {
                    "kind": _ident(item.get("kind")),
                    "source_step": _int(item.get("source_step")),
                    "test_case_id": _ident(item.get("test_case_id"))
                    if item.get("test_case_id")
                    else "",
                }
                for item in _dicts(review.get("unresolved_clarifications"))
            ],
        },
        "metrics": {
            key: value
            for key, value in metrics.items()
            if isinstance(key, str) and _safe_identifier(key) and _int(value) is not None
        },
        "artifacts": {"postman_collection": {"see": "collection.json"}},
    }


_PROJECTIONS = {
    1: _project_step1,
    2: _project_step2,
    3: _project_step3,
    4: _project_step4,
    5: _project_step5,
}


def sanitize_step_result(step: int, result: Any) -> dict[str, Any]:
    """نسخه‌ی قابلِ نوشتنِ نتیجه‌ی یک قدم — فقط فیلدهای فهرستِ مجاز."""
    return _PROJECTIONS[step](result if isinstance(result, dict) else {})


# ── کالکشن: حذفِ متادیتای متنی بدونِ تغییرِ اجرا ─────────────────────────


def collection_semantics(collection: dict) -> dict[str, Any]:
    """هر چیزی که روی اجرای Newman اثر دارد — ترتیب، URL، متد، هدر، بدنه، اسکریپت، متغیر."""
    requests = []

    def walk(items: Any) -> None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("item"), list):
                requests.append(("folder", copy.deepcopy(item.get("event"))))
                walk(item["item"])
            elif isinstance(item.get("request"), dict):
                request = {k: v for k, v in item["request"].items() if k != "description"}
                requests.append((copy.deepcopy(request), copy.deepcopy(item.get("event"))))

    walk(collection.get("item"))
    return {
        "requests": requests,
        "variable": copy.deepcopy(collection.get("variable")),
        "event": copy.deepcopy(collection.get("event")),
        "auth": copy.deepcopy(collection.get("auth")),
        "schema": (collection.get("info") or {}).get("schema"),
    }


def sanitize_collection(collection: dict, issue_key: str, attempt: int) -> dict[str, Any]:
    """نام‌ها و توضیح‌های متنی (احتمالاً برگرفته از Jira) خنثی می‌شوند؛ اجرا یکسان می‌ماند."""
    cleaned = copy.deepcopy(collection)
    info = {
        "name": f"Batch {issue_key} attempt {attempt}",
        "schema": (collection.get("info") or {}).get("schema"),
    }
    cleaned["info"] = info

    def walk(items: Any, counter: list[int]) -> None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            item.pop("description", None)
            if isinstance(item.get("item"), list):
                counter[0] += 1
                item["name"] = f"Folder {counter[0]}"
                walk(item["item"], counter)
            elif isinstance(item.get("request"), dict):
                item["request"].pop("description", None)
                match = _CASE_ID_PREFIX.match(str(item.get("name") or ""))
                counter[1] += 1
                item["name"] = match.group(1) if match else f"Request {counter[1]}"

    walk(cleaned.get("item"), [0, 0])
    if collection_semantics(cleaned) != collection_semantics(collection):
        raise SanitizationError("removing collection metadata would change execution semantics.")
    return cleaned


def _literal_secret(value: Any) -> str:
    """مقدارِ واقعی، نه ارجاع به متغیر («{{token}}» ذخیره‌ی امن است)."""
    text = value.strip() if isinstance(value, str) else ""
    return "" if "{{" in text else text


def execution_secrets(step2: Any, collection: Any) -> set[str]:
    """مقدارهای حساسی که در مقدارهای اجرایی آمده‌اند — فقط در حافظه.

    userinfo و query parameterهای حساسِ URLهای کاتالوگ و کالکشن، مقدارِ متغیرهای
    حساسِ کالکشن، هر جفتِ key/value (هدر، query، auth) با نامِ حساس، و مقدارِ
    اعتبارنامه‌مانندِ کلیدهای حساس در بدنه‌ی raw. این مقدارها
    بدونِ تغییرِ اجرا حذف‌شدنی نیستند؛ پس بررسیِ هر artifact با آن‌ها، کالکشنی را
    که در خودش دارد‌شان منتشرنشده نگه می‌دارد (fail closed).
    """
    secrets: set[str] = set()
    for service in _dicts((step2 or {}).get("services") if isinstance(step2, dict) else None):
        for key in ("source_url", "base_url"):
            secrets |= url_secrets(str(service.get(key) or ""))
    if not isinstance(collection, dict):
        return secrets
    secrets |= variable_secrets(collection)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            key = node.get("key")
            if isinstance(key, str) and (is_secret_key(key) or is_sensitive_header(key)):
                value = _literal_secret(node.get("value"))
                if value:
                    secrets.add(value)
                    scheme, _, rest = value.partition(" ")
                    if rest and scheme.lower() in ("bearer", "basic", "token"):
                        secrets.add(rest.strip())
            raw = node.get("raw")
            if isinstance(raw, str):  # url.raw یا body.raw («"password": "…"» در مثالِ Swagger)
                secrets.update(url_secrets(raw))
                secrets.update(v for v in _keyed_secrets(raw) if _credential_like(v))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, str) and "://" in node:
            secrets.update(url_secrets(node))

    walk(collection)
    return {value for value in secrets if value}


def collection_problems(collection: Any, step3: Any, step4: Any) -> list[str]:
    """اعتبارسنج‌های خودِ قدمِ ۴ روی کالکشن — فهرستِ خالی یعنی قابلِ اجرا."""
    if not isinstance(collection, dict) or not collection.get("item"):
        return ["Step 4 produced no collection."]
    dependencies = [d for d in (step3 or {}).get("data_dependencies") or [] if isinstance(d, dict)]
    names = {
        str(row.get("test_case_id")): str(row.get("name"))
        for row in (step4 or {}).get("requests") or []
        if isinstance(row, dict)
    }
    variables = {str(d.get("variable_name") or "").strip() for d in dependencies} - {""}
    return (
        validate_collection(collection)
        + check_variables_are_available(collection, variables)
        + check_dependency_wiring(collection, dependencies, names)
    )


def _task_source(state: WorkflowState, step1_result: Any, issue_key: str) -> dict[str, Any]:
    """فراداده‌ی منبع برای ردیابی — هرگز متنِ خامِ Jira."""
    sources = [
        {
            "type": _enum(item.get("type"), ("description", "comment", "attachment")),
            "reference": redacted(item.get("reference"))
            if item.get("type") == "attachment"
            else _reference(item.get("reference")),
            "sha256": _sha256_field(item.get("sha256")),
            "chars": _int(item.get("chars")),
        }
        for item in _dicts((step1_result or {}).get("environment_sources"))
    ]
    imported = state.task_import
    return {
        "issue_key": issue_key,
        "source": "jira",
        "task_description_sha256": _sha256(state.input_value(INPUT_TASK_DESCRIPTION)),
        "comments": len(imported.task_input.comments) if imported and imported.task_input else 0,
        "attachments": len(imported.task_input.attachments)
        if imported and imported.task_input
        else 0,
        "sources": sources,
    }


def _step_failure_message(step: int, error: str) -> str:
    """پیامِ ثابت؛ فقط نامِ کلاسِ استثنا (اگر هست) — متنِ خطا ممکن است متنِ مدل/Jira باشد."""
    match = re.search(r"\b([A-Z][A-Za-z]*(?:Error|Exception))\b", error or "")
    return f"Step {step} failed" + (f" ({match.group(1)})." if match else ".")


# ── نوشتنِ artifact ─────────────────────────────────────────────────────────


def _keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(key) for key in value] + [k for item in value.values() for k in _keys(item)]
    if isinstance(value, list):
        return [k for item in value for k in _keys(item)]
    return []


class ArtifactWriter:
    """فایل‌های یک اجرا — فقط داخلِ پوشه‌ی خودش، هرگز بازنویسی."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @classmethod
    def create(cls, root: Path, batch_id: str, issue_key: str, attempt: int) -> ArtifactWriter:
        for label, component in (("batch id", batch_id), ("issue key", issue_key)):
            if not _SAFE_COMPONENT.match(component or ""):
                raise ValueError(f"Unsafe {label} for an artifact path.")
        if not isinstance(attempt, int) or attempt < 1:
            raise ValueError("The attempt number must be a positive integer.")
        root = Path(root).resolve()
        directory = (root / batch_id / issue_key / f"execution_{attempt}").resolve()
        if root not in directory.parents:
            raise ValueError("The artifact path escapes the artifact root.")
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir(exist_ok=False)  # تلاشِ قبلی هرگز بازنویسی نمی‌شود
        return cls(directory)

    def path(self, name: str) -> Path:
        target = (self.directory / name).resolve()
        if self.directory not in target.parents:
            raise ValueError("The artifact file escapes its execution directory.")
        return target

    def write_json(self, name: str, document: Any, secrets: set[str]) -> Path:
        """پس از بررسیِ نبودِ هر مقدارِ محرمانه، نوشتنِ اتمی و فقط اگر فایل از قبل نباشد."""
        verify_clean_json(document, secrets, name)
        verify_clean("\n".join(_keys(document)), secrets, name)  # کلیدها هم، نه فقط مقدارها
        target = self.path(name)
        if target.exists():
            raise FileExistsError(name)
        temporary = target.with_name(f".{target.name}.tmp")
        with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, target)
        return target


# ── اجراگر ──────────────────────────────────────────────────────────────────


class IssueExecutor:
    """یک issue را کامل اجرا می‌کند. وابستگی‌ها برای تست قابلِ جایگزینی‌اند."""

    def __init__(
        self,
        *,
        engine: NewmanExecutionEngine | None = None,
        load_task: Callable[[WorkflowState, str], Any] | None = None,
        run_pipeline: Callable[..., Any] | None = None,
        state_factory: Callable[[], WorkflowState] = WorkflowState,
        on_phase: Callable[[str], None] | None = None,
    ) -> None:
        self._engine = engine or NewmanExecutionEngine()
        self._load_task = load_task or pipeline.load_jira_task
        self._run_pipeline = run_pipeline or pipeline.run_all
        self._state_factory = state_factory
        self._on_phase = on_phase

    def _phase(self, phase: str) -> None:
        if self._on_phase is not None:
            self._on_phase(phase)

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        """هیچ استثنایی بیرون نمی‌دهد؛ هر شکست یک ExecutionOutcomeِ ERROR است."""
        try:
            writer = ArtifactWriter.create(
                request.artifact_root, request.batch_id, request.issue_key, request.attempt
            )
        except (ValueError, OSError) as exc:
            kind = ERROR_INVALID_REQUEST if isinstance(exc, ValueError) else ERROR_ARTIFACT
            return ExecutionOutcome(
                status=EXEC_ERROR,
                error_stage="artifacts",
                error_kind=kind,
                error_message=f"The execution directory could not be created ({type(exc).__name__}).",
            )
        outcome = ExecutionOutcome(status=EXEC_ERROR, artifact_dir=str(writer.directory))
        secrets = self._known_secrets(request)
        try:
            self._execute(request, writer, outcome, secrets)
        except SanitizationError:
            self._fail(
                outcome,
                outcome.phase,
                ERROR_ARTIFACT_SANITIZATION,
                "An artifact could not be persisted safely; it was not published.",
            )
        except Exception as exc:  # noqa: BLE001 — هیچ استثنایی به runner نمی‌رسد
            self._fail(
                outcome,
                outcome.phase,
                ERROR_PIPELINE_EXCEPTION,
                f"Unexpected failure ({type(exc).__name__}).",
            )
        self._write_summary(request, writer, outcome, secrets)
        return outcome

    @staticmethod
    def _known_secrets(request: ExecutionRequest) -> set[str]:
        secrets: set[str] = set()
        for path in (request.environment_path, request.globals_path):
            try:
                secrets |= variable_secrets(json.loads(Path(path).read_text(encoding="utf-8-sig")))
            except (OSError, ValueError):
                continue
        if request.access_token is not None:
            token = request.access_token.value
            secrets |= {token, f"Bearer {token}"}
        return secrets

    @staticmethod
    def _fail(outcome: ExecutionOutcome, stage: str, kind: str, message: str) -> None:
        outcome.status, outcome.error_stage, outcome.error_kind = EXEC_ERROR, stage, kind
        outcome.error_message = message

    @staticmethod
    def _cancelled(outcome: ExecutionOutcome, phase: str) -> None:
        outcome.status, outcome.phase = EXEC_CANCELLED, phase
        outcome.error_stage, outcome.error_kind = phase, ""
        outcome.error_message = "The execution was cancelled."

    def _execute(
        self,
        request: ExecutionRequest,
        writer: ArtifactWriter,
        outcome: ExecutionOutcome,
        secrets: set[str],
    ) -> None:
        cancel = request.cancel_event
        if request.access_token is None:
            self._fail(
                outcome,
                STAGE_AUTHENTICATION,
                ERROR_AUTHENTICATION,
                "No access token is available; the issue was not executed.",
            )
            return
        # ── ۱. دریافتِ issue ──────────────────────────────────────────────
        if cancel is not None and cancel.is_set():
            self._cancelled(outcome, PHASE_JIRA_FETCH)
            return
        outcome.phase = PHASE_JIRA_FETCH
        self._phase(PHASE_JIRA_FETCH)
        state = self._state_factory()
        imported = self._load_task(state, request.issue_key)
        if not getattr(imported, "ok", False):
            stage = str(getattr(imported, "error_stage", "") or "jira")
            label = stage if _safe_identifier(stage) else "issue"
            self._fail(
                outcome, PHASE_JIRA_FETCH, ERROR_JIRA_FETCH, f"The Jira {label} stage failed."
            )
            return
        # مقدارهای اعتبارنامه‌مانندِ متنِ Jira — فقط در حافظه، برای بررسیِ هر artifact
        secrets |= jira_candidate_secrets([source.text for source in pipeline.task_sources(state)])

        # ── ۲. خطِ لوله‌ی موجود با دروازه ─────────────────────────────────
        gate = _Gate(cancel, self._phase, outcome)
        try:
            run = self._run_pipeline(state, before_step=gate)
        finally:
            self._persist_steps(state, writer, outcome, secrets)
        outcome.task_source = _task_source(state, state.result(1), request.issue_key)
        if gate.runnability is not None:
            self._record_cases(outcome, gate.runnability)

        if run.stopped:
            if gate.cancelled:
                self._cancelled(outcome, _STEP_PHASES.get(run.stopped_step, PHASE_STEP1))
            else:
                outcome.status, outcome.phase = EXEC_NOT_EXECUTED, PHASE_STEP3
                outcome.not_executed_reason = NOT_EXECUTED_NO_RUNNABLE
                outcome.review_status = REVIEW_NEEDS_REVIEW  # بی‌دلیل NOT_EXECUTED مجاز نیست
            return
        if not run.succeeded and run.status != pipeline.RUN_BLOCKED:
            self._fail(
                outcome,
                _STEP_PHASES.get(run.failed_step, outcome.phase),
                ERROR_PIPELINE_STEP,
                _step_failure_message(run.failed_step, run.error),
            )
            return

        step3, step4, step5 = state.result(3), state.result(4), state.result(5)
        review = (step5 or {}).get("review") if isinstance(step5, dict) else None
        status = str((review or {}).get("status") or "")
        outcome.final_review_status = status if status in FINAL_REVIEW_STATUSES else ""

        # ── ۳. کالکشن: اعتبار با اعتبارسنج‌های قدمِ ۴، بعد blockedِ قدمِ ۵ ─────
        collection = step4.get("collection") if isinstance(step4, dict) else None
        if collection_problems(collection, step3, step4):
            self._fail(
                outcome,
                PHASE_STEP4,
                ERROR_COLLECTION_INVALID,
                "The Step 4 collection is missing or invalid.",
            )
            return
        if run.status == pipeline.RUN_BLOCKED:
            codes = sorted(
                {
                    str(item.get("code"))
                    for item in _dicts((review or {}).get("issues"))
                    if item.get("code")
                }
            )
            outcome.review_status = REVIEW_NEEDS_REVIEW
            outcome.review_reasons = outcome.review_reasons + [
                {
                    "kind": "FINAL_REVIEW_ISSUE",
                    "code": code,
                    "message": "Step 5 reported a blocking issue.",
                }
                for code in codes
                if _safe_identifier(code)
            ]
            self._fail(
                outcome,
                PHASE_STEP5,
                ERROR_FINAL_REVIEW_BLOCKED,
                "Step 5 blocked the collection ("
                + ", ".join(c for c in codes if _safe_identifier(c))
                + ").",
            )
            return
        unresolved = step4.get("unresolved") if isinstance(step4, dict) else None
        if isinstance(unresolved, list):
            outcome.cases_not_executed = len(unresolved)
        safe_collection = sanitize_collection(collection, request.issue_key, request.attempt)
        collection_path = writer.write_json("collection.json", safe_collection, secrets)
        outcome.collection_path = str(collection_path)

        # ── ۴. Newman (روی همان کالکشنِ نوشته‌شده) ───────────────────────
        if cancel is not None and cancel.is_set():
            self._cancelled(outcome, PHASE_NEWMAN)
            return
        outcome.phase = PHASE_NEWMAN
        self._phase(PHASE_NEWMAN)
        if request.before_newman is not None:
            try:
                request.before_newman()  # آخرین لحظه پیش از شروعِ پروسه
            except Exception:  # noqa: BLE001 — حصار رد کرد: Newman شروع نمی‌شود (fail-closed)
                self._cancelled(outcome, PHASE_NEWMAN)
                outcome.error_message = (
                    "Newman was not started: the shared execution gate could not be confirmed."
                )
                return
        result = self._engine.run(
            NewmanRunRequest(
                collection_path=collection_path,
                environment_path=Path(request.environment_path),
                globals_path=Path(request.globals_path),
                output_dir=writer.directory / "newman",
                options=request.newman_options,
                access_token=request.access_token.value,
            ),
            cancel_event=cancel,
        )
        outcome.newman_executed = True
        self._record_newman(outcome, result)
        if result.status == STATUS_PASS:
            outcome.status, outcome.phase = EXEC_PASS, PHASE_DONE
        elif result.status == STATUS_FAIL:
            outcome.status, outcome.phase = EXEC_FAIL, PHASE_DONE
        elif result.error_kind == ERROR_PROCESS_CANCELLED:
            self._cancelled(outcome, PHASE_NEWMAN)
        else:
            self._fail(
                outcome,
                PHASE_NEWMAN,
                ERROR_NEWMAN,
                f"Newman reported {result.error_kind or 'an error'}.",
            )

    @staticmethod
    def _record_cases(outcome: ExecutionOutcome, runnability: Runnability) -> None:
        outcome.cases_total = runnability.total
        outcome.cases_executable = len(runnability.runnable)
        outcome.cases_not_executed = len(runnability.blocked)
        outcome.review_status, outcome.review_reasons = review_of(runnability)

    @staticmethod
    def _record_newman(outcome: ExecutionOutcome, result: Any) -> None:
        outcome.newman_run_id = result.run_id
        outcome.newman_status = result.status
        outcome.newman_error_kind = result.error_kind
        outcome.newman_no_assertions = result.no_assertions
        outcome.newman_run_dir = result.artifacts.run_dir
        outcome.newman_result_path = result.artifacts.result_json
        outcome.requests_executed = result.stats.requests_executed
        outcome.assertions_total = result.stats.assertions_total
        outcome.assertions_failed = result.stats.assertions_failed

    @staticmethod
    def _persist_steps(
        state: WorkflowState, writer: ArtifactWriter, outcome: ExecutionOutcome, secrets: set[str]
    ) -> None:
        """هر نتیجه‌ی جاری به ترتیب؛ اولین artifactِ ناامن → SanitizationError (قبلی‌ها می‌مانند)."""
        step4 = state.result(4)
        secrets |= execution_secrets(
            state.result(2), step4.get("collection") if isinstance(step4, dict) else None
        )
        for step in range(1, 6):
            result = state.result(step)
            name = f"step{step}.json"
            if result is None or state.is_stale(step) or name in outcome.step_artifacts:
                continue
            outcome.step_artifacts[name] = str(
                writer.write_json(name, sanitize_step_result(step, result), secrets)
            )

    def _write_summary(
        self,
        request: ExecutionRequest,
        writer: ArtifactWriter,
        outcome: ExecutionOutcome,
        secrets: set[str],
    ) -> None:
        summary = {
            "batch_id": request.batch_id,
            "issue_key": request.issue_key,
            "execution_id": request.execution_id,
            "attempt": request.attempt,
            **asdict(outcome),
        }
        try:
            outcome.step_artifacts["summary"] = str(
                writer.write_json("execution_summary.json", summary, secrets)
            )
        except SanitizationError:
            self._fail(
                outcome,
                "artifacts",
                ERROR_ARTIFACT_SANITIZATION,
                "The execution summary could not be persisted safely.",
            )
            outcome.review_reasons = []
            minimal = {
                "execution_id": request.execution_id,
                "status": outcome.status,
                "error_kind": outcome.error_kind,
            }
            try:
                outcome.step_artifacts["summary"] = str(
                    writer.write_json("execution_summary.json", minimal, set())
                )
            except OSError:
                pass
        except OSError:
            pass


class _Gate:
    """before_stepِ run_all: لغو پیش از هر قدم؛ قابلِ اجرا بودن پیش از قدمِ ۳."""

    def __init__(
        self,
        cancel: threading.Event | None,
        on_phase: Callable[[str], None],
        outcome: ExecutionOutcome,
    ) -> None:
        self._cancel = cancel
        self._on_phase = on_phase
        self._outcome = outcome
        self.cancelled = False
        self.runnability: Runnability | None = None

    def __call__(self, step: int, state: WorkflowState) -> bool:
        if self._cancel is not None and self._cancel.is_set():
            self.cancelled = True
            return False
        if step == 3:
            self.runnability = assess_runnability(state.result(1), state.result(2))
            if not self.runnability.runnable:
                return False
        self._outcome.phase = _STEP_PHASES[step]
        self._on_phase(_STEP_PHASES[step])
        return True


__all__ = [
    "ERROR_ARTIFACT",
    "ERROR_ARTIFACT_SANITIZATION",
    "ERROR_AUTHENTICATION",
    "ERROR_COLLECTION_INVALID",
    "ERROR_FINAL_REVIEW_BLOCKED",
    "ERROR_INVALID_REQUEST",
    "ERROR_JIRA_FETCH",
    "ERROR_NEWMAN",
    "ERROR_PIPELINE_EXCEPTION",
    "ERROR_PIPELINE_STEP",
    "STAGE_AUTHENTICATION",
    "NOT_EXECUTED_NO_RUNNABLE",
    "ArtifactWriter",
    "ExecutionOutcome",
    "ExecutionRequest",
    "IssueExecutor",
    "Runnability",
    "assess_runnability",
    "collection_problems",
    "collection_semantics",
    "execution_secrets",
    "jira_candidate_secrets",
    "redacted",
    "review_of",
    "safe_path",
    "safe_url",
    "sanitize_collection",
    "sanitize_step_result",
]
