"""
examples/code_review_scores.py — خواندن و نمایش تاریخچه امتیازها

فایل review_scores.jsonl را می‌خواند و آمار نمایش می‌دهد.

اجرا:
    uv run python examples/code_review_scores.py
    uv run python examples/code_review_scores.py --file path/to/scores.jsonl
    uv run python examples/code_review_scores.py --mr 42
"""

import argparse
import json
from pathlib import Path
from collections import defaultdict


def load_scores(file_path: str) -> list[dict]:
    path = Path(file_path)
    if not path.exists():
        print(f"⚠️  فایل پیدا نشد: {file_path}")
        return []
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def show_summary(records: list[dict]) -> None:
    """آمار کلی همه ریویوها."""
    if not records:
        print("هیچ رکوردی پیدا نشد.")
        return

    print(f"{'─' * 55}")
    print(f"📊 آمار کلی — {len(records)} ریویو")
    print(f"{'─' * 55}")

    total_scores = [r.get("total_score", 0) for r in records]
    print(f"  میانگین امتیاز : {sum(total_scores) / len(total_scores):.1f}")
    print(f"  بهترین امتیاز  : {max(total_scores)}")
    print(f"  بدترین امتیاز  : {min(total_scores)}")

    # جمع severity ها
    sev_totals: dict[str, int] = defaultdict(int)
    for r in records:
        for issue in r.get("issues", []):
            sev_totals[issue["severity"]] += issue["count"]

    print(f"\n  تجمیع مشکلات:")
    for sev in ["critical", "major", "minor", "suggestion"]:
        count = sev_totals.get(sev, 0)
        if count:
            print(f"    {sev:<12}: {count}")
    print()


def show_mr(records: list[dict], mr_iid: int) -> None:
    """نمایش ریویوهای یک MR خاص."""
    mr_records = [r for r in records if r.get("merger_request_id") == mr_iid]
    if not mr_records:
        print(f"هیچ ریویویی برای MR #{mr_iid} پیدا نشد.")
        return

    print(f"{'─' * 55}")
    print(f"📋 MR #{mr_iid} — {len(mr_records)} ریویو")
    print(f"{'─' * 55}")

    for r in mr_records:
        print(f"\n  ریویو در: {r.get('reviewed_at', 'N/A')}")
        print(f"  امتیاز  : {r.get('total_score', 0)}")
        for issue in r.get("issues", []):
            print(f"    {issue['severity']:<12}: count={issue['count']}  score={issue['score']}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="نمایش تاریخچه امتیازهای ریویو")
    parser.add_argument("--file", default="review_scores.jsonl", help="مسیر فایل JSONL")
    parser.add_argument("--mr", type=int, help="فیلتر بر اساس شماره MR")
    args = parser.parse_args()

    records = load_scores(args.file)

    if args.mr:
        show_mr(records, args.mr)
    else:
        show_summary(records)