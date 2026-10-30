"""
source_semantics.py — تحلیلِ معناییِ متنِ تسک برای انتخابِ منابعِ Swagger

    SemanticSourceAnalyzer(...).analyze(task_description, sources) → SemanticAnalysis

متنِ تسک ممکن است فارسی، انگلیسی یا آمیخته باشد ولی متادیتای Swagger انگلیسی
است؛ تطبیقِ واژه‌ایِ source_selection.py فارسی را نمی‌فهمد. این ماژول با همان
LLMClientِ پروژه از مدل می‌خواهد بگوید تسک کدام عملیات‌ها را لازم دارد و هر
عملیات در کدام منبع است — **به انگلیسی و با واژه‌های خودِ Swagger**.

این ماژول فقط شکلِ پاسخ را اعتبارسنجی می‌کند. هیچ ادعای مدل بی‌بررسی پذیرفته
نمی‌شود: source_selection.py هر شناسه‌ی منبع را با رجیستری و هر عملیات را با
متادیتای واقعیِ snapshot می‌سنجد و فقط منبعی را انتخاب می‌کند که دست‌کم یک
عملیاتِ تأییدشده داشته باشد. اطمینانِ (confidence) مدل فقط گزارش می‌شود.

خطاها مثلِ قدم ۲: پاسخِ بریده دوباره تلاش نمی‌شود؛ JSON یا شکلِ نامعتبر یک بار
(``MALFORMED_JSON_RETRIES``) با یادآوری دوباره درخواست می‌شود؛ بعد از آن
``SourceSemanticsError``. فراخوان (source_selection) هرگز بعد از شکست حدس نمی‌زند.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.agents.test_case_generator.api_mapping import MALFORMED_JSON_RETRIES
from src.agents.test_case_generator.json_output import (
    JsonExtractionError,
    extract_json_object,
)
from src.config import LLM_MAX_OUTPUT_TOKENS
from src.debug import DebugConfig
from src.llm_client import LLMClient, truncation_message

_PROMPT_PATH = Path(__file__).parent / "prompts" / "step2_source_selection.md"

_INPUT_SECTION_RE = re.compile(r"^={3,}[ \t]*\nINPUT[ \t]*\n={3,}[ \t]*\n", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(task_description|sources)\s*\}\}")

_RETRY_SUFFIX = (
    "CORRECTION REQUIRED:\n"
    "Your previous response was not a valid JSON object with the required fields. "
    "Return exactly one JSON object with the keys relevant_sources, "
    "required_operations, is_cross_service, needs_clarification and "
    "clarification_reason — and nothing else."
)

# سقفِ تعدادِ آیتم‌ها؛ پاسخِ بزرگ‌تر نشانه‌ی خروجیِ بی‌ربط است و رد می‌شود.
_MAX_ITEMS = 50


class SourceSemanticsError(RuntimeError):
    """تحلیلِ معنایی ممکن نشد (پرامپت، شبکه، پاسخِ بریده یا نامعتبر)."""


@dataclass(frozen=True)
class RelevantSource:
    source_id: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class RequiredOperation:
    """یک عملیاتِ لازم از دیدِ مدل — هنوز تأیید نشده."""

    source_id: str
    description: str
    keywords: tuple[str, ...]
    operation: str = ""


@dataclass(frozen=True)
class SemanticAnalysis:
    relevant_sources: tuple[RelevantSource, ...] = ()
    required_operations: tuple[RequiredOperation, ...] = ()
    is_cross_service: bool = False
    needs_clarification: bool = False
    clarification_reason: str = ""


# ── پرامپت ───────────────────────────────────────────────────────────────────

def build_prompt(
    task_description: str, sources: str, prompt_path: Path | str | None = None
) -> tuple[str, str]:
    """(system prompt، پیامِ کاربر) از فایلِ پرامپت با همان قالبِ INPUTِ قدم ۲."""
    text = Path(prompt_path or _PROMPT_PATH).read_text(encoding="utf-8")
    matches = list(_INPUT_SECTION_RE.finditer(text))
    if not matches:
        raise SourceSemanticsError("Source-selection prompt has no INPUT section.")
    system_prompt = text[: matches[-1].start()].strip()
    template = text[matches[-1].end():].strip()
    if set(_PLACEHOLDER_RE.findall(template)) != {"task_description", "sources"}:
        raise SourceSemanticsError(
            "Source-selection prompt INPUT must contain {{task_description}} and {{sources}}."
        )
    values = {"task_description": task_description.strip(), "sources": sources}
    return system_prompt, _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


# ── اعتبارسنجیِ شکلِ پاسخ ────────────────────────────────────────────────────

def _text(value: Any, where: str, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SourceSemanticsError(f"{where} must be a string.")
    if required and not value.strip():
        raise SourceSemanticsError(f"{where} must not be empty.")
    return value.strip()


def _list(data: dict, key: str) -> list:
    value = data.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise SourceSemanticsError(f"'{key}' must be a list.")
    if len(value) > _MAX_ITEMS:
        raise SourceSemanticsError(f"'{key}' has more than {_MAX_ITEMS} items.")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SourceSemanticsError(f"'{key}[{index}]' must be an object.")
    return value


def _flag(data: dict, key: str) -> bool:
    value = data.get(key, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise SourceSemanticsError(f"'{key}' must be true or false.")
    return value


def parse_analysis(data: Any) -> SemanticAnalysis:
    """dictِ پاسخِ مدل → SemanticAnalysis؛ هر انحراف از قرارداد خطاست.

    پرتاب می‌کند:
        SourceSemanticsError : شکلِ پاسخ با قرارداد نخواند
    """
    if not isinstance(data, dict):
        raise SourceSemanticsError("The response must be a JSON object.")
    if "required_operations" not in data:
        raise SourceSemanticsError("The response has no 'required_operations'.")

    relevant = []
    for index, item in enumerate(_list(data, "relevant_sources")):
        where = f"relevant_sources[{index}]"
        confidence = item.get("confidence", 0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise SourceSemanticsError(f"{where}.confidence must be a number.")
        relevant.append(
            RelevantSource(
                _text(item.get("source_id"), f"{where}.source_id", required=True),
                max(0.0, min(1.0, float(confidence))),
                _text(item.get("reason"), f"{where}.reason"),
            )
        )

    operations = []
    for index, item in enumerate(_list(data, "required_operations")):
        where = f"required_operations[{index}]"
        keywords = item.get("relevant_keywords") or []
        if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
            raise SourceSemanticsError(f"{where}.relevant_keywords must be a list of strings.")
        operations.append(
            RequiredOperation(
                _text(item.get("source_id"), f"{where}.source_id", required=True),
                _text(item.get("operation_description"), f"{where}.operation_description"),
                tuple(k.strip() for k in keywords if k.strip()),
                _text(item.get("operation"), f"{where}.operation"),
            )
        )

    return SemanticAnalysis(
        tuple(relevant),
        tuple(operations),
        _flag(data, "is_cross_service"),
        _flag(data, "needs_clarification"),
        _text(data.get("clarification_reason"), "clarification_reason"),
    )


# ── فراخوانیِ LLM ────────────────────────────────────────────────────────────

class SemanticSourceAnalyzer:
    """یک فراخوانیِ LLM (به‌علاوه‌ی حداکثر یک تلاشِ دوباره برای پاسخِ نامعتبر)."""

    name = "source_selection"

    def __init__(
        self,
        *,
        user_id: str,
        debug_config: DebugConfig | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        prompt_path: Path | str | None = None,
    ) -> None:
        self._user_id = user_id
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
        self._temperature = temperature
        self._max_tokens = LLM_MAX_OUTPUT_TOKENS if max_tokens is None else max_tokens
        self._prompt_path = prompt_path

    def analyze(self, task_description: str, sources: str) -> SemanticAnalysis:
        """پرتاب می‌کند: SourceSemanticsError (یا خطای شبکه‌ی LLMClient)."""
        system_prompt, user_message = build_prompt(
            task_description, sources, self._prompt_path
        )
        llm = LLMClient(user_id=self._user_id, agent_name=self.name)
        attempts = 1 + MALFORMED_JSON_RETRIES
        problem = ""
        for attempt in range(1, attempts + 1):
            message = user_message if attempt == 1 else f"{user_message}\n\n{_RETRY_SUFFIX}"
            raw = llm.chat(
                user_message=message,
                system_prompt=system_prompt,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            truncated = truncation_message(raw)
            if truncated:
                raise SourceSemanticsError(truncated)
            try:
                return parse_analysis(extract_json_object(raw))
            except (JsonExtractionError, SourceSemanticsError) as exc:
                problem = str(exc)
                self._log.warning(
                    "پاسخِ تحلیلِ منابع معتبر نبود",
                    attempt=attempt,
                    max_attempts=attempts,
                    response_chars=len(raw or ""),
                )
        raise SourceSemanticsError(
            f"The task analysis response was not valid after {attempts} attempt(s): {problem}"
        )


__all__ = [
    "RelevantSource",
    "RequiredOperation",
    "SemanticAnalysis",
    "SemanticSourceAnalyzer",
    "SourceSemanticsError",
    "build_prompt",
    "parse_analysis",
]
