"""
تست‌های فیلترِ قطعیِ ارتباطِ API (قدم دوم)

این فیلتر پرامپتِ قدم دوم را کوچک می‌کند، ولی نباید چیزی را بی‌صدا از دست بدهد.
اینجا سه چیز سنجیده می‌شود:

  1. اینکه واقعاً پرامپت را کوچک می‌کند: عملیات‌های بی‌ربط از نامزدها بیرون
     می‌مانند.
  2. اینکه محافظه‌کار است: کاتالوگِ کامل دست‌نخورده می‌ماند، تعریفِ هیچ API ای
     عوض نمی‌شود، و در تردید (هیچ تطبیقی، یا تطبیقِ کامل) کلِ کاتالوگ برمی‌گردد.
  3. اینکه هیچ تست‌کیسی حذف نمی‌شود — فیلتر فقط روی API ها کار می‌کند.
"""

from __future__ import annotations

from src.agents.test_case_generator.api_discovery import (
    DiscoveredApi,
    DiscoveredService,
)
from src.agents.test_case_generator.api_relevance import (
    RelevanceSelection,
    api_tokens,
    extract_test_case_tokens,
    select_candidates,
    tokenize,
)


def _api(method: str, path: str, operation_id: str = "", summary: str = "") -> DiscoveredApi:
    return DiscoveredApi(method, path, operation_id=operation_id, summary=summary)


def _service(name: str, apis: list[DiscoveredApi]) -> DiscoveredService:
    return DiscoveredService(
        name=name,
        source_url=f"https://host/{name}/swagger",
        base_url=f"https://host/{name}",
        apis=apis,
    )


def _catalog() -> list[DiscoveredService]:
    """یک کاتالوگِ کوچک: دو عملیاتِ مربوط به voucher و سه عملیاتِ بی‌ربط."""
    return [
        _service(
            "admin",
            [
                _api(
                    "GET",
                    "/admin/voucher/{id}",
                    operation_id="getVoucherDetails",
                    summary="Get voucher details",
                ),
                _api(
                    "PUT",
                    "/admin/voucher/{id}",
                    operation_id="updateVoucher",
                    summary="Update voucher",
                ),
                _api(
                    "POST",
                    "/admin/invoice/issue",
                    operation_id="issueInvoice",
                    summary="Issue an invoice",
                ),
                _api(
                    "DELETE",
                    "/admin/audit/session",
                    operation_id="purgeAuditSession",
                    summary="Purge audit sessions",
                ),
            ],
        ),
        _service(
            "reporting",
            [
                _api(
                    "GET",
                    "/reporting/quarterly/turnover",
                    operation_id="quarterlyTurnover",
                    summary="Quarterly turnover report",
                )
            ],
        ),
    ]


def _voucher_case() -> dict:
    return {
        "id": "TC-001",
        "title": "Update a voucher amount",
        "type": "positive",
        "steps": ["Change the voucher amount and submit"],
        "expected_result": "The voucher amount is updated.",
    }


def _ids(selection: RelevanceSelection) -> list[str]:
    return [
        api.operation_id for service in selection.services for api in service.apis
    ]


# ── توکن‌سازی ────────────────────────────────────────────────────────────────

class TestTokenize:
    def test_camel_case_identifiers_are_split_into_words(self):
        assert tokenize("getVoucherDetails") == {"voucher", "detail"}

    def test_snake_and_kebab_separators_are_split(self):
        assert tokenize("voucher_history-export") == {"voucher", "history", "export"}

    def test_plurals_and_singulars_share_a_token(self):
        assert tokenize("vouchers") == tokenize("voucher")

    def test_stopwords_are_dropped(self):
        assert tokenize("verify this voucher") == {"voucher"}

    def test_domain_nouns_are_not_stopwords(self):
        """نگه‌داشتنِ یک API بی‌ربط کم‌هزینه است؛ انداختنِ یک API مرتبط پرهزینه."""
        assert tokenize("admin user status error") == {
            "admin",
            "user",
            "status",
            "error",
        }

    def test_very_short_tokens_are_dropped(self):
        assert tokenize("id of a voucher") == {"voucher"}

    def test_non_string_input_yields_nothing(self):
        assert tokenize(None) == set()

    def test_numbers_are_kept(self):
        assert "404" in tokenize("returns 404")


class TestApiTokens:
    def test_path_segments_operation_id_and_summary_all_count(self):
        tokens = api_tokens(
            _api(
                "GET",
                "/admin/voucher/{id}/history",
                operation_id="getVoucherHistory",
                summary="Get the history of a voucher",
            )
        )

        assert {"admin", "voucher", "history"} <= tokens

    def test_path_parameters_do_not_become_tokens(self):
        """{id} نباید توکن شود، وگرنه همه‌ی عملیات‌های پارامتردار به همه وصل می‌شوند."""
        assert "id" not in api_tokens(_api("GET", "/admin/voucher/{id}"))

    def test_the_http_method_is_not_a_token(self):
        assert api_tokens(_api("GET", "/admin/voucher")) == api_tokens(
            _api("POST", "/admin/voucher")
        )


class TestTestCaseTokens:
    def test_nested_values_are_collected(self):
        tokens = extract_test_case_tokens(
            {
                "id": "TC-001",
                "title": "Update a voucher",
                "steps": ["Open the voucher", "Change the amount"],
                "expected_result": {"text": "The voucher amount changes", "code": 200},
            }
        )

        assert {"voucher", "amount", "change", "200"} <= tokens

    def test_schema_keys_are_not_tokens(self):
        """واژه‌های schema چیزی را متمایز نمی‌کنند."""
        assert "expected" not in extract_test_case_tokens({"expected_result": "Voucher updated"})

    def test_a_bare_string_case_is_accepted(self):
        assert "voucher" in extract_test_case_tokens("Update a voucher")


# ── انتخابِ نامزدها ──────────────────────────────────────────────────────────

class TestSelectCandidates:
    def test_relevant_operations_are_retained(self):
        selection = select_candidates([_voucher_case()], _catalog())

        assert "getVoucherDetails" in _ids(selection)
        assert "updateVoucher" in _ids(selection)

    def test_unrelated_operations_are_excluded(self):
        selection = select_candidates([_voucher_case()], _catalog())

        assert "issueInvoice" not in _ids(selection)
        assert "purgeAuditSession" not in _ids(selection)
        assert "quarterlyTurnover" not in _ids(selection)

    def test_a_service_with_no_relevant_operation_is_left_out(self):
        selection = select_candidates([_voucher_case()], _catalog())

        assert [service.name for service in selection.services] == ["admin"]

    def test_the_selection_reports_how_much_it_filtered(self):
        selection = select_candidates([_voucher_case()], _catalog())

        assert selection.total_apis == 5
        assert selection.candidate_apis == 2
        assert selection.filtered is True

    def test_candidate_operations_are_the_original_objects(self):
        """هیچ API ای کپی یا بازنویسی نمی‌شود — فقط ارجاع داده می‌شود."""
        services = _catalog()
        originals = {id(api) for service in services for api in service.apis}

        selection = select_candidates([_voucher_case()], services)

        assert {id(api) for s in selection.services for api in s.apis} <= originals

    def test_the_full_catalog_is_never_modified(self):
        services = _catalog()

        select_candidates([_voucher_case()], services)

        assert [len(service.apis) for service in services] == [4, 1]
        assert services[0].apis[2].operation_id == "issueInvoice"

    def test_operation_definitions_are_not_altered(self):
        services = _catalog()
        before = [api.to_dict() for service in services for api in service.apis]

        selection = select_candidates([_voucher_case()], services)

        after = {api.operation_id: api.to_dict() for s in selection.services for api in s.apis}
        for api in before:
            if api["operation_id"] in after:
                assert after[api["operation_id"]] == api

    def test_the_whole_catalog_is_returned_when_nothing_matches(self):
        """تست‌کیسی که هیچ واژه‌ی مشترکی ندارد نباید به پرامپتِ خالی تبدیل شود."""
        odd_case = {"id": "TC-900", "title": "Reconcile the nightly ledger"}

        selection = select_candidates([odd_case], _catalog())

        assert selection.candidate_apis == selection.total_apis == 5
        assert selection.filtered is False
        assert len(selection.services) == 2

    def test_the_whole_catalog_is_returned_when_everything_matches(self):
        every_case = {
            "id": "TC-901",
            "title": "Voucher invoice audit session turnover reporting",
        }

        selection = select_candidates([every_case], _catalog())

        assert selection.filtered is False
        assert selection.candidate_apis == 5

    def test_an_empty_test_case_list_returns_the_whole_catalog(self):
        selection = select_candidates([], _catalog())

        assert selection.filtered is False
        assert selection.candidate_apis == 5

    def test_an_empty_catalog_is_handled(self):
        selection = select_candidates([_voucher_case()], [])

        assert selection.services == []
        assert selection.total_apis == 0
        assert selection.filtered is False

    def test_candidates_are_the_union_over_all_test_cases(self):
        cases = [
            {"id": "TC-001", "title": "Update a voucher amount"},
            {"id": "TC-002", "title": "Issue an invoice for a customer"},
        ]

        selection = select_candidates(cases, _catalog())

        assert "updateVoucher" in _ids(selection)
        assert "issueInvoice" in _ids(selection)

    def test_an_unmatchable_case_does_not_remove_other_cases_candidates(self):
        """تست‌کیسِ بی‌تطبیق فقط چیزی اضافه نمی‌کند — چیزی حذف نمی‌کند."""
        cases = [
            {"id": "TC-001", "title": "Update a voucher amount"},
            {"id": "TC-900", "title": "Reconcile the nightly ledger"},
        ]

        selection = select_candidates(cases, _catalog())

        assert "updateVoucher" in _ids(selection)
        assert selection.filtered is True

    def test_test_cases_are_never_touched(self):
        """فیلتر فقط API ها را محدود می‌کند؛ فهرستِ تست‌کیس‌ها ورودی است."""
        cases = [_voucher_case()]

        select_candidates(cases, _catalog())

        assert cases == [_voucher_case()]
