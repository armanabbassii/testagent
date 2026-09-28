"""
code_review/state.py — State اختصاصی گراف Code Review
"""

from typing import Annotated
from typing_extensions import TypedDict, NotRequired
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


class ReviewComment(TypedDict):
    """یک نکته کد ریویو روی یک فایل و خط خاص."""
    file_path: str
    line: int | None       # None یعنی کامنت کلی روی فایل
    severity: str          # critical | major | minor | suggestion
    category: str          # security | correctness | performance | style | general
    body: str
    source: NotRequired[str]   # "llm" | "sonar" — منبع تولید نکته (برای breakdown امتیاز)
                                # اگر غایب باشد یعنی "llm" (سازگاری با کد قدیمی)


class CodeReviewState(TypedDict):
    """State گراف code review.

    - mr_iid           : شماره Merge Request که کاربر وارد کرده
    - user_id          : شناسه کاربر
    - created_at       : زمان شروع ریویو (ISO format)
    - mr_title         : عنوان MR (از GitLab)
    - mr_description   : توضیحات MR (از GitLab)
    - mr_source_branch : برنچ مبدا MR — برای clone در SonarAnalyzerAgent لازم است
    - diff             : متن کامل diff تغییرات
    - existing_comments: fingerprint کامنت‌های موجود روی MR (برای dedup)
    - review_comments  : لیست نکات کد ریویو (خروجی ترکیبی LLM + Sonar بعد از merge_findings)
    - sonar_issues     : خروجی خام SonarAnalyzerAgent — قبل از merge با review_comments
    - sonar_enabled    : آیا اسکن Sonar برای این ریویو فعال بوده (برای ثبت در scoring)
    - sonar_scan_mode  : "full" | "diff" — نوع فیلتر issue های سونار (وقتی sonar_enabled=True)
    - score_record     : رکورد امتیازدهی (خروجی CommenterAgent)
    - decision         : تصمیم نهایی — "approve" | "reject" | "needs_work"
    - decision_reason  : دلیل تصمیم که به عنوان کامنت ثبت می‌شود
    - messages         : لاگ پیام‌های داخلی گراف
    """

    mr_iid: int
    user_id: str
    created_at: str
    mr_title: str
    mr_description: str
    mr_source_branch: str
    diff: str
    existing_comments: list[str]
    review_comments: list[ReviewComment]
    sonar_issues: list[ReviewComment]
    sonar_enabled: bool
    sonar_scan_mode: str
    score_record: dict
    decision: str
    decision_reason: str
    messages: Annotated[list[BaseMessage], add_messages]