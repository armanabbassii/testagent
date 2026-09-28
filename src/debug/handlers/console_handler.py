"""
debug/handlers/console_handler.py — خروجی رنگی در ترمینال

ویژگی‌ها:
  - رنگ‌آمیزی ANSI بر اساس سطح لاگ
  - ERROR و WARNING به stderr، بقیه به stdout
  - قابل غیرفعال کردن رنگ با colorize=False یا متغیر NO_COLOR
  - فرمت فشرده (بدون timestamp) یا کامل — قابل انتخاب
"""

import sys
import os
import json
import threading
from src.debug.base_handler import BaseHandler, LogRecord
from src.debug.levels import DebugLevel

_RESET = "\033[0m"
_LEVEL_STYLES: dict[str, str] = {
    "TRACE":   "\033[95m",      # خاکستری
    "DEBUG":   "\033[36m",      # فیروزه‌ای
    "INFO":    "\033[32m",      # سبز
    "WARNING": "\033[33;1m",    # زرد bold
    "ERROR":   "\033[31;1m",    # قرمز bold

    # Custom Colors
    "CYAN": "\033[96m",
    "YELLOW": "\033[93m",
    "OFF":     "",
}

_COLOR_SUPPORTED = (
    sys.stdout.isatty()
    and os.environ.get("NO_COLOR") is None
    and os.environ.get("TERM") != "dumb"
)


class ConsoleHandler(BaseHandler):
    """لاگ را با رنگ‌آمیزی در ترمینال نمایش می‌دهد.

    پارامترها:
        min_level  : حداقل سطح لاگ
        colorize   : رنگ‌آمیزی ANSI (پیش‌فرض: خودکار بر اساس ترمینال)
        show_time  : نمایش timestamp (پیش‌فرض: False — فشرده‌تر)
        fmt        : "text" یا "jsonl"
    """

    def __init__(
        self,
        min_level: DebugLevel = DebugLevel.DEBUG,
        colorize: bool | None = None,
        show_time: bool = False,
        fmt: str = "text",
    ) -> None:
        super().__init__(min_level=min_level)
        self._colorize = colorize if colorize is not None else _COLOR_SUPPORTED
        self._show_time = show_time
        self._fmt = fmt
        self._lock = threading.Lock()

    def _color(self, text: str, level_label: str) -> str:
        if not self._colorize:
            return text
        code = _LEVEL_STYLES.get(level_label, "")
        return f"{code}{text}{_RESET}"

    def _format_text(self, record: LogRecord) -> str:
        level = record.level_label()
        level_str = self._color(f"[{level:<7}]", level)
        agent_str = self._color(f"[{record.agent_name}]", "CYAN")

        parts = []
        if self._show_time:
            parts.append(self._color(record.timestamp[:19], "YELLOW"))
        parts += [level_str, agent_str, record.message]

        line = " ".join(parts)
        if record.extra:
            extras = "  " + " ".join(f"{k}={v!r}" for k, v in record.extra.items())
            line += self._color(extras, "TRACE")
        return line

    def _format_jsonl(self, record: LogRecord) -> str:
        return json.dumps({
            "timestamp":  record.timestamp,
            "level":      record.level_label(),
            "agent_name": record.agent_name,
            "message":    record.message,
            **record.extra,
        }, ensure_ascii=False)

    def emit(self, record: LogRecord) -> None:
        line = (
            self._format_jsonl(record)
            if self._fmt == "jsonl"
            else self._format_text(record)
        )
        # ERROR و WARNING به stderr
        stream = (
            sys.stderr
            if record.level >= DebugLevel.WARNING
            else sys.stdout
        )
        with self._lock:
            print(line, file=stream, flush=True)

    def close(self) -> None:
        pass   # ترمینال نیازی به بستن ندارد