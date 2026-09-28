"""
rules/loader.py — بارگذاری قوانین سفارشی پروژه

ساختار مورد انتظار:
    project_rules/
    ├── code_review/
    │   ├── reviewer.md         ← قوانین کلی کد ریویو
    │   └── languages/
    │       ├── python.md
    │       └── javascript.md
    └── gitlab/
        └── commenter.md        ← قوانین کامنت گذاری

نحوه استفاده:
    loader = RuleLoader(rules_dir="project_rules")

    # بارگذاری یک فایل خاص
    rules = loader.load("code_review/reviewer")

    # بارگذاری چند فایل و ترکیب
    rules = loader.load_many(["code_review/reviewer",
                               "code_review/languages/python"])

    # inject به system prompt
    prompt = loader.inject(base_prompt, ["code_review/reviewer"])
"""

import os
from pathlib import Path


_DEFAULT_RULES_DIR = "project_rules"


class RuleLoader:
    """بارگذاری قوانین سفارشی پروژه از فایل‌های Markdown.

    پارامترها:
        rules_dir : مسیر پوشه قوانین
                    (پیش‌فرض: RULES_DIR از .env یا "project_rules")
    """

    def __init__(self, rules_dir: str | Path | None = None) -> None:
        if rules_dir is None:
            rules_dir = os.getenv("RULES_DIR", _DEFAULT_RULES_DIR)
        self._base = Path(rules_dir)

    # ── بارگذاری ─────────────────────────────────────────────────────────────

    def load(self, rule_path: str) -> str:
        """یک فایل rule را بارگذاری می‌کند.

        پارامتر:
            rule_path : مسیر نسبی بدون پسوند .md
                        مثال: "code_review/reviewer"
                               "code_review/languages/python"

        برمی‌گرداند:
            محتوای فایل یا رشته خالی اگر فایل وجود نداشته باشد
        """
        path = self._base / f"{rule_path}.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def load_many(self, rule_paths: list[str], separator: str = "\n\n---\n\n") -> str:
        """چند فایل rule را بارگذاری و ترکیب می‌کند.

        فایل‌هایی که وجود ندارند نادیده گرفته می‌شوند.
        """
        parts = []
        for rp in rule_paths:
            content = self.load(rp)
            if content:
                parts.append(content)
        return separator.join(parts)

    def load_dir(self, dir_path: str, separator: str = "\n\n---\n\n") -> str:
        """همه فایل‌های .md داخل یک پوشه را به ترتیب الفبایی بارگذاری می‌کند."""
        path = self._base / dir_path
        if not path.is_dir():
            return ""
        files = sorted(path.glob("*.md"))
        parts = [f.read_text(encoding="utf-8").strip() for f in files if f.stat().st_size > 0]
        return separator.join(parts)

    def exists(self, rule_path: str) -> bool:
        """بررسی می‌کند آیا یک فایل rule وجود دارد."""
        return (self._base / f"{rule_path}.md").exists()

    def list_rules(self, dir_path: str = "") -> list[str]:
        """لیست مسیرهای نسبی همه rule های موجود."""
        base = self._base / dir_path if dir_path else self._base
        if not base.is_dir():
            return []
        rules = []
        for f in sorted(base.rglob("*.md")):
            rel = f.relative_to(self._base).with_suffix("")
            rules.append(str(rel))
        return rules

    # ── inject به prompt ──────────────────────────────────────────────────────

    def inject(
        self,
        base_prompt: str,
        rule_paths: list[str],
        section_title: str = "## Project-Specific Rules",
    ) -> str:
        """قوانین را به انتهای یک system prompt اضافه می‌کند.

        اگر هیچ rule ای پیدا نشد، base_prompt بدون تغییر برمی‌گردد.
        """
        rules_content = self.load_many(rule_paths)
        if not rules_content:
            return base_prompt
        return f"{base_prompt}\n\n{section_title}\n\n{rules_content}"

    def inject_dir(
        self,
        base_prompt: str,
        dir_path: str,
        section_title: str = "## Project-Specific Rules",
    ) -> str:
        """همه rule های یک پوشه را به prompt اضافه می‌کند."""
        rules_content = self.load_dir(dir_path)
        if not rules_content:
            return base_prompt
        return f"{base_prompt}\n\n{section_title}\n\n{rules_content}"