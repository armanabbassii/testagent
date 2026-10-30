"""
تست‌های تحلیلِ معناییِ متنِ تسک برای انتخابِ منابعِ Swagger

    source_semantics.py  — پرامپت، فراخوانیِ LLM، تلاشِ دوباره، اعتبارسنجیِ شکل
    source_selection.py  — تأییدِ ادعاهای مدل با رجیستری و snapshotِ واقعی

هیچ LLMِ واقعی صدا زده نمی‌شود: ``FakeLLM`` جای ``LLMClient`` را در
source_semantics می‌گیرد. snapshotها همان سندهای ساختگیِ test_source_selection
هستند. ``FakeLLM`` و ``route_by_task`` را test_pipeline و test_app_wizard هم
استفاده می‌کنند.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agents.test_case_generator import source_semantics
from src.agents.test_case_generator.source_selection import (
    EVIDENCE_ENDPOINT,
    EVIDENCE_SEMANTIC,
    EVIDENCE_SWAGGER_URL,
    SEMANTIC_FAILED,
    SEMANTIC_NOT_NEEDED,
    SEMANTIC_NOT_RUN,
    SEMANTIC_USED,
    select_sources,
)
from src.agents.test_case_generator.source_semantics import (
    SemanticSourceAnalyzer,
    SourceSemanticsError,
    build_prompt,
    parse_analysis,
)
from src.llm_client import LLMResponse
from src.token_usage import report_usage
from tests.unit.test_case_generator.test_source_selection import (
    FIXTURE_OPERATIONS,
    fixture_spec,
    write_snapshots,
)

# ── LLMِ ساختگی ──────────────────────────────────────────────────────────────


def answer(*operations: dict, relevant: tuple[dict, ...] = (), clarification: str = "") -> str:
    """پاسخِ JSONِ مدل با همان قراردادِ پرامپت."""
    return json.dumps(
        {
            "relevant_sources": list(relevant),
            "required_operations": list(operations),
            "is_cross_service": len({op["source_id"] for op in operations}) > 1,
            "needs_clarification": bool(clarification),
            "clarification_reason": clarification or None,
        },
        ensure_ascii=False,
    )


def op(source_id: str, description: str, *keywords: str, operation: str = "") -> dict:
    return {
        "source_id": source_id,
        "operation": operation,
        "operation_description": description,
        "relevant_keywords": list(keywords),
    }


def task_of(message: str) -> str:
    """متنِ تسک از پیامِ ساخته‌شده‌ی پرامپت."""
    return message.split("TASK DESCRIPTION:", 1)[1].split("SOURCES:", 1)[0].strip()


def route_by_task(message: str) -> str:
    """پاسخِ پیش‌فرض برای تست‌های جریان: ووچر → Admin، درخواستِ دیجیتال → CRM."""
    task = task_of(message).lower()
    if "voucher" in task or "ووچر" in task:
        return answer(op("Admin", "Get voucher details", "voucher", operation="GET /admin/voucher/{id}"))
    if "digital" in task or "دیجیتال" in task:
        return answer(op("CRM", "List digital service requests", "digitalService", "requests"))
    return answer(clarification="The task does not name any API capability.")


class FakeLLM:
    """جای LLMClient در source_semantics؛ هر فراخوانی ثبت می‌شود.

    ``responses``: پاسخ‌ها به ترتیب (آخری تکرار می‌شود) — رشته، LLMResponse یا
    استثنا — یا یک تابعِ پیام → پاسخ.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *responses: object,
        router: Callable[[str], object] | None = None,
        usage: object = None,
    ) -> None:
        self.responses = list(responses)
        self.router = router
        self.usage = usage
        self.calls: list[dict] = []
        self.clients: list[dict] = []
        monkeypatch.setattr(source_semantics, "LLMClient", self._client)

    def _client(self, **kwargs):
        self.clients.append(kwargs)
        return self

    def chat(self, *, user_message, system_prompt, temperature, max_tokens):
        self.calls.append(
            {"message": user_message, "system": system_prompt, "temperature": temperature}
        )
        if self.usage is not None:
            report_usage(self.usage)  # همان گزارشی که LLMClient.chat می‌دهد
        reply = (
            self.router(user_message)
            if self.router
            else self.responses[min(len(self.calls), len(self.responses)) - 1]
        )
        if isinstance(reply, BaseException):
            raise reply
        return reply


@pytest.fixture
def snapshots(tmp_path: Path) -> Path:
    return write_snapshots(tmp_path)


def _analyzer() -> Callable:
    return SemanticSourceAnalyzer(user_id="test-user").analyze


def _select(directory: Path, task: str, services: str = "", **kwargs):
    kwargs.setdefault("analyze", _analyzer())
    return select_sources(task, services, directory=directory, **kwargs)


# ── زبان‌ها ──────────────────────────────────────────────────────────────────

class TestLanguages:
    def test_a_persian_task_selects_admin(self, snapshots, monkeypatch):
        llm = FakeLLM(
            monkeypatch,
            answer(op("Admin", "Get voucher details", "voucher", "details", operation="GET /admin/voucher/{id}")),
        )
        task = "ادمین بتواند جزئیات ووچر را مشاهده کند."
        selection = _select(snapshots, task)

        assert selection.keys == ("admin",)
        assert selection.semantic == SEMANTIC_USED
        assert selection.evidence_for("admin")[0].kind == EVIDENCE_SEMANTIC
        assert "GET /admin/voucher/{id}" in selection.evidence_for("admin")[0].detail
        assert task in llm.calls[0]["message"]
        assert llm.calls[0]["temperature"] == 0.0

    def test_a_persian_task_selects_crm_even_though_it_mentions_the_customer(
        self, snapshots, monkeypatch
    ):
        FakeLLM(
            monkeypatch,
            answer(
                op("CRM", "Submit a digital service request", "digitalService", "requests"),
                relevant=({"source_id": "Customer", "confidence": 0.9, "reason": "customer acts"},),
            ),
        )
        selection = _select(snapshots, "مشتری بتواند درخواست خدمات دیجیتال ثبت کند.")

        assert selection.keys == ("crm",)
        assert "GET /crm/digitalService/requests" in selection.evidence_for("crm")[0].detail
        assert any("Related but not selected" in w and "Customer" in w for w in selection.warnings)

    def test_a_persian_task_selects_provider(self, snapshots, monkeypatch):
        FakeLLM(monkeypatch, answer(op("Provider", "Update service settings", "serviceSet", "provider")))
        selection = _select(snapshots, "ارائه‌دهنده بتواند تنظیمات سرویس را ویرایش کند.")
        assert selection.keys == ("provider",)
        assert "GET /provider/serviceSet/{id}" in selection.evidence_for("provider")[0].detail

    def test_an_english_task_without_urls_is_analysed(self, snapshots, monkeypatch):
        llm = FakeLLM(monkeypatch, answer(op("customer", "List official invoices", "officialInvoices", "invoice")))
        selection = _select(snapshots, "The customer can view the official invoices.")
        assert len(llm.calls) == 1
        assert selection.keys == ("customer",)

    @pytest.mark.parametrize(
        ("task", "reply", "key"),
        [
            (
                "ادمین باید بتواند Voucher details را مشاهده کند.",
                answer(op("Admin", "Get voucher details", "voucher")),
                "admin",
            ),
            (
                "Customer بتواند درخواست Digital Service ثبت کند.",
                answer(op("CRM", "Create a digital service request", "digitalService", "requests")),
                "crm",
            ),
            (
                "کاربر بتواند لیست سرویس‌های یک ServiceSet را مشاهده کند.",
                answer(op("Site", "List web services of a service set", "serviceSet", "webServices")),
                "site",
            ),
        ],
    )
    def test_mixed_persian_and_english_tasks(self, snapshots, monkeypatch, task, reply, key):
        FakeLLM(monkeypatch, reply)
        assert _select(snapshots, task).keys == (key,)


# ── رابطه در برابرِ نیاز ─────────────────────────────────────────────────────

class TestRelevanceIsNotRequirement:
    def test_a_genuinely_cross_service_task_selects_every_needed_source(self, snapshots, monkeypatch):
        FakeLLM(
            monkeypatch,
            answer(
                op("Admin", "Create a voucher", "voucher", operation="POST /admin/voucher"),
                op("Customer", "List official invoices", "officialInvoices"),
            ),
        )
        selection = _select(snapshots, "ادمین ووچر بسازد و مشتری آن را در فاکتورهای رسمی ببیند.")
        assert selection.keys == ("admin", "customer")
        assert {item.kind for item in selection.evidence} == {EVIDENCE_SEMANTIC}

    def test_a_keyword_shared_by_two_sources_selects_only_the_needed_one(self, snapshots, monkeypatch):
        operations = FIXTURE_OPERATIONS["customer"] + [
            ("post", "/customer/voucher/redeem", "redeemVoucher", "customer-voucher-controller", "Redeem")
        ]
        (snapshots / "customer.json").write_text(json.dumps(fixture_spec(operations)), encoding="utf-8")
        FakeLLM(
            monkeypatch,
            answer(
                op("Admin", "Get voucher details", "voucher"),
                relevant=(
                    {"source_id": "Admin", "confidence": 0.9, "reason": "voucher details"},
                    {"source_id": "Customer", "confidence": 0.99, "reason": "also has vouchers"},
                ),
            ),
        )
        selection = _select(snapshots, "ادمین بتواند جزئیات ووچر را ببیند.")
        # Customer هم عملیاتِ voucher دارد و مدل با اطمینانِ بالا «مرتبط» دانسته،
        # ولی هیچ عملیاتی از آن لازم نیست.
        assert selection.keys == ("admin",)

    def test_without_semantics_the_shared_keyword_would_select_both(self, snapshots):
        operations = FIXTURE_OPERATIONS["customer"] + [
            ("post", "/customer/voucher/redeem", "redeemVoucher", "customer-voucher-controller", "Redeem")
        ]
        (snapshots / "customer.json").write_text(json.dumps(fixture_spec(operations)), encoding="utf-8")
        # مقایسه: تطبیقِ واژه‌ایِ قطعی هر دو را برمی‌دارد — تحلیلِ معنایی همین را حل می‌کند.
        assert select_sources("Voucher details.", "", directory=snapshots).keys == ("admin", "customer")


# ── تأییدِ ادعاهای مدل ───────────────────────────────────────────────────────

class TestValidation:
    def test_an_unknown_source_id_is_ignored_and_never_selected(self, snapshots, monkeypatch):
        FakeLLM(monkeypatch, answer(op("Billing", "Pay an invoice", "invoice")))
        selection = _select(snapshots, "مشتری فاکتور را پرداخت کند.")
        assert selection.keys == ()
        assert any("unknown Swagger source 'Billing'" in w for w in selection.warnings)
        assert "could not be determined" in selection.error

    def test_an_invented_operation_path_is_rejected(self, snapshots, monkeypatch):
        FakeLLM(monkeypatch, answer(op("Admin", "Teleport a voucher", "voucher", operation="POST /admin/teleport")))
        selection = _select(snapshots, "ادمین ووچر را جابه‌جا کند.")
        assert selection.keys == ()
        assert any("not in its Swagger snapshot" in w for w in selection.warnings)

    def test_an_operation_from_another_source_is_rejected(self, snapshots, monkeypatch):
        FakeLLM(monkeypatch, answer(op("Admin", "Get contact", operation="GET /customer/contact/{id}")))
        assert _select(snapshots, "ادمین تماس را ببیند.").keys == ()

    def test_invented_keywords_are_rejected(self, snapshots, monkeypatch):
        FakeLLM(monkeypatch, answer(op("Admin", "Teleport things", "teleport", "warpDrive")))
        assert _select(snapshots, "یک قابلیتِ جدید.").keys == ()

    def test_source_names_and_generic_words_do_not_verify_an_operation(self, snapshots, monkeypatch):
        FakeLLM(monkeypatch, answer(op("Admin", "Admin list", "admin", "list", "details")))
        assert _select(snapshots, "ادمین لیست را ببیند.").keys == ()

    def test_confidence_does_not_select_a_source(self, snapshots, monkeypatch):
        FakeLLM(
            monkeypatch,
            answer(relevant=({"source_id": "CRM", "confidence": 1.0, "reason": "sure"},)),
        )
        selection = _select(snapshots, "یک کارِ مبهم.")
        assert selection.keys == ()

    def test_a_clarification_request_becomes_an_ambiguity_error(self, snapshots, monkeypatch):
        FakeLLM(monkeypatch, answer(clarification="It is unclear which feature is meant."))
        selection = _select(snapshots, "اصلاحِ مشکلِ دیروز.")
        assert selection.keys == ()
        assert "It is unclear which feature is meant." in selection.error

    def test_no_arbitrary_fallback_when_nothing_is_verified(self, snapshots, monkeypatch):
        FakeLLM(monkeypatch, answer())
        selection = _select(snapshots, "بهبودِ عملکرد.")
        assert selection.keys == ()
        assert selection.error

    def test_a_needed_source_without_a_snapshot_is_an_error(self, tmp_path, monkeypatch):
        write_snapshots(tmp_path, ("admin", "customer"))
        llm = FakeLLM(monkeypatch, answer(op("CRM", "List digital service requests", "digitalService")))
        selection = _select(tmp_path, "درخواست‌های خدمات دیجیتال را فهرست کن.")
        assert str(tmp_path / "crm.json") in selection.error
        assert "snapshot not available" in llm.calls[0]["message"]

    def test_the_prompt_carries_a_compact_summary_not_the_documents(self, snapshots, monkeypatch):
        llm = FakeLLM(monkeypatch, answer(op("Admin", "Get voucher details", "voucher")))
        _select(snapshots, "ووچر")
        message = llm.calls[0]["message"]
        assert "source_id: admin" in message and "source_id: site" in message
        assert "voucher-admin-controller (2 operations): GET /admin/voucher/{id}" in message
        assert '"responses"' not in message and "openapi" not in message


# ── شکست و تلاشِ دوباره ─────────────────────────────────────────────────────

class TestFailures:
    def test_malformed_json_is_retried_once(self, snapshots, monkeypatch):
        llm = FakeLLM(monkeypatch, "not json at all", answer(op("Admin", "Get voucher", "voucher")))
        selection = _select(snapshots, "ووچر را ببیند.")
        assert len(llm.calls) == 2
        assert "CORRECTION REQUIRED" in llm.calls[1]["message"]
        assert selection.keys == ("admin",)

    def test_a_wrong_shape_is_retried_like_malformed_json(self, snapshots, monkeypatch):
        llm = FakeLLM(
            monkeypatch,
            json.dumps({"required_operations": "Admin"}),
            answer(op("Admin", "Get voucher", "voucher")),
        )
        assert _select(snapshots, "ووچر").keys == ("admin",)
        assert len(llm.calls) == 2

    def test_two_malformed_responses_fail_without_guessing(self, snapshots, monkeypatch):
        llm = FakeLLM(monkeypatch, "{broken", "{broken")
        selection = _select(snapshots, "ادمین ووچر را ببیند.")
        assert len(llm.calls) == 2
        assert selection.semantic == SEMANTIC_FAILED
        assert selection.keys == ()
        assert "Task analysis failed" in selection.error

    def test_a_truncated_response_is_not_retried(self, snapshots, monkeypatch):
        llm = FakeLLM(monkeypatch, LLMResponse('{"required_operations": [', finish_reason="length"))
        selection = _select(snapshots, "ووچر")
        assert len(llm.calls) == 1
        assert "truncated" in selection.error

    def test_an_llm_error_is_reported_and_not_retried(self, snapshots, monkeypatch):
        llm = FakeLLM(monkeypatch, ConnectionError("service unavailable"))
        selection = _select(snapshots, "ووچر")
        assert len(llm.calls) == 1
        assert "ConnectionError: service unavailable" in selection.error

    def test_after_a_failure_explicit_evidence_is_kept(self, snapshots, monkeypatch):
        FakeLLM(monkeypatch, ConnectionError("down"))
        selection = _select(snapshots, "ادمین ووچر را ببیند.", "GET /admin/voucher/5")
        assert selection.keys == ("admin",)
        assert selection.error == ""
        assert any("Task analysis failed" in w for w in selection.warnings)

    def test_after_a_failure_english_keyword_evidence_is_kept(self, snapshots, monkeypatch):
        FakeLLM(monkeypatch, ConnectionError("down"))
        selection = _select(snapshots, "List the digital service requests.")
        assert selection.keys == ("crm",)
        assert selection.evidence_for("crm")[0].kind == "metadata"


# ── تقدمِ شواهد ─────────────────────────────────────────────────────────────

class TestPrecedence:
    def test_an_explicit_swagger_url_needs_no_llm_call(self, snapshots, monkeypatch):
        llm = FakeLLM(monkeypatch, AssertionError("must not be called"))
        url = "https://h.test/api/swagger-ui/index.html?urls.primaryName=CRM"
        selection = _select(snapshots, "List requests.", url)
        assert llm.calls == []
        assert selection.keys == ("crm",)
        assert selection.semantic == SEMANTIC_NOT_NEEDED

    def test_an_explicit_endpoint_needs_no_llm_call(self, snapshots, monkeypatch):
        llm = FakeLLM(monkeypatch, AssertionError("must not be called"))
        selection = _select(snapshots, "Show voucher details.", "GET /admin/voucher/7")
        assert llm.calls == []
        assert selection.evidence_for("admin")[0].kind == EVIDENCE_ENDPOINT

    def test_semantics_add_to_but_never_remove_explicit_sources(self, snapshots, monkeypatch):
        FakeLLM(monkeypatch, answer(op("Customer", "List official invoices", "officialInvoices")))
        url = "https://h.test/api/swagger-ui/index.html?urls.primaryName=CRM"
        selection = _select(snapshots, "مشتری فاکتورهای رسمی را ببیند.", url)
        assert selection.keys == ("customer", "crm")
        assert selection.evidence_for("crm")[0].kind == EVIDENCE_SWAGGER_URL
        assert selection.evidence_for("customer")[0].kind == EVIDENCE_SEMANTIC

    def test_semantics_returning_nothing_keep_the_explicit_source(self, snapshots, monkeypatch):
        FakeLLM(monkeypatch, answer())
        selection = _select(snapshots, "درخواست‌ها را ببیند.", "/crm/digitalService/requests")
        assert selection.keys == ("crm",)
        assert selection.error == ""

    def test_manual_selection_never_calls_the_llm(self, snapshots, monkeypatch):
        llm = FakeLLM(monkeypatch, AssertionError("must not be called"))
        selection = _select(snapshots, "ادمین ووچر را ببیند.", manual="site")
        assert llm.calls == []
        assert selection.keys == ("site",)

    def test_without_an_analyzer_the_preview_reports_that_semantics_did_not_run(self, snapshots):
        selection = select_sources("ادمین ووچر را ببیند.", "", directory=snapshots)
        assert selection.semantic == SEMANTIC_NOT_RUN
        assert selection.keys == ()


# ── قراردادِ پاسخ و پرامپت ──────────────────────────────────────────────────

class TestContract:
    def test_a_valid_response_parses(self):
        analysis = parse_analysis(json.loads(route_by_task("TASK DESCRIPTION:\nvoucher\nSOURCES:\n")))
        assert analysis.required_operations[0].source_id == "Admin"
        assert analysis.required_operations[0].operation == "GET /admin/voucher/{id}"

    @pytest.mark.parametrize(
        "data",
        [
            [],
            {},
            {"required_operations": {"a": 1}},
            {"required_operations": ["Admin"]},
            {"required_operations": [{"source_id": ""}]},
            {"required_operations": [{"source_id": "Admin", "relevant_keywords": "voucher"}]},
            {"required_operations": [], "relevant_sources": [{"source_id": "A", "confidence": "high"}]},
            {"required_operations": [], "needs_clarification": "yes"},
        ],
    )
    def test_shape_violations_are_errors(self, data):
        with pytest.raises(SourceSemanticsError):
            parse_analysis(data)

    def test_confidence_is_clamped_and_only_reported(self):
        analysis = parse_analysis(
            {"required_operations": [], "relevant_sources": [{"source_id": "A", "confidence": 7}]}
        )
        assert analysis.relevant_sources[0].confidence == 1.0

    def test_the_prompt_file_has_both_placeholders_and_language_rules(self):
        system, user = build_prompt("ووچر", "SOURCES-BLOCK")
        assert "Persian" in system and "ENGLISH" in system
        assert "ووچر" in user and "SOURCES-BLOCK" in user

    def test_the_analyzer_reports_its_usage(self, monkeypatch):
        llm = FakeLLM(
            monkeypatch,
            answer(),
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )
        SemanticSourceAnalyzer(user_id="u").analyze("task", "sources")
        assert llm.clients == [{"user_id": "u", "agent_name": "source_selection"}]
