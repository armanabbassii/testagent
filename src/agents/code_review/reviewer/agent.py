"""
code_review/reviewer/agent.py — ایجنت کد ریویو

prompt از دو منبع ساخته می‌شود:
  ۱. فایل‌های prompts/ (قوانین پایه — همیشه بارگذاری می‌شوند)
  ۲. project_rules/ (قوانین سفارشی پروژه — اختیاری)

قوانین سفارشی:
  - code_review/reviewer  : قوانین کلی پروژه
  - code_review/languages/: قوانین زبان‌محور (python, javascript, ...)
"""

import json
from pathlib import Path
from langchain_core.messages import AIMessage
from src.agents.code_review.state import CodeReviewState, ReviewComment
from src.llm_client import LLMClient
from src.debug import DebugConfig
from src.rules import RuleLoader

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# ترتیب بارگذاری فایل‌های rule — قابل تغییر یا توسعه
_RULE_FILES: list[str] = [
    "rules/security.md",
    "rules/correctness.md",
    "rules/performance.md",
    "rules/style.md",
]


def _build_system_prompt(rule_loader: RuleLoader | None = None,
                         extra_rule_paths: list[str] | None = None) -> str:
    """prompt پایه را از prompts/ می‌خواند و قوانین پروژه را inject می‌کند."""
    # ۱. prompt پایه از prompts/
    parts: list[str] = [
        (_PROMPTS_DIR / "system.md").read_text(encoding="utf-8"),
        "\n---\n\n# Review Rules\n",
    ]
    for rule_file in _RULE_FILES:
        path = _PROMPTS_DIR / rule_file
        parts.append(
            path.read_text(encoding="utf-8") if path.exists()
            else f"<!-- not found: {rule_file} -->"
        )
    base_prompt = "\n\n".join(parts)

    # ۲. قوانین سفارشی پروژه (اگر loader داده شده)
    if rule_loader is None:
        return base_prompt

    rules_to_load = extra_rule_paths or ["code_review/reviewer"]
    return rule_loader.inject(
        base_prompt=base_prompt,
        rule_paths=rules_to_load,
        section_title="## Project-Specific Review Rules",
    )


def _parse_comments(raw: str) -> list[ReviewComment]:
    clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        items = json.loads(clean)
    except json.JSONDecodeError:
        return [ReviewComment(
            file_path="unknown", line=None, severity="minor", category="general",
            body=f"Reviewer could not produce valid JSON. Raw: {raw[:300]}",
            source="llm",
        )]
    return [
        ReviewComment(
            file_path=item.get("file_path", "unknown"),
            line=item.get("line"),
            severity=item.get("severity", "minor"),
            category=item.get("category", "general"),
            body=item.get("body", ""),
            source="llm",
        )
        for item in items
    ]


class CodeReviewerAgent:
    """ایجنت کد ریویو.

    پارامترها:
        debug_config      : تنظیمات لاگ
        rule_loader       : بارگذار قوانین سفارشی پروژه (اختیاری)
        extra_rule_paths  : مسیرهای اضافه برای بارگذاری rule
                            پیش‌فرض: ["code_review/reviewer"]
    """

    name = "code_reviewer"

    def __init__(
        self,
        debug_config: DebugConfig | None = None,
        rule_loader: RuleLoader | None = None,
        extra_rule_paths: list[str] | None = None,
    ) -> None:
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
        self._rule_loader = rule_loader
        self._extra_rule_paths = extra_rule_paths

    def __call__(self, state: CodeReviewState) -> dict:
        self._log.info("شروع ریویو", mr_iid=state["mr_iid"], mr_title=state["mr_title"])
        self._log.debug("اندازه diff", diff_chars=len(state["diff"]))

        llm = LLMClient(user_id=state["user_id"], agent_name=self.name)
        system_prompt = _build_system_prompt(self._rule_loader, self._extra_rule_paths)
        self._log.trace("system prompt ساخته شد", prompt_chars=len(system_prompt))

        user_message = (
            f"## Merge Request: {state['mr_title']}\n\n"
            f"**Description:** {state['mr_description'] or 'N/A'}\n\n"
            f"## Diff\n\n{state['diff']}"
        )

        self._log.debug("ارسال به LLM")
        raw = llm.chat(
            user_message=user_message,
            system_prompt=system_prompt,
            temperature=0.1,
            max_tokens=4096,
        )
        self._log.trace("پاسخ LLM دریافت شد", response_chars=len(raw))

        comments = _parse_comments(raw)
        self._log.debug("parsing انجام شد", comment_count=len(comments))

        by_severity: dict[str, int] = {}
        for c in comments:
            sev = c["severity"]
            by_severity[sev] = by_severity.get(sev, 0) + 1
            self._log.trace("نکته پیدا شد",
                            severity=sev, category=c.get("category"),
                            file=c["file_path"], line=c.get("line"))

        summary = " | ".join(f"{k}: {v}" for k, v in by_severity.items()) or "no issues"
        self._log.info("ریویو کامل شد", total=len(comments), summary=summary)

        return {
            "review_comments": comments,
            "messages": [AIMessage(
                content=f"ریویو انجام شد — {len(comments)} نکته ({summary})",
                name=self.name,
            )],
        }