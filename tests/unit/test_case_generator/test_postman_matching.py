"""
مرحله‌ی 3e — تطبیقِ محافظه‌کارانه و method-awareِ Postman با عملیاتِ Swagger

تطبیق فقط در برابرِ DiscoveredServiceهای انتخاب‌شده است و مشورتی می‌ماند: انتخابِ
منبع، نامزدهای نگاشت و اعتبارسنجی عوض نمی‌شوند. همه‌ی fixtureها ساختگی‌اند؛ LLM
و کشفِ Swagger ماک‌اند و شبکه‌ای در کار نیست.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.agents.test_case_generator.api_discovery import DiscoveredApi, DiscoveredService
from src.agents.test_case_generator.api_mapping import Step2ApiMappingGenerator
from src.agents.test_case_generator.postman_evidence import (
    HEADER,
    HOST_LITERAL,
    HOST_NONE,
    HOST_VARIABLE,
    MATCHING_NOTE,
    PostmanEndpointEvidence,
    PostmanEvidence,
    collect_postman_evidence,
    render_postman_evidence,
)
from src.agents.test_case_generator.postman_matching import (
    AMBIGUOUS,
    EXACT_MATCH,
    METHOD_MISMATCH,
    PATH_PARAMETER_MATCH,
    TEMPLATE_MATCH,
    UNMATCHED,
    UNRESOLVED,
    SwaggerCandidate,
    match_postman_to_swagger,
)
from src.task_sources import TaskInput
from src.task_sources.attachments import process_attachment
from src.task_sources.sources import TaskSource, sources_from_task_input

V21 = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
SECRET = "S3CR3T-SENTINEL-VALUE"
SENTINELS = (
    SECRET,
    "QUERY-SENTINEL-LEAK",
    "BODY-SENTINEL-LEAK",
    "HEADER-SENTINEL-LEAK",
    "DEFAULT-SENTINEL-HOST",
)
_MAPPING_LLM = "src.agents.test_case_generator.api_mapping.LLMClient"
_DISCOVER = "src.agents.test_case_generator.api_mapping.discover_services"


# ── سازنده‌های ساختگی ────────────────────────────────────────────────────────


def _req(
    method: str,
    path: str,
    *,
    host_kind: str = HOST_NONE,
    host: str = "",
    defaulted: bool = False,
    index: int = 0,
    label: str = "Attachment: c.json",
) -> PostmanEndpointEvidence:
    return PostmanEndpointEvidence(
        source_type="attachment",
        source_reference=label.removeprefix("Attachment: "),
        source_label=label,
        collection_name="Synthetic",
        collection_version="2.1.0",
        request_index=index,
        name=f"request {index}",
        folder=(),
        method=method,
        method_defaulted=defaulted,
        host_kind=host_kind,
        host=host,
        host_variable=host.strip("{}") if host_kind == HOST_VARIABLE else None,
        path=path,
    )


def _service(
    name: str, *operations: tuple[str, str, str], base_url: str = "http://podium-back.devpod.ir/api"
) -> DiscoveredService:
    return DiscoveredService(
        name=name,
        source_url=f"https://host/{name}",
        base_url=base_url,
        apis=[
            DiscoveredApi(method, path, operation_id=op_id) for method, path, op_id in operations
        ],
    )


def _match(request: PostmanEndpointEvidence, *services: DiscoveredService):
    [result] = match_postman_to_swagger(PostmanEvidence((request,)), list(services))
    return result


ADMIN = _service(
    "admin",
    ("GET", "/admin/voucher", "listVouchers"),
    ("POST", "/admin/voucher", "createVoucher"),
    ("GET", "/admin/voucher/{id}", "getVoucher"),
    ("PUT", "/admin/voucher/{id}", "updateVoucher"),
    ("DELETE", "/admin/voucher/{id}", "deleteVoucher"),
    ("GET", "/admin/voucher/list", "voucherList"),
    ("GET", "/admin/serviceSet", "getServiceSets"),
    ("GET", "/admin/dashboard/", "dashboard"),
    ("GET", "/admin/report/{reportId}/items", "reportItems"),
)


def _op(service: str, method: str, path: str, operation_id: str) -> SwaggerCandidate:
    return SwaggerCandidate(service, method, path, operation_id)


# ── دسته‌ها ──────────────────────────────────────────────────────────────────


class TestCategories:
    def test_exact_method_and_path(self):
        result = _match(_req("POST", "/admin/voucher"), ADMIN)

        assert result.category == EXACT_MATCH
        assert result.certainty == "high"
        assert result.successful
        assert result.operations == (_op("admin", "POST", "/admin/voucher", "createVoucher"),)

    def test_same_path_different_method_is_a_method_mismatch(self):
        service = _service("admin", ("GET", "/admin/item", "getItem"))
        result = _match(_req("POST", "/admin/item"), service)

        assert result.category == METHOD_MISMATCH
        assert result.certainty is None
        assert not result.successful
        assert result.operations == ()
        assert result.other_methods == (_op("admin", "GET", "/admin/item", "getItem"),)

    def test_same_method_different_path_is_unmatched(self):
        result = _match(_req("GET", "/admin/coupon"), ADMIN)

        assert result.category == UNMATCHED
        assert result.certainty is None
        assert any("unselected source" in note for note in result.notes)

    def test_colon_parameters_are_path_parameter_matches(self):
        service = _service("admin", ("PUT", "/admin/voucher/{id}", "updateVoucher"))
        result = _match(_req("PUT", "/admin/voucher/:voucherId"), service)

        assert result.category == PATH_PARAMETER_MATCH
        assert result.certainty == "medium"

    def test_postman_variables_are_path_parameter_matches(self):
        service = _service("admin", ("DELETE", "/admin/voucher/{id}", "deleteVoucher"))
        result = _match(_req("DELETE", "/admin/voucher/{{voucherId}}"), service)

        assert result.category == PATH_PARAMETER_MATCH
        assert result.operations == (
            _op("admin", "DELETE", "/admin/voucher/{id}", "deleteVoucher"),
        )

    def test_a_colon_parameter_never_matches_a_literal(self):
        service = _service("admin", ("GET", "/admin/voucher/list", "voucherList"))

        assert _match(_req("GET", "/admin/voucher/:id"), service).category == UNMATCHED

    def test_a_variable_against_a_literal_is_unresolved(self):
        service = _service("admin", ("GET", "/admin/voucher/list", "voucherList"))
        result = _match(_req("GET", "/admin/voucher/{{which}}"), service)

        assert result.category == UNRESOLVED
        assert result.certainty is None
        assert result.operations == ()

    def test_a_variable_that_could_reach_a_concrete_sibling_is_capped_low(self):
        result = _match(_req("GET", "/admin/voucher/{{voucherId}}"), ADMIN)

        assert result.category == PATH_PARAMETER_MATCH
        assert result.operations == (_op("admin", "GET", "/admin/voucher/{id}", "getVoucher"),)
        assert result.certainty == "low"
        assert any("could also reach: GET /admin/voucher/list" in note for note in result.notes)

    @pytest.mark.parametrize("segment", ["v{{version}}", "{{a}}-{{b}}", "{{x}}.json", "{{open"])
    def test_partial_variable_segments_are_unresolved(self, segment):
        result = _match(_req("GET", f"/admin/{segment}/items"), ADMIN)

        assert result.category == UNRESOLVED
        assert any("mixes text and a Postman variable" in note for note in result.notes)

    def test_a_concrete_value_filling_a_template_is_a_template_match(self):
        result = _match(_req("GET", "/admin/report/2024/items"), ADMIN)

        assert result.category == TEMPLATE_MATCH
        assert result.certainty == "medium"
        assert result.operations == (
            _op("admin", "GET", "/admin/report/{reportId}/items", "reportItems"),
        )

    def test_a_concrete_swagger_path_wins_over_a_template(self):
        result = _match(_req("GET", "/admin/voucher/list"), ADMIN)

        assert result.category == EXACT_MATCH
        assert result.operations == (_op("admin", "GET", "/admin/voucher/list", "voucherList"),)

    def test_a_concrete_value_without_a_concrete_path_is_a_template_match(self):
        result = _match(_req("GET", "/admin/voucher/123"), ADMIN)

        assert result.category == TEMPLATE_MATCH
        assert result.operations == (_op("admin", "GET", "/admin/voucher/{id}", "getVoucher"),)

    def test_empty_operation_ids_are_supported(self):
        service = _service("admin", ("GET", "/admin/x", ""))
        result = _match(_req("GET", "/admin/x"), service)

        assert result.operations[0].operation_id == ""
        assert result.to_dict()["operations"][0]["operation_id"] == ""
        assert "service admin)" in render_postman_evidence(
            PostmanEvidence((_req("GET", "/admin/x"),)), matches=(result,)
        )


class TestMethods:
    def test_a_defaulted_get_is_low_certainty_with_a_note(self):
        result = _match(_req("GET", "/admin/serviceSet", defaulted=True), ADMIN)

        assert result.category == EXACT_MATCH
        assert result.certainty == "low"
        assert any("Postman's default GET was assumed" in note for note in result.notes)

    def test_a_defaulted_get_lists_other_methods_on_the_path(self):
        result = _match(_req("GET", "/admin/voucher", defaulted=True), ADMIN)

        assert result.operations == (_op("admin", "GET", "/admin/voucher", "listVouchers"),)
        assert result.other_methods == (_op("admin", "POST", "/admin/voucher", "createVoucher"),)

    def test_a_defaulted_get_with_only_other_methods_is_a_mismatch(self):
        service = _service("admin", ("POST", "/admin/submit", "submit"))
        result = _match(_req("GET", "/admin/submit", defaulted=True), service)

        assert result.category == METHOD_MISMATCH
        assert result.certainty is None

    def test_only_the_same_method_matches_among_many(self):
        result = _match(_req("PUT", "/admin/voucher/{{id}}"), ADMIN)

        assert result.operations == (_op("admin", "PUT", "/admin/voucher/{id}", "updateVoucher"),)
        assert result.other_methods == ()  # فقط برای methodِ پیش‌فرض‌شده فهرست می‌شوند


class TestCandidatesAndServices:
    def test_multiple_postman_requests_stay_separate(self):
        evidence = PostmanEvidence(
            (
                _req("POST", "/admin/voucher", index=0),
                _req("POST", "/admin/voucher", index=1, label="Attachment: other.json"),
            )
        )
        first, second = match_postman_to_swagger(evidence, [ADMIN])

        assert first.operations == second.operations
        assert (first.source_label, first.request_index) == ("Attachment: c.json", 0)
        assert (second.source_label, second.request_index) == ("Attachment: other.json", 1)

    def test_matches_in_several_selected_services_are_ambiguous(self):
        crm = _service("crm", ("POST", "/admin/voucher", "crmCreate"))
        result = _match(_req("POST", "/admin/voucher"), ADMIN, crm)

        assert result.category == AMBIGUOUS
        assert result.certainty is None
        assert set(result.operations) == {
            _op("admin", "POST", "/admin/voucher", "createVoucher"),
            _op("crm", "POST", "/admin/voucher", "crmCreate"),
        }

    def test_only_selected_services_are_searched(self):
        result = _match(_req("GET", "/crm/requests"), ADMIN)

        assert result.category == UNMATCHED


class TestHostsAndBasePaths:
    def test_a_literal_host_equal_to_the_server_with_base_path_removal(self):
        request = _req(
            "POST", "/api/admin/voucher", host_kind=HOST_LITERAL, host="podium-back.devpod.ir"
        )
        result = _match(request, ADMIN)

        assert result.category == EXACT_MATCH
        assert result.certainty == "medium"
        assert any("base path of the matched service was removed" in note for note in result.notes)
        assert not any("differs from the Swagger server host" in note for note in result.notes)

    def test_a_literal_host_that_differs_adds_a_note_only(self):
        request = _req("POST", "/admin/voucher", host_kind=HOST_LITERAL, host="api.other.test:8443")
        result = _match(request, ADMIN)

        assert result.category == EXACT_MATCH
        assert result.certainty == "high"
        assert any(
            "literal host api.other.test:8443 differs from the Swagger server host podium-back.devpod.ir"
            in note
            for note in result.notes
        )

    def test_a_variable_host_is_not_resolved_and_caps_certainty(self):
        request = _req("POST", "/admin/voucher", host_kind=HOST_VARIABLE, host="{{base_url}}")
        result = _match(request, ADMIN)

        assert result.category == EXACT_MATCH
        assert result.certainty == "low"
        assert any("{{base_url}}" in note and "unknown" in note for note in result.notes)

    def test_a_relative_path_matches_without_an_invented_host(self):
        result = _match(_req("POST", "/admin/voucher"), ADMIN)

        assert result.category == EXACT_MATCH
        assert any("no host was supplied" in note for note in result.notes)
        assert "://" not in json.dumps(result.to_dict())

    def test_no_base_path_removal_for_variable_hosts(self):
        request = _req("POST", "/api/admin/voucher", host_kind=HOST_VARIABLE, host="{{base_url}}")

        assert _match(request, ADMIN).category == UNMATCHED

    def test_no_base_path_removal_from_another_service(self):
        other = _service("crm", ("GET", "/crm/x", "x"), base_url="http://h.test/gateway")
        plain = _service(
            "admin", ("POST", "/admin/voucher", "createVoucher"), base_url="http://h.test"
        )
        request = _req("POST", "/gateway/admin/voucher", host_kind=HOST_LITERAL, host="h.test")

        assert _match(request, other, plain).category == UNMATCHED


class TestPathRules:
    def test_a_trailing_slash_without_a_twin_is_ignored_with_a_note(self):
        result = _match(_req("GET", "/admin/dashboard"), ADMIN)

        assert result.category == EXACT_MATCH
        assert result.operations[0].path == "/admin/dashboard/"
        assert any("trailing-slash difference was ignored" in note for note in result.notes)

    def test_a_trailing_slash_with_a_twin_must_match_exactly(self):
        service = _service("admin", ("GET", "/admin/a", "plain"), ("POST", "/admin/a/", "slash"))

        assert _match(_req("GET", "/admin/a"), service).operations == (
            _op("admin", "GET", "/admin/a", "plain"),
        )
        slashed = _match(_req("GET", "/admin/a/"), service)
        assert slashed.category == METHOD_MISMATCH
        assert slashed.other_methods == (_op("admin", "POST", "/admin/a/", "slash"),)

    def test_a_case_only_difference_is_unmatched_with_a_note(self):
        result = _match(_req("GET", "/admin/ServiceSet"), ADMIN)

        assert result.category == UNMATCHED
        assert any(
            "differs only in letter case from: GET /admin/serviceSet" in note
            for note in result.notes
        )

    def test_repeated_slashes_are_ignored_with_a_note(self):
        result = _match(_req("POST", "/admin//voucher"), ADMIN)

        assert result.category == EXACT_MATCH
        assert result.postman_path == "/admin//voucher"
        assert any("repeated slashes" in note for note in result.notes)

    def test_percent_encoding_is_not_decoded(self):
        service = _service("admin", ("GET", "/admin/a b", "space"))

        assert _match(_req("GET", "/admin/a%20b"), service).category == UNMATCHED

    def test_original_paths_are_preserved(self):
        request = _req("GET", "/admin/voucher/:voucherId/")
        result = _match(request, _service("admin", ("GET", "/admin/voucher/{id}", "getVoucher")))

        assert result.postman_path == "/admin/voucher/:voucherId/"
        assert result.operations[0].path == "/admin/voucher/{id}"
        assert request.path == "/admin/voucher/:voucherId/"


# ── زنجیره‌ی واقعی: query، اسرار و جهش ───────────────────────────────────────


def _sources_from_collection(items: list[dict]) -> tuple[TaskSource, ...]:
    content = json.dumps(
        {
            "info": {"name": "Voucher", "schema": V21},
            "item": items,
            "variable": [
                {"key": "base_url", "value": "http://DEFAULT-SENTINEL-HOST/api"},
                {"key": "apiKey", "value": SECRET},
                {"key": "segment", "value": "pre-{{apiKey}}"},
            ],
        }
    )
    data = content.encode("utf-8")
    attachment = process_attachment(
        filename="c.json",
        media_type="application/json",
        size=len(data),
        download=lambda _limit: data,
        max_bytes=10 * 1024 * 1024,
        max_chars=100_000,
    )
    return sources_from_task_input(
        TaskInput("t", "d", attachments=(attachment,), source="Jira", source_reference="P-1")
    )


def _item(name: str, method: str, raw: str, **request) -> dict:
    return {"name": name, "request": {"method": method, "url": raw, **request}}


ITEMS = [
    _item(
        "Create",
        "POST",
        "{{base_url}}/admin/voucher?lang=fa&token=QUERY-SENTINEL-LEAK",
        header=[{"key": "Authorization", "value": "HEADER-SENTINEL-LEAK"}],
        body={"mode": "raw", "raw": "BODY-SENTINEL-LEAK"},
    ),
    _item("Secret", "GET", "{{base_url}}/admin/{{segment}}"),
    _item("Key", "GET", "https://api.test/admin/voucher/{{apiKey}}"),
]


class TestRealChain:
    def test_query_strings_do_not_affect_matching(self):
        evidence = collect_postman_evidence(_sources_from_collection(ITEMS))
        create = match_postman_to_swagger(evidence, [ADMIN])[0]

        assert create.category == EXACT_MATCH
        assert create.postman_path == "/admin/voucher"

    def test_secret_sentinels_never_appear(self):
        evidence = collect_postman_evidence(_sources_from_collection(ITEMS))
        matches = match_postman_to_swagger(evidence, [ADMIN])
        text = (
            repr(matches)
            + json.dumps([m.to_dict() for m in matches])
            + render_postman_evidence(evidence, matches=matches)
        )

        for sentinel in SENTINELS:
            assert sentinel not in text
        assert "pre-" not in text

    def test_structured_source_data_is_not_mutated(self):
        sources = _sources_from_collection(ITEMS)
        source = sources[-1]
        before = (
            source.requests,
            source.collection,
            source.text,
            source.sha256,
            repr(source.requests),
        )

        match_postman_to_swagger(collect_postman_evidence(sources), [ADMIN])

        assert source.requests is before[0]
        assert source.collection is before[1]
        assert (source.text, source.sha256, repr(source.requests)) == before[2:]

    def test_output_is_deterministic(self):
        evidence = collect_postman_evidence(_sources_from_collection(ITEMS))

        assert match_postman_to_swagger(evidence, [ADMIN]) == match_postman_to_swagger(
            evidence, [ADMIN]
        )
        assert render_postman_evidence(
            evidence, matches=match_postman_to_swagger(evidence, [ADMIN])
        ) == (
            render_postman_evidence(evidence, matches=match_postman_to_swagger(evidence, [ADMIN]))
        )


# ── رندر و یکپارچه‌سازیِ قدم ۲ ───────────────────────────────────────────────


class TestRendering:
    def test_each_category_has_an_accurate_label(self):
        requests = (
            _req("POST", "/admin/voucher", index=0),
            _req("PATCH", "/admin/voucher", index=1),
            _req("GET", "/admin/coupon", index=2),
            _req("GET", "/admin/v{{n}}", index=3),
        )
        evidence = PostmanEvidence(requests)
        text = render_postman_evidence(
            evidence, matches=match_postman_to_swagger(evidence, [ADMIN])
        )

        assert (
            "Swagger match: EXACT_MATCH — POST /admin/voucher (service admin, createVoucher) — high certainty"
            in text
        )
        assert (
            "Swagger match: METHOD_MISMATCH (not a match) — other methods: GET /admin/voucher"
            in text
        )
        assert "Swagger match: UNMATCHED in the selected Swagger sources" in text
        assert "Swagger match: UNRESOLVED (path information is insufficient to compare)" in text
        assert MATCHING_NOTE in text

    def test_ambiguous_label(self):
        crm = _service("crm", ("POST", "/admin/voucher", "crmCreate"))
        evidence = PostmanEvidence((_req("POST", "/admin/voucher"),))
        text = render_postman_evidence(
            evidence, matches=match_postman_to_swagger(evidence, [ADMIN, crm])
        )

        assert "Swagger match: AMBIGUOUS (not resolved) — candidates:" in text

    def test_without_matches_the_block_is_unchanged(self):
        evidence = PostmanEvidence((_req("POST", "/admin/voucher"),))

        assert "Swagger match" not in render_postman_evidence(evidence)
        assert MATCHING_NOTE not in render_postman_evidence(evidence)


def _cases() -> list[dict]:
    return [
        {"id": "TC-001", "title": "Create a voucher", "steps": ["Submit"]},
        {"id": "TC-002", "title": "Read a voucher", "steps": ["Open"]},
    ]


def _payload() -> str:
    return json.dumps(
        {
            "mappings": [
                {
                    "test_case_id": "TC-001",
                    "api": {
                        "method": "POST",
                        "path": "/admin/voucher",
                        "operation_id": "createVoucher",
                    },
                    "confidence": "high",
                    "reason": "Matches.",
                    "clarification": "",
                },
                {
                    "test_case_id": "TC-002",
                    "api": {
                        "method": "GET",
                        "path": "/admin/voucher/{id}",
                        "operation_id": "getVoucher",
                    },
                    "confidence": "high",
                    "reason": "Matches.",
                    "clarification": "",
                },
            ],
            "clarifications": [],
        }
    )


def _generate(evidence=..., services=None):
    kwargs = {} if evidence is ... else {"postman_evidence": evidence}
    with patch(_DISCOVER, return_value=(services or [ADMIN], [])), patch(_MAPPING_LLM) as mock_cls:
        mock_cls.return_value.chat.return_value = _payload()
        result = Step2ApiMappingGenerator().generate(
            {"test_cases": _cases()}, ["https://host/swagger"], "user-1", **kwargs
        )
    return result, mock_cls.return_value.chat.call_args_list


class TestStep2Integration:
    def test_matches_reach_the_prompt_and_the_result(self):
        evidence = collect_postman_evidence(_sources_from_collection(ITEMS))
        result, calls = _generate(evidence)

        message = calls[0].kwargs["user_message"]
        assert "Swagger match: EXACT_MATCH — POST /admin/voucher" in message
        matches = result["postman_evidence"]["matches"]
        assert [m["request_index"] for m in matches] == [0, 1, 2]
        assert matches[0]["category"] == EXACT_MATCH
        assert matches[0]["certainty"] == "low"  # میزبانِ متغیر

    def test_selection_candidates_and_mappings_are_unchanged(self):
        evidence = collect_postman_evidence(_sources_from_collection(ITEMS))
        with_evidence, with_calls = _generate(evidence)
        without, without_calls = _generate()

        assert with_evidence["mappings"] == without["mappings"]
        assert with_evidence["services"] == without["services"]
        assert with_evidence["host_resolution"] == without["host_resolution"]
        # همان نامزدهای CANDIDATE APIS؛ فقط بلوکِ Postman افزوده شده.
        with_message = with_calls[0].kwargs["user_message"]
        without_message = without_calls[0].kwargs["user_message"]
        assert (
            with_message.split("\n\n" + HEADER, 1)[0]
            == without_message.split("\n\nMap every test case", 1)[0]
        )

    @pytest.mark.parametrize("evidence", [..., None, PostmanEvidence()])
    def test_no_postman_evidence_keeps_prompt_and_result_identical(self, evidence):
        baseline_result, baseline_calls = _generate()
        result, calls = _generate(evidence)

        assert result == baseline_result
        assert "postman_evidence" not in result
        assert [c.kwargs["user_message"] for c in calls] == [
            c.kwargs["user_message"] for c in baseline_calls
        ]
        assert match_postman_to_swagger(None if evidence is ... else evidence, [ADMIN]) == ()
