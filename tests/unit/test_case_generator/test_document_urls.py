"""
URLهای «سند» در تشخیصِ Swagger — مرحله‌ی اولِ اصلاحِ schemaِ Postman Collection

پیش از این، هر URLی که به ``.json``/``.yaml`` ختم می‌شد Swagger حساب می‌شد؛ پس
``info.schema``ِ یک Postman Collection در پیوستِ Jira
(``https://schema.getpostman.com/json/collection/v2.1.0/collection.json``) یک
«Swaggerِ حل‌نشده» بود و قدم ۲ را متوقف می‌کرد. حالا فقط نشانه‌ی صریح Swagger
است و بقیه «سند»ِ مبهم‌اند: نه خطا، نه endpoint، ولی همیشه در هشدارِ قدم ۲.

هیچ درخواستِ شبکه یا LLMی انجام نمی‌شود؛ snapshotها فایل‌های ساختگیِ tmp_path‌اند.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.test_case_generator.source_selection import (
    MODE_MANUAL,
    endpoint_references,
    select_sources,
)
from src.agents.test_case_generator.swagger_snapshots import (
    SELECTION_ADMIN,
    SWAGGER_SOURCES,
    is_document_url,
    is_swagger_url,
    resolve_service_urls,
    source_for_url,
)
from src.task_sources.text import fence_block

POSTMAN_SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
POSTMAN_SCHEMA_V2 = "https://schema.getpostman.com/json/collection/v2.0.0/collection.json"
PODIUM_HOST = "http://podium.admin2.devpod.ir"
ADMIN_UI = "https://podium-admin.sandpod.ir/api/swagger-ui/index.html?urls.primaryName=Admin"
ADMIN_RAW = "http://podium-back.devpod.ir/api/v3/api-docs/Admin"


# ── snapshotهای ساختگی (همان قالبِ test_source_selection) ─────────────────────

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


def _postman_attachment() -> str:
    """یک پیوستِ Jira با Postman Collection، همان‌طور که to_task_description می‌سازد."""
    collection = {
        "info": {"_postman_id": "1", "name": "Voucher", "schema": POSTMAN_SCHEMA},
        "item": [
            {
                "name": "create voucher",
                "request": {
                    "method": "POST",
                    "url": {
                        "raw": f"{PODIUM_HOST}/api/admin/voucher",
                        "host": ["podium", "admin2", "devpod", "ir"],
                        "path": ["api", "admin", "voucher"],
                    },
                },
            }
        ],
    }
    return (
        "# Task PRJ-1: Create voucher\n\n## Description (original requirement)\n\n"
        "Admin can create a voucher.\n\n## Attachments (1 processed, 0 not processed)\n\n"
        "### Attachment: voucher.postman_collection.json (application/json, 512 bytes)\n\n"
        + fence_block(json.dumps(collection, indent=2), "json")
    )


# ── طبقه‌بندیِ یک URL ─────────────────────────────────────────────────────────


class TestPostmanSchemaIsNotSwagger:
    @pytest.mark.parametrize("url", [POSTMAN_SCHEMA, POSTMAN_SCHEMA_V2])
    def test_a_postman_collection_schema_is_not_swagger(self, url):
        assert is_swagger_url(url) is False

    @pytest.mark.parametrize("url", [POSTMAN_SCHEMA, POSTMAN_SCHEMA_V2])
    def test_a_postman_collection_schema_is_a_document(self, url):
        assert is_document_url(url) is True

    def test_the_schema_host_is_never_swagger_even_with_a_swagger_marker(self):
        # میزبانِ schemaِ قالب سندِ API منتشر نمی‌کند، هر چه در مسیرش باشد.
        assert is_swagger_url("https://schema.getpostman.com/swagger/schema.json") is False

    def test_the_host_check_ignores_case(self):
        assert (
            is_swagger_url(POSTMAN_SCHEMA.replace("schema.getpostman.com", "Schema.GetPostman.com"))
            is False
        )


class TestExplicitSwaggerMarkersStillCount:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.test/swagger.json",
            "https://example.test/docs/swagger.yaml",
            "https://example.test/openapi.json",
            "https://example.test/openapi.yaml",
            "https://example.test/specs/openapi.yml",
            "https://example.test/api/v3/api-docs",
            ADMIN_RAW,
            ADMIN_UI,
            "https://host.test/api/swagger-ui/index.html",
            "https://host.test/docs/index.html?urls.primaryName=Billing",
        ],
    )
    def test_explicit_swagger_documents_are_recognized(self, url):
        assert is_swagger_url(url) is True
        assert is_document_url(url) is False


class TestAmbiguousDataFiles:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.test/admin.json",
            "https://example.test/specs/admin.yaml",
            "https://example.test/files/config.yml",
            "https://example.test/files/sample-response.JSON",
        ],
    )
    def test_a_data_file_without_a_marker_is_not_certain_swagger(self, url):
        assert is_swagger_url(url) is False
        assert is_document_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            f"{PODIUM_HOST}/api/admin/voucher",
            f"{PODIUM_HOST}/api",
            "https://json-schema.org/draft-07/schema",
        ],
    )
    def test_endpoints_are_neither_swagger_nor_documents(self, url):
        assert is_swagger_url(url) is False
        assert is_document_url(url) is False

    def test_registered_sources_still_resolve_unchanged(self):
        assert source_for_url(ADMIN_UI).key == SELECTION_ADMIN
        assert source_for_url(ADMIN_RAW).key == SELECTION_ADMIN
        for source in SWAGGER_SOURCES:
            assert source_for_url(source.remote_url).key == source.key


# ── resolve_service_urls: دسته‌ی documents ─────────────────────────────────────


class TestResolution:
    def test_each_url_lands_in_exactly_one_category(self):
        text = (
            f"{ADMIN_UI}\n{POSTMAN_SCHEMA}\nhttps://example.test/admin.json\n"
            "https://host.test/api/v3/api-docs/Billing\n"
            f"{PODIUM_HOST}/api/admin/voucher"
        )
        resolution = resolve_service_urls(text)

        assert resolution.matched == ((ADMIN_UI, SELECTION_ADMIN),)
        assert resolution.unresolved == ("https://host.test/api/v3/api-docs/Billing",)
        assert resolution.documents == (POSTMAN_SCHEMA, "https://example.test/admin.json")
        assert resolution.endpoints == (f"{PODIUM_HOST}/api/admin/voucher",)

    def test_documents_alone_produce_no_error_but_a_warning(self):
        resolution = resolve_service_urls(POSTMAN_SCHEMA)

        assert resolution.error() == ""
        warning = resolution.document_warning()
        assert POSTMAN_SCHEMA in warning
        assert "not used as Swagger sources" in warning

    def test_no_documents_means_no_warning(self):
        assert resolve_service_urls(ADMIN_UI).document_warning() == ""

    def test_documents_are_not_endpoint_references(self):
        text = f"{POSTMAN_SCHEMA} https://example.test/admin.json {PODIUM_HOST}/api/admin/voucher"
        paths = [path for _, path in endpoint_references(text)]

        assert paths == ["/api/admin/voucher"]


# ── select_sources: کلِ قدم ۲ ─────────────────────────────────────────────────


class TestStep2WithAPostmanAttachment:
    def test_the_collection_schema_does_not_stop_step2(self, snapshots):
        selection = select_sources(_postman_attachment(), "", directory=snapshots)

        assert selection.error == ""
        assert selection.urls.unresolved == ()
        assert POSTMAN_SCHEMA in selection.urls.documents

    def test_the_podium_endpoint_is_still_assigned_to_admin(self, snapshots):
        selection = select_sources(_postman_attachment(), "", directory=snapshots)

        assert selection.ok
        assert selection.keys == (SELECTION_ADMIN,)
        [match] = selection.endpoints
        assert match.path == "/api/admin/voucher"
        assert match.owners == (SELECTION_ADMIN,)
        # پیشوندِ gateway (/api) حذف و عملیات در snapshotِ Admin تأیید شد.
        assert match.operations == ("POST /admin/voucher",)

    def test_the_ignored_schema_is_visible_in_the_step2_warnings(self, snapshots):
        selection = select_sources(_postman_attachment(), "", directory=snapshots)

        assert any(POSTMAN_SCHEMA in warning for warning in selection.warnings)

    def test_manual_selection_keeps_working_and_shows_the_warning(self, snapshots):
        selection = select_sources(_postman_attachment(), "", manual="admin", directory=snapshots)

        assert selection.mode == MODE_MANUAL
        assert selection.keys == (SELECTION_ADMIN,)
        assert selection.error == ""
        assert any(POSTMAN_SCHEMA in warning for warning in selection.warnings)


class TestUserProvidedUrlsAreNeverSilentlyIgnored:
    def test_an_unknown_explicit_swagger_url_in_developed_services_still_stops(self, snapshots):
        url = "https://host.test/api/swagger-ui/index.html?urls.primaryName=Billing"
        selection = select_sources("Admin creates a voucher.", url, directory=snapshots)

        assert url in selection.urls.unresolved
        assert "do not match a registered Swagger source" in selection.error

    def test_an_unknown_explicit_swagger_url_in_the_description_still_stops(self, snapshots):
        url = "https://host.test/specs/openapi.yaml"
        selection = select_sources(f"See {url} for the new API.", "", directory=snapshots)

        assert "do not match a registered Swagger source" in selection.error

    def test_an_ambiguous_file_in_developed_services_is_reported(self, snapshots):
        url = "https://example.test/admin.json"
        selection = select_sources(
            "Admin creates a voucher.",
            f"{url}\nPOST /admin/voucher",
            directory=snapshots,
        )

        assert selection.error == ""
        assert selection.keys == (SELECTION_ADMIN,)
        assert any(url in warning for warning in selection.warnings)

    def test_the_document_warning_survives_an_unrelated_error(self, snapshots):
        unknown = "https://host.test/api/v3/api-docs/Billing"
        selection = select_sources(_postman_attachment(), unknown, directory=snapshots)

        assert selection.error
        assert any(POSTMAN_SCHEMA in warning for warning in selection.warnings)

    def test_the_document_warning_survives_an_invalid_manual_selection(self, snapshots):
        selection = select_sources(_postman_attachment(), "", manual="billing", directory=snapshots)

        assert selection.error
        assert any(POSTMAN_SCHEMA in warning for warning in selection.warnings)
