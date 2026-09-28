"""تست‌های DebugConfig."""

import pytest
from unittest.mock import patch
from src.debug.levels import DebugLevel
from src.debug.config import DebugConfig
from tests.conftest import MemoryHandler


@pytest.fixture
def handler() -> MemoryHandler:
    return MemoryHandler()


class TestDebugConfigGetLogger:
    def test_returns_logger_with_global_level(self, handler):
        config = DebugConfig(global_level=DebugLevel.WARNING, handlers=[handler])
        logger = config.get_logger("agent_a")
        assert logger.level == DebugLevel.WARNING

    def test_returns_logger_with_per_agent_level(self, handler):
        config = DebugConfig(
            global_level=DebugLevel.INFO,
            agent_levels={"agent_a": DebugLevel.TRACE},
            handlers=[handler],
        )
        logger = config.get_logger("agent_a")
        assert logger.level == DebugLevel.TRACE

    def test_singleton_per_agent(self, handler):
        config = DebugConfig(handlers=[handler])
        l1 = config.get_logger("agent_a")
        l2 = config.get_logger("agent_a")
        assert l1 is l2

    def test_different_agents_different_loggers(self, handler):
        config = DebugConfig(handlers=[handler])
        assert config.get_logger("a") is not config.get_logger("b")

    def test_disabled_agent_logger_not_enabled(self, handler):
        config = DebugConfig(
            agent_enabled={"agent_a": False},
            handlers=[handler],
        )
        logger = config.get_logger("agent_a")
        assert not logger.enabled


class TestDebugConfigSetAgentLevel:
    def test_set_level_updates_existing_logger(self, handler):
        config = DebugConfig(global_level=DebugLevel.INFO, handlers=[handler])
        logger = config.get_logger("agent_a")
        config.set_agent_level("agent_a", DebugLevel.ERROR)
        assert logger.level == DebugLevel.ERROR

    def test_off_level_disables_logger(self, handler):
        config = DebugConfig(handlers=[handler])
        logger = config.get_logger("agent_a")
        config.set_agent_level("agent_a", DebugLevel.OFF)
        assert not logger.enabled


class TestDebugConfigDisableEnable:
    def test_disable_agent(self, handler):
        config = DebugConfig(handlers=[handler])
        logger = config.get_logger("agent_a")
        config.disable_agent("agent_a")
        assert not logger.enabled

    def test_enable_agent(self, handler):
        config = DebugConfig(agent_enabled={"agent_a": False}, handlers=[handler])
        logger = config.get_logger("agent_a")
        config.enable_agent("agent_a")
        assert logger.enabled

    def test_enable_agent_with_level(self, handler):
        config = DebugConfig(agent_levels={"agent_a": DebugLevel.INFO}, handlers=[handler])
        config.get_logger("agent_a")
        # set_agent_level مستقیم level را تغییر می‌دهد
        config.set_agent_level("agent_a", DebugLevel.TRACE)
        assert config._loggers["agent_a"].level == DebugLevel.TRACE


class TestDebugConfigOff:
    def test_off_disables_all_logging(self, handler):
        config = DebugConfig.off()
        logger = config.get_logger("any_agent")
        logger.error("should not appear")
        assert len(handler.records) == 0

    def test_off_factory(self):
        config = DebugConfig.off()
        assert not config.enabled


class TestDebugConfigFromEnv:
    def test_disabled_when_logging_enabled_false(self):
        with patch.dict("os.environ", {"LOGGING_ENABLED": "false"}):
            config = DebugConfig.from_env()
        assert not config.enabled

    def test_global_level_from_env(self):
        with patch.dict("os.environ", {
            "LOGGING_ENABLED": "true",
            "LOGGING_LEVEL": "warning",
            "LOGGING_DRIVER": "file",
            "LOGGING_DRIVER_FILE_PATH": "/tmp/test.log",
            "LOGGING_DRIVER_FILE_FORMAT": "text",
        }):
            config = DebugConfig.from_env()
        assert config.global_level == DebugLevel.WARNING

    def test_per_agent_level_from_env(self):
        with patch.dict("os.environ", {
            "LOGGING_ENABLED": "true",
            "LOGGING_LEVEL": "info",
            "LOGGING_DRIVER": "file",
            "LOGGING_DRIVER_FILE_PATH": "/tmp/test.log",
            "LOGGING_DRIVER_FILE_FORMAT": "text",
            "LOGGING_AGENTS_MY_AGENT_LEVEL": "trace",
        }):
            config = DebugConfig.from_env()
        assert config.agent_levels.get("my_agent") == DebugLevel.TRACE

    def test_per_agent_disabled_from_env(self):
        with patch.dict("os.environ", {
            "LOGGING_ENABLED": "true",
            "LOGGING_LEVEL": "info",
            "LOGGING_DRIVER": "file",
            "LOGGING_DRIVER_FILE_PATH": "/tmp/test.log",
            "LOGGING_DRIVER_FILE_FORMAT": "text",
            "LOGGING_AGENTS_MY_AGENT_ENABLED": "false",
        }):
            config = DebugConfig.from_env()
        assert config.agent_enabled.get("my_agent") is False

    def test_invalid_driver_falls_back_with_warning(self):
        import warnings
        with patch.dict("os.environ", {
            "LOGGING_ENABLED": "true",
            "LOGGING_LEVEL": "info",
            "LOGGING_DRIVER": "nonexistent_driver",
        }):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                config = DebugConfig.from_env()
                assert len(w) == 1
        assert config.handlers == []