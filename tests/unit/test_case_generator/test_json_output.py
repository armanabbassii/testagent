"""تست‌های استخراج JSON از پاسخ خام LLM."""

import pytest

from src.agents.test_case_generator.json_output import (
    JsonExtractionError,
    extract_json_object,
)


class TestExtractJsonObject:
    def test_plain_json_object(self):
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_json_inside_fence(self):
        raw = '```json\n{"a": 1}\n```'
        assert extract_json_object(raw) == {"a": 1}

    def test_fence_without_language(self):
        raw = "```\n{\"a\": 1}\n```"
        assert extract_json_object(raw) == {"a": 1}

    def test_surrounding_prose_is_tolerated(self):
        raw = 'Here is the result:\n{"a": 1}\nHope this helps.'
        assert extract_json_object(raw) == {"a": 1}

    def test_nested_objects_survive(self):
        raw = '{"a": {"b": [1, 2]}}'
        assert extract_json_object(raw) == {"a": {"b": [1, 2]}}

    def test_empty_response_raises(self):
        with pytest.raises(JsonExtractionError, match="empty response"):
            extract_json_object("")

    def test_whitespace_only_raises(self):
        with pytest.raises(JsonExtractionError, match="empty response"):
            extract_json_object("   \n  ")

    def test_non_json_raises_with_preview(self):
        with pytest.raises(JsonExtractionError, match="did not return JSON"):
            extract_json_object("I cannot help with that.")

    def test_broken_json_raises(self):
        with pytest.raises(JsonExtractionError, match="invalid JSON"):
            extract_json_object('{"a": 1,,}')

    def test_top_level_array_raises(self):
        with pytest.raises(JsonExtractionError, match="Expected a JSON object"):
            extract_json_object("[1, 2, 3]")

    def test_top_level_scalar_raises(self):
        with pytest.raises(JsonExtractionError, match="got str"):
            extract_json_object('"just a string"')
