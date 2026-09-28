"""
debug/handlers/file_handler.py — ذخیره لاگ در فایل

دو فرمت پشتیبانی می‌شود:
  - "text" : خوانا برای انسان، مناسب برای ترمینال و tail -f
  - "jsonl" : یک JSON per line، مناسب برای parse و تحلیل

rotation اختیاری است — اگر max_bytes تعریف شود، فایل قبل از رسیدن به
آن اندازه به .1، .2، ... تغییر نام می‌یابد (حداکثر backup_count فایل).
"""

import json
import os
import threading
from pathlib import Path
from src.debug.base_handler import BaseHandler, LogRecord
from src.debug.levels import DebugLevel

_LEVEL_COLORS = {
    "TRACE":   "\033[90m",
    "DEBUG":   "\033[36m",
    "INFO":    "\033[32m",
    "WARNING": "\033[33m",
    "ERROR":   "\033[31m",
}
_RESET = "\033[0m"


class FileHandler(BaseHandler):
    """لاگ را در یک فایل text یا jsonl ذخیره می‌کند.

    پارامترها:
        path         : مسیر فایل لاگ (پوشه‌های ناموجود ساخته می‌شوند)
        fmt          : "text" یا "jsonl"
        min_level    : حداقل سطح لاگ برای این handler
        max_bytes    : حداکثر اندازه فایل قبل از rotation (0 = غیرفعال)
        backup_count : تعداد فایل‌های backup نگه‌داشته‌شده
        colorize     : رنگ‌آمیزی ANSI در فرمت text
    """

    def __init__(
        self,
        path: str | Path,
        fmt: str = "text",
        min_level: DebugLevel = DebugLevel.DEBUG,
        max_bytes: int = 10 * 1024 * 1024,  # 10 MB
        backup_count: int = 5,
        colorize: bool = False,
    ) -> None:
        super().__init__(min_level=min_level)

        if fmt not in ("text", "jsonl"):
            raise ValueError(f"فرمت نامعتبر: '{fmt}'. مقادیر مجاز: text, jsonl")

        self._path = Path(path)
        self._fmt = fmt
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._colorize = colorize
        self._lock = threading.Lock()

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a", encoding="utf-8")

    # ── rotation ──────────────────────────────────────────────────────────────

    def _should_rollover(self) -> bool:
        if self._max_bytes <= 0:
            return False
        try:
            return self._path.stat().st_size >= self._max_bytes
        except FileNotFoundError:
            return False

    def _do_rollover(self) -> None:
        """فایل‌های قدیمی را جابجا می‌کند و فایل جدید باز می‌کند."""
        self._file.close()

        # فایل‌های موجود را به عقب می‌بریم: .4→.5، .3→.4، ...
        for i in range(self._backup_count - 1, 0, -1):
            src = Path(f"{self._path}.{i}")
            dst = Path(f"{self._path}.{i + 1}")
            if src.exists():
                src.rename(dst)

        # فایل فعلی را به .1 تبدیل می‌کنیم
        backup = Path(f"{self._path}.1")
        if self._path.exists():
            self._path.rename(backup)

        self._file = self._path.open("a", encoding="utf-8")

    # ── فرمت‌بندی ─────────────────────────────────────────────────────────────

    def _format_text(self, record: LogRecord) -> str:
        level = record.level_label()
        if self._colorize:
            color = _LEVEL_COLORS.get(level, "")
            level_str = f"{color}[{level:<7}]{_RESET}"
        else:
            level_str = f"[{level:<7}]"

        line = f"{record.timestamp} {level_str} [{record.agent_name}] {record.message}"
        if record.extra:
            extras = " ".join(f"{k}={v!r}" for k, v in record.extra.items())
            line += f"  {extras}"
        return line

    def _format_jsonl(self, record: LogRecord) -> str:
        return json.dumps({
            "timestamp":  record.timestamp,
            "level":      record.level_label(),
            "agent_name": record.agent_name,
            "message":    record.message,
            **record.extra,
        }, ensure_ascii=False)

    # ── emit ──────────────────────────────────────────────────────────────────

    def emit(self, record: LogRecord) -> None:
        line = (
            self._format_jsonl(record)
            if self._fmt == "jsonl"
            else self._format_text(record)
        )

        with self._lock:
            if self._should_rollover():
                self._do_rollover()
            self._file.write(line + "\n")
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.close()