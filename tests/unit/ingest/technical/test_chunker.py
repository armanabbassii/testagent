"""تست‌های TechnicalCodeChunker."""

import pytest
from src.ingest.technical.chunker import TechnicalCodeChunker
from src.ingest.technical.models import JavaClassRecord, MethodRecord
from src.vector_store.base import Document


def _method(name: str, **kwargs) -> MethodRecord:
    defaults = MethodRecord(
        name=name, return_type="void", modifiers=[], annotations=[],
        params=[], throws=[], javadoc="", body="", calls=[], external_calls=[],
        start_line=1, end_line=2, http_method=None, endpoint_path=None,
    )
    defaults.update(kwargs)
    return defaults


def _java_class(**kwargs) -> dict:
    defaults = JavaClassRecord(
        kind="java_class", file_path="Foo.java", package="com.x",
        class_name="Foo", qualified_name="com.x.Foo", class_type="class",
        modifiers=[], annotations=[], extends="", implements=[], imports=[],
        javadoc="", is_test=False, is_external_client=False, base_endpoint_path=None,
        methods=[], parse_mode="ast",
    )
    defaults.update(kwargs)
    return defaults


class TestChunkJavaClass:
    def test_produces_one_class_chunk(self):
        docs = TechnicalCodeChunker().chunk([_java_class()])
        assert len([d for d in docs if d.metadata["doc_type"] == "java_class"]) == 1

    def test_produces_one_method_chunk_per_method(self):
        record = _java_class(methods=[_method("a"), _method("b")])
        docs = TechnicalCodeChunker().chunk([record])
        assert len([d for d in docs if d.metadata["doc_type"] == "java_method"]) == 2

    def test_returns_document_instances(self):
        record = _java_class(methods=[_method("a")])
        docs = TechnicalCodeChunker().chunk([record])
        assert all(isinstance(d, Document) for d in docs)

    def test_class_chunk_content_includes_qualified_name(self):
        docs = TechnicalCodeChunker().chunk([_java_class()])
        class_doc = next(d for d in docs if d.metadata["doc_type"] == "java_class")
        assert "com.x.Foo" in class_doc.content

    def test_method_chunk_includes_class_context(self):
        record = _java_class(methods=[_method("bar")])
        docs = TechnicalCodeChunker().chunk([record])
        method_doc = next(d for d in docs if d.metadata["doc_type"] == "java_method")
        assert "com.x.Foo" in method_doc.content
        assert "bar" in method_doc.content

    def test_ids_are_deterministic_across_calls(self):
        record = _java_class(methods=[_method("bar")])
        docs1 = TechnicalCodeChunker().chunk([record])
        docs2 = TechnicalCodeChunker().chunk([record])
        assert [d.id for d in docs1] == [d.id for d in docs2]

    def test_endpoint_only_set_when_method_has_http_mapping(self):
        record = _java_class(base_endpoint_path="/api", methods=[
            _method("list", http_method="GET", endpoint_path="/list"),
            _method("helper"),
        ])
        docs = {d.metadata["method_name"]: d for d in TechnicalCodeChunker().chunk([record])
                if d.metadata["doc_type"] == "java_method"}
        assert docs["list"].metadata["endpoint_path"] == "/api/list"
        assert docs["helper"].metadata["endpoint_path"] is None

    def test_include_bodies_false_omits_body_text(self):
        record = _java_class(methods=[_method("bar", body="return 1;")])
        docs = TechnicalCodeChunker(include_bodies=False).chunk([record])
        method_doc = next(d for d in docs if d.metadata["doc_type"] == "java_method")
        assert "return 1;" not in method_doc.content

    def test_include_bodies_true_keeps_body_text(self):
        record = _java_class(methods=[_method("bar", body="return 1;")])
        docs = TechnicalCodeChunker(include_bodies=True).chunk([record])
        method_doc = next(d for d in docs if d.metadata["doc_type"] == "java_method")
        assert "return 1;" in method_doc.content


class TestExternalCallSurfacing:
    def test_external_call_listed_in_metadata(self):
        record = _java_class(methods=[_method(
            "listItems",
            external_calls=[{"client_call": "getForObject", "http_method": "GET",
                              "path_template": "/nzh/biz/list", "resolved": True}],
        )])
        docs = TechnicalCodeChunker().chunk([record])
        method_doc = next(d for d in docs if d.metadata["doc_type"] == "java_method")
        assert method_doc.metadata["has_external_call"] is True
        assert "/nzh/biz/list" in method_doc.metadata["external_endpoints"]

    def test_external_call_mentioned_in_content(self):
        record = _java_class(methods=[_method(
            "listItems",
            external_calls=[{"client_call": "getForObject", "http_method": "GET",
                              "path_template": "/nzh/biz/list", "resolved": True}],
        )])
        docs = TechnicalCodeChunker().chunk([record])
        method_doc = next(d for d in docs if d.metadata["doc_type"] == "java_method")
        assert "/nzh/biz/list" in method_doc.content

    def test_no_external_call_flagged_false(self):
        record = _java_class(methods=[_method("plain")])
        docs = TechnicalCodeChunker().chunk([record])
        method_doc = next(d for d in docs if d.metadata["doc_type"] == "java_method")
        assert method_doc.metadata["has_external_call"] is False
        assert method_doc.metadata["external_endpoints"] == []

    def test_feign_client_flag_on_class_and_method(self):
        record = _java_class(is_external_client=True, methods=[
            _method("listItems", http_method="GET", endpoint_path="/list"),
        ])
        docs = TechnicalCodeChunker().chunk([record])
        class_doc = next(d for d in docs if d.metadata["doc_type"] == "java_class")
        method_doc = next(d for d in docs if d.metadata["doc_type"] == "java_method")
        assert class_doc.metadata["is_external_client"] is True
        assert method_doc.metadata["is_external_client"] is True


class TestTestCoverageLinkage:
    def test_covered_method_flagged(self):
        production = _java_class(methods=[_method("doWork")])
        test = _java_class(
            file_path="FooTest.java", class_name="FooTest",
            qualified_name="com.x.FooTest", is_test=True,
            methods=[_method("testDoWork", calls=["instance.doWork"])],
        )
        docs = TechnicalCodeChunker().chunk([production, test])
        method_doc = next(d for d in docs if d.metadata.get("method_name") == "doWork")
        assert method_doc.metadata["has_test"] is True
        assert "com.x.FooTest#testDoWork" in method_doc.metadata["tested_by"]

    def test_uncovered_method_not_flagged(self):
        production = _java_class(methods=[_method("doWork"), _method("untested")])
        test = _java_class(
            file_path="FooTest.java", class_name="FooTest", is_test=True,
            methods=[_method("testDoWork", calls=["instance.doWork"])],
        )
        docs = TechnicalCodeChunker().chunk([production, test])
        untested_doc = next(d for d in docs if d.metadata.get("method_name") == "untested")
        assert untested_doc.metadata["has_test"] is False

    def test_multiple_tests_accumulate_in_tested_by(self):
        production = _java_class(methods=[_method("doWork")])
        test1 = _java_class(
            file_path="T1.java", class_name="T1", qualified_name="com.x.T1", is_test=True,
            methods=[_method("t1", calls=["x.doWork"])],
        )
        test2 = _java_class(
            file_path="T2.java", class_name="T2", qualified_name="com.x.T2", is_test=True,
            methods=[_method("t2", calls=["y.doWork"])],
        )
        docs = TechnicalCodeChunker().chunk([production, test1, test2])
        method_doc = next(d for d in docs if d.metadata.get("method_name") == "doWork")
        assert len(method_doc.metadata["tested_by"]) == 2

    def test_test_class_produces_its_own_chunks_too(self):
        test = _java_class(is_test=True, methods=[_method("t1")])
        docs = TechnicalCodeChunker().chunk([test])
        assert any(d.metadata["doc_type"] == "java_class" and d.metadata["is_test"] for d in docs)
        assert any(d.metadata["doc_type"] == "java_method" for d in docs)


class TestChunkMarkdown:
    def _md(self, content: str) -> dict:
        return {"kind": "markdown_doc", "file_path": "notes.md", "title": "Notes", "content": content}

    def test_no_headers_single_chunk(self):
        docs = TechnicalCodeChunker().chunk([self._md("just some text")])
        assert len(docs) == 1
        assert docs[0].metadata["doc_type"] == "technical_doc"

    def test_splits_by_level_two_headers(self):
        content = "intro\n\n## Section A\n\ntext a\n\n## Section B\n\ntext b"
        docs = TechnicalCodeChunker().chunk([self._md(content)])
        sections = [d.metadata["section"] for d in docs]
        assert "Section A" in sections and "Section B" in sections

    def test_intro_before_first_header_kept(self):
        content = "intro text\n\n## Section A\n\ntext a"
        docs = TechnicalCodeChunker().chunk([self._md(content)])
        assert any("intro text" in d.content for d in docs)


class TestChunkSwagger:
    def _swagger(self, paths: dict) -> dict:
        return {"kind": "swagger_spec", "file_path": "openapi.yaml", "format": "yaml", "raw": {"paths": paths}}

    def test_one_chunk_per_operation(self):
        raw_paths = {"/nzh/biz/list": {"get": {"operationId": "list"}, "post": {"operationId": "create"}}}
        docs = TechnicalCodeChunker().chunk([self._swagger(raw_paths)])
        assert len(docs) == 2

    def test_path_and_method_in_metadata(self):
        raw_paths = {"/nzh/biz/list": {"get": {"operationId": "list"}}}
        docs = TechnicalCodeChunker().chunk([self._swagger(raw_paths)])
        assert docs[0].metadata["path"] == "/nzh/biz/list"
        assert docs[0].metadata["http_method"] == "GET"

    def test_non_http_keys_ignored(self):
        raw_paths = {"/x": {"parameters": [], "get": {"operationId": "x"}}}
        docs = TechnicalCodeChunker().chunk([self._swagger(raw_paths)])
        assert len(docs) == 1

    def test_empty_paths_returns_no_chunks(self):
        docs = TechnicalCodeChunker().chunk([self._swagger({})])
        assert docs == []


class TestChunkHtml:
    def test_html_template_single_chunk(self):
        record = {"kind": "html_template", "file_path": "index.html", "content": "<html>hi</html>"}
        docs = TechnicalCodeChunker().chunk([record])
        assert len(docs) == 1
        assert docs[0].metadata["doc_type"] == "html_template"


class TestChunkEmpty:
    def test_empty_records_returns_empty(self):
        assert TechnicalCodeChunker().chunk([]) == []
