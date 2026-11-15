"""
مرحله‌ی 3b — حملِ دادهِ ساختاریافته‌ی Postman از پیوست تا TaskSource

process_attachment پیوستِ JSONی را که Collectionِ v2.0/v2.1 است، پیش از برشِ
max_chars کامل تجزیه می‌کند و نتیجه را در TaskAttachment.postman می‌گذارد؛
sources_from_task_input درخواست‌ها را به TaskSourceِ همان پیوست می‌دهد. متنِ
پیوست، برش، detail، sha256 و to_task_description هیچ تغییری نمی‌کنند.

همه‌ی fixtureها ساختگی‌اند؛ هیچ شبکه، Jira یا LLMی صدا زده نمی‌شود.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.agents.test_case_generator.environment_decisions import extract_environment_decisions
from src.task_sources import (
    ATTACHMENT_FAILED,
    ATTACHMENT_PROCESSED,
    TaskAttachment,
    TaskComment,
    TaskInput,
)
from src.task_sources.attachments import compact_json, process_attachment
from src.task_sources.postman import MAX_REQUESTS, PostmanCollection
from src.task_sources.sources import (
    SOURCE_ATTACHMENT,
    TaskSource,
    sources_for_task,
    sources_from_task_input,
    sources_from_text,
)

V21 = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
V20 = "https://schema.getpostman.com/json/collection/v2.0.0/collection.json"
BASE = "http://podium.admin2.devpod.ir/api"
SECRET = "S3CR3T-SENTINEL-VALUE"
VDI_SENTENCE = "Run every admin API in VDI."


def _collection(count: int = 2, schema: str = V21, padding: str = "") -> dict:
    items = [
        {
            "name": f"request {index}",
            "request": {
                "method": "POST" if index % 2 else "GET",
                "url": {
                    "raw": f"{{{{baseUrl}}}}/admin/voucher/{index}",
                    "host": ["{{baseUrl}}"],
                    "path": ["admin", "voucher", str(index)],
                },
                "description": padding,
            },
        }
        for index in range(count)
    ]
    return {
        "info": {"name": "Voucher", "schema": schema, "description": VDI_SENTENCE},
        "item": [{"name": "Voucher", "item": items}],
        "variable": [{"key": "baseUrl", "value": BASE}, {"key": "token", "value": SECRET}],
    }


def _process(filename: str, content: str | bytes, *, max_chars: int = 100_000) -> TaskAttachment:
    data = content.encode("utf-8") if isinstance(content, str) else content
    return process_attachment(
        filename=filename,
        media_type="application/json",
        size=len(data),
        download=lambda _limit: data,
        max_bytes=10 * 1024 * 1024,
        max_chars=max_chars,
    )


def _task(*attachments: TaskAttachment) -> TaskInput:
    return TaskInput(
        title="Create voucher",
        description="Admin creates a voucher.",
        comments=(TaskComment("dev", "2026-11-01", "Looks good.", id="10001"),),
        attachments=attachments,
        source="Jira",
        source_reference="PRJ-1",
    )


# ── Collectionِ شناخته‌شده ────────────────────────────────────────────────────


class TestRecognisedCollection:
    @pytest.mark.parametrize(("schema", "version"), [(V20, "2.0.0"), (V21, "2.1.0")])
    def test_a_supported_attachment_gets_a_structured_collection(self, schema, version):
        attachment = _process(
            "voucher.postman_collection.json", json.dumps(_collection(schema=schema))
        )

        assert attachment.status == ATTACHMENT_PROCESSED
        assert isinstance(attachment.postman, PostmanCollection)
        assert attachment.postman.version == version
        assert attachment.postman.requests_extracted == 2

    def test_its_task_source_carries_the_requests_with_provenance(self):
        attachment = _process("voucher.postman_collection.json", json.dumps(_collection()))
        sources = sources_from_task_input(_task(attachment))
        [source] = [s for s in sources if s.source_type == SOURCE_ATTACHMENT]

        assert source.label == "Attachment: voucher.postman_collection.json"
        assert source.reference == "voucher.postman_collection.json"
        assert source.requests == attachment.postman.requests
        first = source.requests[0]
        assert (first.method, first.host, first.base_path, first.path) == (
            "GET",
            "podium.admin2.devpod.ir",
            "/api",
            "/admin/voucher/0",
        )
        assert first.folder == ("Voucher",)

    def test_the_pipeline_entry_point_carries_the_requests(self):
        # pipeline.task_sources → sources_for_task (متنِ دست‌نخورده‌ی همان issue)
        task = _task(_process("c.json", json.dumps(_collection())))
        sources = sources_for_task(task.to_task_description(), task)

        assert sum(len(s.requests) for s in sources) == 2

    def test_no_secret_value_reaches_the_attachment_or_source_metadata(self):
        attachment = _process("c.json", json.dumps(_collection()))
        source = sources_from_task_input(_task(attachment))[-1]

        assert SECRET not in repr(attachment.postman)
        assert SECRET not in repr(source.requests)
        assert SECRET not in attachment.detail


# ── Collectionِ بزرگ‌تر از سقفِ پیوست ─────────────────────────────────────────


class TestLargeCollection:
    def test_it_is_parsed_before_the_character_limit(self):
        text = json.dumps(_collection(count=40, padding="x" * 500))
        limit = 2_000
        assert len(text) > limit * 5
        attachment = _process("big.json", text, max_chars=limit)

        assert attachment.postman is not None
        assert attachment.postman.requests_extracted == 40
        assert attachment.postman.requests_omitted == 0

    def test_raw_content_keeps_the_existing_truncation(self):
        text = json.dumps(_collection(count=40, padding="x" * 500))
        attachment = _process("big.json", text, max_chars=2_000)

        # JSONِ معتبر پیش از برش بدونِ تورفتگی ذخیره می‌شود؛ برش همان سقف است.
        stored = compact_json(text)
        assert attachment.truncated is True
        assert attachment.content == stored[:2_000]
        assert attachment.detail == f"text truncated to the first 2000 of {len(stored)} characters"

    def test_the_parser_limits_still_apply_and_are_reported(self):
        text = json.dumps(_collection(count=MAX_REQUESTS + 3))
        attachment = _process("huge.json", text, max_chars=1_000)

        assert attachment.postman.requests_extracted == MAX_REQUESTS
        assert attachment.postman.requests_discovered == MAX_REQUESTS + 3
        assert attachment.postman.requests_omitted == 3
        assert attachment.postman.limited
        assert attachment.truncated is True


# ── پیوست‌هایی که Collection نیستند ──────────────────────────────────────────


def _before_phase_3b(filename: str, content: str, max_chars: int = 100_000) -> TaskAttachment:
    """همان پیوست، بدونِ فیلدِ تازه — برای مقایسه با رفتارِ پیش از این مرحله."""
    attachment = _process(filename, content, max_chars=max_chars)
    return TaskAttachment(
        attachment.filename,
        attachment.media_type,
        attachment.size,
        attachment.status,
        attachment.content,
        attachment.detail,
        attachment.truncated,
    )


class TestNonCollectionAttachments:
    @pytest.mark.parametrize(
        ("filename", "content"),
        [
            ("sample.json", json.dumps({"url": "https://api.test/admin/voucher", "id": 1})),
            ("broken.json", '{"info": {"schema": "' + V21 + '"'),
            ("old.json", json.dumps(_collection(schema=V21.replace("v2.1.0", "v1.0.0")))),
            (
                "v1.json",
                json.dumps(
                    {
                        "id": "c",
                        "order": ["r"],
                        "requests": [{"url": "https://a.test", "method": "GET"}],
                    }
                ),
            ),
            ("postman_id.json", json.dumps({"info": {"_postman_id": "x"}, "item": []})),
        ],
    )
    def test_they_get_no_structured_collection_and_are_otherwise_unchanged(self, filename, content):
        attachment = _process(filename, content)

        assert attachment.postman is None
        assert attachment == _before_phase_3b(filename, content)

    def test_invalid_json_keeps_its_existing_note(self):
        attachment = _process("broken.json", "{not json")

        assert attachment.postman is None
        assert attachment.detail == "the file is not valid JSON; its text is included as-is"

    @pytest.mark.parametrize("filename", ["notes.txt", "notes.md", "spec.yaml"])
    def test_text_attachments_are_never_parsed(self, filename):
        # حتی وقتی محتوایشان یک Collectionِ معتبر است: فقط .json تجزیه می‌شود.
        content = json.dumps(_collection())
        with patch("src.task_sources.attachments.parse_postman_collection") as parse:
            attachment = _process(filename, content)

        parse.assert_not_called()
        assert attachment.postman is None
        assert attachment.content == content

    def test_failed_attachments_are_not_parsed(self):
        attachment = _process("binary.json", b"%PDF-1.4 not text")

        assert attachment.status == ATTACHMENT_FAILED
        assert attachment.postman is None


# ── سازگاری ──────────────────────────────────────────────────────────────────


class TestCompatibility:
    def test_task_description_output_is_unchanged(self):
        content = json.dumps(_collection())
        with_postman = _task(_process("c.json", content))
        without = _task(_before_phase_3b("c.json", content))

        assert with_postman.to_task_description() == without.to_task_description()

    def test_a_large_collection_does_not_change_the_task_description_either(self):
        content = json.dumps(_collection(count=30, padding="y" * 300))
        with_postman = _task(_process("c.json", content, max_chars=1_500))
        without = _task(_before_phase_3b("c.json", content, max_chars=1_500))

        assert with_postman.to_task_description() == without.to_task_description()

    def test_task_source_callers_without_requests_still_work(self):
        source = TaskSource("description", "", "Description", "text")

        assert source.requests == ()
        assert sources_from_text("text")[0].requests == ()

    def test_text_sha256_and_summary_are_unchanged(self):
        content = json.dumps(_collection())
        attachment = _process("c.json", content)
        source = sources_from_task_input(_task(attachment))[-1]
        stored = compact_json(content)
        plain = TaskSource(SOURCE_ATTACHMENT, "c.json", "Attachment: c.json", stored)

        assert source.text == stored
        assert source.sha256 == plain.sha256
        assert source.summary() == plain.summary()
        assert "requests" not in source.summary()

    def test_only_recognised_attachment_sources_get_requests(self):
        task = _task(
            _process("c.json", json.dumps(_collection())),
            _process("sample.json", json.dumps({"a": 1})),
        )
        sources = sources_from_task_input(task)

        assert [(s.label, len(s.requests)) for s in sources] == [
            ("Description", 0),
            ("Comment 1", 0),
            ("Attachment: c.json", 2),
            ("Attachment: sample.json", 0),
        ]

    def test_an_edited_task_text_falls_back_to_a_plain_description(self):
        task = _task(_process("c.json", json.dumps(_collection())))
        edited = task.to_task_description() + "\nedited"

        assert [s.requests for s in sources_for_task(edited, task)] == [()]

    def test_environment_evidence_still_uses_the_original_attachment_text(self):
        attachment = _process("c.json", json.dumps(_collection()))
        sources = sources_from_task_input(_task(attachment))
        decision = {
            "scope": "task",
            "environment": "vdi",
            "evidence": VDI_SENTENCE,
            "source_type": "attachment",
            "source_reference": "c.json",
        }
        result = extract_environment_decisions([decision], sources, ["TC-001"])

        (record,) = result["environment_decisions"]
        assert record["status"] == "ACCEPTED"
        assert record["source"]["label"] == "Attachment: c.json"
        assert record["source"]["sha256"] == sources[-1].sha256
        start, end = record["evidence"]["span"]
        assert attachment.content[start:end] == VDI_SENTENCE

    def test_evidence_cannot_come_from_structured_request_data(self):
        # «/admin/voucher/1» فقط به‌صورتِ مسیرِ ساختاریافته معنا دارد؛ شاهد فقط از text است.
        attachment = _process("c.json", json.dumps(_collection()))
        sources = sources_from_task_input(_task(attachment))
        decision = {
            "scope": "task",
            "environment": "vdi",
            "evidence": "GET podium.admin2.devpod.ir /api/admin/voucher/0",
            "source_type": "attachment",
            "source_reference": "c.json",
        }
        result = extract_environment_decisions([decision], sources, ["TC-001"])

        assert result["environment_decisions"] == []

    @pytest.mark.parametrize("error", [RuntimeError("boom"), RecursionError(), MemoryError()])
    def test_parser_exceptions_cannot_break_attachment_processing(self, error):
        content = json.dumps(_collection())
        with patch("src.task_sources.attachments.parse_postman_collection", side_effect=error):
            attachment = _process("c.json", content)

        assert attachment.status == ATTACHMENT_PROCESSED
        assert attachment.postman is None
        assert attachment.content == compact_json(content)
        assert attachment.detail == ""
