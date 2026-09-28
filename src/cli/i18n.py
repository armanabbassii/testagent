"""
cli/i18n.py — رشته‌های قابل ترجمه CLI

هر رشته با یک کلید در MESSAGES تعریف می‌شود.
برای اضافه کردن زبان جدید، یک دیکشنری جدید بسازید و به LANGUAGES اضافه کنید.
"""

from typing import Literal

Lang = Literal["fa", "en"]
SUPPORTED_LANGS: list[Lang] = ["fa", "en"]
DEFAULT_LANG: Lang = "fa"

_FA: dict[str, str] = {
    # ── عمومی ──────────────────────────────────────────────────────────────
    "app_description": "ابزار خط فرمان برای Local LLM Agent Framework",
    "lang_help": "زبان خروجی (fa یا en)",
    "error_prefix": "خطا",
    "success_prefix": "موفق",

    # ── review ──────────────────────────────────────────────────────────────
    "review_description": "اجرای code review روی یک Merge Request",
    "review_mr_help": "شماره Merge Request",
    "review_hitl_help": "فعال‌سازی Human-in-the-Loop قبل از تصمیم نهایی",
    "review_sonar_mode_help": "نوع اسکن Sonar: full (کل کد) یا diff (فقط خطوط تغییرکرده) — پیش‌فرض: diff",
    "review_enable_sonar": "فعال‌سازی اسکن SonarQube قبل از تصمیم نهایی",
    "review_sonar_note": "اسکن Sonar فعال (حالت: {}) — ممکن است چند دقیقه طول بکشد",
    "review_sonar_running": "در حال اسکن Sonar...",    "review_header": "Code Review — MR #{}",
    "review_hitl_note": "حالت HITL فعال — قبل از تصمیم نهایی تأیید می‌گیرد",
    "review_fetching": "در حال دریافت اطلاعات MR از GitLab...",
    "review_running": "در حال اجرای ریویو...",
    "review_results_header": "نتایج ریویو — {} نکته",
    "review_no_issues": "هیچ مشکلی پیدا نشد.",
    "review_hitl_found": "{} نکته پیدا شد — بررسی کنید و تصمیم بگیرید",
    "review_approved": "تأیید شد — ادامه اجرا...",
    "review_rejected": "رد شد — اجرا لغو شد.",
    "review_score_header": "امتیازدهی",
    "review_total_score": "امتیاز کلی",
    "review_decision_header": "تصمیم نهایی",
    "review_decision_approve": "تأیید (APPROVE)",
    "review_decision_reject": "رد (REJECT)",
    "review_decision_needs_work": "نیاز به اصلاح (NEEDS WORK)",
    "review_hitl_prompt": "ادامه دهم؟",
    "review_hitl_yes": "بله",
    "review_hitl_no": "خیر",

    # ── scores ──────────────────────────────────────────────────────────────
    "scores_description": "نمایش تاریخچه امتیازهای code review",
    "scores_file_help": "مسیر فایل JSONL امتیازها",
    "scores_mr_help": "فیلتر بر اساس شماره MR",
    "scores_last_help": "نمایش N ریویو آخر",
    "scores_header": "تاریخچه امتیازها",
    "scores_total_reviews": "تعداد کل ریویو",
    "scores_avg_score": "میانگین امتیاز",
    "scores_best": "بهترین امتیاز",
    "scores_worst": "بدترین امتیاز",
    "scores_mr_header": "MR #{} — {} ریویو",
    "scores_reviewed_at": "تاریخ ریویو",
    "scores_total_score": "امتیاز",
    "scores_no_records": "هیچ رکوردی پیدا نشد.",
    "scores_file_not_found": "فایل امتیازها پیدا نشد: {}",

    # ── ingest ──────────────────────────────────────────────────────────────
    "ingest_description": "بارگذاری و ذخیره کاتالوگ سرویس‌های بیزینسی در vector store",
    "ingest_file_help": "مسیر فایل JSONL کاتالوگ سرویس‌ها",
    "ingest_collection_help": "نام collection مقصد (پیش‌فرض: business_catalog)",
    "ingest_embed_batch_size_help": "تعداد اسنادی که در هر مرحله به‌صورت هم‌زمان برای تولید بردار (Embedding) پردازش می‌شوند. مقدار پیش فرض 64 ",
    "ingest_header": "Ingest بیزینسی — Collection: {}",
    "ingest_running": "در حال پردازش و ذخیره‌سازی...",
    "ingest_done": "ingest با موفقیت انجام شد",
    "ingest_records": "رکوردهای خام",
    "ingest_chunks": "چانک‌های تولیدشده",
    "ingest_upserted": "ذخیره‌شده در vector store",
    "ingest_technical_description": "بارگذاری و ذخیره سورس جاوا/مستندات فنی در vector store",
    "ingest_technical_path_help": "مسیر پوشه پروژه جاوا/اسپرینگ‌بوت",
    "ingest_technical_collection_help": "نام collection مقصد (پیش‌فرض: technical_catalog)",
    "ingest_technical_no_html_help": "عدم اسکن فایل‌های .html (قالب‌ها)",
    "ingest_technical_header": "Ingest فنی — Collection: {}",
    "ingest_technical_running": "در حال اسکن، چانک‌بندی و ذخیره‌سازی...",

}

_EN: dict[str, str] = {
    # ── general ─────────────────────────────────────────────────────────────
    "app_description": "Command-line tool for Local LLM Agent Framework",
    "lang_help": "Output language (fa or en)",
    "error_prefix": "Error",
    "success_prefix": "Success",

    # ── review ──────────────────────────────────────────────────────────────
    "review_description": "Run code review on a Merge Request",
    "review_mr_help": "Merge Request IID number",
    "review_hitl_help": "Enable Human-in-the-Loop before final decision",
    "review_sonar_mode_help": "Sonar scan mode: full (whole codebase) or diff (changed lines only) — default: diff",
    "review_enable_sonar": "Enable SonarQube scan before final decision",
    "review_sonar_note": "Sonar scan enabled (mode: {}) — this may take a few minutes",
    "review_sonar_running": "Running Sonar scan...",    "review_header": "Code Review — MR #{}",
    "review_hitl_note": "HITL mode active — will ask for approval before final decision",
    "review_fetching": "Fetching MR info from GitLab...",
    "review_running": "Running review...",
    "review_results_header": "Review Results — {} issue(s)",
    "review_no_issues": "No issues found.",
    "review_hitl_found": "{} issue(s) found — review and decide",
    "review_approved": "Approved — continuing execution...",
    "review_rejected": "Rejected — execution cancelled.",
    "review_score_header": "Scoring",
    "review_total_score": "Total Score",
    "review_decision_header": "Final Decision",
    "review_decision_approve": "APPROVED",
    "review_decision_reject": "REJECTED",
    "review_decision_needs_work": "NEEDS WORK",
    "review_hitl_prompt": "Continue?",
    "review_hitl_yes": "yes",
    "review_hitl_no": "no",

    # ── scores ──────────────────────────────────────────────────────────────
    "scores_description": "Show code review score history",
    "scores_file_help": "Path to JSONL scores file",
    "scores_mr_help": "Filter by MR IID",
    "scores_last_help": "Show last N reviews",
    "scores_header": "Score History",
    "scores_total_reviews": "Total reviews",
    "scores_avg_score": "Average score",
    "scores_best": "Best score",
    "scores_worst": "Worst score",
    "scores_mr_header": "MR #{} — {} review(s)",
    "scores_reviewed_at": "Reviewed at",
    "scores_total_score": "Score",
    "scores_no_records": "No records found.",
    "scores_file_not_found": "Scores file not found: {}",

    # ── ingest ──────────────────────────────────────────────────────────────
    "ingest_description": "Load and store business service catalog into vector store",
    "ingest_file_help": "Path to the JSONL service catalog file",
    "ingest_collection_help": "Destination collection name (default: business_catalog)",
    "ingest_embed_batch_size_help": "Specifies the number of documents processed simultaneously in each embedding generation batch. Default: 64",
    "ingest_header": "Business Ingest — Collection: {}",
    "ingest_running": "Processing and storing...",
    "ingest_done": "Ingest completed successfully",
    "ingest_records": "Raw records",
    "ingest_chunks": "Generated chunks",
    "ingest_upserted": "Upserted to vector store",
    "ingest_technical_description": "Load and store Java source / technical docs into vector store",
    "ingest_technical_path_help": "Path to the Java/Spring Boot project folder",
    "ingest_technical_collection_help": "Destination collection name (default: technical_catalog)",
    "ingest_technical_no_html_help": "Skip scanning .html template files",
    "ingest_technical_header": "Technical Ingest — Collection: {}",
    "ingest_technical_running": "Scanning, chunking, and storing...",
}

LANGUAGES: dict[str, dict[str, str]] = {
    "fa": _FA,
    "en": _EN,
}


class Translator:
    """ترجمه رشته‌های CLI بر اساس زبان انتخابی."""

    def __init__(self, lang: Lang = DEFAULT_LANG) -> None:
        if lang not in SUPPORTED_LANGS:
            lang = DEFAULT_LANG
        self._strings = LANGUAGES[lang]
        self.lang = lang

    def t(self, key: str, *args) -> str:
        """رشته ترجمه‌شده را برمی‌گرداند. args برای format استفاده می‌شود."""
        template = self._strings.get(key, f"[{key}]")
        return template.format(*args) if args else template