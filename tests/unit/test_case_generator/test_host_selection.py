"""
تست‌های مرحله‌ی B — انتخابِ قطعیِ میزبانِ هر تست‌کیس بعد از نگاشتِ قدم ۲.

قواعدِ طرحِ v3: پیش‌فرض، تصمیمِ کلِ تسک، تقدمِ تصمیمِ سرویس، محیط‌های مختلط،
تعارض، تصمیمِ گره‌نخورده، تصمیمِ ردشده (با و بدونِ دامنه) و اولویتِ وضعیت‌ها.
"""

from __future__ import annotations

import pytest

from src.agents.test_case_generator.host_selection import (
    HOST_CONFLICT,
    HOST_NEEDS_REVIEW,
    HOST_RESOLVED,
    HostResolutionError,
    base_path_of,
    host_resolution_of,
    resolve_hosts,
    strip_base_path,
)

ADMIN_POST = ("POST", "/admin/serviceSet/{id}/requestPossibility")
ADMIN_GET = ("GET", "/admin/serviceSet/{id}")
REPORT_GET = ("GET", "/report/voucher/{id}")
SERVICES = [
    {"name": "Admin", "base_url": "http://podium-back.devpod.ir/api"},
    {"name": "Report", "base_url": "http://podium-back.devpod.ir"},
]


def _mappings(**operations) -> list[dict]:
    mappings = []
    for case_id, operation in operations.items():
        api = {"method": operation[0], "path": operation[1]} if operation else None
        mappings.append({"test_case_id": case_id.replace("_", "-"), "api": api})
    return mappings


MAPPINGS = _mappings(TC_001=ADMIN_POST, TC_002=ADMIN_GET, TC_003=REPORT_GET, TC_004=None)


def _step1(accepted=(), rejected=()) -> dict:
    return {
        "test_cases": [{"id": f"TC-00{n}"} for n in range(1, 5)],
        "environment_decisions": [
            {"id": f"ENV-{n}", "status": "ACCEPTED", **item}
            for n, item in enumerate(accepted, start=1)
        ],
        "rejected_environment_decisions": [
            {
                "id": f"ENV-{n}",
                "status": "REJECTED",
                "rejection": {"code": "EVIDENCE_NOT_FOUND", "message": "not found"},
                **item,
            }
            for n, item in enumerate(rejected, start=len(accepted) + 1)
        ],
    }


def _service(env, method="", path="", name="", hints=()):
    return {
        "scope": "service",
        "environment": env,
        "service": {"method": method, "path": path, "name": name},
        "test_case_ids": list(hints),
    }


def _task(env):
    return {"scope": "task", "environment": env, "service": {}, "test_case_ids": []}


def _resolve(accepted=(), rejected=(), mappings=MAPPINGS):
    result = resolve_hosts(_step1(accepted, rejected), mappings, SERVICES)
    return result, {item["test_case_id"]: item for item in result["selections"]}


class TestPathHelpers:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://podium-back.devpod.ir/api", "/api"),
            ("http://podium-back.devpod.ir/api/", "/api"),
            ("http://podium-back.devpod.ir", ""),
            ("", ""),
        ],
    )
    def test_base_path_of(self, url, expected):
        assert base_path_of(url) == expected

    def test_strip_base_path_only_strips_a_whole_segment(self):
        assert strip_base_path("/api/admin/x", "/api") == "/admin/x"
        assert strip_base_path("/apiary/x", "/api") == "/apiary/x"
        assert strip_base_path("/admin/x", "") == "/admin/x"


class TestDefaultsAndExplicitDecisions:
    def test_no_decision_means_the_normal_host_for_every_case(self):
        result, by_case = _resolve()
        assert {item["variable"] for item in by_case.values()} == {"base_url"}
        assert {item["basis"] for item in by_case.values()} == {"default"}
        assert result["issue_review"]["status"] == "NONE"

    def test_explicit_vdi_for_one_service(self):
        _, by_case = _resolve([_service("vdi", *ADMIN_POST)])
        assert by_case["TC-001"]["variable"] == "base_url_vdi"
        assert by_case["TC-001"]["basis"] == "service"
        assert by_case["TC-001"]["decision_ids"] == ["ENV-1"]
        assert by_case["TC-002"]["variable"] == "base_url"

    def test_explicit_normal_for_one_service(self):
        _, by_case = _resolve([_service("normal", *REPORT_GET)])
        assert by_case["TC-003"] == {
            "test_case_id": "TC-003",
            "host_status": HOST_RESOLVED,
            "variable": "base_url",
            "environment": "normal",
            "basis": "service",
            "decision_ids": ["ENV-1"],
            "review_causes": [],
        }

    def test_a_task_wide_vdi_decision_applies_to_every_case(self):
        _, by_case = _resolve([_task("vdi")])
        assert {item["variable"] for item in by_case.values()} == {"base_url_vdi"}
        assert {item["basis"] for item in by_case.values()} == {"task"}

    def test_a_service_decision_overrides_the_task_wide_one(self):
        result, by_case = _resolve([_task("vdi"), _service("normal", *REPORT_GET)])
        assert by_case["TC-003"]["variable"] == "base_url"
        assert by_case["TC-003"]["basis"] == "service"
        assert by_case["TC-001"]["variable"] == "base_url_vdi"
        assert result["issue_review"]["status"] == "NONE"

    def test_mixed_environments_across_services(self):
        _, by_case = _resolve(
            [_service("vdi", path=ADMIN_POST[1]), _service("normal", *REPORT_GET)]
        )
        assert by_case["TC-001"]["variable"] == "base_url_vdi"
        assert by_case["TC-003"]["variable"] == "base_url"

    def test_agreeing_duplicates_merge(self):
        _, by_case = _resolve([_service("vdi", *ADMIN_POST), _service("vdi", path=ADMIN_POST[1])])
        assert by_case["TC-001"]["host_status"] == HOST_RESOLVED
        assert by_case["TC-001"]["decision_ids"] == ["ENV-1", "ENV-2"]


class TestLinking:
    def test_a_gateway_prefixed_path_matches(self):
        _, by_case = _resolve(
            [_service("vdi", "POST", "/api/admin/serviceSet/{serviceSetId}/requestPossibility")]
        )
        assert by_case["TC-001"]["variable"] == "base_url_vdi"

    def test_a_path_without_method_matches_every_mapped_method(self):
        decisions = [_service("vdi", path="/admin/serviceSet/{id}")]
        _, by_case = _resolve(
            decisions,
            mappings=_mappings(
                TC_001=("GET", "/admin/serviceSet/{id}"),
                TC_002=("DELETE", "/admin/serviceSet/{id}"),
            ),
        )
        assert by_case["TC-001"]["variable"] == by_case["TC-002"]["variable"] == "base_url_vdi"

    def test_the_hint_links_a_service_named_only_by_its_name(self):
        _, by_case = _resolve([_service("vdi", name="request possibility", hints=["TC-002"])])
        assert by_case["TC-002"]["variable"] == "base_url_vdi"
        assert by_case["TC-001"]["variable"] == "base_url"


class TestConflicts:
    def test_a_same_scope_service_conflict_affects_only_that_service(self):
        result, by_case = _resolve([_service("vdi", *ADMIN_POST), _service("normal", *ADMIN_POST)])
        assert by_case["TC-001"]["host_status"] == HOST_CONFLICT
        assert "variable" not in by_case["TC-001"]
        assert by_case["TC-001"]["review_causes"][0]["decision_ids"] == ["ENV-1", "ENV-2"]
        assert by_case["TC-002"]["host_status"] == HOST_RESOLVED
        assert result["issue_review"]["status"] == "NEEDS_REVIEW"
        assert any("TC-001" in text for text in result["clarifications"])

    def test_a_task_level_conflict_spares_cases_with_their_own_decision(self):
        _, by_case = _resolve([_task("vdi"), _task("normal"), _service("normal", *REPORT_GET)])
        assert by_case["TC-001"]["host_status"] == HOST_CONFLICT
        assert by_case["TC-003"]["host_status"] == HOST_RESOLVED
        assert by_case["TC-003"]["variable"] == "base_url"


class TestUnmatched:
    def test_an_unmatched_decision_changes_no_case_but_needs_review(self):
        result, by_case = _resolve([_service("vdi", "POST", "/billing/invoice")])
        assert {item["variable"] for item in by_case.values()} == {"base_url"}
        assert {item["host_status"] for item in by_case.values()} == {HOST_RESOLVED}
        (decision,) = result["decisions"]
        assert decision["status"] == "UNMATCHED"
        assert decision["matched_test_case_ids"] == []
        assert result["issue_review"]["status"] == "NEEDS_REVIEW"
        assert result["issue_review"]["reasons"][0]["kind"] == "UNMATCHED"
        assert any("could not be tied" in text for text in result["clarifications"])


class TestRejected:
    def test_a_linkable_rejected_service_statement_holds_only_its_cases(self):
        result, by_case = _resolve(rejected=[_service("vdi", *ADMIN_POST)])
        assert by_case["TC-001"]["host_status"] == HOST_NEEDS_REVIEW
        assert by_case["TC-001"]["review_causes"][0]["kind"] == "REJECTED"
        assert "variable" not in by_case["TC-001"]
        assert by_case["TC-003"]["host_status"] == HOST_RESOLVED

    def test_a_rejected_statement_holds_a_case_even_with_a_valid_decision(self):
        _, by_case = _resolve(
            [_service("vdi", *ADMIN_POST)], rejected=[_service("normal", *ADMIN_POST)]
        )
        assert by_case["TC-001"]["host_status"] == HOST_NEEDS_REVIEW

    def test_a_rejected_task_statement_spares_cases_with_a_valid_service_decision(self):
        _, by_case = _resolve([_service("normal", *REPORT_GET)], rejected=[_task("vdi")])
        assert by_case["TC-003"]["host_status"] == HOST_RESOLVED
        assert {by_case[c]["host_status"] for c in ("TC-001", "TC-002", "TC-004")} == {
            HOST_NEEDS_REVIEW
        }

    @pytest.mark.parametrize(
        "rejected",
        [
            {"scope": "", "environment": "vdi", "service": {}, "test_case_ids": []},
            {"scope": "everywhere", "environment": "vdi", "service": {}, "test_case_ids": []},
            _service("vdi", "POST", "/billing/invoice"),
        ],
    )
    def test_an_unscoped_rejection_holds_every_case_without_its_own_decision(self, rejected):
        result, by_case = _resolve([_service("vdi", *ADMIN_POST)], rejected=[rejected])
        assert by_case["TC-001"]["host_status"] == HOST_RESOLVED  # تصمیمِ سرویسِ معتبرِ خودش
        for case_id in ("TC-002", "TC-003", "TC-004"):
            assert by_case[case_id]["host_status"] == HOST_NEEDS_REVIEW
            assert "variable" not in by_case[case_id]
        assert result["issue_review"]["status"] == "NEEDS_REVIEW"
        assert any(reason["kind"] == "REJECTED" for reason in result["issue_review"]["reasons"])

    def test_no_rejected_or_unmatched_statement_ever_yields_a_silent_default(self):
        _, by_case = _resolve(rejected=[_task("normal")])
        assert all(item["host_status"] == HOST_NEEDS_REVIEW for item in by_case.values())
        assert all("variable" not in item for item in by_case.values())


class TestStatusPriority:
    def test_conflict_outranks_needs_review_and_all_causes_are_kept(self):
        _, by_case = _resolve(
            [_service("vdi", *ADMIN_POST), _service("normal", *ADMIN_POST)],
            rejected=[_service("vdi", *ADMIN_POST)],
        )
        item = by_case["TC-001"]
        assert item["host_status"] == HOST_CONFLICT
        assert [cause["kind"] for cause in item["review_causes"]] == ["CONFLICT", "REJECTED"]

    def test_resolution_is_deterministic(self):
        decisions = [_task("vdi"), _service("normal", *REPORT_GET)]
        assert _resolve(decisions)[0] == _resolve(decisions)[0]


class TestPersistedResolution:
    def test_the_stored_resolution_is_read_not_recomputed(self):
        stored = {
            "selections": [
                {"test_case_id": "TC-001", "host_status": HOST_RESOLVED, "variable": "base_url_vdi"}
            ]
        }
        assert host_resolution_of(_step1([_task("normal")]), {"host_resolution": stored}) is stored

    def test_an_old_step2_result_without_decisions_defaults(self):
        resolution = host_resolution_of(
            {"test_cases": [{"id": "TC-001"}]}, {"mappings": [], "services": []}
        )
        assert resolution["selections"][0]["variable"] == "base_url"

    def test_an_old_step2_result_with_step1_decisions_must_be_rerun(self):
        with pytest.raises(HostResolutionError, match="rerun Step 2"):
            host_resolution_of(_step1([_task("vdi")]), {"mappings": [], "services": []})
