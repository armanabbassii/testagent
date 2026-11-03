"""
host_selection.py — انتخابِ میزبانِ هر تست‌کیس ({{base_url}} یا {{base_url_vdi}})

قرارداد URLِ همه‌ی کالکشن‌ها (ویزارد، Batch و agent.py):

    {{<متغیرِ میزبان>}} + <مسیرِ پایه‌ی سرورِ Swagger> + <مسیرِ عملیات> + query
    {{base_url_vdi}}/api/admin/serviceSet/:id/requestPossibility

مقدارِ واقعیِ دو متغیر فقط از Postman Environment می‌آید؛ کالکشن هیچ متغیرِ
میزبانی تعریف نمی‌کند و میزبانِ Swagger هرگز در آن نوشته نمی‌شود.

مرحله‌ی B (این ماژول، قطعی، پایانِ قدم ۲): تصمیم‌های استخراج‌شده‌ی قدم ۱ روی
تست‌کیس‌های نگاشت‌شده نشانده می‌شوند و برای هر تست‌کیس دقیقاً یک انتخاب ذخیره
می‌شود. قدم‌های بعد همین را می‌خوانند و هیچ‌وقت دوباره تصمیم نمی‌گیرند.

قاعده‌ها (طرحِ تأییدشده‌ی v3):

  * هیچ تصمیمِ مرتبطی نیست → base_url (basis=default)
  * تصمیمِ سرویس بر تصمیمِ کلِ تسک مقدم است (تعارض نیست)
  * دو محیطِ متفاوت در یک سطح برای یک تست‌کیس → CONFLICT
  * تصمیمِ ردشده‌ای که اگر معتبر بود می‌توانست میزبانِ تست‌کیس را عوض کند →
    NEEDS_REVIEW (هرگز بازگشتِ بی‌صدا به base_url)
  * تصمیمِ سرویسِ گره‌نخورده → UNMATCHED: به هیچ تست‌کیسی داده نمی‌شود، «عادی»
    هم حساب نمی‌شود؛ فقط هشدار و بازبینیِ issue
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from src.agents.test_case_generator.api_discovery import operation_key

BASE_URL = "base_url"
BASE_URL_VDI = "base_url_vdi"
HOST_VARIABLES = (BASE_URL, BASE_URL_VDI)

ENVIRONMENT_NORMAL = "normal"
ENVIRONMENT_VDI = "vdi"
VARIABLE_FOR_ENVIRONMENT = {ENVIRONMENT_NORMAL: BASE_URL, ENVIRONMENT_VDI: BASE_URL_VDI}

SCOPE_TASK = "task"
SCOPE_SERVICE = "service"

# وضعیتِ تست‌کیس — فقط RESOLVED اجراشدنی است.
HOST_RESOLVED = "RESOLVED"
HOST_CONFLICT = "CONFLICT"
HOST_NEEDS_REVIEW = "NEEDS_REVIEW"

# وضعیتِ تصمیم بعد از مرحله‌ی B (قدم ۱ فقط ACCEPTED/REJECTED می‌شناسد).
DECISION_MATCHED = "MATCHED"
DECISION_UNMATCHED = "UNMATCHED"
DECISION_REJECTED = "REJECTED"

BASIS_DEFAULT = "default"
BASIS_TASK = "task"
BASIS_SERVICE = "service"

CAUSE_CONFLICT = "CONFLICT"
CAUSE_REJECTED = "REJECTED"
CAUSE_UNMATCHED = "UNMATCHED"

ISSUE_REVIEW_NONE = "NONE"
ISSUE_REVIEW_NEEDS_REVIEW = "NEEDS_REVIEW"

UNKNOWN_MARKER = "unknown - requires api mapping"


class HostResolutionError(RuntimeError):
    """نتیجه‌ی قدم ۲ انتخابِ میزبان ندارد در حالی که قدم ۱ تصمیمِ محیط دارد."""


# ── کمکی‌های قراردادِ URL ────────────────────────────────────────────────────


def host_reference(variable: str) -> str:
    return "{{" + variable + "}}"


def base_path_of(base_url: str) -> str:
    """مسیرِ پایه‌ی سرورِ Swagger: «http://host/api» → «/api»؛ بدونِ مسیر → ""."""
    path = urlsplit((base_url or "").strip()).path.rstrip("/")
    if path and not path.startswith("/"):
        path = "/" + path
    return path


def strip_base_path(path: str, base_path: str) -> str:
    """«/api/admin/x» با پایه‌ی «/api» → «/admin/x»؛ مسیرِ بدونِ آن پیشوند دست نمی‌خورد."""
    if base_path and (path == base_path or path.startswith(base_path + "/")):
        return path[len(base_path) :] or "/"
    return path


def apply_host(suite: dict, variable: str, base_path: str) -> None:
    """میزبان و مسیرِ پایه را روی همه‌ی تست‌کیس‌های یک suite می‌گذارد (agent.py).

    تست‌کیسی که از قبل base_path دارد (حالتِ سناریو) دست نمی‌خورد.
    """
    for controller in suite.get("controllers") or []:
        for case in controller.get("test_cases") or []:
            case.setdefault("base_path", base_path)
            case["base_url_var"] = variable


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _known(value: object) -> str:
    text = _clean(value)
    return "" if text.lower() == UNKNOWN_MARKER else text


# ── مرحله‌ی B ────────────────────────────────────────────────────────────────


def _case_ids(step1_result: Any) -> list[str]:
    cases = step1_result.get("test_cases") if isinstance(step1_result, dict) else step1_result
    return [
        _clean(case.get("id"))
        for case in cases or []
        if isinstance(case, dict) and _clean(case.get("id"))
    ]


def _decisions(step1_result: Any, key: str) -> list[dict]:
    if not isinstance(step1_result, dict):
        return []
    return [item for item in step1_result.get(key) or [] if isinstance(item, dict)]


def has_environment_decisions(step1_result: Any) -> bool:
    return bool(
        _decisions(step1_result, "environment_decisions")
        or _decisions(step1_result, "rejected_environment_decisions")
    )


class _Linker:
    """تصمیمِ سرویس → تست‌کیس‌ها: متد+مسیر، فقط مسیر، و در آخر شناسه‌های پیشنهادی."""

    def __init__(self, case_ids: list[str], mappings: list[dict], services: list[dict]) -> None:
        self.case_ids = case_ids
        self.operations: dict[str, tuple[str, str]] = {}
        for mapping in mappings or []:
            api = mapping.get("api") if isinstance(mapping, dict) else None
            case_id = _clean(mapping.get("test_case_id")) if isinstance(mapping, dict) else ""
            if case_id and isinstance(api, dict) and case_id not in self.operations:
                self.operations[case_id] = operation_key(
                    _clean(api.get("method")), _clean(api.get("path"))
                )
        self.base_paths = sorted(
            {base_path_of(_clean(s.get("base_url"))) for s in services or [] if isinstance(s, dict)}
            - {""}
        )

    def _paths(self, path: str) -> set[str]:
        normalized = operation_key("", path)[1]
        return {normalized} | {
            operation_key("", strip_base_path(normalized, base))[1] for base in self.base_paths
        }

    def link(self, service: object, hints: object) -> list[str]:
        service = service if isinstance(service, dict) else {}
        method = _known(service.get("method")).upper()
        path = _known(service.get("path"))
        if path:
            paths = self._paths(path)
            linked = [
                case_id
                for case_id in self.case_ids
                if case_id in self.operations
                and self.operations[case_id][1] in paths
                and (not method or self.operations[case_id][0] == method)
            ]
            if linked:
                return linked
        wanted = (
            {_clean(item) for item in hints or [] if isinstance(item, str)}
            if isinstance(hints, list)
            else set()
        )
        return [case_id for case_id in self.case_ids if case_id in wanted]


def resolve_hosts(step1_result: Any, mappings: list[dict], services: list[dict]) -> dict[str, Any]:
    """انتخابِ قطعیِ میزبانِ هر تست‌کیس + وضعیتِ تصمیم‌ها و بازبینیِ issue.

    تابعِ خالص است: خروجی فقط به همین ورودی‌ها بستگی دارد و ترتیبش پایدار است.
    """
    case_ids = _case_ids(step1_result)
    linker = _Linker(case_ids, mappings, services)
    accepted = _decisions(step1_result, "environment_decisions")
    rejected = _decisions(step1_result, "rejected_environment_decisions")

    decisions: list[dict] = []
    service_links: list[tuple[dict, list[str]]] = []
    task_decisions: list[dict] = []
    clarifications: list[str] = []
    warnings: list[str] = []
    issue_reasons: list[dict] = []

    for decision in accepted:
        decision_id = _clean(decision.get("id"))
        if decision.get("scope") == SCOPE_SERVICE:
            linked = linker.link(decision.get("service"), decision.get("test_case_ids"))
            status = DECISION_MATCHED if linked else DECISION_UNMATCHED
            reason = ""
            if linked:
                service_links.append((decision, linked))
            else:
                reason = (
                    f"{decision_id} names {_service_label(decision.get('service'))}, which "
                    "matches no mapped operation or test case of this task."
                )
                clarifications.append(
                    f"The {decision.get('environment')} environment statement {decision_id} "
                    f'("{_evidence(decision)}") could not be tied to any test case: {reason} '
                    "Clarify which service it applies to."
                )
                issue_reasons.append(
                    {"kind": CAUSE_UNMATCHED, "decision_ids": [decision_id], "message": reason}
                )
        else:
            linked = list(case_ids)
            status, reason = DECISION_MATCHED, ""
            task_decisions.append(decision)
        decisions.append(
            {
                "id": decision_id,
                "status": status,
                "scope": decision.get("scope"),
                "environment": decision.get("environment"),
                "matched_test_case_ids": linked,
                "reason": reason,
            }
        )

    explicit = {case_id for _, linked in service_links for case_id in linked}
    rejected_effects: dict[str, list[dict]] = {case_id: [] for case_id in case_ids}
    for decision in rejected:
        decision_id = _clean(decision.get("id"))
        scope = decision.get("scope")
        rejection = decision.get("rejection") or {}
        message = _clean(rejection.get("message")) or "the statement was rejected"
        linked: list[str] = []
        unscoped = False
        if scope == SCOPE_SERVICE:
            linked = linker.link(decision.get("service"), decision.get("test_case_ids"))
            unscoped = not linked
        elif scope == SCOPE_TASK:
            linked = [case_id for case_id in case_ids if case_id not in explicit]
        else:
            unscoped = True
        if unscoped:
            # دامنه نامعلوم است: هر تست‌کیسی که تصمیمِ سرویسِ معتبرِ خودش را ندارد
            # ممکن است همان موردِ منظور باشد.
            linked = [case_id for case_id in case_ids if case_id not in explicit]
            issue_reasons.append(
                {
                    "kind": CAUSE_REJECTED,
                    "decision_ids": [decision_id],
                    "message": f"{decision_id} was rejected ({message}) and its scope cannot be determined safely.",
                }
            )
        for case_id in linked:
            rejected_effects[case_id].append(decision)
        if linked or unscoped:
            clarifications.append(
                f"The environment statement {decision_id} was rejected: {message}. "
                + (
                    "Its scope cannot be determined, so every test case without its own valid "
                    "service-level decision needs review."
                    if unscoped
                    else "Test cases " + ", ".join(linked) + " need review before they can run."
                )
            )
        else:
            warnings.append(
                f"{decision_id} was rejected ({message}) but cannot change any test case's host."
            )
        decisions.append(
            {
                "id": decision_id,
                "status": DECISION_REJECTED,
                "scope": scope if scope in (SCOPE_TASK, SCOPE_SERVICE) else "",
                "environment": decision.get("environment"),
                "matched_test_case_ids": linked,
                "reason": message + ("; scope cannot be determined" if unscoped else ""),
            }
        )

    selections = [
        _select(case_id, service_links, task_decisions, rejected_effects[case_id], clarifications)
        for case_id in case_ids
    ]

    blocked = [item["test_case_id"] for item in selections if item["host_status"] != HOST_RESOLVED]
    if blocked:
        issue_reasons.insert(
            0,
            {
                "kind": "TEST_CASE_REVIEW",
                "decision_ids": [],
                "message": f"{len(blocked)} test case(s) need an environment decision: {', '.join(blocked)}.",
            },
        )
    return {
        "decisions": decisions,
        "selections": selections,
        "issue_review": {
            "status": ISSUE_REVIEW_NEEDS_REVIEW if issue_reasons else ISSUE_REVIEW_NONE,
            "reasons": issue_reasons,
        },
        "clarifications": clarifications,
        "warnings": warnings,
    }


def _select(
    case_id: str,
    service_links: list[tuple[dict, list[str]]],
    task_decisions: list[dict],
    rejected: list[dict],
    clarifications: list[str],
) -> dict[str, Any]:
    service = [decision for decision, linked in service_links if case_id in linked]
    service_envs = sorted({decision.get("environment") for decision in service})
    task_envs = sorted({decision.get("environment") for decision in task_decisions})

    causes: list[dict] = []
    if len(service_envs) > 1 or (not service_envs and len(task_envs) > 1):
        involved = service if service_envs else task_decisions
        ids = [_clean(decision.get("id")) for decision in involved]
        level = "service" if service_envs else "task-wide"
        message = (
            f"Conflicting {level} environment statements ({', '.join(service_envs or task_envs)}) "
            f"apply to {case_id}: {', '.join(ids)}."
        )
        causes.append({"kind": CAUSE_CONFLICT, "decision_ids": ids, "message": message})
        clarifications.append(f"{message} Clarify which environment {case_id} must use.")
    if rejected:
        ids = [_clean(decision.get("id")) for decision in rejected]
        causes.append(
            {
                "kind": CAUSE_REJECTED,
                "decision_ids": ids,
                "message": f"Rejected environment statement(s) {', '.join(ids)} may apply to {case_id}.",
            }
        )

    if causes:
        status = HOST_CONFLICT if causes[0]["kind"] == CAUSE_CONFLICT else HOST_NEEDS_REVIEW
        return {
            "test_case_id": case_id,
            "host_status": status,
            "decision_ids": [item for cause in causes for item in cause["decision_ids"]],
            "review_causes": causes,
        }

    if service_envs:
        environment, basis, used = service_envs[0], BASIS_SERVICE, service
    elif task_envs:
        environment, basis, used = task_envs[0], BASIS_TASK, task_decisions
    else:
        environment, basis, used = ENVIRONMENT_NORMAL, BASIS_DEFAULT, []
    return {
        "test_case_id": case_id,
        "host_status": HOST_RESOLVED,
        "variable": VARIABLE_FOR_ENVIRONMENT[environment],
        "environment": environment,
        "basis": basis,
        "decision_ids": [_clean(decision.get("id")) for decision in used],
        "review_causes": [],
    }


def _service_label(service: object) -> str:
    service = service if isinstance(service, dict) else {}
    method, path, name = (_known(service.get(key)) for key in ("method", "path", "name"))
    operation = " ".join(part for part in (method.upper(), path) if part)
    if operation and name:
        return f"'{name}' ({operation})"
    return f"'{operation or name or 'an unnamed service'}'"


def _evidence(decision: dict) -> str:
    evidence = decision.get("evidence") if isinstance(decision.get("evidence"), dict) else {}
    return _clean(evidence.get("original")) or _clean(evidence.get("as_returned"))


# ── خواندنِ انتخابِ ذخیره‌شده (قدم ۴ و ۵) ───────────────────────────────────


def host_resolution_of(step1_result: Any, step2_result: Any) -> dict[str, Any]:
    """انتخابِ ذخیره‌شده‌ی قدم ۲.

    نتیجه‌ی قدم ۲ که پیش از این قابلیت ساخته شده انتخاب ندارد؛ فقط اگر قدم ۱ هم
    هیچ تصمیمِ محیطی نداشته باشد، همان تابعِ بالا (که آن‌وقت برای همه base_url
    می‌دهد) جایش را می‌گیرد. در غیرِ این صورت باید قدم ۲ دوباره اجرا شود.
    """
    stored = step2_result.get("host_resolution") if isinstance(step2_result, dict) else None
    if isinstance(stored, dict) and isinstance(stored.get("selections"), list):
        return stored
    if has_environment_decisions(step1_result):
        raise HostResolutionError(
            "The Step 2 result has no host selection although Step 1 extracted environment "
            "decisions — rerun Step 2."
        )
    step2 = step2_result if isinstance(step2_result, dict) else {}
    return resolve_hosts(step1_result, step2.get("mappings") or [], step2.get("services") or [])


def selections_by_case(host_resolution: dict[str, Any]) -> dict[str, dict]:
    return {
        _clean(item.get("test_case_id")): item
        for item in host_resolution.get("selections") or []
        if isinstance(item, dict) and _clean(item.get("test_case_id"))
    }


def unresolved_host_reason(selection: dict | None) -> str:
    """دلیلِ ساخته‌نشدنِ درخواست برای تست‌کیسی که میزبانش RESOLVED نیست."""
    if not selection:
        return "No host selection was recorded for this test case in Step 2, so no request was generated."
    messages = " ".join(
        _clean(cause.get("message")) for cause in selection.get("review_causes") or []
    )
    label = (
        "Environment conflict"
        if selection.get("host_status") == HOST_CONFLICT
        else "Environment needs review"
    )
    return f"{label}: {messages} No request was generated until it is clarified."


__all__ = [
    "BASE_URL",
    "BASE_URL_VDI",
    "BASIS_DEFAULT",
    "BASIS_SERVICE",
    "BASIS_TASK",
    "CAUSE_CONFLICT",
    "CAUSE_REJECTED",
    "CAUSE_UNMATCHED",
    "DECISION_MATCHED",
    "DECISION_REJECTED",
    "DECISION_UNMATCHED",
    "ENVIRONMENT_NORMAL",
    "ENVIRONMENT_VDI",
    "HOST_CONFLICT",
    "HOST_NEEDS_REVIEW",
    "HOST_RESOLVED",
    "HOST_VARIABLES",
    "ISSUE_REVIEW_NEEDS_REVIEW",
    "ISSUE_REVIEW_NONE",
    "SCOPE_SERVICE",
    "SCOPE_TASK",
    "VARIABLE_FOR_ENVIRONMENT",
    "HostResolutionError",
    "apply_host",
    "base_path_of",
    "has_environment_decisions",
    "host_reference",
    "host_resolution_of",
    "resolve_hosts",
    "selections_by_case",
    "strip_base_path",
    "unresolved_host_reason",
]
