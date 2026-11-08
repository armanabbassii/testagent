"""
تست‌های نیتِ میزبان و پروفایلِ اجرا: اعتبارسنجیِ نام‌ها، جدا بودنِ دو مجموعه و هم‌خوانیِ
فهرستِ متغیرهای پویا با postman-collection.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.testcase_box import contract as c
from src.testcase_box.profiles import (
    DYNAMIC_VARIABLES,
    HOST_VARIABLE_FOR_INTENT,
    ExecutionProfile,
    HostIntent,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


class TestHostIntent:
    def test_intent_maps_to_exactly_one_host_variable(self):
        assert HOST_VARIABLE_FOR_INTENT == {
            HostIntent.NORMAL: c.BASE_URL,
            HostIntent.VDI: c.BASE_URL_VDI,
        }
        assert set(HOST_VARIABLE_FOR_INTENT.values()) == set(c.HOST_VARIABLES)

    def test_values(self):
        assert [intent.value for intent in HostIntent] == ["NORMAL", "VDI"]


class TestExecutionProfile:
    def test_sets_are_normalized_to_frozensets(self):
        profile = ExecutionProfile("p", resolved_variables=["a", "b", "a"], empty_variables=("e",))
        assert profile.resolved_variables == frozenset({"a", "b"})
        assert profile.empty_variables == frozenset({"e"})
        assert isinstance(profile.resolved_variables, frozenset)

    def test_defaults_are_empty(self):
        profile = ExecutionProfile("p")
        assert profile.resolved_variables == frozenset() == profile.empty_variables

    @pytest.mark.parametrize("profile_id", ["", "   ", None, 7])
    def test_profile_id_is_required(self, profile_id):
        with pytest.raises(ValueError, match="profile_id"):
            ExecutionProfile(profile_id)

    @pytest.mark.parametrize(
        "names",
        [
            ["{{a}}"],
            ["a}}"],
            ["{a"],
            [""],
            ["  "],
            [7],
            [None],
            "abc",
            b"abc",
            {"a": "value"},
            42,
        ],
        ids=repr,
    )
    def test_invalid_names_are_rejected(self, names):
        with pytest.raises(ValueError, match="resolved_variables"):
            ExecutionProfile("p", resolved_variables=names)
        with pytest.raises(ValueError, match="empty_variables"):
            ExecutionProfile("p", empty_variables=names)

    def test_resolved_and_empty_must_not_overlap(self):
        with pytest.raises(ValueError, match="both"):
            ExecutionProfile("p", resolved_variables={"a", "b"}, empty_variables={"b"})

    def test_errors_never_contain_names(self):
        with pytest.raises(ValueError) as error:
            ExecutionProfile("p", resolved_variables={"secret-name}}"})
        assert "secret-name" not in str(error.value)

    def test_profile_is_immutable(self):
        profile = ExecutionProfile("p", resolved_variables={"a"})
        with pytest.raises(AttributeError):
            profile.profile_id = "q"


class TestDynamicVariables:
    def test_shape(self):
        assert len(DYNAMIC_VARIABLES) == 117
        assert all(re.fullmatch(r"\$[A-Za-z]+", name) for name in DYNAMIC_VARIABLES)
        assert {"$guid", "$timestamp", "$randomInt", "$isoTimestamp"} <= DYNAMIC_VARIABLES

    def test_matches_postman_collection(self):
        source = REPO_ROOT / "node_modules" / "postman-collection" / "lib" / "superstring"
        source = source / "dynamic-variables.js"
        if not source.exists():
            pytest.skip("postman-collection is not installed")
        names = re.findall(r"^ {8}(\$[A-Za-z]+): \{", source.read_text(encoding="utf-8"), re.M)
        assert len(names) == len(set(names))
        assert frozenset(names) == DYNAMIC_VARIABLES
