"""
examples/code_review_basic.py — نمونه Code Review ساده

پیش‌نیاز:
    - GITLAB_URL، GITLAB_TOKEN، GITLAB_PROJECT_ID در .env تنظیم شده باشد

اجرا:
    uv run python examples/code_review_basic.py --mr 42
    uv run python examples/code_review_basic.py --mr 42 --hitl
"""

import argparse
import uuid
from src.agents.code_review import CodeReviewState, build_code_review_graph
from src.agents.hitl import HITLHandler
from src.checkpointer import make_checkpointer
from src.observability.models import TraceContext
from src.observability.observability import Observability  # backend از .env خوانده می‌شود

USER_ID = "user-123"

def _make_initial_state(mr_iid: int) -> CodeReviewState:
    return {
        "mr_iid": mr_iid,
        "user_id": USER_ID,
        "created_at": "",
        "mr_title": "",
        "mr_description": "",
        "mr_source_branch": "",
        "diff": "",
        "existing_comments": [],
        "review_comments": [],
        "sonar_issues": [],
        "sonar_enabled": False,
        "sonar_scan_mode": "diff",
        "score_record": {},
        "decision": "",
        "decision_reason": "",
        "messages": [],
    }


def run_code_review(checkpointer, mr_iid: int, hitl: bool = False) -> None:
    """Code review روی یک Merge Request اجرا می‌کند.

    پارامترها:
        mr_iid : شماره Merge Request
        hitl   : اگر True باشد، قبل از تصمیم نهایی از کاربر تأیید می‌گیرد
    """
    print("=" * 55)
    print(f"🔍  Code Review — MR #{mr_iid}")
    if hitl:
        print("   (حالت HITL — قبل از تصمیم نهایی تأیید می‌گیرد)")
    print("=" * 55 + "\n")

    interrupt = ["decision_maker"] if hitl else []
    graph = build_code_review_graph(
        checkpointer=checkpointer,
        interrupt_before=interrupt,
    )

    obs = Observability.from_env()

    ctx = TraceContext(
        user_id=USER_ID,
        session_id=f"mr-{mr_iid}",
        thread_id=f"cr-{mr_iid}",
        trace_name="code-review",
        metadata={
            "mr_iid": mr_iid,
        },
    )


    config = obs.graph_config(ctx)


    with obs.trace(ctx):

        # اجرا
        print("▶ دریافت اطلاعات MR از GitLab...")
        result = graph.invoke(
            _make_initial_state(mr_iid),
            config=config,
        )

    obs.flush()


    # HITL
    if hitl:
        current = graph.get_state(config)
        comments = current.values.get("review_comments", [])
        print(f"\n📋 {len(comments)} نکته پیدا شد.")
        approved = HITLHandler.prompt_user(current.values)
        result = HITLHandler.resume(graph, approved=approved, config=config)

    # نمایش نتایج
    _print_results(result)


def _print_results(result: dict) -> None:
    comments = result.get("review_comments", [])
    print(f"\n{'─' * 55}")
    print(f"📋 نتایج ریویو — {len(comments)} نکته")
    print(f"{'─' * 55}")

    for c in comments:
        line_info = f" line {c['line']}" if c.get("line") else ""
        cat = c.get("category", "general")
        print(f"\n  [{c['severity'].upper()}][{cat}] {c['file_path']}{line_info}")
        print(f"  {c['body'][:120]}{'...' if len(c['body']) > 120 else ''}")

    score = result.get("score_record", {})
    if score:
        print(f"\n{'─' * 55}")
        print(f"📊 امتیازدهی")
        print(f"{'─' * 55}")
        print(f"  امتیاز کلی: {score.get('total_score', 0)}")
        for i in score.get("issues", []):
            print(f"  {i['severity']:<12}: count={i['count']:2d}  score={i['score']:4d}")

    decision = result.get("decision", "unknown")
    emoji = {"approve": "✅", "reject": "❌", "needs_work": "🔄"}.get(decision, "❓")
    print(f"\n{'─' * 55}")
    print(f"{emoji} تصمیم نهایی: {decision.upper()}")
    reason = result.get("decision_reason", "")
    if reason:
        print(f"   {reason[:250]}{'...' if len(reason) > 250 else ''}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Code Review با GitLab")
    parser.add_argument("--mr", type=int, required=True, help="شماره Merge Request")
    parser.add_argument("--hitl", action="store_true", help="فعال‌سازی Human-in-the-Loop")
    args = parser.parse_args()

    with make_checkpointer() as checkpointer:
        run_code_review(mr_iid=args.mr, hitl=args.hitl, checkpointer=checkpointer)