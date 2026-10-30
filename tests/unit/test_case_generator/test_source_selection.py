"""
تست‌های انتخابِ خودکارِ منابعِ Swagger — source_selection.py

همه‌ی snapshotها سندهای کوچکِ ساختگی در یک پوشه‌ی موقت‌اند (``write_snapshots``)؛
هیچ تستی به data/swagger/ یا شبکه وابسته نیست. ``write_snapshots`` را
tests/unit/ui/test_pipeline.py هم استفاده می‌کند.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from src.agents.test_case_generator import source_selection, swagger_snapshots
from src.agents.test_case_generator.source_selection import (
    EVIDENCE_ENDPOINT,
    EVIDENCE_MANUAL,
    EVIDENCE_METADATA,
    EVIDENCE_SWAGGER_URL,
    MODE_AUTO,
    MODE_MANUAL,
    endpoint_references,
    normalize_path,
    select_sources,
    terms,
)
from src.agents.test_case_generator.swagger_snapshots import (
    SWAGGER_SOURCES,
    SwaggerSource,
)

# ── snapshotهای ساختگی ───────────────────────────────────────────────────────

# (method, path, operationId, tag, summary) برای هر منبع
FIXTURE_OPERATIONS: dict[str, list[tuple[str, str, str, str, str]]] = {
    "admin": [
        ("get", "/admin/voucher/{id}", "getVoucherDetails", "voucher-admin-controller", "Voucher details"),
        ("post", "/admin/voucher", "createVoucher", "voucher-admin-controller", "Create a voucher"),
        ("get", "/admin/acl/groups", "getAclGroups", "group-management-admin-controller", "Groups"),
    ],
    "customer": [
        ("get", "/customer/contact/{id}", "getContact", "customer-contact-controller", "Contact"),
        (
            "get",
            "/customer/official-invoices",
            "getOfficialInvoices",
            "customer-official-invoice-controller",
            "List official invoices",
        ),
    ],
    "crm": [
        (
            "get",
            "/crm/digitalService/requests",
            "getDigitalServiceRequests",
            "crm-digital-service-controller",
            "Digital service requests",
        ),
    ],
    "provider": [
        ("get", "/provider/serviceSet/{id}", "getProviderServiceSet", "provider-service-set-controller", "Service set"),
    ],
    "site": [
        ("get", "/serviceSet", "getServiceSets", "service-set-controller", "Service sets"),
        (
            "get",
            "/serviceSet/{serviceSetId}/webServices",
            "getWebServices",
            "service-set-controller",
            "Web services of a service set",
        ),
    ],
}


def fixture_spec(operations: list[tuple[str, str, str, str, str]]) -> dict:
    paths: dict[str, dict] = {}
    for method, path, operation_id, tag, summary in operations:
        paths.setdefault(path, {})[method] = {
            "operationId": operation_id,
            "tags": [tag],
            "summary": summary,
            "responses": {"200": {"description": "OK"}},
        }
    return {
        "openapi": "3.0.1",
        "info": {"title": "Fixture", "version": "1"},
        "servers": [{"url": "http://podium-back.devpod.ir/api"}],
        "paths": paths,
    }


def write_snapshots(directory: Path, keys: tuple[str, ...] | None = None) -> Path:
    """snapshotهای ساختگیِ منابعِ خواسته‌شده (پیش‌فرض: هر پنج) را می‌نویسد."""
    for key in keys if keys is not None else tuple(FIXTURE_OPERATIONS):
        (directory / f"{key}.json").write_text(
            json.dumps(fixture_spec(FIXTURE_OPERATIONS[key])), encoding="utf-8"
        )
    return directory


@pytest.fixture
def snapshots(tmp_path: Path) -> Path:
    return write_snapshots(tmp_path)


def _select(directory: Path, task: str = "", services: str = "", manual: str = ""):
    return select_sources(task, services, manual=manual, directory=directory)


_ADMIN_UI = (
    "https://podium-admin.sandpod.ir/api/swagger-ui/index.html"
    "?urls.primaryName=Admin#/voucher-admin-controller/getVoucherDetails"
)
_CRM_UI = "https://podium-admin.sandpod.ir/api/swagger-ui/index.html?urls.primaryName=CRM"
_PROVIDER_RAW = "http://podium-back.devpod.ir/api/v3/api-docs/Provider"
_UNKNOWN_UI = "https://host.test/api/swagger-ui/index.html?urls.primaryName=Billing"


# ── رجیستری و توسعه‌پذیری ───────────────────────────────────────────────────

class TestRegistryDriven:
    def test_the_algorithm_has_no_source_specific_branch(self):
        # الگوریتم فقط از رجیستری می‌خواند: هیچ کلید یا نامِ منبعی در کد نیست.
        code = Path(source_selection.__file__).read_text(encoding="utf-8")
        code = re.sub(r'"""[\s\S]*?"""', "", code)  # docstringها
        code = re.sub(r"#.*", "", code)  # توضیح‌ها
        for source in SWAGGER_SOURCES:
            for name in source.identifiers():
                assert not re.search(rf"[\"']{re.escape(name)}[\"'/]", code, re.I), name

    @pytest.mark.parametrize(
        ("url", "key"),
        [
            (_ADMIN_UI, "admin"),
            (_CRM_UI, "crm"),
            (_PROVIDER_RAW, "provider"),
            ("https://x.test/api/swagger-ui/index.html?urls.primaryName=Site", "site"),
            ("https://x.test/api/v3/api-docs/Customer", "customer"),
        ],
    )
    def test_every_registered_group_is_recognised_from_its_swagger_url(self, snapshots, url, key):
        selection = _select(snapshots, services=url)
        assert selection.keys == (key,)
        assert selection.evidence_for(key)[0].kind == EVIDENCE_SWAGGER_URL

    def test_a_new_acl_entry_is_recognised_without_changing_the_algorithm(
        self, snapshots, monkeypatch
    ):
        acl = SwaggerSource(
            key="acl",
            label="ACL",
            filename="acl.json",
            remote_url="http://podium-back.devpod.ir/api/v3/api-docs/ACL",
            path_prefixes=("/acl",),
        )
        monkeypatch.setattr(swagger_snapshots, "SWAGGER_SOURCES", (*SWAGGER_SOURCES, acl))
        (snapshots / "acl.json").write_text(
            json.dumps(
                fixture_spec(
                    [("get", "/acl/policies/{id}", "getPolicy", "acl-policy-controller", "Policy")]
                )
            ),
            encoding="utf-8",
        )

        by_url = _select(snapshots, services="https://h.test/api/swagger-ui/index.html?urls.primaryName=ACL")
        by_path = _select(snapshots, services="GET /acl/policies/7")

        assert by_url.keys == ("acl",)
        assert by_path.keys == ("acl",)
        assert by_path.endpoints[0].operations == ("GET /acl/policies/{id}",)


# ── مسیرهای endpoint ─────────────────────────────────────────────────────────

class TestEndpointMatching:
    @pytest.mark.parametrize(
        ("reference", "key", "operation"),
        [
            ("/admin/voucher/123", "admin", "GET /admin/voucher/{id}"),
            ("https://podium-admin.sandpod.ir/api/admin/voucher/123", "admin", "GET /admin/voucher/{id}"),
            ("/customer/contact/9", "customer", "GET /customer/contact/{id}"),
            ("/crm/digitalService/requests", "crm", "GET /crm/digitalService/requests"),
            ("/provider/serviceSet/123", "provider", "GET /provider/serviceSet/{id}"),
            ("/serviceSet", "site", "GET /serviceSet"),
            ("/serviceSet/123/webServices", "site", "GET /serviceSet/{serviceSetId}/webServices"),
        ],
    )
    def test_endpoints_resolve_to_the_source_whose_snapshot_has_them(
        self, snapshots, reference, key, operation
    ):
        selection = _select(snapshots, services=reference)
        assert selection.keys == (key,)
        assert selection.endpoints[0].verified == (key,)
        assert selection.endpoints[0].operations == (operation,)
        assert selection.evidence_for(key)[0].kind == EVIDENCE_ENDPOINT

    @pytest.mark.parametrize(
        "reference",
        [
            "https://host.test/api/admin/voucher/123?expand=true",
            "/admin/voucher/123/",
            "/admin/voucher/{voucherId}",
            "GET /admin/voucher/123.",
        ],
    )
    def test_query_strings_trailing_slashes_and_parameters_are_handled(self, snapshots, reference):
        selection = _select(snapshots, services=reference)
        assert selection.keys == ("admin",)
        assert selection.endpoints[0].operations == ("GET /admin/voucher/{id}",)

    def test_prefixes_match_whole_segments_not_substrings(self, snapshots):
        selection = _select(snapshots, services="/administrator/voucher/1")
        assert selection.endpoints[0].owners == ()
        assert selection.endpoints[0].prefix == ""
        assert "admin" not in selection.keys

    def test_an_unprefixed_endpoint_is_not_assumed_to_be_site(self, snapshots):
        # نه در snapshotِ Site است، نه پیشوندِ ثبت‌شده‌ای دارد.
        selection = _select(snapshots, services="/groups/7/members")
        assert selection.endpoints[0].owners == ()
        assert selection.keys == ()
        assert "could not be determined" in selection.error

    def test_an_unprefixed_endpoint_of_a_future_source_is_not_classified_as_site(
        self, snapshots, monkeypatch
    ):
        future = SwaggerSource(
            key="acl", label="ACL", filename="acl.json",
            remote_url="http://podium-back.devpod.ir/api/v3/api-docs/ACL",
        )
        monkeypatch.setattr(swagger_snapshots, "SWAGGER_SOURCES", (*SWAGGER_SOURCES, future))
        (snapshots / "acl.json").write_text(
            json.dumps(fixture_spec([("get", "/groups/{name}", "getGroup", "group-controller", "Group")])),
            encoding="utf-8",
        )
        assert _select(snapshots, services="/groups/editors").keys == ("acl",)

    def test_without_the_site_snapshot_an_unprefixed_path_stays_unassigned(self, tmp_path):
        write_snapshots(tmp_path, ("admin", "customer"))
        selection = _select(tmp_path, services="/serviceSet/12/webServices")
        assert selection.keys == ()
        assert "site" in selection.unavailable
        assert "Not searched (no local snapshot): CRM, Provider, Site" in selection.error

    def test_a_prefixed_path_missing_from_its_snapshot_still_belongs_to_it(self, snapshots):
        selection = _select(snapshots, services="/crm/campaigns/5")
        assert selection.keys == ("crm",)
        assert "outdated" in selection.warnings[0]

    def test_a_path_in_two_snapshots_is_a_conflict(self, snapshots):
        (snapshots / "provider.json").write_text(
            json.dumps(fixture_spec([("get", "/serviceSet", "listSets", "x", "Sets")])),
            encoding="utf-8",
        )
        selection = _select(snapshots, services="/serviceSet")
        assert selection.keys == ()
        assert "more than one Swagger source" in selection.error
        assert "Provider" in selection.error and "Site" in selection.error

    def test_a_swagger_url_settles_a_path_found_in_two_snapshots(self, snapshots):
        (snapshots / "provider.json").write_text(
            json.dumps(fixture_spec([("get", "/serviceSet", "listSets", "x", "Sets")])),
            encoding="utf-8",
        )
        selection = _select(
            snapshots,
            services="/serviceSet\nhttps://h.test/api/swagger-ui/index.html?urls.primaryName=Site",
        )
        assert selection.error == ""
        assert selection.keys == ("site",)


class TestReferences:
    def test_swagger_urls_are_not_endpoint_references(self):
        found = endpoint_references(f"{_ADMIN_UI}\nGET /admin/voucher/{{id}} and/or more")
        assert found == [("/admin/voucher/{id}", "/admin/voucher/{id}")]

    def test_normalize_path(self):
        assert normalize_path("https://h/api//admin/x/?a=1#f") == "/api/admin/x"
        assert normalize_path("/serviceSet/") == "/serviceSet"

    def test_terms_split_camel_case_and_ignore_generic_words(self):
        assert terms("getDigitalServiceRequests endpoint") == {"digital"}
        assert terms("official-invoices") == {"official", "invoice"}


# ── سیاستِ انتخاب ───────────────────────────────────────────────────────────

class TestSelectionPolicy:
    def test_an_unknown_swagger_url_is_an_actionable_error(self, snapshots):
        selection = _select(snapshots, services=_UNKNOWN_UI)
        assert selection.keys == ()
        assert _UNKNOWN_UI in selection.error
        assert "SWAGGER_SOURCES" in selection.error
        assert "Admin, Customer, CRM, Provider, Site" in selection.error

    def test_endpoint_and_task_select_several_sources_when_justified(self, snapshots):
        selection = _select(
            snapshots,
            task="The admin creates a voucher; the customer then sees it on the official invoices.",
            services="https://podium-admin.sandpod.ir/api/admin/voucher/12",
        )
        assert selection.keys == ("admin", "customer")
        assert selection.evidence_for("admin")[0].kind == EVIDENCE_ENDPOINT
        assert selection.evidence_for("customer")[0].kind == EVIDENCE_METADATA
        assert "official" in selection.evidence_for("customer")[0].detail

    def test_the_word_admin_alone_does_not_force_admin(self, snapshots):
        selection = _select(snapshots, task="The admin should see the contact of a person.")
        assert selection.keys == ("customer",)
        assert "admin" not in selection.keys

    def test_a_source_name_with_no_matching_metadata_selects_nothing(self, snapshots):
        selection = _select(snapshots, task="Admin and CRM flow for the admin user.")
        assert selection.keys == ()
        assert "could not be determined" in selection.error

    def test_without_any_url_the_task_is_matched_against_metadata(self, snapshots):
        selection = _select(snapshots, task="List the digital service requests.")
        assert selection.keys == ("crm",)
        evidence = selection.evidence_for("crm")[0]
        assert evidence.kind == EVIDENCE_METADATA
        assert "GET /crm/digitalService/requests" in evidence.detail

    def test_the_strongest_metadata_match_wins_over_a_weaker_one(self, snapshots):
        selection = _select(snapshots, task="Show the web services of a service set.")
        # Site (/serviceSet/{id}/webServices) بیشترین امتیاز را دارد؛ Provider فقط
        # «service set» را دارد — نسبتش زیرِ آستانه است.
        assert selection.keys == ("site",)

    def test_two_sources_with_equally_strong_matches_are_both_selected(self, snapshots):
        operations = FIXTURE_OPERATIONS["customer"] + [
            ("post", "/customer/voucher/redeem", "redeemVoucher", "customer-voucher-controller", "Redeem")
        ]
        (snapshots / "customer.json").write_text(json.dumps(fixture_spec(operations)), encoding="utf-8")
        selection = _select(snapshots, task="Voucher handling.")
        # هر دو عملیاتِ مرتبط دارند؛ هیچ‌کدام به‌دلخواه کنار گذاشته نمی‌شود.
        assert selection.keys == ("admin", "customer")
        assert {item.kind for item in selection.evidence} == {EVIDENCE_METADATA}

    def test_a_summary_only_match_is_weaker_than_a_path_match(self, snapshots):
        operations = FIXTURE_OPERATIONS["customer"] + [
            ("get", "/customer/discounts", "getDiscounts", "customer-discount-controller", "Voucher discounts")
        ]
        (snapshots / "customer.json").write_text(json.dumps(fixture_spec(operations)), encoding="utf-8")
        assert _select(snapshots, task="Voucher handling.").keys == ("admin",)

    def test_insufficient_evidence_is_an_error_not_a_fallback(self, snapshots):
        selection = _select(snapshots, task="Fix the bug reported yesterday.")
        assert selection.keys == ()
        assert "could not be determined" in selection.error
        assert "choose the sources manually in Step 2" in selection.error

    def test_a_disabled_source_is_never_selected(self, snapshots, monkeypatch):
        registry = tuple(
            replace(source, enabled=source.key != "crm") for source in SWAGGER_SOURCES
        )
        monkeypatch.setattr(swagger_snapshots, "SWAGGER_SOURCES", registry)

        by_url = _select(snapshots, services=_CRM_UI)
        by_path = _select(snapshots, services="/crm/digitalService/requests")
        by_task = _select(snapshots, task="List the digital service requests.")

        assert "disabled" in by_url.error and by_url.keys == ()
        assert "disabled" in by_path.error and by_path.keys == ()
        assert "crm" not in by_task.keys and "crm" not in by_task.searched

    def test_a_missing_snapshot_of_a_needed_source_is_an_error(self, tmp_path):
        write_snapshots(tmp_path, ("admin", "customer"))
        selection = _select(tmp_path, services="GET /provider/serviceSet/3")
        assert selection.keys == ("provider",)
        assert str(tmp_path / "provider.json") in selection.error
        assert "Update <source>" in selection.error

    def test_a_corrupt_snapshot_is_reported_and_not_searched(self, snapshots):
        (snapshots / "crm.json").write_text("{broken", encoding="utf-8")
        selection = _select(snapshots, services="/admin/voucher/1")
        assert selection.keys == ("admin",)
        assert "crm" in selection.unavailable
        assert any("could not be read" in warning for warning in selection.warnings)

    def test_the_selection_is_ordered_by_the_registry_and_deterministic(self, snapshots):
        services = "/serviceSet\n/admin/voucher/1\n/crm/digitalService/requests"
        first = _select(snapshots, services=services)
        assert first.keys == ("admin", "crm", "site")
        assert _select(snapshots, services=services) == first

    def test_structured_debug_information_has_no_input_urls(self, snapshots):
        selection = _select(snapshots, services=f"{_ADMIN_UI}\n/crm/digitalService/requests")
        info = selection.to_dict()
        assert info["selected"] == ["admin", "crm"]
        assert info["evidence"] == {"admin": ["swagger_url"], "crm": ["endpoint"]}
        assert "sandpod" not in json.dumps(info)
        assert selection.summary() == "Admin (Swagger URL), CRM (endpoint path) — automatic"


class TestManualSelection:
    def test_a_manual_selection_is_respected(self, snapshots):
        selection = _select(snapshots, services="/admin/voucher/1", manual="site")
        assert selection.mode == MODE_MANUAL
        assert selection.keys == ("site",)
        assert selection.error == ""
        assert selection.evidence_for("site")[0].kind == EVIDENCE_MANUAL
        assert any("/admin/voucher/1" in warning for warning in selection.warnings)

    def test_a_manual_selection_conflicting_with_a_swagger_url_is_reported(self, snapshots):
        selection = _select(snapshots, services=_CRM_UI, manual="admin")
        assert _CRM_UI in selection.error
        assert "manual selection (Admin)" in selection.error

    def test_a_manual_selection_without_a_snapshot_is_an_error(self, tmp_path):
        write_snapshots(tmp_path, ("admin",))
        selection = _select(tmp_path, manual="admin,crm")
        assert "crm.json" in selection.error

    def test_an_unknown_stored_manual_value_is_an_error(self, snapshots):
        selection = _select(snapshots, manual="billing")
        assert "Unknown Swagger source" in selection.error

    def test_auto_and_empty_mean_automatic(self, snapshots):
        for manual in ("", "auto"):
            assert _select(snapshots, services="/admin/voucher/1", manual=manual).mode == MODE_AUTO
