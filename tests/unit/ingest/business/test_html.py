"""تست‌های strip_html."""

from src.ingest.business.html import strip_html


class TestStripHtml:
    def test_removes_simple_tags(self):
        assert strip_html("<p>سلام</p>") == "سلام"

    def test_removes_nested_tags(self):
        assert strip_html("<div><b>مهم</b> است</div>") == "مهم است"

    def test_decodes_html_entities(self):
        assert strip_html("A &amp; B") == "A & B"

    def test_collapses_whitespace(self):
        assert strip_html("<p>سلام   \n دنیا</p>") == "سلام دنیا"

    def test_empty_string_returns_empty(self):
        assert strip_html("") == ""

    def test_none_like_empty_returns_empty(self):
        assert strip_html(None) == ""  # type: ignore

    def test_plain_text_unchanged_content(self):
        assert strip_html("متن ساده") == "متن ساده"