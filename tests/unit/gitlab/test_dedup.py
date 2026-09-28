"""تست‌های dedup (جلوگیری از کامنت تکراری)."""

import pytest
from src.agents.code_review.gitlab.agent import _comment_fingerprint


class TestCommentFingerprint:
    def test_same_inputs_same_fingerprint(self):
        fp1 = _comment_fingerprint("src/auth.py", 42, "SQL injection detected")
        fp2 = _comment_fingerprint("src/auth.py", 42, "SQL injection detected")
        assert fp1 == fp2

    def test_different_file_different_fingerprint(self):
        fp1 = _comment_fingerprint("src/auth.py", 42, "body")
        fp2 = _comment_fingerprint("src/other.py", 42, "body")
        assert fp1 != fp2

    def test_different_line_different_fingerprint(self):
        fp1 = _comment_fingerprint("src/auth.py", 42, "body")
        fp2 = _comment_fingerprint("src/auth.py", 43, "body")
        assert fp1 != fp2

    def test_different_body_different_fingerprint(self):
        fp1 = _comment_fingerprint("src/auth.py", 42, "body A")
        fp2 = _comment_fingerprint("src/auth.py", 42, "body B")
        assert fp1 != fp2

    def test_none_line_supported(self):
        fp = _comment_fingerprint("src/auth.py", None, "general comment")
        assert isinstance(fp, str)
        assert len(fp) > 0

    def test_returns_hex_string(self):
        fp = _comment_fingerprint("f.py", 1, "body")
        int(fp, 16)   # باید hex معتبر باشد

    def test_body_truncated_to_80_chars(self):
        """دو body که فقط بعد از ۸۰ کاراکتر اول فرق دارند → fingerprint یکسان."""
        body_a = "A" * 80 + "DIFFERENT_SUFFIX_A"
        body_b = "A" * 80 + "DIFFERENT_SUFFIX_B"
        fp1 = _comment_fingerprint("f.py", 1, body_a)
        fp2 = _comment_fingerprint("f.py", 1, body_b)
        assert fp1 == fp2

    def test_body_shorter_than_80_chars_uses_full_body(self):
        fp1 = _comment_fingerprint("f.py", 1, "short body A")
        fp2 = _comment_fingerprint("f.py", 1, "short body B")
        assert fp1 != fp2

    def test_fingerprint_is_deterministic_across_calls(self):
        results = set()
        for _ in range(100):
            results.add(_comment_fingerprint("f.py", 1, "body"))
        assert len(results) == 1   # همیشه یکی است