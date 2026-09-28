"""
cli/commands/review.py — دستور `review`

استفاده:
    python -m src.cli review --mr 42
    python -m src.cli review --mr 42 --hitl
    python -m src.cli review --mr 42 --sonar
    python -m src.cli review --mr 42 --sonar --sonar-mode full
    python -m src.cli review --mr 42 --hitl --sonar --sonar-mode diff --lang en
"""

import uuid
import argparse
from src.cli.i18n import Translator
from src.cli import output as out
from src.checkpointer import make_checkpointer
from src.agents.code_review import CodeReviewState, build_code_review_graph
from src.agents.hitl import HITLHandler
from src.config import SONAR_SCAN_MODE


def add_parser(subparsers, t: Translator, lang_parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "review",
        help=t.t("review_description"),
        parents=[lang_parent],
    )
    parser.add_argument("--mr", type=int, required=True, help=t.t("review_mr_help"))
    parser.add_argument("--hitl", action="store_true", help=t.t("review_hitl_help"))
    parser.add_argument("--sonar", action="store_true", help=t.t("review_enable_sonar"))
    parser.add_argument(
        "--sonar-mode",
        choices=["full", "diff"],
        default=SONAR_SCAN_MODE,
        help=t.t("review_sonar_mode_help"),
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, t: Translator) -> int:
    """اجرای دستور review. کد خروج: 0 موفق، 1 خطا."""
    mr_iid: int = args.mr
    hitl: bool = args.hitl
    enable_sonar: bool = args.sonar
    sonar_mode: str = args.sonar_mode

    out.header(t.t("review_header", mr_iid))
    if hitl:
        out.info(out.dim(f"({t.t('review_hitl_note')})"))
    if enable_sonar:
        out.info(out.dim(f"({t.t('review_sonar_note', sonar_mode)})"))

    initial_state: CodeReviewState = {
        "mr_iid": mr_iid,
        "user_id": _get_user_id(),
        "created_at": "",
        "mr_title": "",
        "mr_description": "",
        "mr_source_branch": "",
        "diff": "",
        "existing_comments": [],
        "review_comments": [],
        "sonar_issues": [],
        "sonar_enabled": False,
        "sonar_scan_mode": sonar_mode,
        "score_record": {},
        "decision": "",
        "decision_reason": "",
        "messages": [],
    }

    try:
        interrupt = ["decision_maker"] if hitl else []

        with make_checkpointer() as checkpointer:
            graph = build_code_review_graph(
                checkpointer=checkpointer,
                interrupt_before=interrupt,
                enable_sonar=enable_sonar,
                sonar_scan_mode=sonar_mode,
            )
            config = {"configurable": {"thread_id": f"cr-{mr_iid}-{uuid.uuid4()}"}}

            out.step(t.t("review_fetching"))
            if enable_sonar:
                out.step(t.t("review_sonar_running"))
            out.step(t.t("review_running"))
            result = graph.invoke(initial_state, config)

            if hitl:
                current = graph.get_state(config)
                comments = current.values.get("review_comments", [])
                out.info(f"\n  {t.t('review_hitl_found', len(comments))}")
                approved = _hitl_prompt(t)
                if approved:
                    out.step(t.t("review_approved"))
                    result = HITLHandler.resume(graph, approved=True, config=config)
                else:
                    out.warning(t.t("review_rejected"))
                    return 0

        _print_results(result, t)
        return 0

    except Exception as e:
        out.error(f"{t.t('error_prefix')}: {e}")
        return 1


def _print_results(result: dict, t: Translator) -> None:
    comments = result.get("review_comments", [])

    out.section(t.t("review_results_header", len(comments)))
    if not comments:
        out.success(t.t("review_no_issues"))
    else:
        for c in comments:
            out.review_comment(
                severity=c["severity"],
                category=c.get("category", "general"),
                file_path=c["file_path"],
                line=c.get("line"),
                body=c["body"],
            )

    score = result.get("score_record", {})
    if score:
        out.section(t.t("review_score_header"))
        out.score_row(t.t("review_total_score"), score.get("total_score", 0))
        for issue in score.get("issues", []):
            label = out.severity_color(issue["severity"], issue["severity"].capitalize())
            out.score_row(label, f"count={issue['count']}  score={issue['score']}")

        sonar = score.get("sonar", {})
        if sonar.get("enabled"):
            out.section(f"{t.t('review_score_header')} — Sonar ({sonar.get('scan_mode')})")
            out.score_row(t.t("review_total_score"), sonar.get("score", 0))
            for issue in sonar.get("issues", []):
                label = out.severity_color(issue["severity"], issue["severity"].capitalize())
                out.score_row(label, f"count={issue['count']}  score={issue['score']}")

    decision = result.get("decision", "")
    if decision:
        out.section(t.t("review_decision_header"))
        decision_labels = {
            "approve":    t.t("review_decision_approve"),
            "reject":     t.t("review_decision_reject"),
            "needs_work": t.t("review_decision_needs_work"),
        }
        label = decision_labels.get(decision, decision.upper())
        print(f"\n  {out.decision_color(decision, label)}")
        reason = result.get("decision_reason", "")
        if reason:
            print(f"\n  {out.dim(reason[:300])}")
    print()


def _hitl_prompt(t: Translator) -> bool:
    yes_words = {t.t("review_hitl_yes"), "y", "yes", "بله", "آره"}
    answer = out.prompt(t.t("review_hitl_prompt"),
                        [t.t("review_hitl_yes"), t.t("review_hitl_no")])
    return answer in yes_words


def _get_user_id() -> str:
    import os
    return os.getenv("CLI_USER_ID", "cli-user")