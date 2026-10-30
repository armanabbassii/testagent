"""
تست‌های تطبیقِ URLهای Developed Services با منابعِ Swagger — swagger_snapshots.py

URLهایی که کاربر در قدم ۱ وارد می‌کند تعیین می‌کنند قدم ۲ کدام snapshotِ محلی
را بخواند. این تطبیق بدونِ شبکه است و URLِ Swaggerِ ناشناخته هرگز بی‌صدا با
سندِ دیگری جایگزین نمی‌شود.
"""

from __future__ import annotations

import pytest

from src.agents.test_case_generator.api_discovery import swagger_group_name
from src.agents.test_case_generator.swagger_snapshots import (
    SELECTION_ADMIN,
    SELECTION_CUSTOMER,
    SWAGGER_SOURCES,
    extract_urls,
    is_swagger_url,
    resolve_service_urls,
    selection_for_keys,
    source_for_url,
)

# همان شکلِ URLی که placeholderِ صفحه‌ی قدم ۲ نشان می‌دهد (میزبانِ sandpod)
_ADMIN_UI = (
    "https://podium-admin.sandpod.ir/api/swagger-ui/index.html"
    "?urls.primaryName=Admin#/voucher-admin-controller/getVoucherDetails"
)
_CUSTOMER_UI = "https://podium.sandpod.ir/api/swagger-ui/index.html?urls.primaryName=Customer"
_ADMIN_RAW = "http://podium-back.devpod.ir/api/v3/api-docs/Admin"
_CUSTOMER_RAW_OTHER_HOST = "https://podium.sandpod.ir/api/v3/api-docs/Customer"
_UNKNOWN_GROUP = "https://host.test/api/swagger-ui/index.html?urls.primaryName=Billing"
_UNGROUPED_UI = "https://host.test/api/swagger-ui/index.html"
_SPEC_FILE = "https://example.test/swagger.json"
_ENDPOINT = "https://podium-admin.sandpod.ir/api/admin/voucher/12"


class TestSwaggerGroupName:
    def test_the_group_of_a_swagger_ui_url(self):
        assert swagger_group_name(_ADMIN_UI) == "Admin"

    def test_the_group_of_a_raw_openapi_url(self):
        assert swagger_group_name(_ADMIN_RAW) == "Admin"

    def test_a_url_without_a_group(self):
        assert swagger_group_name(_UNGROUPED_UI) == ""
        assert swagger_group_name("http://host/api/v3/api-docs") == ""


class TestExtractUrls:
    def test_urls_are_found_in_free_text_in_order(self):
        text = f"Admin APIs: {_ADMIN_UI}\nGET /admin/voucher/{{id}}\nsee {_CUSTOMER_UI}."
        assert extract_urls(text) == [_ADMIN_UI, _CUSTOMER_UI]

    def test_duplicates_are_dropped(self):
        assert extract_urls(f"{_ADMIN_RAW} {_ADMIN_RAW}") == [_ADMIN_RAW]

    def test_trailing_sentence_punctuation_is_not_part_of_the_url(self):
        assert extract_urls(f"Swagger: {_ADMIN_RAW}.") == [_ADMIN_RAW]

    def test_text_without_urls(self):
        assert extract_urls("POST /admin/voucher") == []
        assert extract_urls("") == []


class TestClassification:
    @pytest.mark.parametrize(
        "url", [_ADMIN_UI, _ADMIN_RAW, _UNGROUPED_UI, _SPEC_FILE, _UNKNOWN_GROUP]
    )
    def test_swagger_documents_are_recognized(self, url):
        assert is_swagger_url(url) is True

    def test_an_endpoint_is_not_a_swagger_document(self):
        assert is_swagger_url(_ENDPOINT) is False


class TestSourceForUrl:
    def test_a_swagger_ui_url_resolves_by_its_group(self):
        assert source_for_url(_ADMIN_UI).key == SELECTION_ADMIN
        assert source_for_url(_CUSTOMER_UI).key == SELECTION_CUSTOMER

    def test_the_registered_raw_url_resolves_exactly(self):
        for source in SWAGGER_SOURCES:
            assert source_for_url(source.remote_url) is source

    def test_the_same_group_on_another_host_resolves_to_the_same_source(self):
        assert source_for_url(_CUSTOMER_RAW_OTHER_HOST).key == SELECTION_CUSTOMER

    @pytest.mark.parametrize("url", [_UNKNOWN_GROUP, _UNGROUPED_UI, _SPEC_FILE, _ENDPOINT])
    def test_unknown_urls_do_not_resolve(self, url):
        assert source_for_url(url) is None


class TestResolveServiceUrls:
    def test_a_single_admin_url_selects_only_the_admin_snapshot(self):
        resolution = resolve_service_urls(f"Voucher details: {_ADMIN_UI}")
        assert resolution.keys == [SELECTION_ADMIN]
        assert resolution.matched == ((_ADMIN_UI, SELECTION_ADMIN),)
        assert resolution.error() == ""

    def test_both_sources_keep_the_registry_order(self):
        resolution = resolve_service_urls(f"{_CUSTOMER_UI}\n{_ADMIN_RAW}")
        assert resolution.keys == [SELECTION_ADMIN, SELECTION_CUSTOMER]

    def test_an_unknown_swagger_url_is_an_error_naming_the_url(self):
        resolution = resolve_service_urls(f"{_ADMIN_UI}\n{_UNKNOWN_GROUP}")
        assert resolution.unresolved == (_UNKNOWN_GROUP,)
        assert _UNKNOWN_GROUP in resolution.error()
        assert "urls.primaryName" in resolution.error()

    def test_endpoint_urls_are_kept_but_do_not_select_a_source(self):
        resolution = resolve_service_urls(f"{_ENDPOINT}")
        assert resolution.endpoints == (_ENDPOINT,)
        assert resolution.keys == []
        assert resolution.error() == ""

    def test_plain_text_resolves_to_nothing(self):
        resolution = resolve_service_urls("POST /admin/voucher, GET /admin/voucher/{id}")
        assert resolution.keys == []
        assert resolution.unresolved == ()


class TestSelectionForKeys:
    def test_keys_map_back_to_a_selection(self):
        assert selection_for_keys([SELECTION_ADMIN]) == SELECTION_ADMIN
        assert selection_for_keys([SELECTION_CUSTOMER]) == SELECTION_CUSTOMER
        assert selection_for_keys([SELECTION_CUSTOMER, SELECTION_ADMIN]) == "admin,customer"

    def test_no_keys_is_no_selection(self):
        assert selection_for_keys([]) == ""
