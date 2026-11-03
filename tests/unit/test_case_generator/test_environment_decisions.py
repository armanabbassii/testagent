"""
تست‌های مرحله‌ی A — سنجشِ تصمیم‌های محیطِ استخراج‌شده در قدم ۱.

هر تصمیم جداگانه سنجیده می‌شود؛ شاهد باید عیناً در منبعِ گفته‌شده باشد؛ تصمیمِ
نامعتبر قدم ۱ را نمی‌شکند و با دلیل و منشأ نگه داشته می‌شود.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.agents.test_case_generator.environment_decisions import (
    REJECT_AMBIGUOUS_SOURCE,
    REJECT_EVIDENCE_NOT_FOUND,
    REJECT_INVALID_ENVIRONMENT,
    REJECT_INVALID_SCOPE,
    REJECT_MALFORMED,
    REJECT_MISSING_EVIDENCE,
    REJECT_MISSING_SERVICE,
    REJECT_UNKNOWN_SOURCE_TYPE,
    extract_environment_decisions,
    normalize_for_match,
    without_environment_fields,
)
from src.agents.test_case_generator.task_analysis import TaskAnalysisGenerator
from src.llm_client import LLMResponse
from src.task_sources.sources import TaskSource, sources_from_text

DESCRIPTION = "Implement the request possibility service.\nThis service must be tested in VDI."
COMMENT_1 = "Reviewed the endpoints."
COMMENT_2 = "The report service must run in the normal environment."
ATTACHMENT = "Notes: run every admin API in VDI please."
BASE_TEXT = "Implement the voucher report service."


def _sources() -> tuple[TaskSource, ...]:
    return (
        TaskSource("description", "", "Description", DESCRIPTION),
        TaskSource("comment", "10001", "Comment 1", COMMENT_1),
        TaskSource("comment", "10002", "Comment 2", COMMENT_2),
        TaskSource("attachment", "notes.md", "Attachment: notes.md", ATTACHMENT),
    )


def _decision(**overrides) -> dict:
    entry = {
        "scope": "service",
        "service": {
            "method": "POST",
            "path": "/admin/serviceSet/{id}/requestPossibility",
            "name": "",
        },
        "environment": "vdi",
        "evidence": "This service must be tested in VDI.",
        "source_type": "description",
        "source_reference": "",
    }
    entry.update(overrides)
    return entry


def _extract(*entries, sources=None, case_ids=("TC-001", "TC-002")):
    return extract_environment_decisions(
        list(entries), sources if sources is not None else _sources(), list(case_ids)
    )


class TestAbsentDecisions:
    @pytest.mark.parametrize("raw", [None, []])
    def test_absent_or_empty_means_no_decision(self, raw):
        result = extract_environment_decisions(raw, _sources(), ["TC-001"])
        assert result["environment_decisions"] == []
        assert result["rejected_environment_decisions"] == []

    def test_sources_are_recorded_without_their_text(self):
        result = extract_environment_decisions([], _sources(), [])
        assert [s["label"] for s in result["environment_sources"]] == [
            "Description",
            "Comment 1",
            "Comment 2",
            "Attachment: notes.md",
        ]
        assert "text" not in result["environment_sources"][0]
        assert len(result["environment_sources"][0]["sha256"]) == 64


class TestAcceptedEvidence:
    def test_a_description_statement_is_accepted_with_provenance(self):
        (record,) = _extract(_decision())["environment_decisions"]
        assert record["id"] == "ENV-1"
        assert record["status"] == "ACCEPTED"
        assert record["evidence"]["original"] == "This service must be tested in VDI."
        start, end = record["evidence"]["span"]
        assert DESCRIPTION[start:end] == record["evidence"]["original"]
        assert record["source"]["type"] == "description"
        assert record["source"]["sha256"] == _sources()[0].sha256
        assert record["evidence"]["normalization"] == "v2"

    def test_a_comment_label_is_resolved_to_the_jira_comment_id(self):
        (record,) = _extract(
            _decision(
                environment="normal",
                evidence=COMMENT_2,
                source_type="comment",
                source_reference="Comment 2",
            )
        )["environment_decisions"]
        assert record["source"]["reference"] == "10002"
        assert record["source"]["label"] == "Comment 2"
        assert record["warnings"] == []

    def test_an_attachment_filename_is_kept_as_its_reference(self):
        (record,) = _extract(
            _decision(
                scope="task",
                service=None,
                evidence="run every admin API in VDI please.",
                source_type="attachment",
                source_reference="notes.md",
            )
        )["environment_decisions"]
        assert record["source"]["reference"] == "notes.md"
        assert record["scope"] == "task"

    def test_test_case_ids_are_an_optional_hint(self):
        (record,) = _extract(
            _decision(service={"name": "request possibility"}, test_case_ids=["TC-002", "TC-9"])
        )["environment_decisions"]
        assert record["test_case_ids"] == ["TC-002"]
        assert any("TC-9" in warning for warning in record["warnings"])


class TestNormalization:
    def test_persian_letter_variants_zwnj_and_spacing_match(self):
        source = TaskSource("description", "", "Task description", "این سرویس در‌محیط  VDI تست شود.")
        evidence = "اين سرويس درمحيط VDI تست شود."  # ي عربی، بدونِ ZWNJ، یک فاصله
        (record,) = _extract(_decision(evidence=evidence), sources=(source,))[
            "environment_decisions"
        ]
        assert record["evidence"]["original"] == source.text  # متنِ اصلی دست نخورده
        assert record["evidence"]["as_returned"] == evidence
        assert record["evidence"]["normalized"] == normalize_for_match(source.text)

    def test_normalization_does_not_ignore_case(self):
        result = _extract(_decision(evidence="this service must be tested in vdi."))
        assert (
            result["rejected_environment_decisions"][0]["rejection"]["code"]
            == REJECT_EVIDENCE_NOT_FOUND
        )


ZWNJ = "‌"
PODIUM_SENTENCE = "، این تست ها با ادرس vdi میبایست تولید شوند"


def _description(text: str) -> TaskSource:
    return TaskSource("description", "", "Description", text)


def _status(result) -> str:
    rejected = result["rejected_environment_decisions"]
    return rejected[0]["rejection"]["code"] if rejected else "ACCEPTED"


class TestPersianSpacingV2:
    """v2: «تست ها» / «تست‌ها» / «تستها» یکی‌اند — فقط در مرزِ پیشوند/پسوندِ فارسی."""

    @pytest.mark.parametrize(
        ("source_text", "evidence"),
        [
            ("این تست ها با VDI اجرا شوند", f"این تست{ZWNJ}ها با VDI اجرا شوند"),
            ("این تست ها با VDI اجرا شوند", "این تستها با VDI اجرا شوند"),
            (f"این تست{ZWNJ}ها با VDI اجرا شوند", "این تست ها با VDI اجرا شوند"),
            ("این تستها با VDI اجرا شوند", f"این تست{ZWNJ}ها با VDI اجرا شوند"),
            ("سرویس می بایست در VDI باشد", "سرویس میبایست در VDI باشد"),
            ("سرویس نمی تواند در VDI باشد", f"سرویس نمی{ZWNJ}تواند در VDI باشد"),
            ("سرویس های جدید در VDI", "سرویسهای جدید در VDI"),
        ],
    )
    def test_space_zwnj_and_joined_forms_match_at_morpheme_boundaries(self, source_text, evidence):
        source = _description(source_text)
        result = _extract(_decision(scope="task", evidence=evidence), sources=(source,))
        (record,) = result["environment_decisions"]
        start, end = record["evidence"]["span"]
        assert record["evidence"]["original"] == source_text[start:end] == source_text
        assert record["evidence"]["as_returned"] == evidence
        assert record["evidence"]["normalization"] == "v2"

    def test_the_accepted_evidence_is_the_original_slice_with_its_exact_span(self):
        text = "مقدمه.\nاین تست ها باید در VDI اجرا شوند.\nپایان."
        source = _description(text)
        evidence = f"تست{ZWNJ}ها باید در VDI اجرا شوند"
        (record,) = _extract(_decision(scope="task", evidence=evidence), sources=(source,))[
            "environment_decisions"
        ]
        start, end = record["evidence"]["span"]
        assert text[start:end] == "تست ها باید در VDI اجرا شوند" == record["evidence"]["original"]
        assert record["evidence"]["original"] != evidence
        assert record["source"]["sha256"] == source.sha256
        assert source.text == text  # منبع دست نخورده

    @pytest.mark.parametrize(
        ("source_text", "evidence"),
        [
            ("این تست ها با vdi اجرا شوند", "این تست ها با VDI اجرا شوند"),  # بزرگی/کوچکی
            ("این تست ها با VDI اجرا شوند", "این تست ها با vdi اجرا شوند"),
            ("با ادرس vdi تولید شوند", "با آدرس vdi تولید شوند"),  # املا
            ("با آدرس vdi تولید شوند", "با ادرس vdi تولید شوند"),
            ("این سرویس در VDI تست شود", "این سرویسدر VDI تست شود"),  # فاصله‌ی معمولی
            ("این سرویس در VDI تست شود", "این سرویس در VDIتست شود"),  # فارسی/لاتین
            ("در VDI، تست شود", "در VDI تست شود"),  # نشانه‌گذاری
            ("نمی‌خواهد در VDI باشد", "نخواهد در VDI باشد"),  # حرفِ متفاوت
        ],
    )
    def test_case_spelling_ordinary_spaces_and_punctuation_stay_strict(self, source_text, evidence):
        result = _extract(
            _decision(scope="task", evidence=evidence), sources=(_description(source_text),)
        )
        assert _status(result) == REJECT_EVIDENCE_NOT_FOUND

    def test_ambiguity_is_unchanged_across_spacing_variants(self):
        sources = (
            TaskSource("comment", "1", "Comment 1", "این تست ها با VDI اجرا شوند"),
            TaskSource("comment", "2", "Comment 2", f"این تست{ZWNJ}ها با VDI اجرا شوند"),
        )
        result = _extract(
            _decision(scope="task", evidence="این تستها با VDI اجرا شوند", source_type="comment"),
            sources=sources,
        )
        assert _status(result) == REJECT_AMBIGUOUS_SOURCE

    def test_v1_compatible_cases_still_match(self):
        # ZWNJ + فاصله همان رفتارِ v1 را دارد: یک فاصله
        source = _description(f"این سرویس در{ZWNJ} محیط VDI‏ تست شود")
        result = _extract(
            _decision(scope="task", evidence="اين سرويس در محیط VDI تست شود"), sources=(source,)
        )
        assert _status(result) == "ACCEPTED"

    def test_positions_survive_dropped_marks_and_collapsed_spaces(self):
        text = f"‏الف  تست{ZWNJ}ـها   با VDI.\n"
        assert normalize_for_match(text) == "الف تستها با VDI."
        (record,) = _extract(
            _decision(scope="task", evidence="تست ها با VDI"), sources=(_description(text),)
        )["environment_decisions"]
        start, end = record["evidence"]["span"]
        assert (start, end) == (text.index("تست"), text.index("VDI") + 3)
        assert text[start:end] == f"تست{ZWNJ}ـها   با VDI"

    def test_podium_sentence_end_to_end(self):
        """PODIUM-3346: مدل «تست‌ها» برگرداند؛ شاهد پذیرفته و میزبان VDI است."""
        from src.agents.test_case_generator.host_selection import resolve_hosts
        from src.task_sources.sources import sources_from_task_input
        from src.task_sources.task_input import TaskInput

        description = "پیاده‌سازی درخواستِ امکان‌سنجی" + PODIUM_SENTENCE
        task = TaskInput(
            title="Request possibility",
            description=description,
            source="jira",
            source_reference="PODIUM-3346",
        )
        sources = sources_from_task_input(task)
        evidence = PODIUM_SENTENCE.replace("تست ها", f"تست{ZWNJ}ها")
        decision = {
            "scope": "task",
            "environment": "vdi",
            "evidence": evidence,
            "source_type": "description",
            "source_reference": "",
        }
        step1 = TestTaskAnalysisIntegration()._generate(
            TestTaskAnalysisIntegration._payload(environment_decisions=[decision]),
            sources=sources,
        )
        assert step1["rejected_environment_decisions"] == []
        (record,) = step1["environment_decisions"]
        start, end = record["evidence"]["span"]
        assert description[start:end] == record["evidence"]["original"]
        assert record["evidence"]["original"] == PODIUM_SENTENCE  # برشِ دقیقِ Jira
        assert record["evidence"]["as_returned"] == evidence
        assert record["source"]["sha256"] == sources[0].sha256 == _description(description).sha256

        (selection,) = resolve_hosts(
            step1,
            [{"test_case_id": "TC-001", "api": {"method": "POST", "path": "/admin/x"}}],
            [{"name": "Admin", "base_url": "http://example.invalid/api"}],
        )["selections"]
        assert selection["host_status"] == "RESOLVED"
        assert selection["variable"] == "base_url_vdi"

    def test_the_podium_sentence_is_still_rejected_for_vdi_casing_or_spelling(self):
        source = _description(PODIUM_SENTENCE)
        for evidence in (
            PODIUM_SENTENCE.replace("vdi", "VDI"),
            PODIUM_SENTENCE.replace("ادرس", "آدرس"),
        ):
            result = _extract(_decision(scope="task", evidence=evidence), sources=(source,))
            assert _status(result) == REJECT_EVIDENCE_NOT_FOUND


class TestRejection:
    @pytest.mark.parametrize(
        ("overrides", "code"),
        [
            ({"scope": "global"}, REJECT_INVALID_SCOPE),
            ({"environment": "staging"}, REJECT_INVALID_ENVIRONMENT),
            ({"evidence": "  "}, REJECT_MISSING_EVIDENCE),
            ({"service": {}}, REJECT_MISSING_SERVICE),
            ({"source_type": "email"}, REJECT_UNKNOWN_SOURCE_TYPE),
            ({"evidence": "The service must run in VDI."}, REJECT_EVIDENCE_NOT_FOUND),
        ],
    )
    def test_each_rejection_code(self, overrides, code):
        result = _extract(_decision(**overrides))
        assert result["environment_decisions"] == []
        (record,) = result["rejected_environment_decisions"]
        assert record["status"] == "REJECTED"
        assert record["rejection"]["code"] == code
        assert record["raw"]["environment"] == _decision(**overrides)["environment"]

    def test_a_malformed_entry_is_rejected_not_fatal(self):
        result = extract_environment_decisions(["oops", _decision()], _sources(), ["TC-001"])
        assert [r["rejection"]["code"] for r in result["rejected_environment_decisions"]] == [
            REJECT_MALFORMED
        ]
        assert len(result["environment_decisions"]) == 1

    def test_a_paraphrase_or_summary_is_not_evidence(self):
        result = _extract(_decision(evidence="The task asks for VDI testing of this service."))
        assert (
            result["rejected_environment_decisions"][0]["rejection"]["code"]
            == REJECT_EVIDENCE_NOT_FOUND
        )

    def test_generated_header_text_is_not_a_source(self):
        from src.task_sources.sources import sources_from_task_input
        from src.task_sources.task_input import TaskInput

        task = TaskInput(
            title="Use VDI",
            description="No environment here.",
            source="jira",
            source_reference="P-1",
        )
        result = extract_environment_decisions(
            [_decision(evidence="# Task P-1: Use VDI")], sources_from_task_input(task), []
        )
        assert (
            result["rejected_environment_decisions"][0]["rejection"]["code"]
            == REJECT_EVIDENCE_NOT_FOUND
        )

    def test_rejected_decisions_keep_the_evidence_and_source_that_were_found(self):
        (record,) = _extract(_decision(scope="everywhere"))["rejected_environment_decisions"]
        assert record["evidence"]["original"] == "This service must be tested in VDI."
        assert record["source"]["label"] == "Description"


class TestSourceReferences:
    def test_the_cited_source_wins_when_the_sentence_also_appears_elsewhere(self):
        duplicate = TaskSource("comment", "10003", "Comment 3", COMMENT_2)
        (record,) = _extract(
            _decision(evidence=COMMENT_2, source_type="comment", source_reference="Comment 3"),
            sources=_sources() + (duplicate,),
        )["environment_decisions"]
        assert record["source"]["reference"] == "10003"
        assert record["warnings"] == []

    def test_a_wrong_reference_with_a_unique_match_is_corrected_with_a_warning(self):
        (record,) = _extract(
            _decision(evidence=COMMENT_2, source_type="comment", source_reference="Comment 1")
        )["environment_decisions"]
        assert record["source"]["reference"] == "10002"
        assert record["warnings"][0].startswith("REFERENCE_CORRECTED")

    def test_a_missing_reference_with_several_matches_is_ambiguous(self):
        duplicate = TaskSource("comment", "10003", "Comment 3", COMMENT_2)
        result = _extract(
            _decision(evidence=COMMENT_2, source_type="comment", source_reference=""),
            sources=_sources() + (duplicate,),
        )
        (record,) = result["rejected_environment_decisions"]
        assert record["rejection"]["code"] == REJECT_AMBIGUOUS_SOURCE
        assert "Comment 2" in record["rejection"]["message"]

    def test_evidence_is_not_searched_in_another_source_type(self):
        result = _extract(_decision(evidence=COMMENT_2, source_type="description"))
        assert (
            result["rejected_environment_decisions"][0]["rejection"]["code"]
            == REJECT_EVIDENCE_NOT_FOUND
        )

    def test_manual_text_has_only_a_description_source(self):
        text = "Comment from lead: the voucher API must be tested in VDI."
        (record,) = _extract(
            _decision(evidence="the voucher API must be tested in VDI.", source_type="comment"),
            sources=sources_from_text(text),
        )["environment_decisions"]
        assert record["source"]["type"] == "description"
        assert record["warnings"][0].startswith("REFERENCE_CORRECTED")


class TestOrderAndWarnings:
    def test_ids_follow_source_order_not_model_order(self):
        result = _extract(
            _decision(
                evidence=COMMENT_2,
                environment="normal",
                source_type="comment",
                source_reference="Comment 2",
            ),
            _decision(),
        )
        assert [(r["id"], r["source"]["label"]) for r in result["environment_decisions"]] == [
            ("ENV-1", "Description"),
            ("ENV-2", "Comment 2"),
        ]

    def test_identical_input_gives_identical_output(self):
        entries = [_decision(), _decision(scope="bad")]
        assert _extract(*entries) == _extract(*entries)

    def test_a_vdi_mention_without_a_decision_is_warned_only(self):
        result = extract_environment_decisions([], _sources(), ["TC-001"])
        assert any("mentions VDI" in warning for warning in result["environment_warnings"])
        assert result["environment_decisions"] == []

    def test_persian_vdi_mention_is_warned(self):
        result = extract_environment_decisions([], sources_from_text("روی وی‌دی‌آی تست شود"), [])
        assert result["environment_warnings"]


class TestVdiMentionWarning:
    """هشدارِ «VDI ذکر شده» فقط برای ذکری است که شاهدِ هیچ تصمیمی آن را نپوشانده."""

    NORMAL_NON_VDI = "All APIs should use the normal environment (non-VDI)."

    @staticmethod
    def _vdi_warnings(result) -> list[str]:
        return [w for w in result["environment_warnings"] if "mentions VDI" in w]

    def test_a_normal_non_vdi_task_decision_gives_no_false_warning(self):
        source = TaskSource(
            "description", "", "Task description", BASE_TEXT + "\n" + self.NORMAL_NON_VDI
        )
        result = _extract(
            _decision(
                scope="task", service=None, environment="normal", evidence=self.NORMAL_NON_VDI
            ),
            sources=(source,),
        )
        assert result["environment_decisions"][0]["environment"] == "normal"
        assert self._vdi_warnings(result) == []

    def test_an_explicit_normal_service_decision_gives_no_warning(self):
        sentence = "The report service must run in the normal environment, not in VDI."
        source = TaskSource("description", "", "Task description", sentence)
        result = _extract(_decision(environment="normal", evidence=sentence), sources=(source,))
        assert len(result["environment_decisions"]) == 1
        assert self._vdi_warnings(result) == []

    def test_an_accepted_vdi_decision_gives_no_warning(self):
        assert (
            self._vdi_warnings(
                _extract(_decision(source_type="description"), sources=_sources()[:1])
            )
            == []
        )

    def test_a_standalone_mention_without_a_decision_is_informational(self):
        source = TaskSource("description", "", "Task description", "Some admins use a VDI session.")
        result = extract_environment_decisions([], (source,), ["TC-001"])
        (warning,) = self._vdi_warnings(result)
        assert "Informational only" in warning
        assert result["environment_decisions"] == result["rejected_environment_decisions"] == []

    def test_an_uncovered_mention_still_warns_next_to_a_covered_one(self):
        text = self.NORMAL_NON_VDI + "\nNote: the support team works from a VDI desktop."
        source = TaskSource("description", "", "Task description", text)
        result = _extract(
            _decision(
                scope="task", service=None, environment="normal", evidence=self.NORMAL_NON_VDI
            ),
            sources=(source,),
        )
        assert len(self._vdi_warnings(result)) == 1

    def test_the_warning_names_only_sources_with_uncovered_mentions(self):
        result = _extract(
            _decision(
                evidence="run every admin API in VDI please.",
                source_type="attachment",
                source_reference="notes.md",
                scope="task",
                service=None,
            )
        )
        (warning,) = self._vdi_warnings(result)
        assert "Description" in warning
        assert "notes.md" not in warning

    def test_a_located_rejected_decision_covers_its_own_mention(self):
        result = _extract(_decision(scope="everywhere"), sources=_sources()[:1])
        assert (
            result["rejected_environment_decisions"][0]["rejection"]["code"] == REJECT_INVALID_SCOPE
        )
        assert self._vdi_warnings(result) == []

    def test_an_ambiguous_rejected_quote_covers_every_place_it_occurs(self):
        sentence = "The voucher API must be tested in VDI."
        sources = (
            TaskSource("comment", "1", "Comment 1", sentence),
            TaskSource("comment", "2", "Comment 2", sentence),
        )
        result = _extract(
            _decision(evidence=sentence, source_type="comment", source_reference=""),
            sources=sources,
        )
        assert (
            result["rejected_environment_decisions"][0]["rejection"]["code"]
            == REJECT_AMBIGUOUS_SOURCE
        )
        assert self._vdi_warnings(result) == []

    def test_a_quote_not_found_in_the_task_does_not_hide_the_real_mention(self):
        source = TaskSource("description", "", "Task description", "Test this in VDI please.")
        result = _extract(_decision(evidence="It must run in VDI."), sources=(source,))
        assert (
            result["rejected_environment_decisions"][0]["rejection"]["code"]
            == REJECT_EVIDENCE_NOT_FOUND
        )
        assert len(self._vdi_warnings(result)) == 1


class TestStepThreePrompt:
    def test_environment_fields_never_reach_the_step3_prompt(self):
        step1 = {
            "task_summary": "x",
            "test_cases": [],
            "environment_decisions": [{"id": "ENV-1"}],
            "rejected_environment_decisions": [],
            "environment_sources": [],
            "environment_warnings": [],
        }
        assert set(without_environment_fields(step1)) == {"task_summary", "test_cases"}

        from src.agents.test_case_generator.scenario_analysis import render_prompt

        rendered = render_prompt("{{step1_result}} | {{step2_result}}", step1, {})
        assert "environment_decisions" not in rendered
        assert "ENV-1" not in rendered


class TestTaskAnalysisIntegration:
    """قدم ۱ با پاسخِ ساختگیِ LLM: فیلدِ تازه اختیاری است و هیچ‌وقت قدم را نمی‌شکند."""

    @staticmethod
    def _payload(**extra) -> dict:
        payload = {
            "task_summary": "Request possibility.",
            "identified_requirements": [],
            "test_cases": [
                {
                    "id": "TC-001",
                    "title": "Request possibility",
                    "type": "positive",
                    "priority": "high",
                    "preconditions": [],
                    "steps": ["Send the request"],
                    "expected_result": "Accepted.",
                    "related_service": {"method": "POST", "path": "/admin/x"},
                    "auth_intent": "inherit",
                }
            ],
            "clarifications": [],
            "variables": [],
        }
        payload.update(extra)
        return payload

    def _generate(self, payload: dict, sources=None) -> dict:
        response = LLMResponse(json.dumps(payload), finish_reason="stop")
        with patch("src.agents.test_case_generator.task_analysis.LLMClient") as client:
            client.return_value.chat.return_value = response
            return TaskAnalysisGenerator().generate(DESCRIPTION, "u", sources=sources)

    def test_an_absent_field_preserves_the_previous_result(self):
        result = self._generate(self._payload())
        assert result["environment_decisions"] == []
        assert result["rejected_environment_decisions"] == []
        assert result["test_cases"][0]["id"] == "TC-001"

    def test_without_sources_the_task_text_is_the_description_source(self):
        result = self._generate(self._payload(environment_decisions=[_decision()]))
        (record,) = result["environment_decisions"]
        assert record["source"]["label"] == "Task description"

    def test_an_invalid_decision_does_not_fail_step1(self):
        result = self._generate(
            self._payload(environment_decisions=[_decision(), {"scope": 3}, "junk"]),
            sources=_sources(),
        )
        assert len(result["environment_decisions"]) == 1
        assert len(result["rejected_environment_decisions"]) == 2
        assert result["clarifications"] == []  # قدم ۱ ابهامِ تازه‌ای نمی‌سازد

    def test_a_non_list_field_is_rejected_individually(self):
        result = self._generate(self._payload(environment_decisions={"scope": "task"}))
        assert result["rejected_environment_decisions"][0]["rejection"]["code"] in (
            REJECT_INVALID_ENVIRONMENT,
            REJECT_MISSING_EVIDENCE,
        )
