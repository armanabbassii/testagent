"""تست‌های BusinessServiceChunker."""

from src.ingest.business.chunker import (
    BusinessServiceChunker, _stable_id, _first, _format_price, _format_fields,
)
from src.vector_store.base import Document


def _group(**kwargs) -> dict:
    defaults = {
        "id": 1,
        "name": "پرداخت",
        "slug": "payment",
        "categories": ["مالی"],
        "tags": ["پرداخت", "کیف پول"],
        "provider": "شرکت الف",
        "providerId": 10,
        "introduction": "معرفی کوتاه",
        "content": "<p>توضیحات <b>کامل</b> با HTML</p>",
        "link": "https://example.com/payment",
        "webservices": [
            {"id": 101, "name": "پرداخت آنلاین", "description": "توضیح ۱", "price": 0,
             "input": [{"name": "amount", "required": True, "defaultValue": ""}],
             "header": []},
            {"id": 102, "name": "استعلام موجودی", "descriptin": "توضیح ۲ (تایپو)", "price": -1,
             "input": [], "header": [{"name": "token", "required": True, "deafultValue": "ندارد"}]},
        ],
    }
    defaults.update(kwargs)
    return defaults


class TestStableId:
    def test_same_input_same_id(self):
        assert _stable_id(1, "group") == _stable_id(1, "group")

    def test_different_input_different_id(self):
        assert _stable_id(1, "group") != _stable_id(2, "group")

    def test_returns_valid_uuid_format(self):
        import uuid
        result = _stable_id(1, "group")
        uuid.UUID(result)   # نباید exception بدهد


class TestFirst:
    def test_returns_first_present_key(self):
        assert _first({"a": "x"}, "a", "b") == "x"

    def test_falls_back_to_second_key(self):
        assert _first({"b": "y"}, "a", "b") == "y"

    def test_returns_default_when_missing(self):
        assert _first({}, "a", "b", default="d") == "d"

    def test_skips_empty_string_value(self):
        assert _first({"a": "", "b": "y"}, "a", "b") == "y"


class TestFormatPrice:
    def test_zero_is_free(self):
        assert _format_price(0) == "رایگان"

    def test_minus_one_is_formula_based(self):
        assert "فرمول" in _format_price(-1)

    def test_positive_number_shown(self):
        assert "1000" in _format_price(1000)

    def test_none_is_unknown(self):
        assert _format_price(None) == "نامشخص"


class TestFormatFields:
    def test_empty_list_returns_none_message(self):
        assert "ندارد" in _format_fields([], "پارامترها")

    def test_includes_field_name_and_required(self):
        fields = [{"name": "amount", "required": True, "defaultValue": "10"}]
        result = _format_fields(fields, "ورودی")
        assert "amount" in result
        assert "الزامی" in result
        assert "10" in result

    def test_supports_typo_default_value_key(self):
        fields = [{"name": "token", "required": False, "deafultValue": "abc"}]
        result = _format_fields(fields, "هدر")
        assert "abc" in result


class TestBusinessServiceChunkerChunk:
    def test_produces_one_group_chunk_per_record(self):
        docs = BusinessServiceChunker().chunk([_group()])
        assert len([d for d in docs if d.metadata["doc_type"] == "service_group"]) == 1

    def test_produces_one_chunk_per_webservice(self):
        docs = BusinessServiceChunker().chunk([_group()])
        assert len([d for d in docs if d.metadata["doc_type"] == "webservice"]) == 2

    def test_group_chunk_strips_html_from_content(self):
        docs = BusinessServiceChunker().chunk([_group()])
        group_doc = next(d for d in docs if d.metadata["doc_type"] == "service_group")
        assert "<p>" not in group_doc.content
        assert "<b>" not in group_doc.content
        assert "کامل" in group_doc.content

    def test_service_chunk_contains_group_context(self):
        docs = BusinessServiceChunker().chunk([_group()])
        service_doc = next(d for d in docs if d.metadata.get("service_id") == 101)
        assert "پرداخت" in service_doc.content
        assert "شرکت الف" in service_doc.content

    def test_service_chunk_supports_typo_description_key(self):
        docs = BusinessServiceChunker().chunk([_group()])
        service_doc = next(d for d in docs if d.metadata.get("service_id") == 102)
        assert "توضیح ۲" in service_doc.content

    def test_service_chunk_price_formula_based(self):
        docs = BusinessServiceChunker().chunk([_group()])
        service_doc = next(d for d in docs if d.metadata.get("service_id") == 102)
        assert "فرمول" in service_doc.content

    def test_group_chunk_lists_service_names(self):
        docs = BusinessServiceChunker().chunk([_group()])
        group_doc = next(d for d in docs if d.metadata["doc_type"] == "service_group")
        assert "پرداخت آنلاین" in group_doc.content
        assert "استعلام موجودی" in group_doc.content

    def test_returns_document_instances(self):
        docs = BusinessServiceChunker().chunk([_group()])
        assert all(isinstance(d, Document) for d in docs)

    def test_no_webservices_only_group_chunk(self):
        docs = BusinessServiceChunker().chunk([_group(webservices=[])])
        assert len(docs) == 1

    def test_empty_records_returns_empty(self):
        assert BusinessServiceChunker().chunk([]) == []

    def test_ids_are_deterministic_across_calls(self):
        docs1 = BusinessServiceChunker().chunk([_group()])
        docs2 = BusinessServiceChunker().chunk([_group()])
        assert [d.id for d in docs1] == [d.id for d in docs2]

    def test_service_metadata_includes_group_info(self):
        docs = BusinessServiceChunker().chunk([_group()])
        service_doc = next(d for d in docs if d.metadata.get("service_id") == 101)
        assert service_doc.metadata["group_id"] == 1
        assert service_doc.metadata["provider"] == "شرکت الف"