"""
api_mapping.py — نگاشتِ تست‌کیس‌های قدم اول به عملیات‌های واقعیِ API

جریانِ قدم دوم:

    تست‌کیس‌ها (قدم ۱) + سرویس‌های کشف‌شده (api_discovery)
        → LLM فقط برای «معنای کسب‌وکاری» و «انتخابِ عملیات»
        → اعتبارسنجیِ سخت‌گیرانه در برابر عملیات‌های واقعی
        → نتیجه‌ی ساختاریافته‌ی قدم دوم برای بازبینیِ انسانی

قاعده‌ی اصلی: LLM هیچ اطلاعاتِ Swagger ای نمی‌سازد. هر نگاشتی که به عملیاتی
اشاره کند که در سند وجود ندارد خطا است — نه یک نگاشتِ کم‌اعتماد. اگر نگاشت
قابلِ تعیین نباشد، `api` باید null بماند و ابهام برای انسان گزارش شود.

این ماژول هیچ‌چیز را اجرا نمی‌کند: نه API ای صدا زده می‌شود، نه Postman ساخته
می‌شود و نه متغیرِ زمانِ اجرا استخراج می‌شود.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from typing_extensions import TypedDict

from src.agents.test_case_generator.api_discovery import (
    DiscoveredApi,
    DiscoveredService,
    SwaggerLoadError,
    discover_services,
    operation_ids,
    operation_index,
    operation_key,
)
from src.agents.test_case_generator.json_output import (
    JsonExtractionError,
    extract_json_object,
)
from src.debug import DebugConfig
from src.llm_client import LLMClient

_PROMPT_PATH = Path(__file__).parent / "prompts" / "step2_api_mapping.md"

# مقادیرِ مجازِ اطمینانِ نگاشت
_VALID_CONFIDENCE = ("high", "medium", "low")

# متدهای HTTP ای که یک نگاشت مجاز است به آن‌ها اشاره کند
_VALID_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE")

# بخشِ INPUT در فایلِ پرامپت
_INPUT_SECTION_RE = re.compile(
    r"^={3,}[ \t]*\nINPUT[ \t]*\n={3,}[ \t]*\n", re.MULTILINE
)

# جای‌نگهدارهای فایلِ پرامپتِ قدم دوم
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(test_cases|discovered_apis)\s*\}\}")

_PROMPT_PLACEHOLDERS = ("test_cases", "discovered_apis")


class ApiMappingError(RuntimeError):
    """ورودیِ نامعتبر قدم دوم یا نگاشتی که با قرارداد نمی‌خواند."""


class MappedApi(TypedDict):
    """اشاره به یک عملیاتِ کشف‌شده."""

    method: str
    path: str
    operation_id: str


class ApiMapping(TypedDict):
    """نگاشتِ یک تست‌کیس به یک عملیات — یا null وقتی نگاشت قابلِ تعیین نیست."""

    test_case_id: str
    api: MappedApi | None
    confidence: str
    reason: str
    clarification: str


class Step2Result(TypedDict):
    services: list[dict[str, Any]]
    mappings: list[ApiMapping]
    clarifications: list[str]


# ── ساختِ prompt ─────────────────────────────────────────────────────────────

def split_prompt(text: str) -> tuple[str, str]:
    """فایلِ پرامپتِ قدم دوم را به (system prompt، قالبِ پیامِ کاربر) تقسیم می‌کند.

    پرتاب می‌کند:
        ApiMappingError : بخشِ INPUT نباشد یا جای‌نگهدارها کامل نباشند
    """
    matches = list(_INPUT_SECTION_RE.finditer(text))
    if not matches:
        raise ApiMappingError(
            "Prompt file has no INPUT section — expected a line 'INPUT' between "
            "two '====' separators."
        )

    match = matches[-1]
    system_prompt = text[: match.start()].strip()
    user_template = text[match.end() :].strip()
    if not system_prompt or not user_template:
        raise ApiMappingError(
            "Prompt file has an empty system prompt or an empty INPUT section."
        )

    found = set(_PLACEHOLDER_RE.findall(user_template))
    missing = set(_PROMPT_PLACEHOLDERS) - found
    if missing:
        raise ApiMappingError(
            "Prompt INPUT section is missing placeholder(s): "
            + ", ".join(sorted(f"{{{{{name}}}}}" for name in missing))
        )
    return system_prompt, user_template


def render_prompt(
    template: str,
    test_cases: list[dict],
    services: list[DiscoveredService],
) -> str:
    """قالب را با تست‌کیس‌ها و فهرستِ عملیات‌های کشف‌شده پر می‌کند."""
    values = {
        "test_cases": json.dumps(test_cases, ensure_ascii=False, indent=2),
        "discovered_apis": json.dumps(
            [_service_for_prompt(service) for service in services],
            ensure_ascii=False,
            indent=2,
        ),
    }
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


def build_prompt(
    prompt_path: Path | str,
    test_cases: list[dict],
    services: list[DiscoveredService],
) -> tuple[str, str]:
    """(system prompt، پیامِ کاربرِ ساخته‌شده) را از فایلِ پرامپت برمی‌گرداند."""
    text = Path(prompt_path).read_text(encoding="utf-8")
    system_prompt, template = split_prompt(text)
    return system_prompt, render_prompt(template, test_cases, services)


def _service_for_prompt(service: DiscoveredService) -> dict[str, Any]:
    """نمای فشرده‌ی یک سرویس برای پرامپت.

    LLM فقط به method/path/operationId/summary نیاز دارد تا معنا را تطبیق دهد؛
    پارامترها و بدنه‌های کامل به پرامپت فرستاده نمی‌شوند.
    """
    return {
        "name": service.name,
        "base_url": service.base_url,
        "operations": [
            {
                "method": api.method,
                "path": api.path,
                "operation_id": api.operation_id,
                "summary": api.summary,
            }
            for api in service.apis
        ],
    }


# ── کمکی‌های اعتبارسنجی ──────────────────────────────────────────────────────

def _clean(value: object) -> str:
    """رشته را trim می‌کند؛ هر چیزِ دیگری رشته‌ی خالی می‌شود."""
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: object, where: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        errors.append(f"{where}: must be an array of strings.")
        return []
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"{where}[{index}]: must be a string.")
            continue
        if item.strip():
            items.append(item.strip())
    return items


def _resolve_operation(
    reference: object,
    index: dict[tuple[str, str], DiscoveredApi],
    by_operation_id: dict[str, list[DiscoveredApi]],
) -> DiscoveredApi | None:
    """اشاره‌ی LLM را به یک عملیاتِ واقعیِ کشف‌شده resolve می‌کند.

    اول با (متد، مسیر) و در صورتِ نبود با operationId تطبیق داده می‌شود. اگر
    عملیات پیدا نشود یا متد با عملیاتِ پیدا‌شده نخواند، None برمی‌گردد تا
    نگاشتِ ساختگی هرگز پذیرفته نشود.
    """
    if not isinstance(reference, dict):
        return None

    method = _clean(reference.get("method")).upper()
    path = _clean(reference.get("path"))
    operation_id = _clean(reference.get("operation_id"))

    matched: DiscoveredApi | None = None
    if method and path:
        matched = index.get(operation_key(method, path))

    if matched is None and operation_id:
        candidates = by_operation_id.get(operation_id, [])
        matched = candidates[0] if len(candidates) == 1 else None

    if matched is None:
        return None
    if method and matched.method.upper() != method:
        return None
    return matched


# ── اعتبارسنجیِ خروجیِ LLM ───────────────────────────────────────────────────

def validate_mapping_payload(
    data: dict,
    *,
    test_cases: list[dict],
    services: list[DiscoveredService],
) -> tuple[list[ApiMapping], list[str]]:
    """خروجیِ JSON مدل را اعتبارسنجی و نرمال می‌کند.

    همه‌ی خطاها یک‌جا جمع و در یک پیام گزارش می‌شوند. نگاشت باید:

      - برای هر تست‌کیس دقیقاً یک ورودی داشته باشد
      - به تست‌کیسی اشاره نکند که وجود ندارد
      - در صورتِ اشاره به API، همان عملیاتِ کشف‌شده را نام ببرد

    پرتاب می‌کند:
        ApiMappingError : خروجی با قرارداد نخواند
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ApiMappingError("LLM output for Step 2 is not a JSON object.")

    clarifications = _string_list(data.get("clarifications"), "clarifications", errors)

    index = operation_index(services)
    by_operation_id = operation_ids(services)

    known_ids = [str(case.get("id", "")) for case in test_cases]
    mappings: list[ApiMapping] = []
    seen: set[str] = set()

    raw_mappings = data.get("mappings")
    if not isinstance(raw_mappings, list) or not raw_mappings:
        errors.append("'mappings' must be a non-empty array.")
        raw_mappings = []

    for position, raw in enumerate(raw_mappings):
        where = f"mappings[{position}]"
        if not isinstance(raw, dict):
            errors.append(f"{where}: expected an object.")
            continue

        case_id = _clean(raw.get("test_case_id"))
        if not case_id:
            errors.append(f"{where}: 'test_case_id' is missing or empty.")
        elif case_id not in known_ids:
            errors.append(f"{where}: unknown test_case_id {case_id!r}.")
        elif case_id in seen:
            errors.append(f"{where}: duplicate mapping for {case_id!r}.")
        if case_id:
            seen.add(case_id)

        confidence = _clean(raw.get("confidence")).lower()
        if confidence not in _VALID_CONFIDENCE:
            errors.append(
                f"{where}: 'confidence' must be one of {_VALID_CONFIDENCE}, "
                f"got {raw.get('confidence')!r}."
            )

        reason = _clean(raw.get("reason"))
        if not reason:
            errors.append(f"{where}: 'reason' is missing or empty.")

        raw_api = raw.get("api")
        api: MappedApi | None = None
        if raw_api is not None:
            resolved = _resolve_operation(raw_api, index, by_operation_id)
            if resolved is None:
                errors.append(
                    f"{where}: 'api' does not match any operation discovered from "
                    f"Swagger: {raw_api!r}."
                )
            else:
                api = MappedApi(
                    method=resolved.method.upper(),
                    path=resolved.path,
                    operation_id=resolved.operation_id,
                )

        mappings.append(
            ApiMapping(
                test_case_id=case_id,
                api=api,
                confidence=confidence if confidence in _VALID_CONFIDENCE else "low",
                reason=reason,
                clarification=_clean(raw.get("clarification")),
            )
        )

    if not errors:
        unmapped = [case_id for case_id in known_ids if case_id not in seen]
        if unmapped:
            errors.append(
                "no mapping was returned for test case(s): " + ", ".join(unmapped)
            )

    if errors:
        raise ApiMappingError(
            "LLM output does not match the required Step 2 mapping format:\n  - "
            + "\n  - ".join(errors)
        )

    order = {case_id: position for position, case_id in enumerate(known_ids)}
    mappings.sort(key=lambda mapping: order.get(mapping["test_case_id"], 0))
    return mappings, clarifications


# ── استخراجِ ورودیِ قدم اول ──────────────────────────────────────────────────

def extract_test_cases(step1_result: Any) -> list[dict]:
    """تست‌کیس‌ها را از نتیجه‌ی قدم اول بیرون می‌کشد.

    هم نتیجه‌ی کاملِ قدم اول و هم یک لیستِ خامِ تست‌کیس پذیرفته می‌شود.

    پرتاب می‌کند:
        ApiMappingError : ساختار نامعتبر باشد یا هیچ تست‌کیسی نداشته باشد
    """
    if isinstance(step1_result, list):
        cases: object = step1_result
    elif isinstance(step1_result, dict):
        cases = step1_result.get("test_cases")
    else:
        raise ApiMappingError("Step 1 result must be a JSON object or an array.")

    if not isinstance(cases, list) or not cases:
        raise ApiMappingError("Step 1 result does not contain any test case.")

    test_cases: list[dict] = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ApiMappingError(f"test_cases[{index}] is not an object.")
        case_id = str(case.get("id") or "").strip()
        if not case_id:
            raise ApiMappingError(f"test_cases[{index}] has no 'id'.")
        if case_id in seen:
            raise ApiMappingError(f"duplicate test case id {case_id!r}.")
        seen.add(case_id)
        test_cases.append(case)
    return test_cases


def build_result(
    services: list[DiscoveredService],
    mappings: list[ApiMapping],
    clarifications: list[str],
) -> Step2Result:
    """نتیجه‌ی نهاییِ ساختاریافته‌ی قدم دوم را می‌سازد."""
    return Step2Result(
        services=[service.to_dict() for service in services],
        mappings=mappings,
        clarifications=clarifications,
    )


# ── عاملِ نگاشت ──────────────────────────────────────────────────────────────

class ApiMappingAgent:
    """تست‌کیس‌ها را به عملیات‌های کشف‌شده نگاشت می‌کند.

    تنها وظیفه‌ی LLM اینجا «فهمِ معنا و انتخابِ عملیات» است؛ ساختارِ Swagger از
    قبل به‌صورتِ قطعی استخراج شده و مدل اجازه‌ی افزودن به آن را ندارد.
    """

    name = "api_mapping"

    def __init__(
        self,
        debug_config: DebugConfig | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        prompt_path: Path | str | None = None,
    ) -> None:
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
        self._prompt_path = Path(prompt_path) if prompt_path else _PROMPT_PATH
        self._temperature = temperature
        self._max_tokens = max_tokens

    def map_test_cases(
        self,
        test_cases: list[dict],
        services: list[DiscoveredService],
        user_id: str,
    ) -> tuple[list[ApiMapping], list[str]]:
        """(نگاشت‌ها، ابهام‌های گزارش‌شده) را برمی‌گرداند."""
        system_prompt, user_message = build_prompt(
            self._prompt_path, test_cases, services
        )
        self._log.info(
            "شروع نگاشتِ تست‌کیس به API",
            user_id=user_id,
            test_cases=len(test_cases),
            operations=sum(len(service.apis) for service in services),
        )
        self._log.trace(
            "prompt نگاشت ساخته شد",
            system_chars=len(system_prompt),
            user_chars=len(user_message),
        )

        llm = LLMClient(user_id=user_id, agent_name=self.name)
        raw = llm.chat(
            user_message=user_message,
            system_prompt=system_prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        self._log.trace("پاسخ LLM دریافت شد", response_chars=len(raw or ""))

        try:
            data = extract_json_object(raw)
        except JsonExtractionError as exc:
            raise ApiMappingError(str(exc)) from exc

        return validate_mapping_payload(
            data, test_cases=test_cases, services=services
        )


class Step2ApiMappingGenerator:
    """قدم دوم: کشفِ API از Swagger و نگاشتِ تست‌کیس‌های قدم اول به آن‌ها.

    پارامترها:
        debug_config : تنظیماتِ لاگِ پروژه
        temperature  : دمای LLM — صفر، چون انتخابِ عملیات باید تکرارپذیر باشد
        max_tokens   : سقفِ توکنِ پاسخ
        prompt_path  : مسیرِ فایلِ پرامپت (برای تست قابلِ جایگزینی است)
        session      : نشستِ HTTP برای دریافتِ سند (برای تست قابلِ جایگزینی است)
    """

    name = "step2_api_mapping"

    def __init__(
        self,
        debug_config: DebugConfig | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        prompt_path: Path | str | None = None,
        session: Any = None,
    ) -> None:
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
        self._discovery_session = session
        self._agent = ApiMappingAgent(
            debug_config=debug_config,
            temperature=temperature,
            max_tokens=max_tokens,
            prompt_path=prompt_path,
        )

    def generate(
        self,
        step1_result: Any,
        service_sources: list[str],
        user_id: str,
    ) -> Step2Result:
        """نتیجه‌ی کاملِ قدم دوم را می‌سازد.

        پرتاب می‌کند:
            ValueError      : ورودیِ خالی باشد
            ApiMappingError : سرویسی کشف نشود یا نگاشت با قرارداد نخواند
        """
        test_cases = extract_test_cases(step1_result)

        sources = [source.strip() for source in service_sources if source.strip()]
        if not sources:
            raise ValueError("No Swagger source was provided — nothing to discover.")

        self._log.info(
            "شروع کشفِ API", user_id=user_id, sources=len(sources)
        )
        services, load_errors = discover_services(sources, session=self._discovery_session)
        if not services:
            raise ApiMappingError(
                "No Swagger document could be loaded:\n  - " + "\n  - ".join(load_errors)
            )

        operations = sum(len(service.apis) for service in services)
        self._log.info(
            "کشفِ API کامل شد",
            services=len(services),
            operations=operations,
            load_errors=len(load_errors),
        )

        mappings, clarifications = self._agent.map_test_cases(
            test_cases, services, user_id
        )

        counts = Counter(
            mapping["confidence"] for mapping in mappings if mapping["api"]
        )
        self._log.info(
            "نگاشت کامل شد",
            total=len(mappings),
            mapped=sum(1 for mapping in mappings if mapping["api"]),
            high=counts.get("high", 0),
            medium=counts.get("medium", 0),
        )
        if any(mapping["api"] is None for mapping in mappings):
            self._log.warning(
                "برخی تست‌کیس‌ها نگاشت نشدند — برای بازبینیِ انسانی گزارش شدند",
                unmapped=sum(1 for mapping in mappings if mapping["api"] is None),
            )

        return build_result(services, mappings, list(load_errors) + clarifications)


__all__ = [
    "ApiMapping",
    "ApiMappingAgent",
    "ApiMappingError",
    "MappedApi",
    "Step2ApiMappingGenerator",
    "Step2Result",
    "SwaggerLoadError",
    "build_prompt",
    "build_result",
    "extract_test_cases",
    "render_prompt",
    "split_prompt",
    "validate_mapping_payload",
]
