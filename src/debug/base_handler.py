"""
debug/base_handler.py — Abstract interface برای همه handler های لاگ

هر handler باید این interface را پیاده‌سازی کند.
این طراحی اجازه می‌دهد بدون تغییر در AgentLogger، handler جدید
(مثلاً database، Elasticsearch، Slack، ...) اضافه شود.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from src.debug.levels import DebugLevel


@dataclass
class LogRecord:
    """یک رکورد لاگ که به handler پاس می‌شود."""

    level: DebugLevel
    agent_name: str
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    extra: dict = field(default_factory=dict)

    def level_label(self) -> str:
        return self.level.label()


class BaseHandler(ABC):
    """Interface مشترک برای همه handler های لاگ.

    الگوی استفاده:
        handler = FileHandler(path="logs/debug.log")
        logger = AgentLogger(agent_name="reviewer", handlers=[handler])
        logger.debug("شروع ریویو", extra={"mr_iid": 42})
    """

    def __init__(self, min_level: DebugLevel = DebugLevel.DEBUG) -> None:
        """
        پارامترها:
            min_level : فقط رکوردهایی با این سطح یا بالاتر نوشته می‌شوند.
                        این فیلتر مستقل از فیلتر AgentLogger است.
        """
        self.min_level = min_level

    def should_handle(self, record: LogRecord) -> bool:
        return record.level >= self.min_level

    @abstractmethod
    def emit(self, record: LogRecord) -> None:
        """رکورد را در مقصد مناسب ذخیره می‌کند."""
        ...

    @abstractmethod
    def close(self) -> None:
        """منابع باز (فایل، connection، ...) را آزاد می‌کند."""
        ...

    def handle(self, record: LogRecord) -> None:
        """فیلتر می‌کند و در صورت عبور از فیلتر emit را صدا می‌زند."""
        if self.should_handle(record):
            self.emit(record)