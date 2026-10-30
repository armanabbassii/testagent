"""تست‌های snapshotهای محلیِ Swagger (منبعِ عادیِ قدم دوم)."""

import json

import pytest

from src.agents.test_case_generator.swagger_snapshots import (
    DEFAULT_SELECTION,
    SELECTION_ADMIN,
    SELECTION_AUTO,
    SELECTION_BOTH,
    SELECTION_CRM,
    SELECTION_CUSTOMER,
    SELECTION_PROVIDER,
    SELECTION_SITE,
    SNAPSHOT_DIR,
    SWAGGER_SOURCES,
    SwaggerSnapshotError,
    enabled_sources,
    expand_selection,
    load_snapshot,
    load_snapshots,
    selection_for_keys,
    selection_label,
    snapshot_available,
    snapshot_path,
    source_for,
)


def _spec(operation_id: str = "getVoucherDetails", path: str = "/admin/voucher/{id}") -> dict:
    """یک سندِ OpenAPI v3 کوچک و معتبر."""
    return {
        "openapi": "3.0.1",
        "info": {"title": "Snapshot API", "version": "1.0.0"},
        "servers": [{"url": "https://podium-admin.sandpod.ir"}],
        "paths": {
            path: {
                "get": {
                    "operationId": operation_id,
                    "summary": "Get voucher details",
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }


def _write(directory, filename: str, document) -> None:
    text = document if isinstance(document, str) else json.dumps(document)
    (directory / filename).write_text(text, encoding="utf-8")


@pytest.fixture
def snapshots(tmp_path):
    """هر دو snapshot را در یک پوشه‌ی موقت می‌نویسد (نه در data/swagger/)."""
    _write(tmp_path, "admin.json", _spec("getVoucherDetails", "/admin/voucher/{id}"))
    _write(tmp_path, "customer.json", _spec("getMyVouchers", "/customer/voucher"))
    return tmp_path


# ── رجیستری و انتخاب ─────────────────────────────────────────────────────────

class TestSourceRegistry:
    def test_the_five_initial_sources_are_registered(self):
        assert [source.key for source in SWAGGER_SOURCES] == [
            SELECTION_ADMIN,
            SELECTION_CUSTOMER,
            SELECTION_CRM,
            SELECTION_PROVIDER,
            SELECTION_SITE,
        ]

    @pytest.mark.parametrize(
        ("key", "group", "prefixes"),
        [
            (SELECTION_ADMIN, "Admin", ("/admin",)),
            (SELECTION_CUSTOMER, "Customer", ("/customer",)),
            (SELECTION_CRM, "CRM", ("/crm",)),
            (SELECTION_PROVIDER, "Provider", ("/provider",)),
            # Site مسیرهای بی‌پیشوند دارد: مالکیت فقط با متادیتا ثابت می‌شود.
            (SELECTION_SITE, "Site", ()),
        ],
    )
    def test_each_source_has_its_group_snapshot_and_prefixes(self, key, group, prefixes):
        source = source_for(key)
        assert source.group == group
        assert source.filename == f"{key}.json"
        assert source.remote_url.endswith(f"/v3/api-docs/{group}")
        assert source.path_prefixes == prefixes
        assert source.enabled is True

    def test_the_existing_admin_and_customer_urls_are_unchanged(self):
        assert source_for(SELECTION_ADMIN).remote_url == (
            "http://podium-back.devpod.ir/api/v3/api-docs/Admin"
        )
        assert source_for(SELECTION_CUSTOMER).remote_url == (
            "http://podium-back.devpod.ir/api/v3/api-docs/Customer"
        )

    def test_every_source_maps_to_a_json_snapshot(self):
        assert source_for(SELECTION_ADMIN).filename == "admin.json"
        assert source_for(SELECTION_CUSTOMER).filename == "customer.json"

    def test_disabled_sources_are_registered_but_not_enabled(self, monkeypatch):
        from dataclasses import replace

        from src.agents.test_case_generator import swagger_snapshots

        registry = tuple(
            replace(source, enabled=source.key != SELECTION_CRM) for source in SWAGGER_SOURCES
        )
        monkeypatch.setattr(swagger_snapshots, "SWAGGER_SOURCES", registry)
        assert source_for(SELECTION_CRM).enabled is False
        assert SELECTION_CRM not in [source.key for source in enabled_sources()]

    def test_snapshot_availability_is_reported_per_source(self, snapshots):
        assert snapshot_available(SELECTION_ADMIN, directory=snapshots) is True
        assert snapshot_available(SELECTION_CRM, directory=snapshots) is False

    def test_snapshots_live_under_data_swagger(self):
        assert SNAPSHOT_DIR.name == "swagger"
        assert SNAPSHOT_DIR.parent.name == "data"

    def test_the_default_directory_is_used_when_none_is_given(self):
        assert snapshot_path(SELECTION_ADMIN).parent == SNAPSHOT_DIR

    def test_an_unknown_source_names_the_valid_ones(self):
        with pytest.raises(SwaggerSnapshotError, match="Unknown Swagger source"):
            source_for("partner")

    def test_the_unknown_source_error_lists_the_registered_keys(self):
        with pytest.raises(SwaggerSnapshotError) as excinfo:
            source_for("partner")

        assert "'admin'" in str(excinfo.value)
        assert "'customer'" in str(excinfo.value)


class TestExpandSelection:
    def test_admin_selects_only_admin(self):
        assert expand_selection(SELECTION_ADMIN) == [SELECTION_ADMIN]

    def test_customer_selects_only_customer(self):
        assert expand_selection(SELECTION_CUSTOMER) == [SELECTION_CUSTOMER]

    def test_the_legacy_both_value_still_reads_admin_and_customer(self):
        assert expand_selection(SELECTION_BOTH) == [SELECTION_ADMIN, SELECTION_CUSTOMER]

    def test_the_default_selection_is_automatic_not_both(self):
        assert DEFAULT_SELECTION == SELECTION_AUTO
        assert expand_selection(DEFAULT_SELECTION) == []
        assert expand_selection("") == []

    def test_several_keys_expand_in_registry_order(self):
        assert expand_selection("site,crm") == [SELECTION_CRM, SELECTION_SITE]

    def test_keys_map_back_to_a_selection(self):
        assert selection_for_keys([SELECTION_SITE, SELECTION_ADMIN]) == "admin,site"
        assert selection_for_keys([]) == ""

    def test_case_and_whitespace_do_not_matter(self):
        assert expand_selection("  ADMIN  ") == [SELECTION_ADMIN]

    def test_an_unknown_selection_is_rejected(self):
        with pytest.raises(SwaggerSnapshotError, match="Unknown Swagger source"):
            expand_selection("everything")

    def test_every_registered_source_is_selectable(self):
        for source in SWAGGER_SOURCES:
            assert expand_selection(source.key) == [source.key]

    def test_labels_are_human_readable(self):
        assert selection_label(SELECTION_ADMIN) == "Admin"
        assert selection_label(SELECTION_CUSTOMER) == "Customer"
        assert selection_label(SELECTION_BOTH) == "Admin + Customer"
        assert selection_label("crm,provider") == "CRM + Provider"
        assert selection_label(SELECTION_AUTO) == "Automatic"


# ── خواندن ───────────────────────────────────────────────────────────────────

class TestLoadSnapshot:
    def test_admin_snapshot_loads(self, snapshots):
        service = load_snapshot(SELECTION_ADMIN, directory=snapshots)

        assert service.name == "Admin"
        assert service.base_url == "https://podium-admin.sandpod.ir"
        assert [api.operation_id for api in service.apis] == ["getVoucherDetails"]

    def test_customer_snapshot_loads(self, snapshots):
        service = load_snapshot(SELECTION_CUSTOMER, directory=snapshots)

        assert service.name == "Customer"
        assert [api.operation_id for api in service.apis] == ["getMyVouchers"]

    def test_the_name_is_deterministic_not_taken_from_the_document_title(self, snapshots):
        # عنوانِ سند «Snapshot API» است؛ نامِ سرویس باید همان برچسبِ منبع بماند.
        assert load_snapshot(SELECTION_ADMIN, directory=snapshots).name == "Admin"

    def test_the_source_url_records_where_the_snapshot_came_from(self, snapshots):
        service = load_snapshot(SELECTION_ADMIN, directory=snapshots)

        assert service.source_url == source_for(SELECTION_ADMIN).remote_url

    def test_the_operations_come_from_the_file_not_from_the_network(self, tmp_path):
        # عملیاتی که فقط در فایل هست باید دیده شود: ثابت می‌کند سند از فایل
        # خوانده شده، نه از نشانیِ ثبت‌شده.
        _write(tmp_path, "admin.json", _spec("onlyInTheLocalFile", "/local/only"))

        service = load_snapshot(SELECTION_ADMIN, directory=tmp_path)

        assert [api.operation_id for api in service.apis] == ["onlyInTheLocalFile"]

    def test_a_missing_snapshot_fails_naming_the_path(self, tmp_path):
        with pytest.raises(SwaggerSnapshotError, match="was not found at"):
            load_snapshot(SELECTION_ADMIN, directory=tmp_path)

        with pytest.raises(SwaggerSnapshotError) as excinfo:
            load_snapshot(SELECTION_ADMIN, directory=tmp_path)
        assert str(tmp_path / "admin.json") in str(excinfo.value)

    def test_the_missing_snapshot_message_names_the_source(self, tmp_path):
        with pytest.raises(SwaggerSnapshotError, match="Admin"):
            load_snapshot(SELECTION_ADMIN, directory=tmp_path)

    def test_invalid_json_fails_clearly(self, tmp_path):
        _write(tmp_path, "admin.json", "{not json")

        with pytest.raises(SwaggerSnapshotError, match="could not be read"):
            load_snapshot(SELECTION_ADMIN, directory=tmp_path)

    def test_a_document_without_paths_fails_clearly(self, tmp_path):
        _write(tmp_path, "admin.json", {"openapi": "3.0.1", "info": {"title": "Empty"}})

        with pytest.raises(SwaggerSnapshotError, match="not a usable OpenAPI document"):
            load_snapshot(SELECTION_ADMIN, directory=tmp_path)

    def test_an_empty_document_fails_clearly(self, tmp_path):
        _write(tmp_path, "admin.json", {})

        with pytest.raises(SwaggerSnapshotError, match="not a usable OpenAPI document"):
            load_snapshot(SELECTION_ADMIN, directory=tmp_path)

    def test_an_unknown_source_is_rejected_before_any_read(self, tmp_path):
        with pytest.raises(SwaggerSnapshotError, match="Unknown Swagger source"):
            load_snapshot("partner", directory=tmp_path)


class TestLoadSnapshots:
    def test_both_snapshots_load_in_the_requested_order(self, snapshots):
        services, errors = load_snapshots(
            [SELECTION_ADMIN, SELECTION_CUSTOMER], directory=snapshots
        )

        assert errors == []
        assert [service.name for service in services] == ["Admin", "Customer"]

    def test_only_the_requested_snapshot_is_loaded(self, snapshots):
        services, errors = load_snapshots([SELECTION_CUSTOMER], directory=snapshots)

        assert errors == []
        assert [service.name for service in services] == ["Customer"]

    def test_one_broken_snapshot_does_not_stop_the_others(self, snapshots):
        (snapshots / "admin.json").write_text("{not json", encoding="utf-8")

        services, errors = load_snapshots(
            [SELECTION_ADMIN, SELECTION_CUSTOMER], directory=snapshots
        )

        assert [service.name for service in services] == ["Customer"]
        assert len(errors) == 1
        assert "admin.json" in errors[0]

    def test_an_empty_selection_loads_nothing(self, snapshots):
        services, errors = load_snapshots([], directory=snapshots)

        assert services == []
        assert errors == []
