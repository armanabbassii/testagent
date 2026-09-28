"""
cli/commands/scores.py — دستور `scores`

استفاده:
    python -m src.cli scores
    python -m src.cli scores --mr 42
    python -m src.cli scores --last 5
    python -m src.cli scores --last 5 --lang en
    python -m src.cli scores --file path/to/scores.jsonl --lang fa
"""

import json
import argparse
from pathlib import Path
from src.cli.i18n import Translator
from src.cli import output as out


def add_parser(subparsers, t: Translator, lang_parent: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser(
        "scores",
        help=t.t("scores_description"),
        parents=[lang_parent],
    )
    parser.add_argument("--file", default="review_scores.jsonl", help=t.t("scores_file_help"))
    parser.add_argument("--mr", type=int, default=None, help=t.t("scores_mr_help"))
    parser.add_argument("--last", type=int, default=None, help=t.t("scores_last_help"))
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, t: Translator) -> int:
    path = Path(args.file)

    if not path.exists():
        out.warning(t.t("scores_file_not_found", args.file))
        return 1

    records = _load(path)
    if not records:
        out.info(t.t("scores_no_records"))
        return 0

    if args.mr is not None:
        records = [r for r in records if r.get("merger_request_id") == args.mr]

    if args.last is not None:
        records = records[-args.last:]

    if not records:
        out.info(t.t("scores_no_records"))
        return 0

    out.header(t.t("scores_header"))

    if args.mr is not None:
        _print_mr_scores(records, args.mr, t)
    else:
        _print_summary(records, t)
        _print_records(records, t)

    return 0


def _load(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def _print_summary(records: list[dict], t: Translator) -> None:
    scores = [r.get("total_score", 0) for r in records]
    out.section(t.t("scores_header"))
    out.score_row(t.t("scores_total_reviews"), len(records))
    out.score_row(t.t("scores_avg_score"), f"{sum(scores) / len(scores):.1f}")
    out.score_row(t.t("scores_best"), max(scores))
    out.score_row(t.t("scores_worst"), min(scores))


def _print_records(records: list[dict], t: Translator) -> None:
    for r in records:
        mr = r.get("merger_request_id", "?")
        score = r.get("total_score", 0)
        reviewed_at = r.get("reviewed_at", "")[:19]
        score_colored = out.decision_color(
            "approve" if score >= -5 else ("needs_work" if score >= -15 else "reject"),
            str(score),
        )
        print(f"\n  MR #{mr}  {out.bold(score_colored)}  {out.dim(reviewed_at)}")
        for issue in r.get("issues", []):
            sev = issue["severity"]
            print(f"    {out.severity_color(sev, f'{sev:<12}')}  count={issue['count']}  score={issue['score']}")


def _print_mr_scores(records: list[dict], mr_iid: int, t: Translator) -> None:
    out.section(t.t("scores_mr_header", mr_iid, len(records)))
    for r in records:
        score = r.get("total_score", 0)
        reviewed_at = r.get("reviewed_at", "")[:19]
        out.score_row(t.t("scores_reviewed_at"), reviewed_at)
        out.score_row(t.t("scores_total_score"), score)
        for issue in r.get("issues", []):
            sev = issue["severity"]
            print(f"    {out.severity_color(sev, f'  {sev}'):<20}  count={issue['count']}  score={issue['score']}")
        print()