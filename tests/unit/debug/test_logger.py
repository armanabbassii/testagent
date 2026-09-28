"""تست‌های AgentLogger."""

import pytest
from src.debug.levels import DebugLevel
from src.debug.logger import AgentLogger
from tests.conftest import MemoryHandler


@pytest.fixture
def handler() -> MemoryHandler:
    return MemoryHandler(min_level=DebugLevel.TRACE)


@pytest.fixture
def logger(handler) -> AgentLogger:
    return AgentLogger(
        agent_name="test_agent",
        level=DebugLevel.DEBUG,
        handlers=[handler],
        enabled=True,
    )


class TestAgentLoggerFiltering:
    def test_message_above_level_is_logged(self, logger, handler):
        logger.info("test message")
        assert "test message" in handler.messages()

    def test_message_below_level_is_blocked(self, logger, handler):
        # logger در DEBUG — TRACE نباید رد شود
        logger.trace("ignored trace")
        assert "ignored trace" not in handler.messages()

    def test_message_at_exact_level_is_logged(self, logger, handler):
        logger.debug("at level message")
        assert "at level message" in handler.messages()

    def test_disabled_logger_logs_nothing(self, handler):
        logger = AgentLogger("disabled", level=DebugLevel.TRACE,
                             handlers=[handler], enabled=False)
        logger.error("should not appear")
        assert len(handler.records) == 0

    def test_off_level_logs_nothing(self, handler):
        logger = AgentLogger("off_agent", level=DebugLevel.OFF,
                             handlers=[handler], enabled=True)
        logger.error("should not appear")
        assert len(handler.records) == 0


class TestAgentLoggerMethods:
    def test_all_level_methods(self, logger, handler):
        logger.level = DebugLevel.TRACE
        logger.trace("t")
        logger.debug("d")
        logger.info("i")
        logger.warning("w")
        logger.error("e")
        assert handler.levels() == ["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"]

    def test_extra_fields_in_record(self, logger, handler):
        logger.info("msg", user_id="u1", count=42)
        record = handler.records[-1]
        assert record.extra["user_id"] == "u1"
        assert record.extra["count"] == 42

    def test_agent_name_in_record(self, logger, handler):
        logger.info("msg")
        assert handler.records[-1].agent_name == "test_agent"


class TestAgentLoggerHandlerManagement:
    def test_add_handler(self, logger):
        new_handler = MemoryHandler()
        logger.add_handler(new_handler)
        logger.info("broadcast")
        assert "broadcast" in new_handler.messages()

    def test_remove_handler(self, logger, handler):
        logger.remove_handler(handler)
        logger.info("after remove")
        assert len(handler.records) == 0

    def test_multiple_handlers_receive_same_record(self):
        h1, h2 = MemoryHandler(), MemoryHandler()
        logger = AgentLogger("multi", level=DebugLevel.TRACE, handlers=[h1, h2])
        logger.info("broadcast")
        assert "broadcast" in h1.messages()
        assert "broadcast" in h2.messages()


class TestAgentLoggerIsEnabledFor:
    def test_enabled_for_level_above_threshold(self, logger):
        assert logger.is_enabled_for(DebugLevel.INFO)

    def test_not_enabled_for_level_below_threshold(self, logger):
        assert not logger.is_enabled_for(DebugLevel.TRACE)

    def test_disabled_logger_not_enabled_for_any(self, handler):
        logger = AgentLogger("x", level=DebugLevel.TRACE,
                             handlers=[handler], enabled=False)
        assert not logger.is_enabled_for(DebugLevel.ERROR)