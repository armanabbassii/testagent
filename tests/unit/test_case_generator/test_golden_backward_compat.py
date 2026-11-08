"""
سازگاریِ رو به عقب — مقایسه‌ی بایت‌به‌بایتِ خروجیِ قدم ۴ و ۵ با نسخه‌ی قبلی

پوشه‌ی fixtures/golden_v1 پیش از افزودنِ طراحیِ تست (test_design)، قیدهای
پارامتر و مسیرهای تودرتوی پاسخ با کدِ قبلی ساخته شده است: ورودی‌های قدم ۱ تا ۳
(بدونِ هیچ فیلدِ جدید)، نتیجه‌ی قدم ۴، نتیجه‌ی قدم ۵ و رشته‌ی دانلودیِ کالکشن.

این تست‌ها تضمین می‌کنند که نتیجه‌های قدیمی دقیقاً همان کالکشن را می‌سازند و
فیلدهای افزایشیِ قدم دوم (constraints / field_paths) بدونِ test_design هیچ
اثری روی کالکشن ندارند. هیچ LLM، شبکه یا Swagger ِ واقعی صدا زده نمی‌شود.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.agents.test_case_generator.final_review import FinalReviewGenerator
from src.agents.test_case_generator.postman_generation import Step4PostmanGenerator
from ui.formatting import collection_json, postman_collection

GOLDEN = Path(__file__).parent / "fixtures" / "golden_v1"
CHAINS = sorted(path.name for path in GOLDEN.iterdir() if path.is_dir())


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _inputs(name: str) -> dict:
    return json.loads((GOLDEN / name / "inputs.json").read_text(encoding="utf-8"))


def _run(inputs: dict) -> tuple[dict, dict]:
    step4 = Step4PostmanGenerator(**inputs["generator"]).generate(
        inputs["step1"], inputs["step2"], inputs["step3"]
    )
    review = FinalReviewGenerator().generate(
        inputs["step1"], inputs["step2"], inputs["step3"], step4
    )
    return step4, review


def test_golden_fixtures_exist():
    assert len(CHAINS) >= 6


@pytest.mark.parametrize("name", CHAINS)
def test_old_results_build_the_same_collection_byte_for_byte(name):
    step4, review = _run(_inputs(name))
    folder = GOLDEN / name
    assert _dump(step4) == (folder / "step4.json").read_text(encoding="utf-8")
    assert _dump(review) == (folder / "step5.json").read_text(encoding="utf-8")
    assert collection_json(postman_collection(review)) == (
        folder / "collection.postman_collection.json"
    ).read_text(encoding="utf-8")


def _with_additive_step2_keys(step2: dict) -> dict:
    """همان نتیجه‌ی قدم دوم با کلیدهای جدیدِ اختیاری روی هر عملیات."""
    enriched = copy.deepcopy(step2)
    for service in enriched.get("services") or []:
        for api in service.get("apis") or []:
            for parameter in api.get("parameters") or []:
                parameter["constraints"] = {"minimum": 0, "maximum": 50, "default": 10}
            for response in (api.get("responses") or {}).values():
                if isinstance(response, dict):
                    response["field_paths"] = ["result", "result.items[].id"]
    return enriched


@pytest.mark.parametrize("name", CHAINS)
def test_additive_step2_keys_without_test_design_do_not_change_the_collection(name):
    inputs = _inputs(name)
    inputs["step2"] = _with_additive_step2_keys(inputs["step2"])
    step4, _ = _run(inputs)
    expected = json.loads((GOLDEN / name / "step4.json").read_text(encoding="utf-8"))
    assert step4["collection"] == expected["collection"]
    assert step4["requests"] == expected["requests"]
    assert step4["unresolved"] == expected["unresolved"]


@pytest.mark.parametrize("name", CHAINS)
def test_additive_step1_fields_without_test_design_do_not_change_the_collection(name):
    inputs = _inputs(name)
    step1 = inputs["step1"]
    step1["requirements"] = [
        {"id": "R1", "text": "A requirement.", "kind": "behavior", "source": "description"}
    ]
    for case in step1["test_cases"]:
        case["requirement_refs"] = ["R1"]
        case["checks"] = [{"kind": "other", "description": "A check.", "fields": []}]
        case["input_intent"] = [{"name": "", "intent": "default"}]
    step1["dropped_test_cases"] = []
    step4, _ = _run(inputs)
    expected = json.loads((GOLDEN / name / "step4.json").read_text(encoding="utf-8"))
    assert step4["collection"] == expected["collection"]
