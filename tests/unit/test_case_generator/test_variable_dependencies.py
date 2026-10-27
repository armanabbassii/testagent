"""
تست‌های رگرسیونِ «متغیرِ درخواستیِ کاربر» — از تسک تا JSONِ نهاییِ Postman

سناریوی مرجع (همان مثالِ تسک، با قراردادِ واقعیِ snapshotِ Admin):

    TC-001  POST /admin/voucher/amount   ایجادِ بن — تولیدکننده‌ی voucherId
    TC-002  POST /admin/voucher/amount   ایجادِ بنِ نامعتبر (منفی)
    TC-003  GET  /admin/voucher/{id}     دریافتِ بنِ ساخته‌شده — مصرف‌کننده
    TC-004  GET  /admin/voucher/{id}     دریافتِ بنِ ناموجود (منفی، ورودیِ مستقل)
    TC-005  PUT  /admin/voucher/{id}     ویرایشِ بنِ ساخته‌شده — مصرف‌کننده‌ی دوم

پاسخِ این عملیات‌ها در Swagger فقط ``{"type": "object"}`` است (مثلِ سندِ
واقعی)، پس مسیرِ result.body.id قابلِ تأیید با قرارداد نیست و باید همان‌طور که
کاربر گفته حفظ و گزارش شود.

زنجیره‌ی واقعی اجرا می‌شود: اعتبارسنجیِ قدم ۱ → کشفِ Swagger و build_resultِ قدم
۲ → قراردادِ فشرده‌ی ۲→۳ → عاملِ قدم ۳ (فقط LLM با پاسخِ ثابت جایگزین شده) →
قدم ۴ → قدم ۵ → همان رشته‌ی JSONی که برای دانلود ساخته می‌شود. اسکریپت‌های
تولیدشده با Node و یک شبیه‌سازِ کوچکِ ``pm`` واقعاً اجرا می‌شوند (اگر Node نصب
نباشد، فقط همان تست‌ها skip می‌شوند). هیچ تستی به سرویسِ واقعی وصل نمی‌شود.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.agents.test_case_generator.api_discovery import discover_from_spec
from src.agents.test_case_generator.api_mapping import (
    ApiMapping,
    MappedApi,
    build_result,
    compact_step2_result,
)
from src.agents.test_case_generator.final_review import FinalReviewGenerator
from src.agents.test_case_generator.postman_generation import (
    PostmanGenerationError,
    Step4PostmanGenerator,
    check_dependency_wiring,
)
from src.agents.test_case_generator.scenario_analysis import (
    ScenarioAnalysisAgent,
    ScenarioAnalysisError,
    validate_scenario_payload,
)
from src.agents.test_case_generator.task_analysis import (
    TaskAnalysisError,
    validate_analysis,
)
from src.agents.test_case_generator.variable_dependencies import (
    VariableInstructionError,
    normalize_response_path,
    requested_variables,
)
from ui.formatting import collection_json, postman_collection

_LLM = "src.agents.test_case_generator.scenario_analysis.LLMClient"

_INSTRUCTION = (
    "voucherId ایجاد شده باید به عنوان گلوبال وریبل در قسمت اسکریپت های پستمن "
    "وارد شود."
)


# ── fixtureها ────────────────────────────────────────────────────────────────

def _untyped_ok() -> dict:
    """پاسخِ بدونِ ساختار — دقیقاً شکلِ پاسخ‌های snapshotِ واقعیِ Admin."""
    return {
        "200": {
            "description": "OK",
            "content": {"*/*": {"schema": {"type": "object"}}},
        }
    }


def _id_parameter() -> dict:
    return {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}


def _voucher_swagger() -> dict:
    return {
        "openapi": "3.0.1",
        "info": {"title": "Admin", "version": "1.0"},
        "servers": [{"url": "http://podium-back.devpod.ir/api"}],
        "paths": {
            "/admin/voucher/amount": {
                "post": {
                    "operationId": "defineAmountVoucher",
                    "tags": ["voucher-admin-controller"],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "amount": {"type": "integer"},
                                        "count": {"type": "integer"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": _untyped_ok(),
                }
            },
            "/admin/voucher/{id}": {
                "get": {
                    "operationId": "getVoucherDetails",
                    "tags": ["voucher-admin-controller"],
                    "parameters": [_id_parameter()],
                    "responses": _untyped_ok(),
                },
                "put": {
                    "operationId": "updateVoucher",
                    "tags": ["voucher-admin-controller"],
                    "parameters": [_id_parameter()],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                }
                            }
                        }
                    },
                    "responses": _untyped_ok(),
                },
            },
        },
    }


_CREATE = MappedApi(
    method="POST", path="/admin/voucher/amount", operation_id="defineAmountVoucher"
)
_GET = MappedApi(method="GET", path="/admin/voucher/{id}", operation_id="getVoucherDetails")
_PUT = MappedApi(method="PUT", path="/admin/voucher/{id}", operation_id="updateVoucher")

_CASES = [
    ("TC-001", "Create an amount voucher", "positive", _CREATE),
    ("TC-002", "Create a voucher without a name is rejected", "negative", _CREATE),
    ("TC-003", "Retrieve the created voucher", "positive", _GET),
    ("TC-004", "Retrieve a voucher that does not exist", "negative", _GET),
    ("TC-005", "Update the created voucher", "positive", _PUT),
]


def _requested(**overrides) -> dict:
    variable = {
        "name": "voucherId",
        "scope": "global",
        "source_test_case_id": "TC-001",
        "response_path": "result.body.id",
        "required": True,
        "consumers": [
            {"test_case_id": "TC-003", "location": "path", "parameter": ""},
            {"test_case_id": "TC-005", "location": "", "parameter": ""},
        ],
        "instruction": _INSTRUCTION,
    }
    variable.update(overrides)
    return variable


def _step1(variables: list[dict] | None = None, cases=_CASES) -> dict:
    """نتیجه‌ی قدم اول — از همان اعتبارسنجِ قدم اول عبور می‌کند."""
    raw = {
        "task_summary": "Voucher scenario for the admin APIs",
        "identified_requirements": ["Create a voucher and read it back by its id."],
        "test_cases": [
            {
                "id": case_id,
                "title": title,
                "type": case_type,
                "priority": "high",
                "preconditions": [],
                "steps": [title],
                "expected_result": f"{title} behaves as specified.",
                "related_service": {"method": api["method"], "path": api["path"]},
            }
            for case_id, title, case_type, api in cases
        ],
        "clarifications": [],
    }
    if variables is not None:
        raw["variables"] = variables
    return validate_analysis(raw)


def _step2(swagger: dict | None = None, cases=_CASES) -> dict:
    """نتیجه‌ی واقعیِ قدم دوم — کشفِ Swagger و build_result."""
    service = discover_from_spec(
        swagger or _voucher_swagger(), source_url="http://swagger.local/Admin"
    )
    mappings = [
        ApiMapping(
            test_case_id=case_id,
            api=api,
            confidence="high",
            reason="Mapped by the test fixture.",
            clarification="",
        )
        for case_id, _, _, api in cases
    ]
    return build_result([service], mappings, [])


def _llm_dependency(**overrides) -> dict:
    """پیشنهادِ «معقول ولی ناقصِ» یک LLM: نامِ دیگر، مسیرِ حدسی و یک مقصدِ منفی."""
    dependency = {
        "variable_name": "createdVoucherId",
        "source": {"test_case_id": "TC-001", "location": "response.body", "path": "$.id"},
        "targets": [
            {"test_case_id": "TC-003", "location": "path", "parameter": "id"},
            {"test_case_id": "TC-005", "location": "path", "parameter": "id"},
            {"test_case_id": "TC-004", "location": "path", "parameter": "id"},
        ],
        "confidence": "medium",
        "reason": "The created voucher is read back and updated.",
    }
    dependency.update(overrides)
    return dependency


def _llm_payload(dependencies: list[dict] | None = None, order: list[str] | None = None) -> dict:
    ids = [case_id for case_id, *_ in _CASES]
    order = order or ids
    return {
        "scenarios": [
            {
                "id": "SC-001",
                "title": "Voucher lifecycle",
                "test_case_ids": ["TC-001", "TC-003", "TC-005"],
                "reason": "Create, read and update the same voucher.",
            },
            {
                "id": "SC-002",
                "title": "Voucher validation",
                "test_case_ids": ["TC-002", "TC-004"],
                "reason": "Invalid input and missing resources.",
            },
        ],
        "execution_order": [
            {"test_case_id": case_id, "order": position, "depends_on": []}
            for position, case_id in enumerate(order, start=1)
        ],
        "data_dependencies": [_llm_dependency()] if dependencies is None else dependencies,
        "clarifications": [],
    }


def _step3(step1: dict, step2: dict, payload: dict | None = None) -> dict:
    """عاملِ واقعیِ قدم سوم؛ فقط پاسخِ LLM ثابت است."""
    with patch(_LLM) as client:
        llm = MagicMock()
        llm.chat.return_value = json.dumps(payload or _llm_payload())
        client.return_value = llm
        scenarios, order, dependencies, clarifications = ScenarioAnalysisAgent().analyze(
            step1, compact_step2_result(step2), user_id="test"
        )
    return {
        "scenarios": scenarios,
        "execution_order": order,
        "data_dependencies": dependencies,
        "clarifications": clarifications,
    }


def _pipeline(variables=None, payload=None) -> dict:
    step1 = _step1([_requested()] if variables is None else variables)
    step2 = _step2()
    step3 = _step3(step1, step2, payload)
    step4 = Step4PostmanGenerator().generate(step1, step2, step3)
    review = FinalReviewGenerator().generate(step1, step2, step3, step4)
    # دقیقاً همان رشته‌ای که قدم پنجم برای دانلود می‌دهد
    serialized = json.loads(collection_json(postman_collection(review)))
    return {
        "step1": step1, "step2": step2, "step3": step3, "step4": step4,
        "review": review, "collection": serialized,
    }


def _items(collection: dict) -> list[dict]:
    return [item for folder in collection["item"] for item in folder["item"]]


def _item(collection: dict, case_id: str) -> dict:
    for item in _items(collection):
        if item["name"].startswith(f"{case_id} "):
            return item
    raise AssertionError(f"{case_id} has no request")


def _script(item: dict, listen: str) -> list[str]:
    for event in item.get("event") or []:
        if event["listen"] == listen:
            return event["script"]["exec"]
    return []


def _order(collection: dict) -> list[str]:
    return [item["name"].split(" ")[0] for item in _items(collection)]


@pytest.fixture(scope="module")
def voucher() -> dict:
    return _pipeline()


# ── اجرای واقعیِ اسکریپت‌ها با Node ──────────────────────────────────────────

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(_NODE is None, reason="Node.js is not installed")

# شبیه‌سازِ حداقلیِ pm: فقط APIهایی که اسکریپت‌های تولیدشده واقعاً صدا می‌زنند.
_HARNESS = r"""
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const results = [];
const store = (initial) => {
  const data = Object.assign({}, initial || {});
  return {
    data,
    get: (key) => data[key],
    set: (key, value) => { data[key] = value; },
    unset: (key) => { delete data[key]; },
  };
};
const globals = store(input.globals);
const collectionVariables = store(input.collectionVariables);
let skipped = false;

function expect(value, message) {
  const fail = (text) => { throw new Error((message ? message + ": " : "") + text); };
  const to = {
    be: {
      within(low, high) {
        if (!(value >= low && value <= high)) fail(`expected ${value} within ${low}..${high}`);
      },
      get true() { if (value !== true) fail(`expected ${value} to be true`); return true; },
    },
    not: {
      be: {
        oneOf(list) {
          if (list.some((item) => item === value)) fail(`expected ${JSON.stringify(value)} not to be empty`);
        },
      },
    },
    eql(expected) { if (JSON.stringify(value) !== JSON.stringify(expected)) fail("not equal"); },
  };
  Object.defineProperty(to, "exist", {
    get() { if (value === undefined || value === null) fail("expected to exist"); return true; },
  });
  return { to };
}

const pm = {
  test(name, fn) {
    try { fn(); results.push({ name, passed: true }); }
    catch (error) { results.push({ name, passed: false, error: String(error.message) }); }
  },
  expect,
  response: {
    code: input.code,
    json() {
      if (input.body === null) throw new Error("Response is not JSON");
      return JSON.parse(JSON.stringify(input.body));
    },
    headers: { get: (name) => (input.headers || {})[name] },
    to: { have: { status(code) { if (input.code !== code) throw new Error(`status ${input.code}`); } } },
  },
  globals,
  collectionVariables,
  execution: input.noSkip ? undefined : { skipRequest() { skipped = true; } },
};

let thrown = null;
try { new Function("pm", input.script)(pm); }
catch (error) { thrown = String(error.message); }
process.stdout.write(JSON.stringify({
  results, skipped, thrown, globals: globals.data, collectionVariables: collectionVariables.data,
}));
"""


def _run(lines: list[str], *, code=200, body=None, headers=None, globals_=None, no_skip=False) -> dict:
    payload = {
        "script": "\n".join(lines),
        "code": code,
        "body": body,
        "headers": headers or {},
        "globals": globals_ or {},
        "collectionVariables": {},
        "noSkip": no_skip,
    }
    completed = subprocess.run(
        [_NODE, "-e", _HARNESS],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)


# ── قدم ۱: تشخیص و اعتبارسنجیِ دستورِ متغیر ─────────────────────────────────

class TestStep1Contract:
    def test_requested_variables_survive_step1_validation(self):
        variables = _step1([_requested()])["variables"]
        assert variables == [
            {
                "name": "voucherId",
                "scope": "global",
                "source_test_case_id": "TC-001",
                "response_path": "$.result.body.id",
                "required": True,
                "consumers": [
                    {"test_case_id": "TC-003", "location": "path", "parameter": ""},
                    {"test_case_id": "TC-005", "location": "", "parameter": ""},
                ],
                "instruction": _INSTRUCTION,
            }
        ]

    def test_a_task_without_variables_gets_an_empty_list(self):
        assert _step1()["variables"] == []

    def test_the_scope_defaults_to_global(self):
        variables = _step1([_requested(scope="")])["variables"]
        assert variables[0]["scope"] == "global"

    @pytest.mark.parametrize(
        ("raw", "normalized"),
        [
            ("result.body.id", "$.result.body.id"),
            ("$.result.body.id", "$.result.body.id"),
            ("$result.body.id", "$.result.body.id"),
            ("data.items[0].code", "$.data.items[0].code"),
            ("", ""),
        ],
    )
    def test_response_paths_are_normalized(self, raw, normalized):
        assert normalize_response_path(raw) == normalized

    @pytest.mark.parametrize(
        ("override", "message"),
        [
            ({"name": "voucher-id"}, "cannot be used as a Postman variable name"),
            ({"scope": "environment"}, "'scope' must be one of"),
            ({"source_test_case_id": "TC-404"}, "unknown source test case"),
            ({"source_test_case_id": "TC-002"}, "is a negative test case"),
            ({"response_path": "result body id"}, "is not a simple JSON path"),
            ({"consumers": [{"test_case_id": "TC-001"}]}, "cannot also consume it"),
            ({"consumers": [{"test_case_id": "TC-003", "location": "cookie"}]}, "'location'"),
        ],
    )
    def test_an_invalid_instruction_is_rejected(self, override, message):
        with pytest.raises(TaskAnalysisError, match=message):
            _step1([_requested(**override)])

    def test_duplicate_variable_names_are_rejected(self):
        with pytest.raises(TaskAnalysisError, match="duplicate variable name"):
            _step1([_requested(), _requested()])

    def test_later_steps_reject_a_hand_edited_invalid_instruction(self):
        step1 = _step1([_requested()])
        step1["variables"][0]["name"] = "voucher id"
        with pytest.raises(VariableInstructionError):
            requested_variables(step1)
        with pytest.raises(ScenarioAnalysisError, match="variables"):
            _step3(step1, _step2())


# ── قدم ۳: تبدیلِ قطعی به data_dependency ───────────────────────────────────

class TestStep3Resolution:
    def test_the_user_intent_wins_over_the_llm_proposal(self, voucher):
        dependencies = voucher["step3"]["data_dependencies"]
        assert dependencies == [
            {
                "variable_name": "voucherId",
                "source": {
                    "test_case_id": "TC-001",
                    "location": "response.body",
                    "path": "$.result.body.id",
                },
                "targets": [
                    {"test_case_id": "TC-003", "location": "path", "parameter": "id"},
                    {"test_case_id": "TC-005", "location": "path", "parameter": "id"},
                ],
                "confidence": "high",
                "reason": f"Requested in the task: {_INSTRUCTION}",
                "scope": "global",
                "required": True,
                "origin": "task",
            }
        ]

    def test_an_unverifiable_path_is_kept_and_reported(self, voucher):
        notes = [
            item
            for item in voucher["step3"]["clarifications"]
            if item["type"] == "response_path"
        ]
        assert len(notes) == 1
        assert notes[0]["test_case_id"] == "TC-001"
        assert "'$.result.body.id' (specified in the task)" in notes[0]["message"]
        assert "documents no fields" in notes[0]["message"]

    def test_a_documented_mismatch_is_reported_not_silently_replaced(self):
        swagger = _voucher_swagger()
        swagger["paths"]["/admin/voucher/amount"]["post"]["responses"] = {
            "200": {
                "description": "OK",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"id": {"type": "integer"}},
                        }
                    }
                },
            }
        }
        step1 = _step1([_requested()])
        step3 = _step3(step1, _step2(swagger))
        dependency = step3["data_dependencies"][0]
        assert dependency["source"]["path"] == "$.result.body.id"
        assert any(
            "does not start with a documented response field" in item["message"]
            for item in step3["clarifications"]
        )

    def test_the_contract_fills_a_gap_only_when_it_is_unambiguous(self):
        """بدونِ پیشنهادِ LLM: GET /{id} تنها یک ورودی دارد و پر می‌شود؛ PUT /{id}
        علاوه بر مسیر بدنه هم دارد، پس حدس زده نمی‌شود و ابهام ثبت می‌شود."""
        step1 = _step1(
            [
                _requested(
                    consumers=[
                        {"test_case_id": "TC-003", "location": "", "parameter": ""},
                        {"test_case_id": "TC-005", "location": "", "parameter": ""},
                    ]
                )
            ]
        )
        step3 = _step3(step1, _step2(), _llm_payload(dependencies=[]))
        targets = step3["data_dependencies"][0]["targets"]
        assert targets == [{"test_case_id": "TC-003", "location": "path", "parameter": "id"}]
        assert any(
            "cannot determine where 'TC-005'" in item["message"]
            for item in step3["clarifications"]
        )

    def test_the_llm_proposal_fills_what_the_user_left_empty(self, voucher):
        """TC-005 بدونِ محل/پارامتر آمده بود؛ پیشنهادِ LLM (path/id) آن را پر کرد."""
        targets = voucher["step3"]["data_dependencies"][0]["targets"]
        assert {"test_case_id": "TC-005", "location": "path", "parameter": "id"} in targets

    def test_an_undeterminable_consumer_becomes_a_clarification(self):
        step1 = _step1(
            [
                _requested(
                    consumers=[{"test_case_id": "TC-005", "location": "body", "parameter": ""}]
                )
            ]
        )
        step3 = _step3(step1, _step2(), _llm_payload(dependencies=[]))
        assert step3["data_dependencies"][0]["targets"] == []
        assert any(
            "cannot determine where 'TC-005'" in item["message"]
            for item in step3["clarifications"]
        )

    def test_an_unknown_consumer_parameter_is_not_wired(self):
        step1 = _step1(
            [
                _requested(
                    consumers=[
                        {"test_case_id": "TC-003", "location": "path", "parameter": "voucher"}
                    ]
                )
            ]
        )
        step3 = _step3(step1, _step2(), _llm_payload(dependencies=[]))
        assert step3["data_dependencies"][0]["targets"] == []
        assert any("'voucher' is not a path parameter" in item["message"]
                   for item in step3["clarifications"])

    def test_an_undocumented_location_is_kept_but_reported(self):
        step1 = _step1(
            [
                _requested(
                    consumers=[
                        {"test_case_id": "TC-003", "location": "query", "parameter": "ref"}
                    ]
                )
            ]
        )
        step3 = _step3(step1, _step2(), _llm_payload(dependencies=[]))
        assert step3["data_dependencies"][0]["targets"] == [
            {"test_case_id": "TC-003", "location": "query", "parameter": "ref"}
        ]
        assert any("documents no query parameters" in item["message"]
                   for item in step3["clarifications"])

    def test_without_a_path_the_llm_path_is_used(self):
        step1 = _step1([_requested(response_path="")])
        step3 = _step3(step1, _step2())
        assert step3["data_dependencies"][0]["source"]["path"] == "$.id"
        assert step3["data_dependencies"][0]["variable_name"] == "voucherId"

    def test_without_any_path_nothing_is_guessed(self):
        step1 = _step1([_requested(response_path="")])
        step3 = _step3(step1, _step2(), _llm_payload(dependencies=[]))
        assert step3["data_dependencies"] == []
        assert any("identifies where the value is" in item["message"]
                   for item in step3["clarifications"])

    def test_an_unrelated_inferred_dependency_is_kept(self):
        """وابستگیِ استنتاجیِ دیگر دست‌نخورده می‌ماند — با اسکوپِ قبلی (collection)."""
        other = _llm_dependency(
            variable_name="updatedName",
            source={"test_case_id": "TC-005", "location": "response.body", "path": "$.name"},
            targets=[{"test_case_id": "TC-004", "location": "query", "parameter": "name"}],
        )
        swagger = _voucher_swagger()
        swagger["paths"]["/admin/voucher/{id}"]["get"]["parameters"].append(
            {"name": "name", "in": "query", "schema": {"type": "string"}}
        )
        step1 = _step1([_requested()])
        step3 = _step3(step1, _step2(swagger), _llm_payload([_llm_dependency(), other]))
        names = {d["variable_name"]: d for d in step3["data_dependencies"]}
        assert set(names) == {"voucherId", "updatedName"}
        assert names["updatedName"]["origin"] == "inferred"
        assert names["updatedName"]["scope"] == "collection"

    def test_validation_without_requested_variables_is_unchanged(self):
        """بدونِ متغیرِ درخواستی، خروجیِ اعتبارسنج همان خروجیِ قبلی است."""
        step1, step2 = _step1(), _step2()
        payload = _llm_payload([_llm_dependency(targets=[
            {"test_case_id": "TC-003", "location": "path", "parameter": "id"}
        ])])
        result = validate_scenario_payload(
            copy.deepcopy(payload),
            test_cases=step1["test_cases"],
            step2_result=compact_step2_result(step2),
        )
        with_empty = validate_scenario_payload(
            copy.deepcopy(payload),
            test_cases=step1["test_cases"],
            step2_result=compact_step2_result(step2),
            requested=[],
        )
        assert result == with_empty
        assert result[2][0]["origin"] == "inferred"


# ── A: استخراجِ متغیر ────────────────────────────────────────────────────────

class TestA_Extraction:
    def test_the_producer_script_extracts_the_requested_path(self, voucher):
        script = _script(_item(voucher["collection"], "TC-001"), "test")
        assert script[-11:] == [
            'pm.globals.unset("voucherId");',
            'pm.test("Response provides result.body.id and saves it as global '
            "variable 'voucherId'\", function () {",
            '    pm.expect(pm.response.code, "HTTP status of the response that provides '
            'voucherId").to.be.within(200, 299);',
            "    const response = pm.response.json();",
            '    pm.expect(response.result, "response.result").to.exist;',
            '    pm.expect(response.result.body, "response.result.body").to.exist;',
            "    const value = response.result.body.id;",
            '    pm.expect(value, "response.result.body.id").to.not.be.oneOf([undefined, null, ""]);',
            '    pm.globals.set("voucherId", value);',
            "});",
            "",
        ]

    @needs_node
    def test_running_the_script_saves_the_value_under_the_requested_name(self, voucher):
        script = _script(_item(voucher["collection"], "TC-001"), "test")
        outcome = _run(script, body={"result": {"body": {"id": 4711, "name": "Summer"}}})
        assert outcome["globals"] == {"voucherId": 4711}
        assert all(result["passed"] for result in outcome["results"])


# ── B: اعتبارسنجیِ فیلدِ الزامی ──────────────────────────────────────────────

class TestB_RequiredValue:
    @needs_node
    @pytest.mark.parametrize(
        ("code", "body"),
        [
            (200, {}),
            (200, {"result": None}),
            (200, {"result": {}}),
            (200, {"result": {"body": {}}}),
            (200, {"result": {"body": {"id": None}}}),
            (200, {"result": {"body": {"id": ""}}}),
            (200, None),
            (400, {"result": {"body": {"id": 4711}}}),
        ],
    )
    def test_a_missing_value_fails_and_saves_nothing(self, voucher, code, body):
        script = _script(_item(voucher["collection"], "TC-001"), "test")
        outcome = _run(script, code=code, body=body, globals_={"voucherId": "stale-from-last-run"})

        extraction = [r for r in outcome["results"] if "saves it as global" in r["name"]]
        assert len(extraction) == 1 and extraction[0]["passed"] is False
        # نه مقدارِ نامعتبرِ جدید، نه مقدارِ کهنه‌ی اجرای قبلی
        assert "voucherId" not in outcome["globals"]

    def test_the_status_assertion_stays_separate_from_the_extraction(self, voucher):
        """assertionِ وضعیتِ موجود تکرار یا حذف نمی‌شود."""
        script = "\n".join(_script(_item(voucher["collection"], "TC-001"), "test"))
        assert script.count("pm.globals.set(") == 1
        assert script.count("pm.globals.unset(") == 1


# ── C: جایگذاری در مصرف‌کننده ────────────────────────────────────────────────

class TestC_ConsumerSubstitution:
    def test_the_positive_retrieval_uses_the_variable_in_its_path(self, voucher):
        url = _item(voucher["collection"], "TC-003")["request"]["url"]
        assert url["raw"] == "{{baseUrl}}/admin/voucher/{{voucherId}}"
        assert url["path"] == ["admin", "voucher", "{{voucherId}}"]
        assert "variable" not in url
        assert ":id" not in url["raw"]

    def test_unrelated_request_data_is_preserved(self, voucher):
        request = _item(voucher["collection"], "TC-005")["request"]
        assert request["url"]["raw"] == "{{baseUrl}}/admin/voucher/{{voucherId}}"
        assert json.loads(request["body"]["raw"]) == {"name": "string"}
        headers = {h["key"]: h["value"] for h in request["header"]}
        assert headers["Authorization"] == "{{token}}"
        assert headers["Content-Type"] == "application/json"


# ── D: نام‌ها و مسیرهای دلخواه ───────────────────────────────────────────────

def _generic_swagger(location: str, parameter: str) -> dict:
    """یک API کاملاً متفاوت: ساختِ «thing» و مصرفِ مقدار در محلِ دلخواه."""
    consumer: dict = {"operationId": "useThing", "responses": _untyped_ok()}
    if location == "body":
        root = parameter.split(".", 1)[0]
        consumer["requestBody"] = {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            root: {"type": "object", "properties": {"ref": {"type": "string"}}},
                            "note": {"type": "string"},
                        },
                    }
                }
            }
        }
    else:
        consumer["parameters"] = [
            {"name": parameter, "in": location, "required": True, "schema": {"type": "string"}}
        ]
    consumer_path = f"/things/{{{parameter}}}/use" if location == "path" else "/things/use"
    return {
        "openapi": "3.0.1",
        "info": {"title": "Things", "version": "1.0"},
        "servers": [{"url": "https://things.example.com"}],
        "paths": {
            "/things": {"post": {"operationId": "createThing", "responses": _untyped_ok()}},
            consumer_path: {"post": consumer},
        },
    }


class TestD_GenericNamesAndPaths:
    @pytest.mark.parametrize(
        ("name", "response_path", "location", "parameter", "accessor"),
        [
            ("userId", "id", "path", "userId", "response.id"),
            ("orderNumber", "data.orderNumber", "query", "orderNumber", "response.data.orderNumber"),
            ("sessionToken", "auth.tokens[0].value", "header", "X-Session", "response.auth.tokens[0].value"),
            ("parentRef", "payload.ref", "body", "parent.ref", "response.payload.ref"),
        ],
    )
    def test_any_name_path_and_location(self, name, response_path, location, parameter, accessor):
        cases = [
            ("TC-001", "Create a thing", "positive",
             MappedApi(method="POST", path="/things", operation_id="createThing")),
            ("TC-002", "Use the created thing", "positive",
             MappedApi(
                 method="POST",
                 path=f"/things/{{{parameter}}}/use" if location == "path" else "/things/use",
                 operation_id="useThing",
             )),
        ]
        variable = {
            "name": name,
            "scope": "global",
            "source_test_case_id": "TC-001",
            "response_path": response_path,
            "consumers": [
                {"test_case_id": "TC-002", "location": location, "parameter": parameter}
            ],
        }
        step1 = _step1([variable], cases=cases)
        step2 = _step2(_generic_swagger(location, parameter), cases=cases)
        payload = {
            "scenarios": [
                {"id": "SC-001", "title": "Things", "test_case_ids": ["TC-001", "TC-002"],
                 "reason": "One flow."}
            ],
            "execution_order": [
                {"test_case_id": "TC-001", "order": 1, "depends_on": []},
                {"test_case_id": "TC-002", "order": 2, "depends_on": ["TC-001"]},
            ],
            "data_dependencies": [],
            "clarifications": [],
        }
        step3 = _step3(step1, step2, payload)
        collection = Step4PostmanGenerator().generate(step1, step2, step3)["collection"]

        producer = "\n".join(_script(_item(collection, "TC-001"), "test"))
        assert f"const value = {accessor};" in producer
        assert f'pm.globals.set("{name}", value);' in producer

        request = _item(collection, "TC-002")["request"]
        reference = "{{" + name + "}}"
        if location == "path":
            assert request["url"]["raw"] == f"{{{{baseUrl}}}}/things/{reference}/use"
        elif location == "query":
            assert request["url"]["query"] == [{"key": parameter, "value": reference}]
        elif location == "header":
            assert {h["key"]: h["value"] for h in request["header"]}[parameter] == reference
        else:
            body = json.loads(request["body"]["raw"])
            assert body["parent"]["ref"] == reference
            # فیلدهای هم‌سطحِ دیگر دست نمی‌خورند
            assert body["note"] == "string"

    @pytest.mark.parametrize(
        "module",
        ["variable_dependencies.py", "scenario_analysis.py", "postman_builder.py",
         "postman_generation.py", "final_review.py"],
    )
    def test_no_endpoint_or_variable_name_is_hardcoded(self, module):
        """کدِ قابلیت هیچ نام یا مسیرِ خاصی از مثالِ voucher ندارد.

        docstringها و توضیح‌ها مثال می‌زنند؛ این‌جا فقط کدِ واقعی (شناسه‌ها و
        رشته‌های غیرِ docstring) بررسی می‌شود.
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[3] / "src" / "agents" / "test_case_generator"
        tree = ast.parse((root / module).read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        code_text = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    code_text.append(node.value)
            elif isinstance(node, ast.Name):
                code_text.append(node.id)
            elif isinstance(node, ast.Attribute):
                code_text.append(node.attr)
        joined = "\n".join(code_text).lower()
        for word in ("voucher", "result.body", "result_body"):
            assert word not in joined


# ── E: چند مصرف‌کننده ────────────────────────────────────────────────────────

class TestE_MultipleConsumers:
    def test_every_consumer_references_and_guards_the_same_variable(self, voucher):
        for case_id in ("TC-003", "TC-005"):
            item = _item(voucher["collection"], case_id)
            assert "{{voucherId}}" in item["request"]["url"]["raw"]
            guard = "\n".join(_script(item, "prerequest"))
            assert 'pm.globals.get("voucherId")' in guard

    def test_the_value_is_extracted_only_once(self, voucher):
        setters = [
            item["name"]
            for item in _items(voucher["collection"])
            if 'pm.globals.set("voucherId"' in "\n".join(_script(item, "test"))
        ]
        assert setters == ["TC-001 — Create an amount voucher"]


# ── F: وابستگیِ اجرا ─────────────────────────────────────────────────────────

class TestF_ExecutionDependency:
    def test_the_producer_runs_before_its_consumers(self, voucher):
        order = _order(voucher["collection"])
        assert order.index("TC-001") < order.index("TC-003") < order.index("TC-005")

    def test_a_contradicting_step3_order_is_repaired(self):
        payload = _llm_payload(order=["TC-005", "TC-003", "TC-002", "TC-004", "TC-001"])
        result = _pipeline(payload=payload)
        order = _order(result["collection"])
        assert order.index("TC-001") < order.index("TC-003")
        assert order.index("TC-001") < order.index("TC-005")
        assert any("was moved after 'TC-001'" in w for w in result["step4"]["warnings"])

    def test_a_cross_folder_cycle_is_reported_not_hidden(self):
        """وقتی بازچینی ممکن نیست، ترتیب دست نمی‌خورد و هشدار می‌ماند."""
        step1 = _step1([_requested()])
        step2 = _step2()
        step3 = _step3(step1, step2)
        # TC-004 (پوشه‌ی دوم) چیزی تولید می‌کند که TC-001 (پوشه‌ی اول) مصرف می‌کند
        step3["data_dependencies"].append(
            {
                "variable_name": "otherName",
                "source": {"test_case_id": "TC-002", "location": "response.body", "path": "$.name"},
                "targets": [{"test_case_id": "TC-001", "location": "body", "parameter": "name"}],
                "confidence": "low",
                "reason": "Fixture for a folder cycle.",
            }
        )
        step3["data_dependencies"][0]["targets"].append(
            {"test_case_id": "TC-004", "location": "path", "parameter": "id"}
        )
        result = Step4PostmanGenerator().generate(step1, step2, step3)
        assert any("is not set when the request runs" in w for w in result["warnings"])

    def test_a_consumer_guard_is_generated(self, voucher):
        guard = _script(_item(voucher["collection"], "TC-003"), "prerequest")
        text = "\n".join(guard)
        assert "pm.execution.skipRequest();" in text
        assert "'TC-001' must run successfully before this request." in text

    @needs_node
    def test_without_the_value_the_consumer_fails_and_is_not_sent(self, voucher):
        guard = _script(_item(voucher["collection"], "TC-003"), "prerequest")
        outcome = _run(guard, globals_={})
        assert outcome["skipped"] is True
        assert [r["passed"] for r in outcome["results"]] == [False]

    @needs_node
    def test_older_runtimes_without_skip_request_stop_with_an_error(self, voucher):
        guard = _script(_item(voucher["collection"], "TC-003"), "prerequest")
        outcome = _run(guard, globals_={}, no_skip=True)
        assert "voucherId' is not set" in outcome["thrown"]

    @needs_node
    def test_with_the_value_the_consumer_runs(self, voucher):
        guard = _script(_item(voucher["collection"], "TC-003"), "prerequest")
        outcome = _run(guard, globals_={"voucherId": 4711})
        assert outcome["skipped"] is False and outcome["thrown"] is None
        assert [r["passed"] for r in outcome["results"]] == [True]

    @needs_node
    def test_producer_then_consumer_end_to_end(self, voucher):
        produced = _run(
            _script(_item(voucher["collection"], "TC-001"), "test"),
            body={"result": {"body": {"id": 99}}},
        )
        consumed = _run(
            _script(_item(voucher["collection"], "TC-003"), "prerequest"),
            globals_=produced["globals"],
        )
        assert consumed["skipped"] is False
        failed = _run(
            _script(_item(voucher["collection"], "TC-001"), "test"),
            body={"result": {"body": {}}},
            globals_={"voucherId": 98},
        )
        blocked = _run(
            _script(_item(voucher["collection"], "TC-003"), "prerequest"),
            globals_=failed["globals"],
        )
        assert blocked["skipped"] is True


# ── G: سناریوهای منفی ────────────────────────────────────────────────────────

class TestG_NegativeScenarios:
    def test_a_negative_create_does_not_touch_the_variable(self, voucher):
        script = "\n".join(_script(_item(voucher["collection"], "TC-002"), "test"))
        assert "voucherId" not in script

    def test_a_non_existent_voucher_case_uses_its_own_input(self, voucher):
        """پیشنهادِ اشتباهِ LLM (TC-004 به‌عنوانِ مقصد) کنار گذاشته شده است."""
        item = _item(voucher["collection"], "TC-004")
        assert "{{voucherId}}" not in json.dumps(item)
        assert item["request"]["url"]["raw"] == "{{baseUrl}}/admin/voucher/:id"
        assert _script(item, "prerequest") == []

    def test_a_negative_source_is_rejected_in_step1(self):
        with pytest.raises(TaskAnalysisError, match="negative test case"):
            _step1([_requested(source_test_case_id="TC-004")])


# ── H: JSONِ سریال‌شده‌ی نهایی ───────────────────────────────────────────────

class TestH_SerializedCollection:
    def test_the_exported_json_is_wired_end_to_end(self, voucher):
        collection = voucher["collection"]
        names = {
            row["test_case_id"]: row["name"] for row in voucher["step4"]["requests"]
        }
        assert check_dependency_wiring(
            collection, voucher["step3"]["data_dependencies"], names
        ) == []

        producer = _item(collection, "TC-001")
        assert [event["listen"] for event in producer["event"]] == ["test"]
        text = "\n".join(_script(producer, "test"))
        assert 'pm.globals.set("voucherId", value);' in text
        assert "to.not.be.oneOf([undefined, null, \"\"])" in text

        assert _item(collection, "TC-003")["request"]["url"]["raw"].endswith(
            "/admin/voucher/{{voucherId}}"
        )

    def test_the_variable_is_global_and_not_shadowed(self, voucher):
        collection = voucher["collection"]
        dumped = json.dumps(collection)
        assert "pm.collectionVariables" not in dumped
        assert "voucherId" not in {variable["key"] for variable in collection["variable"]}

    def test_step5_reports_no_issue(self, voucher):
        review = voucher["review"]["review"]
        assert review["issues"] == []
        assert review["status"] != "blocked"

    def test_a_stale_step4_collection_is_blocked_in_step5(self, voucher):
        """کالکشنی که قبل از این قاعده ساخته شده (یک‌خطی، بدونِ guard) رد می‌شود."""
        step4 = copy.deepcopy(voucher["step4"])
        producer = _item(step4["collection"], "TC-001")
        producer["event"] = [
            {
                "listen": "test",
                "script": {
                    "type": "text/javascript",
                    "exec": ['pm.collectionVariables.set("voucherId", jsonData.result.body.id);'],
                },
            }
        ]
        review = FinalReviewGenerator().generate(
            voucher["step1"], voucher["step2"], voucher["step3"], step4
        )
        codes = {issue["code"] for issue in review["review"]["issues"]}
        assert "broken_dependency" in codes
        assert review["review"]["status"] == "blocked"

    def test_a_missing_requested_variable_is_flagged(self, voucher):
        """Step 3ِ قدیمی (بدونِ وابستگی) با تسکی که متغیر خواسته ناسازگار است."""
        step3 = copy.deepcopy(voucher["step3"])
        step3["data_dependencies"] = []
        step4 = Step4PostmanGenerator().generate(voucher["step1"], voucher["step2"], step3)
        assert any("has no data dependency for it" in w for w in step4["warnings"])
        review = FinalReviewGenerator().generate(
            voucher["step1"], voucher["step2"], step3, step4
        )
        codes = {issue["code"] for issue in review["review"]["issues"]}
        assert "missing_requested_variable" in codes

    def test_step4_refuses_a_global_variable_shadowed_by_a_collection_variable(self, voucher):
        step2 = copy.deepcopy(voucher["step2"])
        step3 = copy.deepcopy(voucher["step3"])
        builder = MagicMock()
        real = Step4PostmanGenerator()._builder

        def build(**kwargs):
            kwargs["extra_variables"] = [{"key": "voucherId", "value": ""}]
            return real.build(**kwargs)

        builder.build.side_effect = build
        with pytest.raises(PostmanGenerationError, match="shadow"):
            Step4PostmanGenerator(postman_builder=builder).generate(
                voucher["step1"], step2, step3
            )


# ── I: سازگاری با گذشته ──────────────────────────────────────────────────────

class TestI_BackwardCompatibility:
    def test_a_task_without_variables_generates_no_variable_scripts(self):
        result = _pipeline(variables=[], payload=_llm_payload(dependencies=[]))
        dumped = json.dumps(result["collection"])
        assert "pm.globals" not in dumped
        assert "prerequest" not in dumped
        assert result["step3"]["data_dependencies"] == []
        assert result["review"]["review"]["issues"] == []
        # پارامترِ مسیرِ بدونِ مقدار همان رفتارِ قبلی را دارد
        url = _item(result["collection"], "TC-003")["request"]["url"]
        assert url["raw"] == "{{baseUrl}}/admin/voucher/:id"
        assert url["variable"] == [{"key": "id", "value": ""}]

    def test_an_old_step1_result_without_the_field_still_works(self):
        step1 = _step1()
        step1.pop("variables")
        assert requested_variables(step1) == []
        step2 = _step2()
        step3 = _step3(step1, step2, _llm_payload(dependencies=[]))
        assert Step4PostmanGenerator().generate(step1, step2, step3)["warnings"] == []

    def test_an_old_step3_dependency_keeps_the_collection_scope(self):
        """نتیجه‌ی ذخیره‌شده‌ی قدیمی بدونِ scope/required/origin."""
        step1 = _step1()
        step2 = _step2()
        step3 = _step3(step1, step2, _llm_payload(dependencies=[]))
        step3["data_dependencies"] = [
            {
                "variable_name": "createdVoucherId",
                "source": {"test_case_id": "TC-001", "location": "response.body", "path": "$.id"},
                "targets": [{"test_case_id": "TC-003", "location": "path", "parameter": "id"}],
                "confidence": "high",
                "reason": "Stored before scopes existed.",
            }
        ]
        collection = Step4PostmanGenerator().generate(step1, step2, step3)["collection"]
        producer = "\n".join(_script(_item(collection, "TC-001"), "test"))
        assert 'pm.collectionVariables.set("createdVoucherId", value);' in producer
        assert "pm.globals" not in producer
        assert _item(collection, "TC-003")["request"]["url"]["raw"].endswith(
            "/admin/voucher/{{createdVoucherId}}"
        )
