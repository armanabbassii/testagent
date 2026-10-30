"""تست‌های پاک‌سازیِ متن، wiki markupِ Jira و متنِ ساختاریافته‌ی TaskInput."""

from __future__ import annotations

from src.task_sources import (
    ATTACHMENT_FAILED,
    ATTACHMENT_PROCESSED,
    ATTACHMENT_UNSUPPORTED,
    TaskAttachment,
    TaskComment,
    TaskInput,
)
from src.task_sources.jira.markup import normalize_wiki
from src.task_sources.text import fence_block, normalize_text


class TestNormalizeText:
    def test_crlf_and_cr_become_lf(self):
        assert normalize_text("a\r\nb\rc") == "a\nb\nc"

    def test_non_breaking_spaces_become_spaces(self):
        assert normalize_text("دو api و متن x") == "دو api و متن x"

    def test_the_persian_zero_width_non_joiner_is_kept(self):
        assert normalize_text("می‌شود") == "می‌شود"

    def test_bom_trailing_spaces_and_blank_runs_are_removed(self):
        assert normalize_text("﻿\n\nfirst   \n\n\n\nsecond\t\n\n") == "first\n\nsecond"

    def test_none_is_empty(self):
        assert normalize_text(None) == ""


class TestFenceBlock:
    def test_the_fence_is_longer_than_any_run_inside(self):
        block = fence_block("```\ncode\n~~~~~\n```", "json")
        assert block.startswith("~~~~~~json\n")
        assert block.endswith("\n~~~~~~")

    def test_the_minimum_fence_has_four_tildes(self):
        assert fence_block("x") == "~~~~\nx\n~~~~"


class TestWikiMarkup:
    def test_text_outside_code_blocks_is_cleaned(self):
        assert normalize_wiki("h3. Title\r\n\r\n\r\n*bold* text  ") == "h3. Title\n\n*bold* text"

    def test_code_blocks_are_kept_verbatim_and_fenced(self):
        source = (
            "Example:\r\n{code:json}\r\n{\"a\":  1, \r\n\r\n\r\n\"b\": \"x  \"}\r\n{code}\r\nAfter"
        )

        result = normalize_wiki(source)

        # داخلِ بلوک فقط پایانِ خط عوض می‌شود: فاصله‌ها، NBSP و خط‌های خالی می‌مانند.
        assert result == (
            'Example:\n\n~~~~json\n{"a":  1, \n\n\n"b": "x  "}\n~~~~\n\nAfter'
        )

    def test_noformat_blocks_have_no_language(self):
        result = normalize_wiki("{noformat}curl -X GET 'https://x/api?a=1&b=2'{noformat}")
        assert result == "~~~~\ncurl -X GET 'https://x/api?a=1&b=2'\n~~~~"

    def test_code_language_parameters(self):
        assert normalize_wiki("{code:title=a.json|language=json}1{code}").startswith("~~~~json")
        assert normalize_wiki("{code:Java|title=x}1{code}").startswith("~~~~java")

    def test_an_unterminated_block_is_left_as_text(self):
        assert normalize_wiki("{code}\nx = 1") == "{code}\nx = 1"

    def test_links_and_mentions_are_not_rewritten(self):
        text = "see [MR|https://git.test/mr/1] and [~reviewer] /api/admin/x"
        assert normalize_wiki(text) == text


def _task(**overrides) -> TaskInput:
    values = {
        "title": "Add voucher types",
        "description": "Two new endpoints.",
        "comments": (
            TaskComment("Dev One", "2026-01-01 10:00 +03:30", "Endpoints: /api/admin/types"),
            TaskComment("Lead", "2026-01-02 09:00 +03:30", "Please log time."),
        ),
        "attachments": (
            TaskAttachment("sample.json", "application/json", 12, ATTACHMENT_PROCESSED, '{"a": 1}'),
            TaskAttachment("shot.png", "image/png", 99, ATTACHMENT_UNSUPPORTED, detail="no"),
            TaskAttachment("bad.txt", "text/plain", 5, ATTACHMENT_FAILED, detail="not text"),
        ),
        "source": "jira",
        "source_reference": "DEMO-7",
        "source_url": "https://jira.test/browse/DEMO-7",
        "metadata": {"issue_type": "Story", "status": "Open"},
    }
    values.update(overrides)
    return TaskInput(**values)


class TestTaskDescription:
    def test_sections_appear_in_order_with_their_origin(self):
        text = _task().to_task_description()

        positions = [
            text.index(heading)
            for heading in (
                "# Task DEMO-7: Add voucher types",
                "Source: jira DEMO-7 (https://jira.test/browse/DEMO-7) · type: Story · status: Open",
                "How to read this task",
                "## Description (original requirement)",
                "## Comments (2, oldest first)",
                "### Comment 1 — Dev One, 2026-01-01 10:00 +03:30",
                "### Comment 2 — Lead, 2026-01-02 09:00 +03:30",
                "## Attachments (1 processed, 2 not processed)",
                "### Attachment: sample.json (application/json, 12 bytes)",
                "### Attachments not processed",
            )
        ]
        assert positions == sorted(positions)

    def test_attachment_content_is_fenced_with_its_language(self):
        assert '~~~~json\n{"a": 1}\n~~~~' in _task().to_task_description()

    def test_unprocessed_attachments_are_listed_with_their_reason(self):
        text = _task().to_task_description()
        assert "- shot.png: unsupported file type — no" in text
        assert "- bad.txt: failed — not text" in text

    def test_every_comment_is_kept_in_full(self):
        long_body = "x" * 20_000
        text = _task(comments=(TaskComment("A", "d", long_body),)).to_task_description()
        assert long_body in text

    def test_an_issue_without_content_says_so(self):
        text = _task(description="", comments=(), attachments=(), metadata={}).to_task_description()
        assert "(The issue has no description.)" in text
        assert "(No comments.)" in text
        assert "(No attachments.)" in text
        assert "type:" not in text

    def test_an_attachment_note_is_shown(self):
        note = TaskAttachment("a.json", "", 1, ATTACHMENT_PROCESSED, "{", detail="not valid JSON")
        assert "Note: not valid JSON" in _task(attachments=(note,)).to_task_description()

    def test_metadata_does_not_affect_equality(self):
        assert _task(metadata={"retrieved_at": "1"}) == _task(metadata={"retrieved_at": "2"})
