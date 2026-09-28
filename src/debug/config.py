"""
debug/config.py — DebugConfig: مرکز کنترل debug

سلسله‌مراتب تنظیمات:
  1. global: LOGGING_ENABLED / LOGGING_LEVEL / LOGGING_DRIVER / LOGGING_DRIVER_FILE_*
  2. per-agent: LOGGING_AGENTS_{NAME}_ENABLED / LOGGING_AGENTS_{NAME}_LEVEL
     (driver ارث‌بری می‌شود از global)

نمونه .env:
    LOGGING_ENABLED=true
    LOGGING_LEVEL=info
    LOGGING_DRIVER=file
    LOGGING_DRIVER_FILE_PATH=logs/app.log
    LOGGING_DRIVER_FILE_FORMAT=text

    LOGGING_AGENTS_CODE_REVIEWER_LEVEL=trace
    LOGGING_AGENTS_DECISION_MAKER_ENABLED=false
"""

import os
from src.debug.levels import DebugLevel
from src.debug.base_handler import BaseHandler
from src.debug.logger import AgentLogger


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool = True) -> bool:
    val = _env(key).lower()
    if not val:
        return default
    return val in ("true", "1", "yes")


def _agent_env_prefix(agent_name: str) -> str:
    """نام ایجنت را به پیشوند متغیر env تبدیل می‌کند.

    مثال: "code_reviewer" → "LOGGING_AGENTS_CODE_REVIEWER"
    """
    return f"LOGGING_AGENTS_{agent_name.upper()}"


def _build_handler_from_env() -> "BaseHandler | None":
    """handler را بر اساس LOGGING_DRIVER می‌سازد."""
    from src.debug.handlers.file_handler import FileHandler
    from src.debug.handlers.console_handler import ConsoleHandler

    driver = _env("LOGGING_DRIVER", "file").lower()

    if driver == "file":
        path = _env("LOGGING_DRIVER_FILE_PATH", "logs/app.log")
        fmt  = _env("LOGGING_DRIVER_FILE_FORMAT", "text").lower()
        colorize  = _env_bool("LOGGING_DRIVER_FILE_COLORIZE", False)

        if fmt not in ("text", "jsonl"):
            fmt = "text"
        return FileHandler(path=path, fmt=fmt, colorize=colorize)

    if driver == "console":
        fmt       = _env("LOGGING_DRIVER_CONSOLE_FORMAT", "text").lower()
        show_time = _env_bool("LOGGING_DRIVER_CONSOLE_SHOW_TIME", default=False)
        colorize  = _env_bool("LOGGING_DRIVER_CONSOLE_COLORIZE", True)
  
        return ConsoleHandler(fmt=fmt, show_time=show_time, colorize=colorize)

    raise ValueError(
        f"درایور لاگ نامعتبر: '{driver}'. "
        f"مقادیر مجاز: file, console"
    )


class DebugConfig:
    """تنظیمات مرکزی debug برای کل پروژه."""

    def __init__(
        self,
        global_level: DebugLevel = DebugLevel.INFO,
        agent_levels: dict[str, DebugLevel] | None = None,
        agent_enabled: dict[str, bool] | None = None,
        handlers: list[BaseHandler] | None = None,
        enabled: bool = True,
    ) -> None:
        self.global_level = global_level
        self.agent_levels: dict[str, DebugLevel] = agent_levels or {}
        self.agent_enabled: dict[str, bool] = agent_enabled or {}
        self.handlers: list[BaseHandler] = handlers or []
        self.enabled = enabled
        self._loggers: dict[str, AgentLogger] = {}

    # ── بارگذاری از env ───────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "DebugConfig":
        """تمام تنظیمات را از متغیرهای محیطی (فایل .env) می‌خواند.

        این متد باید بعد از load_dotenv() فراخوانی شود.
        اگر LOGGING_ENABLED=false باشد، یک config خاموش برمی‌گرداند.
        """
        if not _env_bool("LOGGING_ENABLED", default=True):
            return cls.off()

        # سطح global
        global_level = DebugLevel.from_str(_env("LOGGING_LEVEL", "info"))

        # handler از driver config
        try:
            handler = _build_handler_from_env()
            handlers = [handler] if handler else []
        except ValueError as e:
            import warnings
            warnings.warn(str(e))
            handlers = []

        # تنظیمات per-agent — اسکن همه env vars که با LOGGING_AGENTS_ شروع می‌شوند
        agent_levels: dict[str, DebugLevel] = {}
        agent_enabled: dict[str, bool] = {}

        for key, value in os.environ.items():
            if not key.startswith("LOGGING_AGENTS_"):
                continue
            # LOGGING_AGENTS_CODE_REVIEWER_LEVEL → ["CODE", "REVIEWER", "LEVEL"]
            # LOGGING_AGENTS_CODE_REVIEWER_ENABLED → ["CODE", "REVIEWER", "ENABLED"]
            rest = key[len("LOGGING_AGENTS_"):]   # CODE_REVIEWER_LEVEL
            if rest.endswith("_LEVEL"):
                agent_name = rest[:-len("_LEVEL")].lower()
                try:
                    agent_levels[agent_name] = DebugLevel.from_str(value)
                except ValueError:
                    pass
            elif rest.endswith("_ENABLED"):
                agent_name = rest[:-len("_ENABLED")].lower()
                agent_enabled[agent_name] = value.strip().lower() in ("true", "1", "yes")

        return cls(
            global_level=global_level,
            agent_levels=agent_levels,
            agent_enabled=agent_enabled,
            handlers=handlers,
            enabled=True,
        )

    # ── تنظیم پویا ───────────────────────────────────────────────────────────

    def set_agent_level(self, agent_name: str, level: DebugLevel) -> None:
        self.agent_levels[agent_name] = level
        if agent_name in self._loggers:
            self._loggers[agent_name].level = level
            self._loggers[agent_name].enabled = self._is_agent_enabled(agent_name, level)

    def disable_agent(self, agent_name: str) -> None:
        self.agent_enabled[agent_name] = False
        if agent_name in self._loggers:
            self._loggers[agent_name].enabled = False

    def enable_agent(self, agent_name: str, level: DebugLevel | None = None) -> None:
        self.agent_enabled[agent_name] = True
        if level:
            self.set_agent_level(agent_name, level)
        elif agent_name in self._loggers:
            self._loggers[agent_name].enabled = True

    def add_handler(self, handler: BaseHandler) -> None:
        self.handlers.append(handler)
        for logger in self._loggers.values():
            logger.add_handler(handler)

    def _is_agent_enabled(self, agent_name: str, level: DebugLevel) -> bool:
        """بررسی می‌کند ایجنت باید فعال باشد یا نه."""
        if not self.enabled:
            return False
        if agent_name in self.agent_enabled:
            return self.agent_enabled[agent_name]
        return level != DebugLevel.OFF

    # ── ساخت logger ──────────────────────────────────────────────────────────

    def get_logger(self, agent_name: str) -> AgentLogger:
        if agent_name in self._loggers:
            return self._loggers[agent_name]

        level = self.agent_levels.get(agent_name, self.global_level)
        enabled = self._is_agent_enabled(agent_name, level)

        logger = AgentLogger(
            agent_name=agent_name,
            level=level,
            handlers=list(self.handlers),
            enabled=enabled,
        )
        self._loggers[agent_name] = logger
        return logger

    def close_all(self) -> None:
        for handler in self.handlers:
            handler.close()

    # ── factory methods ───────────────────────────────────────────────────────

    @classmethod
    def off(cls) -> "DebugConfig":
        return cls(enabled=False)

    @classmethod
    def simple(
        cls,
        level: DebugLevel | str = DebugLevel.INFO,
        log_file: str = "logs/debug.log",
        fmt: str = "text",
    ) -> "DebugConfig":
        """یک config ساده برای تست و توسعه — بدون نیاز به .env."""
        from src.debug.handlers.file_handler import FileHandler
        if isinstance(level, str):
            level = DebugLevel.from_str(level)
        return cls(global_level=level, handlers=[FileHandler(path=log_file, fmt=fmt)])