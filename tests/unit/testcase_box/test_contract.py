"""
تست‌های قراردادِ readiness: قاعده‌ی وضعیت، ترتیب/یکتایی، هم‌خوانیِ ثابت‌ها با منبعِ
اصلی و جداماندنِ importهای Box از ماژول‌های سنگین.
"""

from __future__ import annotations

import ast
import itertools
import sys
from pathlib import Path

import pytest

from src.testcase_box import contract as c
from src.testcase_box.contract import Finding, Readiness, ScriptReview

REPO_ROOT = Path(__file__).resolve().parents[3]
BOX_PACKAGE = REPO_ROOT / "src" / "testcase_box"
# شدت‌هایی که M3 تولید می‌کند؛ CONFIGURATION فقط مالِ آماده‌سازیِ M4 است
_SEVERITIES = (c.SEVERITY_ERROR, c.SEVERITY_REVIEW, c.SEVERITY_WARNING, c.SEVERITY_INFO)
_M4_CONFIGURATION_CODES = {
    "PROFILE_HOST_MISSING",
    "PROFILE_HOST_EMPTY",
    "VARIABLE_UNAVAILABLE",
}


def _f(severity: str, code: str = "X", location: str = "$", message: str = "m") -> Finding:
    return Finding(code, severity, location, message)


class TestIssueTable:
    def test_every_issue_has_a_known_severity_and_message(self):
        for code, (severity, template) in c.ISSUES.items():
            assert code == code.upper()
            assert severity in (*_SEVERITIES, c.SEVERITY_CONFIGURATION)
            assert template.strip()
        assert set(c.SEVERITY_RANK) == {*_SEVERITIES, c.SEVERITY_CONFIGURATION}

    def test_configuration_severity_is_only_used_by_preparation_codes(self):
        configuration = {
            code for code, (sev, _) in c.ISSUES.items() if sev == c.SEVERITY_CONFIGURATION
        }
        assert configuration == _M4_CONFIGURATION_CODES
        rank = c.SEVERITY_RANK
        assert rank[c.SEVERITY_REVIEW] < rank[c.SEVERITY_CONFIGURATION] < rank[c.SEVERITY_WARNING]
        # M3 هرگز CONFIGURATION تولید نمی‌کند؛ status_for آن را مسدودکننده نمی‌داند
        assert c.status_for([_f(c.SEVERITY_CONFIGURATION)]) == c.STATUS_READY

    def test_host_intent_mismatch_message_names_both_variables(self):
        item = c.finding(
            "HOST_INTENT_MISMATCH", "$", intent="VDI", expected="base_url_vdi", actual="base_url"
        )
        assert "{{base_url_vdi}}" in item.message and "{{base_url}}" in item.message

    def test_review_severity_is_only_used_by_review_codes(self):
        review = {code for code, (sev, _) in c.ISSUES.items() if sev == c.SEVERITY_REVIEW}
        assert review == {"SCRIPT_REVIEW_REQUIRED", "SCRIPT_REJECTED", "SCRIPT_APPROVAL_REVOKED"}
        assert c.ISSUES["SCRIPT_APPROVED"][0] == c.SEVERITY_INFO
        assert c.ISSUES["SCRIPT_SEND_REQUEST"][0] == c.SEVERITY_ERROR
        assert c.ISSUES["SCRIPT_FINGERPRINT_MISMATCH"][0] == c.SEVERITY_ERROR

    def test_finding_uses_the_table(self):
        item = c.finding("HEADER_DUPLICATE", "$.request.header", name="X-A", count=2)
        assert item.severity == c.SEVERITY_WARNING
        assert item.message == "Header 'X-A' is set 2 times."
        with pytest.raises(KeyError):
            c.finding("NOT_A_CODE", "$")


class TestStatusRules:
    @pytest.mark.parametrize(
        "present",
        [
            set(combo)
            for size in range(len(_SEVERITIES) + 1)
            for combo in itertools.combinations(_SEVERITIES, size)
        ],
        ids=lambda s: "+".join(sorted(s)) or "none",
    )
    def test_every_severity_combination(self, present):
        status = c.status_for([_f(sev) for sev in present])
        if c.SEVERITY_ERROR in present:
            assert status == c.STATUS_NOT_EXECUTABLE
        elif c.SEVERITY_REVIEW in present:
            assert status == c.STATUS_NEEDS_SCRIPT_REVIEW
        else:
            assert status == c.STATUS_READY

    def test_approved_info_never_forces_review(self):
        approved = c.finding("SCRIPT_APPROVED", "$")
        assert c.status_for([approved]) == c.STATUS_READY
        assert c.status_for([approved, c.finding("AUTH_HEADER_MISSING", "$")]) == c.STATUS_READY

    @pytest.mark.parametrize(
        ("status", "ready"),
        [
            (c.STATUS_READY, True),
            (c.STATUS_NEEDS_SCRIPT_REVIEW, False),
            (c.STATUS_NOT_EXECUTABLE, False),
        ],
    )
    def test_ready_is_derived_from_status(self, status, ready):
        review = ScriptReview(False, None, "NOT_REQUIRED", ())
        assert Readiness(None, status, None, (), (), (), review).ready is ready


class TestOrdering:
    def test_sorted_by_severity_code_location_message(self):
        items = [
            _f(c.SEVERITY_INFO, "A"),
            _f(c.SEVERITY_ERROR, "B", "$.b"),
            _f(c.SEVERITY_ERROR, "B", "$.a"),
            _f(c.SEVERITY_WARNING, "A"),
            _f(c.SEVERITY_REVIEW, "Z"),
            _f(c.SEVERITY_ERROR, "A"),
        ]
        assert [(f.severity, f.code, f.location) for f in c.ordered(items)] == [
            (c.SEVERITY_ERROR, "A", "$"),
            (c.SEVERITY_ERROR, "B", "$.a"),
            (c.SEVERITY_ERROR, "B", "$.b"),
            (c.SEVERITY_REVIEW, "Z", "$"),
            (c.SEVERITY_WARNING, "A", "$"),
            (c.SEVERITY_INFO, "A", "$"),
        ]

    def test_identical_findings_are_deduplicated_but_distinct_messages_kept(self):
        one = _f(c.SEVERITY_WARNING, "W", "$.x", "field a")
        other = _f(c.SEVERITY_WARNING, "W", "$.x", "field b")
        assert c.ordered([one, one, other, one]) == (one, other)

    def test_order_is_independent_of_input_order(self):
        items = [_f(sev, code) for sev in _SEVERITIES for code in ("B", "A")]
        assert c.ordered(items) == c.ordered(list(reversed(items)))


class TestHelpers:
    def test_safe_name(self):
        assert c.safe_name("X-Request-Id") == "X-Request-Id"
        assert c.safe_name("$guid") == "$guid"
        assert c.safe_name("has space") == "<unnamed>"
        assert c.safe_name("x" * 65) == "<unnamed>"
        assert c.safe_name(5) == "<unnamed>"

    def test_variable_reference(self):
        assert c.is_variable_reference(" {{token}} ")
        assert not c.is_variable_reference("Bearer {{token}}")
        assert not c.is_variable_reference("{{a}}{{b}}")

    def test_variable_regex_matches_postman_semantics(self):
        assert c.VARIABLE_RE.findall("{{a}}/{{ b }}/{{}}/{{c{{d}}") == ["a", " b ", "", "d"]


class TestDrift:
    """ثابت‌های کپی‌شده باید با منبعِ اصلی یکی بمانند (importِ سنگین فقط در تست)."""

    def test_host_variables(self):
        from src.agents.test_case_generator.host_selection import HOST_VARIABLES

        assert c.HOST_VARIABLES == HOST_VARIABLES

    def test_token_variable(self):
        from src.test_execution.newman.models import TOKEN_VARIABLE

        assert c.TOKEN_VARIABLE == TOKEN_VARIABLE

    def test_secret_patterns(self):
        from src.test_execution.newman import redaction

        assert c.SECRET_KEY_RE.pattern == redaction.SECRET_KEY_RE.pattern
        assert c.SECRET_KEY_RE.flags == redaction.SECRET_KEY_RE.flags
        assert c.SENSITIVE_HEADERS == redaction.SENSITIVE_HEADERS
        assert c.CORS_HEADERS == redaction.CORS_HEADERS
        for name in ("Authorization", "Cookie", "X-Session-Id", "Access-Control-Max-Age", "Accept"):
            assert c.is_sensitive_header(name) == redaction.is_sensitive_header(name)

    def test_variable_pattern_matches_postman_collection(self):
        source = REPO_ROOT / "node_modules" / "postman-collection" / "lib" / "superstring"
        source = source / "index.js"
        if not source.exists():
            pytest.skip("postman-collection is not installed")
        assert "REGEX_EXTRACT_VARS: /" + c.VARIABLE_RE.pattern + "/g" in source.read_text(
            encoding="utf-8"
        )


# استثناهای صریحِ مرزِ وابستگی:
#   * runner.py (M7.5) — موتورِ Newman، Gate ِ مشترک و منبعِ توکن؛
#   * recovery.py (M7.6) — فقط Gate ِ مشترک (بازیابی هیچ چیزی اجرا نمی‌کند).
# فقط run_service.py (میزبانِ UI، M7.6) runner را import می‌کند؛ هیچ ماژولِ دیگری.
RUNNER_MODULE = "runner.py"
RUNNER_DEPENDENCIES = ("src.test_execution.newman", "src.execution_gate", "src.auth")
MODULE_DEPENDENCIES = {
    RUNNER_MODULE: RUNNER_DEPENDENCIES,
    "recovery.py": ("src.execution_gate",),
}
RUNNER_IMPORTERS = frozenset({"run_service.py"})


def _imports(module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def _box_modules():
    # بازگشتی — زیرپوشه‌ای نمی‌تواند از این مرز فرار کند
    return sorted(BOX_PACKAGE.rglob("*.py"))


def test_box_modules_import_only_stdlib_and_box():
    """ایستا (بی‌subprocess، بی‌.env): هیچ importِ runtime از agents/newman/auth/batch —
    جز همان سه وابستگیِ صریحِ runner.py."""
    allowed_stdlib = set(sys.stdlib_module_names) | {"__future__"}
    offenders = []
    for module in _box_modules():
        allowed_extra = MODULE_DEPENDENCIES.get(module.name, ())
        for name in _imports(module):
            if name.startswith("src.testcase_box") or name.split(".")[0] in allowed_stdlib:
                continue
            if any(name == dep or name.startswith(dep + ".") for dep in allowed_extra):
                continue
            offenders.append(f"{module.relative_to(BOX_PACKAGE)}: {name}")
    assert offenders == []


def test_only_the_runner_is_the_newman_boundary():
    assert (BOX_PACKAGE / RUNNER_MODULE).is_file()
    assert [m.name for m in _box_modules() if m.parent != BOX_PACKAGE] == []  # بی‌زیرپوشه
    runner_imports = _imports(BOX_PACKAGE / RUNNER_MODULE)
    for dependency in RUNNER_DEPENDENCIES:
        assert any(name.startswith(dependency) for name in runner_imports), dependency

    # جهتِ وابستگی یک‌طرفه است: هیچ ماژولِ دیگری از Box runner را import نمی‌کند
    def imports_runner(module) -> bool:
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import) and any(
                a.name.startswith("src.testcase_box.runner") for a in node.names
            ):
                return True
            if isinstance(node, ast.ImportFrom):
                if (node.module or "").startswith("src.testcase_box.runner"):
                    return True
                if node.module == "src.testcase_box" and any(
                    a.name == "runner" for a in node.names
                ):
                    return True
        return False

    importers = {m.name for m in _box_modules() if m.name != RUNNER_MODULE and imports_runner(m)}
    assert importers <= RUNNER_IMPORTERS  # فقط میزبانِ UI
    # recovery هرگز موتور یا توکن را نمی‌بیند
    recovery_imports = _imports(BOX_PACKAGE / "recovery.py")
    assert not any(n.startswith(("src.test_execution", "src.auth")) for n in recovery_imports)
    assert not imports_runner(BOX_PACKAGE / "recovery.py")
    for forbidden in (
        "src.batch_testing",
        "src.agents",
        "ui",
        "src.task_sources",
        "src.llm_client",
    ):
        assert not any(n == forbidden or n.startswith(forbidden + ".") for n in runner_imports)
