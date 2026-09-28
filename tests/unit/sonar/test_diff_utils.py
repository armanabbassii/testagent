"""
tests/unit/sonar/test_diff_utils.py — تست parse_added_lines

نمونه diff متن دقیقاً با فرمت GitLabClient.get_mr_diff مطابقت دارد:
    "### {file_path}\n{unified diff hunks}"
"""

from src.agents.code_review.sonar.diff_utils import parse_added_lines


class TestParseAddedLines:
    def test_empty_diff_returns_empty_dict(self):
        assert parse_added_lines("") == {}

    def test_single_file_single_hunk_mixed_changes(self):
        diff_text = (
            "### src/foo.py\n"
            "@@ -10,3 +10,4 @@\n"
            " def foo():\n"
            "-    return 1\n"
            "+    return 2\n"
            "+    return 3\n"
            " \n"
        )
        result = parse_added_lines(diff_text)
        assert result["src/foo.py"] == {11, 12}

    def test_pure_addition_lines_at_correct_numbers(self):
        diff_text = (
            "### src/bar.py\n"
            "@@ -5,2 +5,4 @@\n"
            " existing line\n"
            "+new line one\n"
            "+new line two\n"
            " another existing line\n"
        )
        result = parse_added_lines(diff_text)
        assert result["src/bar.py"] == {6, 7}

    def test_pure_deletion_only_file_not_in_result(self):
        diff_text = (
            "### src/removed_stuff.py\n"
            "@@ -1,3 +1,1 @@\n"
            "-line one\n"
            "-line two\n"
            " remaining line\n"
        )
        result = parse_added_lines(diff_text)
        assert "src/removed_stuff.py" not in result

    def test_multiple_files_parsed_independently(self):
        diff_text = (
            "### src/a.py\n"
            "@@ -1,1 +1,2 @@\n"
            " context\n"
            "+added in a\n"
            "### src/b.py\n"
            "@@ -1,1 +1,2 @@\n"
            " context\n"
            "+added in b\n"
        )
        result = parse_added_lines(diff_text)
        assert result["src/a.py"] == {2}
        assert result["src/b.py"] == {2}

    def test_multiple_hunks_in_same_file(self):
        diff_text = (
            "### src/multi.py\n"
            "@@ -1,1 +1,2 @@\n"
            " context\n"
            "+first added\n"
            "@@ -20,1 +21,2 @@\n"
            " context\n"
            "+second added\n"
        )
        result = parse_added_lines(diff_text)
        assert result["src/multi.py"] == {2, 22}

    def test_new_file_hunk_starting_at_line_one(self):
        diff_text = (
            "### src/new_file.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+line one\n"
            "+line two\n"
            "+line three\n"
        )
        result = parse_added_lines(diff_text)
        assert result["src/new_file.py"] == {1, 2, 3}

    def test_context_lines_do_not_count_as_added(self):
        diff_text = (
            "### src/context_only.py\n"
            "@@ -1,3 +1,3 @@\n"
            " line one\n"
            " line two\n"
            " line three\n"
        )
        result = parse_added_lines(diff_text)
        assert "src/context_only.py" not in result

    def test_lines_outside_any_hunk_are_ignored(self):
        # اگر متنی قبل از اولین @@ باشد (نباید معمولاً پیش بیاید)، نادیده گرفته می‌شود
        diff_text = (
            "### src/foo.py\n"
            "some preamble text\n"
            "@@ -1,1 +1,2 @@\n"
            " context\n"
            "+added\n"
        )
        result = parse_added_lines(diff_text)
        assert result["src/foo.py"] == {2}