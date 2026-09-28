"""
code_review/merge_findings.py — ترکیب خروجی CodeReviewerAgent و SonarAnalyzerAgent

این node بعد از اجرای موازی code_reviewer و sonar_analyzer اجرا می‌شود و
دو لیست review_comments (LLM) و sonar_issues (استاتیک) را در یک لیست
یکپارچه در review_comments می‌ریزد تا commenter/scorer/decision بدون تغییر
با آن کار کنند.

نکته: در این نسخه هیچ dedup‌ای بین دو منبع انجام نمی‌شود (منابعشان کاملاً
متفاوت است: LLM در برابر static analysis) — اگر در آینده overlap دیده شد،
می‌توان از همان الگوی fingerprint موجود در gitlab/agent.py استفاده کرد.
"""

from langchain_core.messages import AIMessage
from src.agents.code_review.state import CodeReviewState
from src.debug import DebugConfig


class MergeFindingsNode:
    """node ترکیب نتایج LLM review و Sonar static analysis."""

    name = "merge_findings"

    def __init__(self, debug_config: DebugConfig | None = None) -> None:
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)

    def __call__(self, state: CodeReviewState) -> dict:
        llm_comments = state.get("review_comments", [])
        sonar_comments = state.get("sonar_issues", [])
        merged = [*llm_comments, *sonar_comments]

        self._log.info(
            "ترکیب نتایج انجام شد",
            llm_count=len(llm_comments), sonar_count=len(sonar_comments), total=len(merged),
        )

        return {
            "review_comments": merged,
            "messages": [AIMessage(
                content=f"ترکیب نتایج: {len(llm_comments)} از LLM + {len(sonar_comments)} از Sonar = {len(merged)} مورد",
                name=self.name,
            )],
        }