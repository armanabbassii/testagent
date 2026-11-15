"""
منبع‌آگاهیِ URLها در انتخابِ منابعِ Swagger — مرحله‌ی دومِ اصلاح

هر URL با منبعش (Developed Services، Description، هر Comment، هر Attachment)
طبقه‌بندی می‌شود:

    Swaggerِ ناشناخته در Developed Services یا Description → خطا (مثلِ قبل)
    Swaggerِ ناشناخته فقط در Comment یا Attachment       → فقط هشدار
    Swaggerِ ثبت‌شده در هر منبعی                         → شاهد (مثلِ قبل)
    سند (schemaِ Postman، admin.json) و endpoint          → مستقل از منبع (مثلِ قبل)

منابع از یک TaskInputِ واقعی با همان sources_from_task_input ساخته می‌شوند که
pipeline.task_sources استفاده می‌کند. هیچ درخواستِ شبکه یا LLMی انجام نمی‌شود.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.test_case_generator.source_selection import (
    DEVELOPED_SERVICES_LABEL,
    EVIDENCE_SWAGGER_URL,
    EXPLICIT_URL_ORIGINS,
    MODE_MANUAL,
    ORIGIN_DEVELOPED_SERVICES,
    select_sources,
)
from src.agents.test_case_generator.swagger_snapshots import (
    SELECTION_ADMIN,
    SELECTION_CRM,
    resolve_service_urls,
    resolve_urls_by_origin,
)
from src.task_sources.sources import (
    SOURCE_ATTACHMENT,
    SOURCE_COMMENT,
    SOURCE_DESCRIPTION,
    sources_for_task,
    sources_from_task_input,
)
from src.task_sources.task_input import (
    ATTACHMENT_PROCESSED,
    TaskAttachment,
    TaskComment,
    TaskInput,
)

UNKNOWN_SWAGGER = "https://host.test/api/swagger-ui/index.html?urls.primaryName=Billing"
UNKNOWN_RAW = "https://host.test/api/v3/api-docs/Billing"
ADMIN_UI = "https://podium-admin.sandpod.ir/api/swagger-ui/index.html?urls.primaryName=Admin"
CRM_RAW = "http://podium-back.devpod.ir/api/v3/api-docs/CRM"
POSTMAN_SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
PODIUM_ENDPOINT = "http://podium.admin2.devpod.ir/api/admin/voucher"

_ERROR = "do not match a registered Swagger source"
_CONTEXT = "appear only in Jira comments or attachments"


# ── snapshotهای ساختگی ───────────────────────────────────────────────────────

_OPERATIONS = {
    "admin": [
        ("get", "/admin/voucher/{id}", "getVoucher"),
        ("post", "/admin/voucher", "createVoucher"),
    ],
    "customer": [("get", "/customer/contact/{id}", "getContact")],
    "crm": [("get", "/crm/requests", "getRequests")],
    "provider": [("get", "/provider/serviceSet/{id}", "getServiceSet")],
    "site": [("get", "/serviceSet", "getServiceSets")],
}


@pytest.fixture
def snapshots(tmp_path: Path) -> Path:
    for key, operations in _OPERATIONS.items():
        paths: dict[str, dict] = {}
        for method, path, operation_id in operations:
            paths.setdefault(path, {})[method] = {
                "operationId": operation_id,
                "tags": [f"{key}-controller"],
                "summary": operation_id,
                "responses": {"200": {"description": "OK"}},
            }
        spec = {
            "openapi": "3.0.1",
            "info": {"title": key, "version": "1"},
            "servers": [{"url": "http://podium-back.devpod.ir/api"}],
            "paths": paths,
        }
        (tmp_path / f"{key}.json").write_text(json.dumps(spec), encoding="utf-8")
    return tmp_path


def _attachment(filename: str, content: str) -> TaskAttachment:
    return TaskAttachment(
        filename, "application/json", len(content), ATTACHMENT_PROCESSED, content=content
    )


def _postman(*urls: str) -> str:
    """Postman Collection با info.schema و یک درخواست برای هر URL."""
    return json.dumps(
        {
            "info": {"name": "Voucher", "schema": POSTMAN_SCHEMA},
            "item": [
                {"name": f"r{i}", "request": {"method": "POST", "url": {"raw": url}}}
                for i, url in enumerate(urls)
            ],
        },
        indent=2,
    )


def _task(
    description: str = "Admin creates a voucher via POST /admin/voucher.",
    comments: tuple[str, ...] = (),
    attachments: tuple[TaskAttachment, ...] = (),
) -> TaskInput:
    return TaskInput(
        title="Create voucher",
        description=description,
        comments=tuple(
            TaskComment("dev", "2026-11-01", body, id=str(100 + i))
            for i, body in enumerate(comments)
        ),
        attachments=attachments,
        source="Jira",
        source_reference="PRJ-1",
        source_url="https://jira.example.test/browse/PRJ-1",
    )


def _select(snapshots: Path, task: TaskInput, services: str = "", manual: str = ""):
    """همان مسیرِ pipeline: متنِ ساخته‌شده از TaskInput + منابعِ همان TaskInput."""
    text = task.to_task_description()
    return select_sources(
        text,
        services,
        manual=manual,
        directory=snapshots,
        sources=sources_for_task(text, task),
    )


# ── سیاستِ منابع ─────────────────────────────────────────────────────────────


class TestPolicy:
    def test_only_developed_services_and_description_are_explicit(self):
        assert EXPLICIT_URL_ORIGINS == frozenset({ORIGIN_DEVELOPED_SERVICES, SOURCE_DESCRIPTION})
        assert SOURCE_COMMENT not in EXPLICIT_URL_ORIGINS
        assert SOURCE_ATTACHMENT not in EXPLICIT_URL_ORIGINS

    def test_the_jira_sources_carry_their_labels(self):
        task = _task(comments=("note",), attachments=(_attachment("a.json", "{}"),))
        labels = [(s.source_type, s.label) for s in sources_from_task_input(task)]

        assert labels == [
            (SOURCE_DESCRIPTION, "Description"),
            (SOURCE_COMMENT, "Comment 1"),
            (SOURCE_ATTACHMENT, "Attachment: a.json"),
        ]


# ── Swaggerِ ناشناخته بسته به منبع ────────────────────────────────────────────


class TestUnknownSwaggerByOrigin:
    def test_in_the_description_it_still_stops_step2(self, snapshots):
        selection = _select(snapshots, _task(description=f"Spec: {UNKNOWN_SWAGGER}"))

        assert UNKNOWN_SWAGGER in selection.urls.unresolved
        assert _ERROR in selection.error
        assert f"{UNKNOWN_SWAGGER} (from Description)" in selection.error

    def test_in_developed_services_it_still_stops_step2(self, snapshots):
        selection = _select(snapshots, _task(), services=UNKNOWN_RAW)

        assert UNKNOWN_RAW in selection.urls.unresolved
        assert f"{UNKNOWN_RAW} (from {DEVELOPED_SERVICES_LABEL})" in selection.error

    def test_in_an_attachment_it_only_warns(self, snapshots):
        task = _task(
            attachments=(_attachment("spec-links.json", f'{{"swagger": "{UNKNOWN_SWAGGER}"}}'),)
        )
        selection = _select(snapshots, task)

        assert selection.error == ""
        assert selection.keys == (SELECTION_ADMIN,)
        assert selection.urls.unresolved == ()
        assert selection.urls.context_unresolved == (UNKNOWN_SWAGGER,)
        [warning] = [w for w in selection.warnings if _CONTEXT in w]
        assert f"{UNKNOWN_SWAGGER} (from Attachment: spec-links.json)" in warning

    def test_in_a_comment_it_only_warns_by_default(self, snapshots):
        selection = _select(snapshots, _task(comments=(f"old spec was {UNKNOWN_RAW}",)))

        assert selection.error == ""
        assert selection.urls.context_unresolved == (UNKNOWN_RAW,)
        assert any(f"{UNKNOWN_RAW} (from Comment 1)" in w for w in selection.warnings)

    def test_the_warning_survives_an_explicit_error(self, snapshots):
        task = _task(comments=(f"see {UNKNOWN_RAW}",))
        selection = _select(snapshots, task, services=UNKNOWN_SWAGGER)

        assert _ERROR in selection.error
        assert UNKNOWN_RAW not in selection.error
        assert any(UNKNOWN_RAW in w for w in selection.warnings)


# ── یک URL در چند منبع ────────────────────────────────────────────────────────


class TestSharedUrls:
    def test_all_origins_are_kept_in_order(self):
        resolution = resolve_urls_by_origin(
            [
                ("Developed Services", True, ""),
                ("Description", True, f"x {PODIUM_ENDPOINT}"),
                ("Comment 1", False, PODIUM_ENDPOINT),
                ("Attachment: c.json", False, PODIUM_ENDPOINT),
            ]
        )

        assert resolution.endpoints == (PODIUM_ENDPOINT,)
        assert resolution.origins_of(PODIUM_ENDPOINT) == (
            "Description",
            "Comment 1",
            "Attachment: c.json",
        )

    def test_an_explicit_origin_wins_over_a_context_origin(self, snapshots):
        task = _task(
            description=f"Spec: {UNKNOWN_SWAGGER}",
            attachments=(_attachment("links.json", f'"{UNKNOWN_SWAGGER}"'),),
        )
        selection = _select(snapshots, task)

        assert selection.urls.unresolved == (UNKNOWN_SWAGGER,)
        assert selection.urls.context_unresolved == ()
        assert f"{UNKNOWN_SWAGGER} (from Description, Attachment: links.json)" in selection.error

    def test_comment_and_attachment_together_stay_a_warning(self, snapshots):
        task = _task(
            comments=(UNKNOWN_RAW,),
            attachments=(_attachment("links.json", f'"{UNKNOWN_RAW}"'),),
        )
        selection = _select(snapshots, task)

        assert selection.error == ""
        assert any(
            f"{UNKNOWN_RAW} (from Comment 1, Attachment: links.json)" in w
            for w in selection.warnings
        )

    def test_a_url_repeated_in_one_source_is_listed_once(self):
        resolution = resolve_urls_by_origin([("Comment 1", False, f"{UNKNOWN_RAW} {UNKNOWN_RAW}")])

        assert resolution.context_unresolved == (UNKNOWN_RAW,)
        assert resolution.origins_of(UNKNOWN_RAW) == ("Comment 1",)


# ── رفتارهایی که نباید عوض شوند ───────────────────────────────────────────────


class TestUnchangedBehaviour:
    @pytest.mark.parametrize("where", ["description", "comment", "attachment", "services"])
    def test_a_registered_swagger_url_is_evidence_from_any_origin(self, snapshots, where):
        task = _task(
            description=f"Uses {CRM_RAW}"
            if where == "description"
            else "Fetch CRM requests via GET /crm/requests.",
            comments=(f"CRM: {CRM_RAW}",) if where == "comment" else (),
            attachments=(_attachment("crm.json", f'"{CRM_RAW}"'),) if where == "attachment" else (),
        )
        selection = _select(snapshots, task, services=CRM_RAW if where == "services" else "")

        assert selection.error == ""
        assert SELECTION_CRM in selection.keys
        assert selection.urls.matched == ((CRM_RAW, SELECTION_CRM),)
        [evidence] = [
            e for e in selection.evidence_for(SELECTION_CRM) if e.kind == EVIDENCE_SWAGGER_URL
        ]
        assert evidence.detail.startswith(f"{CRM_RAW} (from ")

    def test_the_postman_schema_in_an_attachment_is_still_a_document(self, snapshots):
        task = _task(
            attachments=(_attachment("voucher.postman_collection.json", _postman(PODIUM_ENDPOINT)),)
        )
        selection = _select(snapshots, task)

        assert selection.error == ""
        assert selection.urls.documents == (POSTMAN_SCHEMA,)
        assert any(
            f"{POSTMAN_SCHEMA} (from Attachment: voucher.postman_collection.json)" in w
            for w in selection.warnings
        )

    def test_attachment_endpoints_are_still_mapping_evidence(self, snapshots):
        task = _task(
            description="Admin creates a voucher.",
            attachments=(
                _attachment("voucher.postman_collection.json", _postman(PODIUM_ENDPOINT)),
            ),
        )
        selection = _select(snapshots, task)

        assert selection.keys == (SELECTION_ADMIN,)
        match = next(m for m in selection.endpoints if m.path == "/api/admin/voucher")
        assert match.owners == (SELECTION_ADMIN,)
        assert match.operations == ("POST /admin/voucher",)

    def test_manual_selection_is_unchanged_and_keeps_the_warning(self, snapshots):
        task = _task(comments=(UNKNOWN_RAW,))
        selection = _select(snapshots, task, manual="admin")

        assert selection.mode == MODE_MANUAL
        assert selection.keys == (SELECTION_ADMIN,)
        assert selection.error == ""
        assert any(UNKNOWN_RAW in w for w in selection.warnings)

    def test_manual_selection_still_stops_on_an_explicit_unknown_swagger(self, snapshots):
        selection = _select(snapshots, _task(), services=UNKNOWN_RAW, manual="admin")

        assert _ERROR in selection.error

    def test_without_sources_the_whole_text_is_explicit(self, snapshots):
        # فراخوانِ قدیمی (بدونِ sources): هر Swaggerِ ناشناخته‌ی متن خطاست.
        text = _task(comments=(UNKNOWN_RAW,)).to_task_description()
        selection = select_sources(text, "", directory=snapshots)

        assert UNKNOWN_RAW in selection.urls.unresolved
        assert _ERROR in selection.error

    def test_an_edited_jira_text_falls_back_to_the_strict_mode(self, snapshots):
        # sources_for_task: متنِ ویرایش‌شده دیگر منابعِ Jira را ندارد.
        task = _task(comments=(UNKNOWN_RAW,))
        edited = task.to_task_description() + "\nEdited by the user."
        selection = select_sources(
            edited, "", directory=snapshots, sources=sources_for_task(edited, task)
        )

        assert _ERROR in selection.error

    def test_resolve_service_urls_keeps_its_strict_single_text_contract(self):
        resolution = resolve_service_urls(f"{UNKNOWN_RAW} {ADMIN_UI}")

        assert resolution.unresolved == (UNKNOWN_RAW,)
        assert resolution.context_unresolved == ()
        assert resolution.matched == ((ADMIN_UI, SELECTION_ADMIN),)
        # متنِ بی‌منبع برچسبی ندارد؛ پیام‌ها مثلِ قبل فقط URL را نام می‌برند.
        assert resolution.describe(UNKNOWN_RAW) == UNKNOWN_RAW
        assert f"  - {UNKNOWN_RAW}\n" in resolution.error()


class TestPipelinePassesTheSources:
    def test_plan_swagger_sources_forwards_the_task_sources(self, monkeypatch):
        from ui import pipeline

        captured = {}

        def fake_select(task_description, developed_services, **kwargs):
            captured.update(kwargs)
            return "selection"

        monkeypatch.setattr(pipeline, "select_sources", fake_select)
        monkeypatch.setattr(pipeline, "task_sources", lambda state: ("SOURCES",))

        assert pipeline.plan_swagger_sources(pipeline.WorkflowState()) == "selection"
        assert captured["sources"] == ("SOURCES",)
