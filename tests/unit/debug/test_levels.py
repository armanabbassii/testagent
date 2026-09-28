"""تست‌های DebugLevel."""

import pytest
from src.debug.levels import DebugLevel


class TestDebugLevelOrdering:
    def test_trace_is_lowest(self):
        assert DebugLevel.TRACE < DebugLevel.DEBUG

    def test_full_ordering(self):
        levels = [DebugLevel.TRACE, DebugLevel.DEBUG, DebugLevel.INFO,
                  DebugLevel.WARNING, DebugLevel.ERROR, DebugLevel.OFF]
        assert levels == sorted(levels)

    def test_off_is_highest(self):
        assert DebugLevel.OFF > DebugLevel.ERROR

    def test_level_passes_filter(self):
        # WARNING باید از فیلتر INFO رد شود
        assert DebugLevel.WARNING >= DebugLevel.INFO

    def test_debug_blocked_by_info_filter(self):
        assert not (DebugLevel.DEBUG >= DebugLevel.INFO)


class TestDebugLevelFromStr:
    def test_lowercase(self):
        assert DebugLevel.from_str("debug") == DebugLevel.DEBUG

    def test_uppercase(self):
        assert DebugLevel.from_str("INFO") == DebugLevel.INFO

    def test_mixed_case(self):
        assert DebugLevel.from_str("Warning") == DebugLevel.WARNING

    def test_all_valid_levels(self):
        for name in ["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "OFF"]:
            assert DebugLevel.from_str(name) == DebugLevel[name]

    def test_invalid_level_raises(self):
        with pytest.raises(ValueError, match="نامعتبر"):
            DebugLevel.from_str("VERBOSE")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            DebugLevel.from_str("")


class TestDebugLevelLabel:
    def test_label_matches_name(self):
        assert DebugLevel.INFO.label() == "INFO"
        assert DebugLevel.TRACE.label() == "TRACE"
        assert DebugLevel.ERROR.label() == "ERROR"