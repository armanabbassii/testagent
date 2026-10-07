"""
api_relevance.py — فیلترِ قطعیِ ارتباط، برای کم‌کردنِ پرامپتِ قدم دوم

مسئله: کشفِ Swagger کامل و قطعی است و باید کامل بماند، ولی فرستادنِ همه‌ی
عملیات‌های کشف‌شده به LLM می‌تواند پرامپت را به ده‌ها هزار توکن برساند (در یک
اجرای واقعی: ۵۵٬۲۶۵ توکن ورودی) و پاسخ را از سقفِ توکنِ خروجی رد کند.

راه‌حل: یک لایه‌ی قطعی و محافظه‌کار بینِ «کاتالوگِ کامل» و «پرامپتِ LLM»:

    Swagger کامل
      → کشفِ قطعی                       (بی‌عوض — api_discovery.py)
      → کاتالوگِ کاملِ کشف‌شده          (بی‌عوض — در دسترسِ برنامه)
      → فیلترِ محافظه‌کارِ ارتباط        ← همین ماژول
      → نامزدهای مرتبط
      → نگاشتِ معناییِ LLM              (api_mapping.py)
      → اعتبارسنجیِ قطعی در برابرِ کاتالوگِ کامل   (بی‌عوض)

چرا این کار امن است:

  * تطبیق فقط اشتراکِ توکنیِ قطعی است — بدونِ فازی، بدونِ امتیاز، بدونِ
    embedding. هیچ چیزی «حدس» زده نمی‌شود و نتیجه تکرارپذیر است.
  * هیچ API ای ساخته نمی‌شود و هیچ تعریفی تغییر نمی‌کند: خروجیِ فیلتر فقط
    ارجاع به همان ``DiscoveredApi`` های اصلی است (کپیِ سطحیِ سرویس، با
    فهرستِ فیلترشده — خودِ آبجکت‌ها دست‌نخورده‌اند).
  * هیچ تست‌کیسی حذف نمی‌شود: فهرستِ تست‌کیس‌ها دست‌نخورده به LLM می‌رود.
  * اگر فیلتر چیزی انتخاب نکند، یا همه‌چیز را انتخاب کند، کاتالوگِ کامل
    برگردانده می‌شود — شکستِ فیلتر هرگز به پرامپتِ ناقص تبدیل نمی‌شود.
  * تست‌کیسی که هیچ اشتراکی با هیچ API ای ندارد حذف نمی‌شود؛ فقط نمی‌تواند
    نامزدِ تازه اضافه کند — و در api_mapping.py دوباره در برابرِ کاتالوگِ
    کامل بررسی می‌شود.
  * اعتبارسنجیِ نهایی همیشه در برابرِ کاتالوگِ کامل انجام می‌شود، پس یک نگاشتِ
    بیرون از نامزدها رد می‌شود، نه اینکه بی‌صدا بپذیرد.

فهرستِ ایست‌واژه‌ها عمداً کوتاه است و فقط واژه‌های دستوری، واژه‌های حمل‌ونقلِ
HTTP که در همه‌ی مسیرها تکرار می‌شوند و فعل‌های عمومیِ تست‌کیس‌ها را در بر
می‌گیرد. اسم‌های دامنه‌ای (مثلِ admin، user، status) عمداً ایست‌واژه نیستند:
نگه‌داشتنِ یک API بی‌ربط کم‌هزینه است، ولی انداختنِ یک API مرتبط پرهزینه است.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from src.agents.test_case_generator.api_discovery import (
    DiscoveredApi,
    DiscoveredService,
)

# توکن‌های کوتاه‌تر از این نادیده گرفته می‌شوند («id»، «to»، «a»). پارامترهای
# مسیر مثلِ {id} هم همین‌جا می‌افتند تا همه‌ی عملیات‌های پارامتردار به همه‌ی
# تست‌کیس‌هایی که کلمه‌ی «id» دارند وصل نشوند.
_MIN_TOKEN_LENGTH = 3

# شناسه‌ها به کلمه شکسته می‌شوند: getVoucherDetails → get voucher details
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# هر چیزِ غیرِ حرف/رقم جداکننده است (زیرخط، خط تیره، اسلش، آکولاد، فاصله).
_SEPARATOR_RE = re.compile(r"[^A-Za-z0-9]+")

# پارامترهای مسیر: {id}، {voucherId} — قبل از توکن‌سازی برداشته می‌شوند.
_PATH_PARAMETER_RE = re.compile(r"\{[^{}]*\}")

# واژه‌هایی که هیچ‌چیز را متمایز نمی‌کنند و اگر بمانند همه‌چیز به همه‌چیز وصل
# می‌شود. عمداً کوتاه: فقط دستور زبان، حمل‌ونقلِ HTTP و فعل‌های عمومیِ تست.
_STOPWORDS = frozenset(
    {
        # دستور زبان
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "by",
        "for", "with", "from", "into", "as", "if", "then", "than", "not",
        "no", "any", "all", "each", "other", "same", "such", "only", "also",
        "more", "most", "less", "least", "but", "so", "can", "should",
        "must", "will", "would", "shall", "may", "might", "have", "has",
        "had", "do", "does", "did", "is", "are", "be", "been", "was", "were",
        "it", "its", "this", "that", "these", "those", "there", "here",
        "your", "you", "our", "their", "them", "they", "his", "her", "who",
        "which", "what", "when", "where", "how", "why", "very", "just",
        "about", "after", "before", "between", "during", "over", "under",
        "again", "once", "out", "up", "down", "off", "own", "too", "both",
        "few", "many", "much", "some", "every", "either", "neither",
        "because", "while", "although", "however", "therefore", "via",
        # حمل‌ونقلِ HTTP — در همه‌ی مسیرها/خلاصه‌ها تکرار می‌شود
        "api", "apis", "http", "https", "json", "xml", "rest", "url", "urls",
        "endpoint", "endpoints", "request", "requests", "response",
        "responses", "header", "headers", "body", "query", "param", "params",
        "parameter", "parameters", "field", "fields", "get", "post", "put",
        "patch", "delete", "head", "options", "null", "true", "false",
        # فعل‌های عمومیِ تست‌کیس — تقریباً در همه‌ی تست‌کیس‌ها هستند
        "test", "tests", "case", "cases", "testcase", "step", "steps",
        "scenario", "scenarios", "given", "verify", "verifies",
        "verification", "check", "checks", "ensure", "ensures", "expected",
        "expect", "expects", "actual", "using", "use", "used", "make",
        "makes", "successfully", "correctly", "properly", "according",
        "following", "below", "above",
    }
)


@dataclass(frozen=True)
class RelevanceSelection:
    """نتیجه‌ی فیلتر: نامزدهای مرتبط به‌همراهِ آمارِ کل.

    ``services`` همان سرویس‌های اصلی است — فقط فهرستِ ``apis`` هرکدام به
    نامزدهای مرتبط محدود شده. اگر فیلتر چیزی کم نکرده باشد، ``services``
    دقیقاً همان فهرستِ ورودی است.
    """

    services: list[DiscoveredService]
    total_apis: int
    candidate_apis: int

    @property
    def filtered(self) -> bool:
        """آیا واقعاً چیزی از پرامپت کم شده است؟"""
        return self.candidate_apis < self.total_apis


def _singular(token: str) -> str:
    """جمعِ ساده را به مفرد برمی‌گرداند — روی هر دو طرف یکسان اعمال می‌شود.

    «vouchers» و «voucher» باید یکی حساب شوند. عمداً ساده است: فقط یک «s»
    پایانی و آن هم وقتی واژه به ss/us/is ختم نشود («status» دست‌نخورده می‌ماند).
    """
    if len(token) >= 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: object) -> set[str]:
    """توکن‌های معنادارِ یک رشته — شناسه‌ها شکسته، ایست‌واژه‌ها حذف."""
    if text is None:
        return set()
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", str(text))
    tokens: set[str] = set()
    for raw in _SEPARATOR_RE.split(spaced):
        token = raw.lower()
        if len(token) < _MIN_TOKEN_LENGTH or token in _STOPWORDS:
            continue
        tokens.add(_singular(token))
    return tokens


def api_tokens(api: DiscoveredApi) -> set[str]:
    """توکن‌های یک عملیات: مسیر (بدونِ پارامترها) + operation_id + خلاصه.

    متدِ HTTP عمداً توکن نمی‌شود — GET و POST روی یک مسیر واژه‌ی متمایزکننده‌ی
    معناداری اضافه نمی‌کنند و فقط هر دو را به یک تست‌کیس وصل می‌کنند.
    """
    path = _PATH_PARAMETER_RE.sub(" ", api.path or "")
    tokens = tokenize(path)
    tokens |= tokenize(api.operation_id)
    tokens |= tokenize(api.summary)
    return tokens


def test_case_tokens(test_case: object) -> set[str]:
    """توکن‌های یک تست‌کیس: همه‌ی رشته‌های داخلش، در هر عمقی.

    عمداً به شکلِ schema وابسته نیست تا تغییرِ schemaی قدم اول این فیلتر را
    نشکند. کلیدها (``id``، ``title``، ...) توکن نمی‌شوند — واژه‌های schema
    چیزی را متمایز نمی‌کنند.
    """
    tokens: set[str] = set()
    for text in _strings_in(test_case):
        tokens |= tokenize(text)
    return tokens


def _strings_in(value: object) -> list[str]:
    """همه‌ی رشته‌ها و عددهای داخلِ یک ساختارِ تودرتو را جمع می‌کند."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, dict):
        collected: list[str] = []
        for item in value.values():
            collected.extend(_strings_in(item))
        return collected
    if isinstance(value, (list, tuple, set)):
        collected = []
        for item in value:
            collected.extend(_strings_in(item))
        return collected
    return []


def select_candidates(
    test_cases: list[dict],
    services: list[DiscoveredService],
) -> RelevanceSelection:
    """نامزدهای مرتبط را انتخاب می‌کند؛ در تردید، کاتالوگِ کامل را برمی‌گرداند.

    هر عملیاتی که اشتراکِ توکنیِ غیرخالی با هیچ تست‌کیسی نداشته باشد از
    پرامپت کنار گذاشته می‌شود — ولی هرگز از برنامه: کاتالوگِ کاملِ کشف‌شده
    دست‌نخورده در نتیجه‌ی قدم دوم می‌ماند و اعتبارسنجی هم با همان انجام می‌شود.
    """
    total = sum(len(service.apis) for service in services)

    case_tokens: set[str] = set()
    for test_case in test_cases:
        case_tokens |= test_case_tokens(test_case)

    selected: list[DiscoveredService] = []
    candidate_count = 0
    for service in services:
        matched = [api for api in service.apis if api_tokens(api) & case_tokens]
        if not matched:
            continue
        # کپیِ سرویس با فهرستِ محدودشده — خودِ DiscoveredApi ها دست‌نخورده‌اند
        # و سرویسِ اصلی هم تغییر نمی‌کند.
        selected.append(replace(service, apis=matched))
        candidate_count += len(matched)

    if not case_tokens or candidate_count == 0 or candidate_count >= total:
        # فیلتر چیزی کم نکرد (یا هیچی پیدا نکرد) → کاتالوگِ کامل، دست‌نخورده.
        return RelevanceSelection(
            services=list(services), total_apis=total, candidate_apis=total
        )

    return RelevanceSelection(
        services=selected, total_apis=total, candidate_apis=candidate_count
    )


__all__ = [
    "RelevanceSelection",
    "api_tokens",
    "select_candidates",
    "test_case_tokens",
    "tokenize",
]
