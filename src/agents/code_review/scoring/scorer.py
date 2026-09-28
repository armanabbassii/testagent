"""
scoring/scorer.py — محاسبه امتیاز کد ریویو و ذخیره در JSONL

امتیازها:
  critical   : -10
  major      : -7
  minor      : -3
  suggestion : 0

خروجی JSONL (یک رکورد JSON per line):
  project_id, merger_request_id, user_id,
  issues (by severity), categories (by category), total_score,
  sonar (breakdown مستقل issue‌های Sonar + enabled/scan_mode — نگاه کنید
  به docstring تابع save_score)،
  created_at, reviewed_at
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from src.agents.code_review.state import ReviewComment

# ── ثابت‌های امتیازدهی ────────────────────────────────────────────────────────
SEVERITY_SCORES: dict[str, int] = {
    "critical":   -10,
    "major":       -7,
    "minor":       -3,
    "suggestion":   0,
}

SEVERITY_ORDER = ["critical", "major", "minor", "suggestion"]
CATEGORY_ORDER = ["security", "correctness", "performance", "style", "general"]


def calculate_score(comments: list[ReviewComment]) -> dict:
    """امتیاز کلی و تفکیک‌شده را از لیست کامنت‌ها محاسبه می‌کند.

    این تابع منبع (source) کامنت‌ها را در نظر نمی‌گیرد — برای breakdown
    اختصاصی Sonar، این تابع را روی زیرلیست فیلترشده (source == "sonar")
    دوباره صدا بزنید (نگاه کنید به save_score).

    برمی‌گرداند:
        {
          "total_score": int,
          "issues": [{"severity": ..., "count": ..., "score": ...}, ...],
          "categories": [{"category": ..., "count": ..., "score": ...}, ...],
        }
    """
    sev_count: dict[str, int] = {s: 0 for s in SEVERITY_ORDER}
    sev_score: dict[str, int] = {s: 0 for s in SEVERITY_ORDER}

    cat_count: dict[str, int] = {c: 0 for c in CATEGORY_ORDER}
    cat_score: dict[str, int] = {c: 0 for c in CATEGORY_ORDER}

    for comment in comments:
        sev = comment.get("severity", "minor")
        cat = comment.get("category", "general")
        pts = SEVERITY_SCORES.get(sev, -1)

        sev_count[sev] = sev_count.get(sev, 0) + 1
        sev_score[sev] = sev_score.get(sev, 0) + pts
        cat_count[cat] = cat_count.get(cat, 0) + 1
        cat_score[cat] = cat_score.get(cat, 0) + pts

    issues = [
        {"severity": s, "count": sev_count[s], "score": sev_score[s]}
        for s in SEVERITY_ORDER
        if sev_count[s] > 0
    ]

    categories = [
        {"category": c, "count": cat_count[c], "score": cat_score[c]}
        for c in CATEGORY_ORDER
        if cat_count[c] > 0
    ]

    total_score = sum(s["score"] for s in issues)

    return {
        "total_score": total_score,
        "issues": issues,
        "categories": categories,
    }


def _build_sonar_summary(
    comments: list[ReviewComment],
    sonar_enabled: bool,
    sonar_scan_mode: str | None,
) -> dict:
    """breakdown مستقل Sonar را از زیرمجموعه‌ای از comments که source=='sonar'
    دارند می‌سازد.

    این تفکیک لازم است چون امتیاز کلی (calculate_score روی همه comments)
    شامل نکات LLM هم می‌شود؛ اینجا فقط سهم Sonar جدا گزارش می‌شود تا معلوم
    باشد چه مقدار از افت امتیاز ناشی از static analysis بوده (و با چه
    scan_mode ای).
    """
    if not sonar_enabled:
        return {"enabled": False, "scan_mode": None, "issue_count": 0,
                "score": 0, "issues": [], "categories": []}

    sonar_comments = [c for c in comments if c.get("source") == "sonar"]
    sonar_data = calculate_score(sonar_comments)

    return {
        "enabled": True,
        "scan_mode": sonar_scan_mode,
        "issue_count": len(sonar_comments),
        "score": sonar_data["total_score"],
        "issues": sonar_data["issues"],
        "categories": sonar_data["categories"],
    }


def save_score(
    project_id: str | int,
    mr_iid: int,
    user_id: str,
    comments: list[ReviewComment],
    created_at: str,
    output_path: str | Path = "review_scores.jsonl",
    sonar_enabled: bool = False,
    sonar_scan_mode: str | None = None,
) -> dict:
    """امتیاز را محاسبه و به فایل JSONL اضافه می‌کند.

    پارامترهای sonar_enabled/sonar_scan_mode برای ثبت جداگانه سهم Sonar
    در امتیاز نهایی هستند (نگاه کنید به _build_sonar_summary). اگر
    sonar_enabled=False باشد، فیلد "sonar" فقط {"enabled": False, ...}
    خالی خواهد بود.

    هر بار یک خط JSON به انتهای فایل append می‌شود (نه overwrite).
    برمی‌گرداند: رکورد کامل که نوشته شد.
    """
    score_data = calculate_score(comments)
    sonar_summary = _build_sonar_summary(comments, sonar_enabled, sonar_scan_mode)

    record = {
        "project_id":        int(project_id),
        "merger_request_id": mr_iid,
        "user_id":           user_id,
        "issues":            score_data["issues"],
        "categories":        score_data["categories"],
        "total_score":       score_data["total_score"],
        "sonar":             sonar_summary,
        "created_at":        created_at,
        "reviewed_at":       datetime.now(timezone.utc).isoformat(),
    }

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return record


def format_score_comment(score_record: dict) -> str:
    """یک متن ارزیابی خوانا برای ثبت به عنوان کامنت روی MR می‌سازد."""
    total = score_record["total_score"]
    issues = score_record["issues"]
    categories = score_record["categories"]
    sonar = score_record.get("sonar", {"enabled": False})

    if total == 0:
        score_line = "🏆 **Score: 0** — No issues found. Clean code!"
    elif total >= -5:
        score_line = f"🟡 **Score: {total}** — Minor issues only."
    elif total >= -15:
        score_line = f"🟠 **Score: {total}** — Several issues need attention."
    else:
        score_line = f"🔴 **Score: {total}** — Significant issues found."

    sev_emoji = {"critical": "🔴", "major": "🟠", "minor": "🟡", "suggestion": "💡"}
    issues_rows = "\n".join(
        f"| {sev_emoji.get(i['severity'], '')} {i['severity'].capitalize()} "
        f"| {i['count']} | {i['score']} |"
        for i in issues
    ) or "| — | 0 | 0 |"

    cat_emoji = {"security": "🔒", "correctness": "✅", "performance": "⚡", "style": "✏️", "general": "📝"}
    cat_rows = "\n".join(
        f"| {cat_emoji.get(c['category'], '')} {c['category'].capitalize()} "
        f"| {c['count']} | {c['score']} |"
        for c in categories
    ) or "| — | 0 | 0 |"

    sonar_section = _format_sonar_section(sonar)

    return (
        f"## 📊 Code Review Score\n\n"
        f"{score_line}\n\n"
        f"### Issues by Severity\n\n"
        f"| Severity | Count | Score |\n"
        f"|----------|-------|-------|\n"
        f"{issues_rows}\n\n"
        f"### Issues by Category\n\n"
        f"| Category | Count | Score |\n"
        f"|----------|-------|-------|\n"
        f"{cat_rows}\n\n"
        f"{sonar_section}"
        f"---\n"
        f"*Reviewed at: {score_record['reviewed_at']}*"
    )


def _format_sonar_section(sonar: dict) -> str:
    """بخش اختصاصی Sonar را برای کامنت امتیاز می‌سازد — اگر فعال نبوده، خالی برمی‌گرداند."""
    if not sonar.get("enabled"):
        return ""

    mode_labels = {"full": "🔍 اسکن کامل پروژه", "diff": "✂️ فقط خطوط تغییرکرده"}
    mode_label = mode_labels.get(sonar.get("scan_mode"), sonar.get("scan_mode", "نامشخص"))

    sonar_issues_rows = "\n".join(
        f"| {i['severity'].capitalize()} | {i['count']} | {i['score']} |"
        for i in sonar.get("issues", [])
    ) or "| — | 0 | 0 |"

    return (
        f"### 🧪 SonarQube ({mode_label})\n\n"
        f"تعداد issue: **{sonar.get('issue_count', 0)}** | "
        f"سهم از امتیاز: **{sonar.get('score', 0)}**\n\n"
        f"| Severity | Count | Score |\n"
        f"|----------|-------|-------|\n"
        f"{sonar_issues_rows}\n\n"
    )