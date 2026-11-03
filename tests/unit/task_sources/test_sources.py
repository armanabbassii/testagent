"""
تست‌های منابعِ شاهد — همان متن‌هایی که مدل در بخش‌های Description/Comments/
Attachments می‌بیند، بدونِ سرصفحه‌های تولیدی.
"""

from __future__ import annotations

from src.task_sources.jira import JiraClient, import_issue
from src.task_sources.sources import (
    sources_for_task,
    sources_from_task_input,
    sources_from_text,
)
from src.task_sources.task_input import (
    ATTACHMENT_PROCESSED,
    ATTACHMENT_UNSUPPORTED,
    TaskAttachment,
    TaskComment,
    TaskInput,
)
from tests.unit.task_sources.fake_jira import FakeJira, adfs_settings, comment, issue_body


def _task(**overrides) -> TaskInput:
    values = {
        "title": "Voucher types",
        "description": "  Add an endpoint.  ",
        "comments": (
            TaskComment("A", "d1", " First comment ", id="501"),
            TaskComment("B", "d2", "Second comment", id="502"),
        ),
        "attachments": (
            TaskAttachment("notes.md", "text/markdown", 9, ATTACHMENT_PROCESSED, "Run in VDI"),
            TaskAttachment("shot.png", "image/png", 9, ATTACHMENT_UNSUPPORTED),
        ),
        "source": "jira",
        "source_reference": "DEMO-7",
    }
    values.update(overrides)
    return TaskInput(**values)


class TestSourcesFromTaskInput:
    def test_one_source_per_section_with_real_references(self):
        sources = sources_from_task_input(_task())
        assert [(s.source_type, s.reference, s.label, s.text) for s in sources] == [
            ("description", "", "Description", "Add an endpoint."),
            ("comment", "501", "Comment 1", "First comment"),
            ("comment", "502", "Comment 2", "Second comment"),
            ("attachment", "notes.md", "Attachment: notes.md", "Run in VDI"),
        ]

    def test_generated_headers_are_in_no_source(self):
        joined = " ".join(source.text for source in sources_from_task_input(_task()))
        assert "# Task DEMO-7" not in joined
        assert "How to read this task" not in joined

    def test_the_comment_id_does_not_change_the_task_text(self):
        with_ids = _task()
        without_ids = _task(
            comments=tuple(TaskComment(c.author, c.created, c.body) for c in with_ids.comments)
        )
        assert with_ids.to_task_description() == without_ids.to_task_description()

    def test_summary_has_metadata_but_no_text(self):
        summary = sources_from_task_input(_task())[1].summary()
        assert summary["reference"] == "501"
        assert summary["chars"] == len("First comment")
        assert "text" not in summary


class TestSourcesForTask:
    def test_the_rendered_text_of_the_same_input_uses_its_sources(self):
        task = _task()
        assert sources_for_task(task.to_task_description(), task) == sources_from_task_input(task)

    def test_edited_or_unrelated_text_is_manual(self):
        task = _task()
        assert sources_for_task("edited", task) == sources_from_text("edited")
        assert sources_for_task("manual", None) == sources_from_text("manual")


class TestImporterKeepsCommentIds:
    def test_jira_comment_ids_are_recorded(self):
        server = FakeJira(
            issues={"DEMO-7": issue_body()}, comments={"DEMO-7": [comment(1), comment(2)]}
        )
        client = JiraClient(adfs_settings(), session=server.session())

        imported = import_issue("DEMO-7", settings=adfs_settings(), client=client)

        assert [item.id for item in imported.task_input.comments] == ["1", "2"]
