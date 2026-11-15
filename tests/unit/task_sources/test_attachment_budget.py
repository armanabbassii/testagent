"""
ورودیِ قدم اول — فشرده‌سازیِ بی‌اتلافِ JSON و بودجه‌ی کلِ متنِ پیوست‌ها

* JSONِ معتبر بدونِ فاصله‌های بیرون از رشته ذخیره می‌شود؛ هیچ کلید، مقدار، عدد یا
  رشته‌ای عوض نمی‌شود و برش پس از فشرده‌سازی است.
* متنِ پیوست‌های یک issue روی هم از JIRA_MAX_TOTAL_ATTACHMENT_CHARS بیشتر نمی‌شود؛
  پیوستی که جا نشود با یادداشتِ دلیل بریده می‌شود — هرگز بی‌صدا.
* Description و Commentها هرگز در بودجه حساب یا بریده نمی‌شوند.
* Collectionِ Postman پیش از برش تجزیه می‌شود، پس خلاصه‌اش کامل می‌ماند.

هیچ شبکه‌ای لازم نیست: Jiraِ ساختگی (fake_jira).
"""

from __future__ import annotations

import json

import pytest

from src.agents.test_case_generator.postman_summary import build_postman_summary
from src.task_sources.attachments import compact_json, process_attachment
from src.task_sources.jira import JiraClient, JiraConfigError, import_issue
from src.task_sources.sources import sources_for_task
from src.task_sources.task_input import ATTACHMENT_PROCESSED, ATTACHMENT_UNSUPPORTED
from tests.unit.task_sources.fake_jira import (
    FakeJira,
    adfs_settings,
    attachment_meta,
    comment,
    issue_body,
)

V21 = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
VDI = "این سرویس در vdi تست شود."
DESCRIPTION = "درصد کوپن باید بین ۱ تا ۱۰۰ باشد. POST /admin/voucher/percent"
BUDGET_NOTE = "total attachment text budget of"


def _process(filename: str, text: str, max_chars: int = 100_000, reason: str = ""):
    data = text.encode("utf-8")
    return process_attachment(
        filename=filename,
        media_type="",
        size=len(data),
        download=lambda limit: data,
        max_bytes=10 * 1024 * 1024,
        max_chars=max_chars,
        limit_reason=reason,
    )


def _collection(count: int) -> str:
    items = [
        {
            "name": f"request {i}",
            "request": {
                "method": "POST",
                "body": {
                    "mode": "raw",
                    "raw": json.dumps({"percent": i, "name": "کوپن  ویژه"}, ensure_ascii=False),
                },
                "url": {
                    "raw": f"{{{{baseUrl}}}}/admin/voucher/{i}",
                    "host": ["{{baseUrl}}"],
                    "path": ["admin", "voucher", str(i)],
                },
                "description": VDI,
            },
        }
        for i in range(count)
    ]
    collection = {"info": {"name": "Vouchers", "schema": V21}, "item": items}
    # همان قالبِ خروجیِ Postman: تورفتگی با tab.
    return json.dumps(collection, ensure_ascii=False, indent="\t")


# ── فشرده‌سازیِ JSON ─────────────────────────────────────────────────────────


class TestCompactJson:
    def test_it_is_lossless(self):
        value = {
            "text": 'two  spaces, a "quote", a back\\slash, \n and \t escapes',
            "persian": "تست ها و تست‌ها",
            "numbers": [1, 2.50, -3e2, 0],
            "nested": {"empty": "", "list": [], "null": None, "flag": True},
        }
        pretty = json.dumps(value, ensure_ascii=False, indent="\t")
        compact = compact_json(pretty)

        assert json.loads(compact) == json.loads(pretty)
        assert len(compact) < len(pretty)
        assert "two  spaces" in compact and "تست ها و تست‌ها" in compact

    def test_number_spelling_is_kept_exactly(self):
        assert compact_json('{"a": 1.0e+2, "b": [ 1.50 ]}') == '{"a":1.0e+2,"b":[1.50]}'

    @pytest.mark.parametrize("text", ['{"a": ', "{not json", "", "[1, 2"])
    def test_invalid_json_is_not_touched(self, text):
        assert compact_json(text) is None

    def test_an_invalid_json_attachment_keeps_its_text_and_note(self):
        attachment = _process("broken.json", '{"a":  1,')
        assert attachment.content == '{"a":  1,'
        assert "not valid JSON" in attachment.detail

    @pytest.mark.parametrize("filename", ["notes.txt", "notes.md", "spec.yaml"])
    def test_only_json_files_are_compacted(self, filename):
        text = json.dumps({"a": 1}, indent=2)
        assert _process(filename, text).content == text

    def test_it_runs_before_truncation_so_more_content_fits(self):
        text = _collection(30)
        limit = 3_000
        attachment = _process("c.postman_collection.json", text, max_chars=limit)
        compact = compact_json(text)

        assert attachment.content == compact[:limit]
        assert compact[:limit].count('"name":"request') > text[:limit].count('"name": "request')
        assert (
            attachment.detail == f"text truncated to the first {limit} of {len(compact)} characters"
        )

    def test_quoted_evidence_inside_json_strings_survives(self):
        attachment = _process("c.json", _collection(2))
        assert VDI in attachment.content
        assert "کوپن  ویژه" in attachment.content  # فاصله‌ی داخلِ رشته می‌ماند

    def test_the_postman_collection_is_parsed_from_the_original_text(self):
        attachment = _process("c.json", _collection(40), max_chars=500)
        assert attachment.postman is not None
        assert attachment.postman.requests_extracted == 40


# ── بودجه‌ی کلِ پیوست‌ها (importer) ──────────────────────────────────────────


def _import(files: dict[str, bytes], **settings):
    metas = [
        attachment_meta(
            name,
            len(data),
            "application/json" if name.endswith(".json") else "text/plain",
            number,
        )
        for number, (name, data) in enumerate(files.items(), start=1)
    ]
    server = FakeJira(
        issues={"DEMO-7": issue_body(description=DESCRIPTION, attachments=metas)},
        comments={"DEMO-7": [comment(1, "درصد صفر هم رد شود.", author="QA"), comment(2, VDI)]},
        files=files,
    )
    config = adfs_settings(**settings)
    result = import_issue(
        "DEMO-7", settings=config, client=JiraClient(config, session=server.session())
    )
    assert result.ok, result.error
    return result


class TestTotalAttachmentBudget:
    def test_attachments_beyond_the_budget_are_cut_with_a_reason(self):
        files = {f"part{i}.txt": (str(i) * 80_000).encode() for i in range(1, 5)}
        result = _import(files, max_attachment_chars=100_000, max_total_attachment_chars=200_000)
        a1, a2, a3, a4 = result.task_input.attachments

        assert (len(a1.content), len(a2.content), len(a3.content), len(a4.content)) == (
            80_000,
            80_000,
            40_000,
            0,
        )
        assert not a1.truncated and not a2.truncated
        assert a3.truncated and a4.truncated
        assert a3.detail.startswith("text truncated to the first 40000 of 80000 characters: ")
        assert BUDGET_NOTE in a3.detail and "JIRA_MAX_TOTAL_ATTACHMENT_CHARS" in a3.detail
        assert a4.detail.startswith("text not included (80000 characters): ")
        assert all(a.status == ATTACHMENT_PROCESSED for a in (a1, a2, a3, a4))
        # یادداشت‌ها هم در متنِ تسک (برای مدل) و هم در هشدارها (برای کاربر) هست.
        description = result.task_input.to_task_description()
        assert description.count(BUDGET_NOTE) == 2
        assert sum(BUDGET_NOTE in w for w in result.warnings) == 2

    def test_the_per_attachment_limit_still_applies_without_a_budget_note(self):
        files = {"big.txt": ("x" * 150_000).encode()}
        result = _import(files, max_attachment_chars=100_000, max_total_attachment_chars=200_000)
        (attachment,) = result.task_input.attachments

        assert len(attachment.content) == 100_000
        assert attachment.detail == "text truncated to the first 100000 of 150000 characters"

    def test_skipped_attachments_do_not_use_the_budget(self):
        files = {
            "shot.png": b"\x89PNG",
            "a.txt": ("a" * 60_000).encode(),
            "b.txt": ("b" * 60_000).encode(),
        }
        result = _import(files, max_attachment_chars=100_000, max_total_attachment_chars=120_000)
        png, a, b = result.task_input.attachments

        assert png.status == ATTACHMENT_UNSUPPORTED
        assert len(a.content) == len(b.content) == 60_000
        assert not b.truncated

    def test_description_and_comments_are_never_cut(self):
        files = {f"part{i}.txt": ("z" * 50_000).encode() for i in range(3)}
        result = _import(files, max_attachment_chars=100_000, max_total_attachment_chars=10)
        description = result.task_input.to_task_description()

        assert DESCRIPTION in description
        assert "درصد صفر هم رد شود." in description
        assert VDI in description
        assert sum(len(a.content) for a in result.task_input.attachments) == 10

    def test_a_cut_collection_still_gives_a_complete_postman_summary(self):
        files = {
            "spec.txt": ("s" * 100_000).encode(),
            "vouchers.postman_collection.json": _collection(25).encode(),
        }
        result = _import(files, max_attachment_chars=100_000, max_total_attachment_chars=100_100)
        task = result.task_input
        collection_attachment = task.attachments[1]

        assert len(collection_attachment.content) == 100
        assert collection_attachment.truncated
        assert collection_attachment.postman.requests_extracted == 25
        sources = sources_for_task(task.to_task_description(), task)
        summary = build_postman_summary(sources)
        assert summary.extracted == summary.included == 25
        assert "- POST /admin/voucher/24" in summary.text

    def test_everything_fits_under_the_default_budget_unchanged(self):
        files = {"notes.md": "## Notes\n- percent is an integer\n".encode()}
        result = _import(files)
        (attachment,) = result.task_input.attachments

        assert attachment.content == "## Notes\n- percent is an integer"
        assert attachment.detail == ""


class TestBudgetSettings:
    def test_the_default_is_two_full_attachments(self):
        settings = adfs_settings()
        assert settings.max_total_attachment_chars == 200_000
        assert settings.max_total_attachment_chars == 2 * settings.max_attachment_chars

    @pytest.mark.parametrize("value", [0, -1])
    def test_a_non_positive_budget_is_a_configuration_error(self, value):
        with pytest.raises(JiraConfigError, match="JIRA_MAX_TOTAL_ATTACHMENT_CHARS must be > 0"):
            adfs_settings(max_total_attachment_chars=value).validate()

    def test_from_config_reads_the_setting(self, monkeypatch):
        from src import config
        from src.task_sources.jira.settings import JiraSettings

        monkeypatch.setattr(config, "JIRA_MAX_TOTAL_ATTACHMENT_CHARS", 12_345)
        assert JiraSettings.from_config().max_total_attachment_chars == 12_345
