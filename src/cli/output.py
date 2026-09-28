"""
cli/output.py — رنگ‌آمیزی و قالب‌بندی خروجی ترمینال

از ANSI color codes استفاده می‌کند.
اگر ترمینال از رنگ پشتیبانی نکند (CI/pipe)، رنگ‌ها حذف می‌شوند.
"""

import os
import sys

# تشخیص پشتیبانی از رنگ
_COLOR_SUPPORTED = (
    sys.stdout.isatty()
    and os.environ.get("NO_COLOR") is None
    and os.environ.get("TERM") != "dumb"
)

# ── کدهای رنگی ANSI ──────────────────────────────────────────────────────────
_R = "\033[0m"   # reset
_COLORS = {
    "red":     "\033[31m",
    "green":   "\033[32m",
    "yellow":  "\033[33m",
    "blue":    "\033[34m",
    "cyan":    "\033[36m",
    "white":   "\033[37m",
    "bold":    "\033[1m",
    "dim":     "\033[2m",
}

SEV_COLORS = {
    "critical":   "red",
    "major":      "yellow",
    "minor":      "cyan",
    "suggestion": "dim",
}

DECISION_COLORS = {
    "approve":    "green",
    "reject":     "red",
    "needs_work": "yellow",
}


def _c(text: str, color: str) -> str:
    if not _COLOR_SUPPORTED:
        return text
    code = _COLORS.get(color, "")
    return f"{code}{text}{_R}"


def bold(text: str) -> str:
    return _c(text, "bold")


def dim(text: str) -> str:
    return _c(text, "dim")


def severity_color(severity: str, text: str) -> str:
    return _c(text, SEV_COLORS.get(severity, "white"))


def decision_color(decision: str, text: str) -> str:
    return _c(text, DECISION_COLORS.get(decision, "white"))


# ── توابع چاپ ────────────────────────────────────────────────────────────────

def header(text: str, width: int = 55) -> None:
    line = "─" * width
    print(f"\n{bold(line)}")
    print(f"  {bold(text)}")
    print(bold(line))


def section(text: str, width: int = 55) -> None:
    print(f"\n{dim('─' * width)}")
    print(f"  {bold(text)}")
    print(dim("─" * width))


def info(text: str) -> None:
    print(f"  {text}")


def success(text: str) -> None:
    print(f"  {_c('✅', 'green')} {text}")


def warning(text: str) -> None:
    print(f"  {_c('⚠️', 'yellow')}  {text}")


def error(text: str) -> None:
    print(f"  {_c('❌', 'red')} {text}", file=sys.stderr)


def step(text: str) -> None:
    print(f"  {_c('▶', 'cyan')} {text}")


def prompt(text: str, options: list[str]) -> str:
    opts = "/".join(options)
    return input(f"\n  {bold(text)} ({opts}): ").strip().lower()


def review_comment(severity: str, category: str, file_path: str,
                   line: int | None, body: str) -> None:
    line_info = f" :{line}" if line else ""
    sev_label = severity_color(severity, f"[{severity.upper()}]")
    cat_label = dim(f"[{category}]")
    location = bold(f"{file_path}{line_info}")
    print(f"\n  {sev_label} {cat_label} {location}")
    # body را با indent نمایش بده
    for part in body[:200].split("\n"):
        print(f"     {dim(part)}")
    if len(body) > 200:
        print(f"     {dim('...')}")


def score_row(label: str, value, width: int = 20) -> None:
    print(f"  {label:<{width}}: {bold(str(value))}")