"""تست‌های TechnicalSourceLoader."""

import pytest
from src.ingest.technical.loader import TechnicalSourceLoader


_JAVA_SRC = "package com.x;\npublic class Foo { public void bar() {} }\n"


class TestTechnicalSourceLoaderLoad:
    def test_missing_path_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            TechnicalSourceLoader().load(tmp_path / "nope")

    def test_loads_java_file(self, tmp_path):
        (tmp_path / "Foo.java").write_text(_JAVA_SRC, encoding="utf-8")
        records = TechnicalSourceLoader().load(tmp_path)
        assert len(records) == 1
        assert records[0]["kind"] == "java_class"

    def test_loads_markdown_file(self, tmp_path):
        (tmp_path / "readme.md").write_text("# Title\n\nbody", encoding="utf-8")
        records = TechnicalSourceLoader().load(tmp_path)
        assert records[0]["kind"] == "markdown_doc"
        assert records[0]["title"] == "Title"

    def test_markdown_title_defaults_to_filename(self, tmp_path):
        (tmp_path / "notes.md").write_text("no heading here", encoding="utf-8")
        records = TechnicalSourceLoader().load(tmp_path)
        assert records[0]["title"] == "notes"

    def test_loads_swagger_json(self, tmp_path):
        (tmp_path / "swagger.json").write_text('{"paths": {}}', encoding="utf-8")
        records = TechnicalSourceLoader().load(tmp_path)
        assert records[0]["kind"] == "swagger_spec"
        assert records[0]["format"] == "json"

    def test_loads_swagger_yaml(self, tmp_path):
        (tmp_path / "openapi.yaml").write_text("paths: {}\n", encoding="utf-8")
        records = TechnicalSourceLoader().load(tmp_path)
        assert records[0]["kind"] == "swagger_spec"
        assert records[0]["format"] == "yaml"

    def test_json_file_without_swagger_name_ignored(self, tmp_path):
        (tmp_path / "data.json").write_text('{"a": 1}', encoding="utf-8")
        records = TechnicalSourceLoader().load(tmp_path)
        assert records == []

    def test_loads_html_by_default(self, tmp_path):
        (tmp_path / "page.html").write_text("<html></html>", encoding="utf-8")
        records = TechnicalSourceLoader().load(tmp_path)
        assert records[0]["kind"] == "html_template"

    def test_html_skipped_when_disabled(self, tmp_path):
        (tmp_path / "page.html").write_text("<html></html>", encoding="utf-8")
        records = TechnicalSourceLoader(include_html=False).load(tmp_path)
        assert records == []

    def test_ignores_build_directory(self, tmp_path):
        build_dir = tmp_path / "target"
        build_dir.mkdir()
        (build_dir / "Generated.java").write_text(_JAVA_SRC, encoding="utf-8")
        records = TechnicalSourceLoader().load(tmp_path)
        assert records == []

    def test_ignores_git_directory(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config.md").write_text("x", encoding="utf-8")
        records = TechnicalSourceLoader().load(tmp_path)
        assert records == []

    def test_unrelated_extension_ignored(self, tmp_path):
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
        records = TechnicalSourceLoader().load(tmp_path)
        assert records == []

    def test_scans_nested_directories(self, tmp_path):
        nested = tmp_path / "src" / "main" / "java" / "com" / "x"
        nested.mkdir(parents=True)
        (nested / "Foo.java").write_text(_JAVA_SRC, encoding="utf-8")
        records = TechnicalSourceLoader().load(tmp_path)
        assert len(records) == 1

    def test_broken_file_does_not_stop_scan(self, tmp_path):
        (tmp_path / "swagger_bad.json").write_text("{not valid json", encoding="utf-8")
        (tmp_path / "Foo.java").write_text(_JAVA_SRC, encoding="utf-8")
        records = TechnicalSourceLoader().load(tmp_path)
        # فایل swagger خراب رد می‌شود، فایل جاوا باید همچنان پردازش شود
        assert any(r["kind"] == "java_class" for r in records)

    def test_multiple_files_all_collected(self, tmp_path):
        (tmp_path / "Foo.java").write_text(_JAVA_SRC, encoding="utf-8")
        (tmp_path / "readme.md").write_text("# T\nbody", encoding="utf-8")
        records = TechnicalSourceLoader().load(tmp_path)
        kinds = {r["kind"] for r in records}
        assert kinds == {"java_class", "markdown_doc"}
