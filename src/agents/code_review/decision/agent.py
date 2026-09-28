"""
code_review/decision/agent.py — ایجنت تصمیم‌گیرنده

prompt را از دو فایل .md ترکیب می‌کند:
  prompts/system.md  ← دستورالعمل کلی و فرمت خروجی
  prompts/rules.md   ← قوانین تصمیم‌گیری
"""

import json
from pathlib import Path
from langchain_core.messages import AIMessage
from src.agents.code_review.state import CodeReviewState
from src.agents.code_review.gitlab.agent import GitLabClient
from src.llm_client import LLMClient
from src.debug import DebugConfig
from src.rules import RuleLoader

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _build_system_prompt(rule_loader: RuleLoader | None = None,
                         extra_rule_paths: list[str] | None = None) -> str:
    parts = [
        (_PROMPTS_DIR / "system.md").read_text(encoding="utf-8"),
        (_PROMPTS_DIR / "rules.md").read_text(encoding="utf-8"),
    ]
    base_prompt = "\n\n---\n\n".join(parts)

    if rule_loader is None:
        return base_prompt

    rules_to_load = extra_rule_paths or ["gitlab/commenter"]
    return rule_loader.inject(
        base_prompt=base_prompt,
        rule_paths=rules_to_load,
        section_title="## Project-Specific Decision Rules",
    )


def _parse_decision(raw: str) -> tuple[str, str]:
    clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(clean)
        return data.get("decision", "needs_work"), data.get("reason", "")
    except json.JSONDecodeError:
        return "needs_work", f"Could not parse decision output. Raw: {raw[:200]}"


def _format_comments(comments: list) -> str:
    if not comments:
        return "No issues found."
    lines = []
    for i, c in enumerate(comments, 1):
        line_info = f" line {c['line']}" if c.get("line") else ""
        lines.append(f"{i}. [{c['severity'].upper()}] [{c.get('category','general').upper()}] {c['file_path']}{line_info}\n   {c['body']}")
    return "\n\n".join(lines)


def _build_decision_comment(decision: str, reason: str, score_record: dict) -> str:
    """کامنت نهایی شامل تصمیم + خلاصه امتیاز می‌سازد."""
    emoji = {"approve": "✅", "reject": "❌", "needs_work": "🔄"}.get(decision, "🔄")
    label = {"approve": "APPROVED", "reject": "REJECTED", "needs_work": "NEEDS WORK"}.get(decision, decision.upper())

    total = score_record.get("total_score", 0)
    issues = score_record.get("issues", [])
    issue_summary = " | ".join(f"{i['severity'].capitalize()}: {i['count']}" for i in issues) or "No issues"
    return (
        f"## {emoji} Code Review Decision — {label}\n\n"
        f"{reason}\n\n---\n\n"
        f"**Final Score:** `{total}` &nbsp;|&nbsp; {issue_summary}\n\n"
        f"*For full breakdown see the score comment above.*\n\n"
        f"---\n*Reviewed by automated code review agent*"
    )


class DecisionMakerAgent:
    name = "decision_maker"

    def __init__(
        self,
        debug_config: DebugConfig | None = None,
        rule_loader: RuleLoader | None = None,
        extra_rule_paths: list[str] | None = None,
    ) -> None:
        self._gl = GitLabClient()
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)
        self._rule_loader = rule_loader
        self._extra_rule_paths = extra_rule_paths

    def __call__(self, state: CodeReviewState) -> dict:
        comments = state.get("review_comments", [])
        score_record = state.get("score_record", {})

        self._log.info("شروع تصمیم‌گیری", mr_iid=state["mr_iid"],
                       comment_count=len(comments),
                       total_score=score_record.get("total_score"))

        llm = LLMClient(user_id=state["user_id"], agent_name=self.name)
        user_message = (
            f"## Merge Request: {state['mr_title']}\n\n"
            f"**Description:** {state['mr_description'] or 'N/A'}\n\n"
            f"**Total Score:** {score_record.get('total_score', 'N/A')}\n\n"
            f"## Review Comments\n\n{_format_comments(comments)}"
        )

        self._log.debug("ارسال به LLM")
        raw = llm.chat(
            user_message=user_message,
            system_prompt=_build_system_prompt(self._rule_loader, self._extra_rule_paths),
            temperature=0.0,
            max_tokens=1024,
        )
        self._log.trace("پاسخ LLM", response=raw[:200])

        decision, reason = _parse_decision(raw)
        self._log.info("تصمیم گرفته شد", decision=decision, reason_preview=reason[:80])

        self._log.debug("ثبت کامنت تصمیم روی GitLab")
        self._gl.post_comment(state["mr_iid"], _build_decision_comment(decision, reason, score_record))

        # approve یا unapprove
        try:
            if decision == "approve":
                self._gl.approve_mr(state["mr_iid"])
                self._log.info("MR approve شد")
            else:
                self._gl.unapprove_mr(state["mr_iid"])
                self._log.info("MR unapprove شد")
        except Exception as e:
            self._log.warning("approve/unapprove ناموفق", error=str(e))

        label = {"approve": "APPROVED", "reject": "REJECTED", "needs_work": "NEEDS WORK"}.get(decision, decision.upper())
        return {
            "decision": decision,
            "decision_reason": reason,
            "messages": [AIMessage(
                content=f"تصمیم: {label} | امتیاز: {score_record.get('total_score','?')} | {reason[:100]}",
                name=self.name,
            )],
        }