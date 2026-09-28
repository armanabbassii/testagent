"""
security/prompt_guard.py — محافظت در برابر Prompt Injection

دو لایه محافظت:
  ۱. sanitize_input()  : بررسی ورودی کاربر قبل از درج در prompt
  ۲. validate_output() : بررسی خروجی مدل برای تشخیص نشت اطلاعات

سطح‌بندی (GuardLevel):
  OFF      : غیرفعال (پیش‌فرض برای production تنظیم‌شده)
  LENIENT  : فقط الگوهای آشکار و خطرناک
  MODERATE : تعادل بین امنیت و usability
  STRICT   : حساس‌ترین حالت — ممکن است false positive داشته باشد

نحوه استفاده:
    guard = PromptGuard.from_env()
    safe_input = guard.sanitize_input(user_message)
    # اگر خطر جدی باشد PromptInjectionError پرتاب می‌شود
    # اگر خطر ملایم باشد، ورودی sanitize شده برمی‌گردد
"""

import os
import re
from enum import IntEnum
from dataclasses import dataclass, field


# ── خطا ──────────────────────────────────────────────────────────────────────

class PromptInjectionError(ValueError):
    """وقتی یک تلاش برای injection جدی تشخیص داده می‌شود."""

    def __init__(self, message: str, pattern: str = "", matched_text: str = "") -> None:
        super().__init__(message)
        self.pattern = pattern
        self.matched_text = matched_text


# ── سطح‌بندی ─────────────────────────────────────────────────────────────────

class GuardLevel(IntEnum):
    OFF      = 0
    LENIENT  = 1
    MODERATE = 2
    STRICT   = 3

    @classmethod
    def from_str(cls, value: str) -> "GuardLevel":
        try:
            return cls[value.upper()]
        except KeyError:
            valid = [m.name for m in cls]
            raise ValueError(f"GuardLevel نامعتبر: '{value}'. مقادیر مجاز: {valid}")


# ── الگوها ───────────────────────────────────────────────────────────────────

@dataclass
class InjectionPattern:
    """یک الگوی injection با سطح خطر و توضیح."""
    pattern: re.Pattern
    level: GuardLevel      # حداقل سطح guard برای فعال شدن این چک
    description: str
    block: bool = True     # True = پرتاب خطا | False = فقط sanitize/warning


def _p(pattern: str, level: GuardLevel, desc: str, block: bool = True) -> InjectionPattern:
    return InjectionPattern(
        pattern=re.compile(pattern, re.IGNORECASE | re.DOTALL),
        level=level,
        description=desc,
        block=block,
    )


# ─────────────────────────────────────────────────────────────────────────────
# لیست الگوهای injection
# ترتیب مهم است: بلاک‌کننده‌ها اول، سپس sanitize
# ─────────────────────────────────────────────────────────────────────────────
INJECTION_PATTERNS: list[InjectionPattern] = [

    # ── تلاش برای override کردن system prompt (بلاک) ──────────────────────
    _p(
        r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?|context)",
        GuardLevel.LENIENT,
        "تلاش برای ignore کردن دستورالعمل‌های قبلی",
    ),
    _p(
        r"disregard\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)",
        GuardLevel.LENIENT,
        "تلاش برای disregard کردن دستورالعمل‌ها",
    ),
    _p(
        r"forget\s+(everything|all)\s+(you('ve|\s+have)\s+been\s+told|above|before)",
        GuardLevel.LENIENT,
        "تلاش برای reset کردن حافظه مدل",
    ),
    _p(
        r"your\s+(new\s+)?(instructions?|rules?|directives?|purpose|role|task|goal)\s+(are|is|will be)\s*:",
        GuardLevel.LENIENT,
        "تلاش برای تعریف دستورالعمل جدید",
    ),
    _p(
        r"(you are|you're|act as|pretend (to be|you are)|roleplay as|simulate)\s+.{0,50}(hacker|malicious|evil|unrestricted|DAN|jailbreak)",
        GuardLevel.LENIENT,
        "تلاش برای persona injection مخرب",
    ),

    # ── تلاش برای استخراج system prompt (بلاک) ───────────────────────────
    _p(
        r"(print|show|reveal|display|repeat|output|tell me)\s+(me\s+)?(your\s+)?(system\s+prompt|initial\s+prompt|instructions?|context|configuration)",
        GuardLevel.MODERATE,
        "تلاش برای استخراج system prompt",
    ),
    _p(
        r"(what were you|what are you)\s+(told|instructed|asked|programmed|configured)",
        GuardLevel.MODERATE,
        "تلاش برای استخراج دستورالعمل‌های اولیه",
    ),

    # ── delimiter injection (بلاک) ────────────────────────────────────────
    _p(
        r"<\|?(system|user|assistant|human|im_start|im_end)\|?>",
        GuardLevel.MODERATE,
        "تلاش برای تزریق delimiter مدل",
    ),
    _p(
        r"\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>|\[SYSTEM\]|\[USER\]",
        GuardLevel.MODERATE,
        "تلاش برای تزریق delimiter Llama",
    ),

    # ── jailbreak کلاسیک (بلاک) ──────────────────────────────────────────
    _p(
        r"\bDAN\b.{0,100}(do anything|no restrictions|no limits|without restrictions)",
        GuardLevel.LENIENT,
        "الگوی jailbreak DAN",
    ),
    _p(
        r"(developer|maintenance|debug|admin|god|sudo|root)\s+mode\s+(enabled|activated|on)",
        GuardLevel.MODERATE,
        "ادعای حالت توسعه‌دهنده/debug",
    ),
    _p(
        r"token\s+budget|end of context|context window full",
        GuardLevel.STRICT,
        "تلاش برای دستکاری context window",
    ),

    # ── indirect injection از محتوای خارجی (بلاک) ────────────────────────
    _p(
        r"(note to (ai|llm|assistant|model)|attention (ai|llm|assistant)|hey (ai|llm|assistant))\s*:",
        GuardLevel.MODERATE,
        "تلاش برای indirect prompt injection",
    ),

    # ── موارد کمتر جدی (sanitize نه بلاک) ───────────────────────────────
    _p(
        r"translate\s+the\s+(above|following)\s+(to|into)\s+\w+\s+and\s+(also|then)",
        GuardLevel.STRICT,
        "زنجیره دستوری مشکوک",
        block=False,
    ),
]

# الگوهای بررسی output (نشت system prompt)
OUTPUT_LEAK_PATTERNS: list[InjectionPattern] = [
    _p(
        r"(my system prompt\s+(is|was)|my instructions?\s+(are|were|say)|i('ve| have) been (told|instructed|programmed))",
        GuardLevel.MODERATE,
        "نشت احتمالی system prompt در خروجی",
        block=False,
    ),
    _p(
        r"(you are|you're)\s+.{0,50}(helpful assistant|ai assistant).{0,100}(your|the)\s+(system|initial)\s+prompt",
        GuardLevel.STRICT,
        "بازگویی system prompt در خروجی",
        block=False,
    ),
]


# ── PromptGuard ───────────────────────────────────────────────────────────────

class PromptGuard:
    """محافظ در برابر prompt injection.

    پارامترها:
        level         : سطح حساسیت (OFF, LENIENT, MODERATE, STRICT)
        max_input_len : حداکثر طول ورودی — طولانی‌تر truncate می‌شود (None = بدون محدودیت)
        raise_on_warn : اگر True باشد، pattern های غیر-block هم exception می‌اندازند
    """

    def __init__(
        self,
        level: GuardLevel = GuardLevel.MODERATE,
        max_input_len: int | None = 10_000,
        raise_on_warn: bool = False,
    ) -> None:
        self.level = level
        self.max_input_len = max_input_len
        self.raise_on_warn = raise_on_warn

    # ── factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "PromptGuard":
        """تنظیمات را از متغیرهای محیطی می‌خواند.

        متغیرها:
            PROMPT_GUARD_LEVEL         : OFF | LENIENT | MODERATE | STRICT
            PROMPT_GUARD_MAX_INPUT_LEN : عدد صحیح یا "none"
            PROMPT_GUARD_RAISE_ON_WARN : true | false
        """
        level_str = os.getenv("PROMPT_GUARD_LEVEL", "MODERATE")
        try:
            level = GuardLevel.from_str(level_str)
        except ValueError:
            level = GuardLevel.MODERATE

        max_len_str = os.getenv("PROMPT_GUARD_MAX_INPUT_LEN", "10000").lower()
        max_input_len = None if max_len_str in ("none", "0", "") else int(max_len_str)

        raise_on_warn = os.getenv("PROMPT_GUARD_RAISE_ON_WARN", "false").lower() in ("true", "1")

        return cls(level=level, max_input_len=max_input_len, raise_on_warn=raise_on_warn)

    @classmethod
    def off(cls) -> "PromptGuard":
        return cls(level=GuardLevel.OFF)

    # ── بررسی ────────────────────────────────────────────────────────────────

    def _check_patterns(
        self,
        text: str,
        patterns: list[InjectionPattern],
        context: str = "input",
    ) -> list[dict]:
        """همه الگوها را روی متن اجرا می‌کند و نتایج را برمی‌گرداند."""
        findings: list[dict] = []
        for pat in patterns:
            if pat.level > self.level:
                continue
            match = pat.pattern.search(text)
            if match:
                findings.append({
                    "description": pat.description,
                    "pattern": pat.pattern.pattern,
                    "matched": match.group(0)[:100],
                    "block": pat.block,
                    "context": context,
                })
        return findings

    def sanitize_input(self, text: str) -> str:
        """ورودی کاربر را بررسی و sanitize می‌کند.

        - اگر pattern بلاک‌کننده پیدا شود: PromptInjectionError پرتاب می‌شود
        - اگر سطح غیرفعال (OFF) باشد: متن بدون تغییر برمی‌گردد
        - اگر pattern هشداری پیدا شود و raise_on_warn=True باشد: exception می‌اندازد

        برمی‌گرداند متن (احتمالاً truncate شده) در صورت پاس شدن چک‌ها.
        """
        if self.level == GuardLevel.OFF:
            return text

        # ۱. بررسی طول
        if self.max_input_len and len(text) > self.max_input_len:
            text = text[:self.max_input_len]

        # ۲. بررسی الگوهای injection
        findings = self._check_patterns(text, INJECTION_PATTERNS, context="input")

        for finding in findings:
            if finding["block"]:
                raise PromptInjectionError(
                    f"تلاش برای prompt injection تشخیص داده شد: {finding['description']}",
                    pattern=finding["pattern"],
                    matched_text=finding["matched"],
                )
            if self.raise_on_warn:
                raise PromptInjectionError(
                    f"محتوای مشکوک در ورودی: {finding['description']}",
                    pattern=finding["pattern"],
                    matched_text=finding["matched"],
                )

        return text

    def validate_output(self, text: str) -> list[dict]:
        """خروجی مدل را برای تشخیص نشت اطلاعات بررسی می‌کند.

        این متد exception نمی‌اندازد — فقط لیست یافته‌ها برمی‌گرداند.
        لاگ کردن یا تصمیم‌گیری درباره آن‌ها بر عهده caller است.
        """
        if self.level == GuardLevel.OFF:
            return []
        return self._check_patterns(text, OUTPUT_LEAK_PATTERNS, context="output")

    def is_safe(self, text: str) -> bool:
        """True اگر متن بدون exception از sanitize_input رد شود."""
        try:
            self.sanitize_input(text)
            return True
        except PromptInjectionError:
            return False