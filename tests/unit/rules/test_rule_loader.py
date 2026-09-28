"""تست‌های RuleLoader."""

import pytest
from pathlib import Path
from src.rules.loader import RuleLoader


@pytest.fixture
def rules_dir(tmp_path) -> Path:
    """یک ساختار rule واقعی برای تست."""
    d = tmp_path / "project_rules"
    (d / "code_review" / "languages").mkdir(parents=True)
    (d / "gitlab").mkdir(parents=True)

    (d / "code_review" / "reviewer.md").write_text("## Reviewer Rules\n- Write in Persian")
    (d / "code_review" / "languages" / "python.md").write_text("## Python Rules\n- Use type hints")
    (d / "code_review" / "languages" / "javascript.md").write_text("## JS Rules\n- Use const")
    (d / "gitlab" / "commenter.md").write_text("## Commenter Rules\n- Be concise")

    return d


@pytest.fixture
def loader(rules_dir) -> RuleLoader:
    return RuleLoader(rules_dir=rules_dir)


class TestRuleLoaderLoad:
    def test_load_existing_file(self, loader):
        content = loader.load("code_review/reviewer")
        assert "Reviewer Rules" in content
        assert "Persian" in content

    def test_load_nonexistent_returns_empty(self, loader):
        assert loader.load("nonexistent/file") == ""

    def test_load_strips_whitespace(self, loader):
        content = loader.load("code_review/reviewer")
        assert content == content.strip()

    def test_load_nested_path(self, loader):
        content = loader.load("code_review/languages/python")
        assert "Python Rules" in content

    def test_load_without_md_extension(self, loader):
        # پسوند .md نباید در rule_path باشد
        content = loader.load("gitlab/commenter")
        assert "Commenter Rules" in content


class TestRuleLoaderLoadMany:
    def test_load_many_combines_files(self, loader):
        result = loader.load_many(["code_review/reviewer", "gitlab/commenter"])
        assert "Reviewer Rules" in result
        assert "Commenter Rules" in result

    def test_load_many_skips_missing(self, loader):
        result = loader.load_many(["code_review/reviewer", "nonexistent/file"])
        assert "Reviewer Rules" in result
        assert len(result) > 0

    def test_load_many_empty_list(self, loader):
        assert loader.load_many([]) == ""

    def test_load_many_all_missing(self, loader):
        assert loader.load_many(["no/a", "no/b"]) == ""

    def test_load_many_custom_separator(self, loader):
        result = loader.load_many(
            ["code_review/reviewer", "gitlab/commenter"],
            separator="\n===\n"
        )
        assert "===" in result

    def test_load_many_order_preserved(self, loader):
        result = loader.load_many(["code_review/reviewer", "gitlab/commenter"])
        reviewer_pos = result.index("Reviewer Rules")
        commenter_pos = result.index("Commenter Rules")
        assert reviewer_pos < commenter_pos


class TestRuleLoaderLoadDir:
    def test_load_dir_loads_all_files(self, loader):
        result = loader.load_dir("code_review/languages")
        assert "Python Rules" in result
        assert "JS Rules" in result

    def test_load_dir_nonexistent_returns_empty(self, loader):
        assert loader.load_dir("nonexistent/dir") == ""

    def test_load_dir_alphabetical_order(self, loader):
        result = loader.load_dir("code_review/languages")
        js_pos = result.index("JS Rules")
        py_pos = result.index("Python Rules")
        # javascript.md قبل از python.md الفبایی است
        assert js_pos < py_pos


class TestRuleLoaderExists:
    def test_existing_rule_returns_true(self, loader):
        assert loader.exists("code_review/reviewer") is True

    def test_missing_rule_returns_false(self, loader):
        assert loader.exists("nonexistent/rule") is False

    def test_nested_rule_exists(self, loader):
        assert loader.exists("code_review/languages/python") is True


class TestRuleLoaderListRules:
    def test_list_all_rules(self, loader):
        rules = loader.list_rules()
        assert any("reviewer" in r for r in rules)
        assert any("commenter" in r for r in rules)
        assert any("python" in r for r in rules)

    def test_list_rules_from_subdir(self, loader):
        rules = loader.list_rules("code_review/languages")
        assert any("python" in r for r in rules)
        assert any("javascript" in r for r in rules)

    def test_list_nonexistent_dir(self, loader):
        assert loader.list_rules("nonexistent") == []


class TestRuleLoaderInject:
    def test_inject_appends_rules_to_prompt(self, loader):
        result = loader.inject("Base prompt.", ["code_review/reviewer"])
        assert "Base prompt." in result
        assert "Reviewer Rules" in result

    def test_inject_missing_rule_returns_base(self, loader):
        result = loader.inject("Base prompt.", ["nonexistent/rule"])
        assert result == "Base prompt."

    def test_inject_custom_section_title(self, loader):
        result = loader.inject("Base.", ["code_review/reviewer"],
                               section_title="## Custom Title")
        assert "## Custom Title" in result

    def test_inject_multiple_rules(self, loader):
        result = loader.inject(
            "Base.",
            ["code_review/reviewer", "gitlab/commenter"]
        )
        assert "Reviewer Rules" in result
        assert "Commenter Rules" in result

    def test_inject_dir(self, loader):
        result = loader.inject_dir("Base.", "code_review/languages")
        assert "Python Rules" in result
        assert "JS Rules" in result

    def test_inject_dir_missing_returns_base(self, loader):
        result = loader.inject_dir("Base.", "nonexistent")
        assert result == "Base."


class TestRuleLoaderFromEnv:
    def test_reads_rules_dir_from_env(self, rules_dir):
        import os
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("RULES_DIR", str(rules_dir))
            loader = RuleLoader()
        content = loader.load("code_review/reviewer")
        assert "Reviewer Rules" in content

    def test_default_dir_when_env_not_set(self, tmp_path):
        import os
        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("RULES_DIR", raising=False)
            loader = RuleLoader()
        # فقط بررسی می‌کنیم بدون خطا ساخته شد
        assert loader is not None