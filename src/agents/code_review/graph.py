"""
code_review/graph.py — گراف LangGraph برای Code Review

توپولوژی (بدون Sonar):
    START → gitlab_fetcher → code_reviewer → gitlab_commenter → decision_maker → END

توپولوژی (با enable_sonar=True):
    START → gitlab_fetcher ─┬→ code_reviewer  ──┐
                             └→ sonar_analyzer ──┴→ merge_findings → gitlab_commenter → decision_maker → END

debug_config پیش‌فرض از .env خوانده می‌شود (DebugConfig.from_env()).
برای override در تست، یک config دستی پاس بده.

Checkpointer:
    از Redis استفاده می‌شود (پیش‌فرض).
    برای تست بدون Redis، checkpointer را مستقیم پاس بده.
"""

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.base import BaseCheckpointSaver
from src.agents.code_review.state import CodeReviewState
from src.agents.code_review.gitlab import GitLabFetcherAgent, GitLabCommenterAgent
from src.agents.code_review.reviewer import CodeReviewerAgent
from src.agents.code_review.decision import DecisionMakerAgent
from src.debug import DebugConfig
from src.rules import RuleLoader
from src.config import SONAR_SCAN_MODE


def build_code_review_graph(
    checkpointer: BaseCheckpointSaver,
    interrupt_before: list[str] | None = None,
    debug_config: DebugConfig | None = None,
    score_output_path: str = "review_scores.jsonl",
    rule_loader: RuleLoader | None = None,
    reviewer_rule_paths: list[str] | None = None,
    decision_rule_paths: list[str] | None = None,
    enable_sonar: bool = False,
    sonar_scan_mode: str = SONAR_SCAN_MODE,
    sonar_client=None,
):
    """گراف code review را می‌سازد و کامپایل‌شده برمی‌گرداند.

    پارامترها:
        checkpointer        : checkpointer آماده
        interrupt_before    : مثال ["decision_maker"] برای HITL
        debug_config        : اگر None باشد از .env خوانده می‌شود
        score_output_path   : مسیر فایل JSONL
        rule_loader         : بارگذار قوانین سفارشی پروژه (اختیاری)
        reviewer_rule_paths : مسیرهای rule برای reviewer
                              پیش‌فرض: ["code_review/reviewer"]
        decision_rule_paths : مسیرهای rule برای decision maker
                              پیش‌فرض: ["gitlab/commenter"]
        enable_sonar        : اگر True باشد، node موازی sonar_analyzer اضافه
                              می‌شود که کد MR را با SonarQube اسکن می‌کند
                              (docs/architecture/sonarqube.md)
        sonar_scan_mode      : "full" (همه issue‌ها) یا "diff" (فقط خطوط
                              تغییرکرده) — پیش‌فرض از SONAR_SCAN_MODE در .env
        sonar_client         : برای تست — SonarClient آماده به‌جای ساخت داخلی
    """
    cfg = debug_config if debug_config is not None else DebugConfig.from_env()

    fetcher   = GitLabFetcherAgent(debug_config=cfg)
    reviewer  = CodeReviewerAgent(
        debug_config=cfg,
        rule_loader=rule_loader,
        extra_rule_paths=reviewer_rule_paths,
    )
    commenter = GitLabCommenterAgent(debug_config=cfg, score_output_path=score_output_path)
    decision  = DecisionMakerAgent(
        debug_config=cfg,
        rule_loader=rule_loader,
        extra_rule_paths=decision_rule_paths,
    )

    builder = StateGraph(CodeReviewState)
    builder.add_node(fetcher.name,   fetcher)
    builder.add_node(reviewer.name,  reviewer)
    builder.add_node(commenter.name, commenter)
    builder.add_node(decision.name,  decision)
    builder.add_edge(START, fetcher.name)

    if enable_sonar:
        from src.agents.code_review.sonar import SonarAnalyzerAgent
        from src.agents.code_review.merge_findings import MergeFindingsNode

        sonar = SonarAnalyzerAgent(debug_config=cfg, sonar_client=sonar_client, scan_mode=sonar_scan_mode)
        merge = MergeFindingsNode(debug_config=cfg)
        builder.add_node(sonar.name, sonar)
        builder.add_node(merge.name, merge)

        builder.add_edge(fetcher.name, reviewer.name)   # اجرای موازی
        builder.add_edge(fetcher.name, sonar.name)
        builder.add_edge(reviewer.name, merge.name)     # fan-in
        builder.add_edge(sonar.name,    merge.name)
        builder.add_edge(merge.name,    commenter.name)
    else:
        builder.add_edge(fetcher.name,  reviewer.name)
        builder.add_edge(reviewer.name, commenter.name)

    builder.add_edge(commenter.name, decision.name)
    builder.add_edge(decision.name,  END)

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before or [],
    )