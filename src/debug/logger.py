"""
debug/logger.py — AgentLogger

کلاس اصلی که هر ایجنت یک نمونه از آن دارد.
سطح debug را از DebugConfig می‌خواند (global یا per-agent).
پیام‌ها را به همه handler های ثبت‌شده پاس می‌دهد.
"""

from src.debug.levels import DebugLevel
from src.debug.base_handler import BaseHandler, LogRecord


class AgentLogger:
    """لاگر اختصاصی هر ایجنت.

    پارامترها:
        agent_name : نام ایجنت (برای نمایش در لاگ)
        level      : حداقل سطح لاگ برای این ایجنت
        handlers   : لیست handler هایی که پیام‌ها را دریافت می‌کنند
        enabled    : اگر False باشد تمام لاگ‌ها سایلنت می‌شوند
    """

    def __init__(
        self,
        agent_name: str,
        level: DebugLevel = DebugLevel.INFO,
        handlers: list[BaseHandler] | None = None,
        enabled: bool = True,
    ) -> None:
        self.agent_name = agent_name
        self.level = level
        self.enabled = enabled
        self._handlers: list[BaseHandler] = handlers or []

    def add_handler(self, handler: BaseHandler) -> None:
        self._handlers.append(handler)

    def remove_handler(self, handler: BaseHandler) -> None:
        self._handlers.remove(handler)

    def _log(self, level: DebugLevel, message: str, extra: dict | None = None) -> None:
        if not self.enabled or level < self.level:
            return
        record = LogRecord(
            level=level,
            agent_name=self.agent_name,
            message=message,
            extra=extra or {},
        )
        for handler in self._handlers:
            handler.handle(record)

    # ── متدهای راحت ──────────────────────────────────────────────────────────

    def trace(self, message: str, **extra) -> None:
        self._log(DebugLevel.TRACE, message, extra)

    def debug(self, message: str, **extra) -> None:
        self._log(DebugLevel.DEBUG, message, extra)

    def info(self, message: str, **extra) -> None:
        self._log(DebugLevel.INFO, message, extra)

    def warning(self, message: str, **extra) -> None:
        self._log(DebugLevel.WARNING, message, extra)

    def error(self, message: str, **extra) -> None:
        self._log(DebugLevel.ERROR, message, extra)

    def is_enabled_for(self, level: DebugLevel) -> bool:
        """بررسی می‌کند که آیا این سطح لاگ می‌شود (برای جلوگیری از ساخت پیام‌های گران‌قیمت)."""
        return self.enabled and level >= self.level