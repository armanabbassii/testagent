"""تست‌های کشفِ API از سندِ Swagger/OpenAPI (قدم دوم)."""

import json

import pytest

from src.agents.test_case_generator.api_discovery import (
    AUTHORIZATION_VALUE,
    DiscoveredApi,
    DiscoveredService,
    SwaggerLoadError,
    discover_from_spec,
    discover_services,
    load_service,
    operation_ids,
    operation_index,
    operation_key,
    split_sources,
)


def _spec(**overrides) -> dict:
    """یک سندِ OpenAPI v3 کوچک با چند عملیاتِ متفاوت."""
    spec = {
        "openapi": "3.0.1",
        "info": {"title": "Admin API", "version": "2.1.0"},
        "servers": [{"url": "https://podium-admin.sandpod.ir"}],
        "paths": {
            "/admin/voucher/{id}": {
                "get": {
                    "operationId": "getVoucherDetails",
                    "summary": "Get voucher details",
                    "tags": ["voucher-admin-controller"],
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                            "description": "Voucher identifier",
                        },
                        {
                            "name": "verbose",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean"},
                        },
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Voucher"}
                                }
                            }
                        },
                        "404": {"description": "Not found"},
                    },
                },
                "put": {
                    "operationId": "updateVoucher",
                    "summary": "Update voucher",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["amount"],
                                    "properties": {"amount": {"type": "number"}},
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Updated"}},
                },
            },
            "/admin/voucher/{id}/active": {
                "put": {
                    "operationId": "activateVoucher",
                    "summary": "Activate voucher",
                    "responses": {"200": {"description": "Activated"}},
                }
            },
        },
        "components": {
            "schemas": {
                "Voucher": {
                    "type": "object",
                    "required": ["amount"],
                    "properties": {"amount": {"type": "number"}},
                }
            }
        },
    }
    spec.update(overrides)
    return spec


# ── کشفِ عملیات‌ها ───────────────────────────────────────────────────────────

class TestDiscoverFromSpec:
    def test_all_operations_are_discovered(self):
        service = discover_from_spec(_spec())

        assert [(api.method, api.path) for api in service.apis] == [
            ("GET", "/admin/voucher/{id}"),
            ("PUT", "/admin/voucher/{id}"),
            ("PUT", "/admin/voucher/{id}/active"),
        ]

    def test_operation_id_and_summary_are_extracted(self):
        service = discover_from_spec(_spec())
        get = service.apis[0]

        assert get.operation_id == "getVoucherDetails"
        assert get.summary == "Get voucher details"

    def test_path_and_query_parameters_are_extracted(self):
        service = discover_from_spec(_spec())
        parameters = {param["name"]: param for param in service.apis[0].parameters}

        assert parameters["id"]["in"] == "path"
        assert parameters["id"]["required"] is True
        assert parameters["id"]["type"] == "string"
        assert parameters["verbose"]["in"] == "query"
        assert parameters["verbose"]["required"] is False

    def test_request_body_is_extracted_with_required_fields(self):
        service = discover_from_spec(_spec())
        put = service.apis[1]

        assert put.request_body == {"amount": 0}

    def test_responses_carry_success_and_error_codes(self):
        service = discover_from_spec(_spec())
        responses = service.apis[0].responses

        assert "200" in responses
        assert "404" in responses
        assert responses["200"]["fields"] == ["amount"]

    def test_base_url_comes_from_servers(self):
        assert discover_from_spec(_spec()).base_url == "https://podium-admin.sandpod.ir"

    def test_base_url_comes_from_v2_host_and_base_path(self):
        spec = _spec(
            servers=None,
            swagger="2.0",
            host="api.example.com",
            basePath="/v1",
            schemes=["https"],
        )
        assert discover_from_spec(spec).base_url == "https://api.example.com/v1"

    def test_relative_server_url_is_resolved_against_source(self):
        spec = _spec(servers=[{"url": "/api"}])

        service = discover_from_spec(spec, source_url="https://host.example/v3/api-docs")

        assert service.base_url == "https://host.example/api"

    def test_undeterminable_base_url_is_reported_not_guessed(self):
        spec = _spec(servers=None)

        service = discover_from_spec(spec)

        assert service.base_url == ""
        assert any("Base URL" in note for note in service.unresolved)

    def test_requires_auth_from_global_security(self):
        service = discover_from_spec(_spec(security=[{"bearerAuth": []}]))

        assert service.requires_auth is True
        assert service.authorization == {
            "header": "Authorization",
            "value": AUTHORIZATION_VALUE,
        }

    def test_no_authorization_when_spec_does_not_require_it(self):
        service = discover_from_spec(_spec())

        assert service.requires_auth is False
        assert service.authorization is None

    def test_name_comes_from_swagger_group_in_source_url(self):
        url = "https://host/api/swagger-ui/index.html?urls.primaryName=Admin#/x/y"

        assert discover_from_spec(_spec(), source_url=url).name == "Admin"

    def test_name_falls_back_to_document_title(self):
        assert discover_from_spec(_spec(), source_url="https://host/v3/api-docs").name == (
            "Admin API"
        )

    def test_explicit_name_wins(self):
        assert discover_from_spec(_spec(), name="custom").name == "custom"

    def test_spec_without_paths_is_rejected(self):
        with pytest.raises(SwaggerLoadError, match="no 'paths'"):
            discover_from_spec({"openapi": "3.0.1", "paths": {}})

    def test_empty_spec_is_rejected(self):
        with pytest.raises(SwaggerLoadError, match="empty"):
            discover_from_spec({})

    def test_non_dict_spec_is_rejected(self):
        with pytest.raises(SwaggerLoadError):
            discover_from_spec(["not", "a", "spec"])  # type: ignore[arg-type]

    def test_to_dict_matches_the_step2_shape(self):
        api = discover_from_spec(_spec()).apis[0].to_dict()

        assert set(api) == {
            "method",
            "path",
            "operation_id",
            "summary",
            "parameters",
            "request_body",
            "responses",
        }


# ── بارگذاری از منبع ─────────────────────────────────────────────────────────

class TestLoadService:
    def test_loads_from_a_local_file(self, tmp_path):
        path = tmp_path / "openapi.json"
        path.write_text(json.dumps(_spec()), encoding="utf-8")

        service = load_service(str(path))

        assert len(service.apis) == 3
        assert service.source_url == str(path)

    def test_invalid_local_file_is_reported(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(SwaggerLoadError, match="could not be read"):
            load_service(str(path))

    def test_empty_source_is_rejected(self):
        with pytest.raises(SwaggerLoadError, match="empty"):
            load_service("   ")

    def test_non_url_non_file_source_is_rejected(self):
        with pytest.raises(SwaggerLoadError, match="neither a URL nor a readable file"):
            load_service("./does-not-exist.json")

    def test_discover_services_collects_errors_without_stopping(self, tmp_path):
        path = tmp_path / "openapi.json"
        path.write_text(json.dumps(_spec()), encoding="utf-8")

        services, errors = discover_services([str(path), "./missing.json"])

        assert len(services) == 1
        assert len(errors) == 1


# ── کلیدِ تطبیق ──────────────────────────────────────────────────────────────

class TestOperationKey:
    def test_method_is_upper_cased(self):
        assert operation_key("get", "/x") == ("GET", "/x")

    def test_placeholder_names_are_ignored(self):
        assert operation_key("GET", "/admin/voucher/{voucherId}") == operation_key(
            "GET", "/admin/voucher/{id}"
        )

    def test_leading_and_trailing_slashes_are_normalized(self):
        assert operation_key("GET", "admin/voucher/") == ("GET", "/admin/voucher")

    def test_root_path_is_preserved(self):
        assert operation_key("GET", "/") == ("GET", "/")


class TestOperationIndexes:
    def _services(self):
        return [
            DiscoveredService(
                name="admin",
                source_url="u",
                base_url="b",
                apis=[
                    DiscoveredApi("GET", "/admin/voucher/{id}", operation_id="getVoucherDetails"),
                    DiscoveredApi("PUT", "/admin/voucher/{id}", operation_id="updateVoucher"),
                ],
            )
        ]

    def test_index_is_keyed_by_method_and_path(self):
        index = operation_index(self._services())

        assert index[("GET", "/admin/voucher/{}")].operation_id == "getVoucherDetails"

    def test_ids_map_to_their_operations(self):
        ids = operation_ids(self._services())

        assert [api.method for api in ids["updateVoucher"]] == ["PUT"]


class TestSplitSources:
    def test_splits_lines_and_trims(self):
        assert split_sources("  https://a  \n\nhttps://b\n") == ["https://a", "https://b"]

    def test_removes_duplicates_keeping_order(self):
        assert split_sources("https://a\nhttps://b\nhttps://a") == ["https://a", "https://b"]

    def test_empty_text_gives_no_sources(self):
        assert split_sources("") == []
