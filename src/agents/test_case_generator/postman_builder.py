"""
test_case_generator/postman_builder.py — ساخت Postman Collection از تست‌کیس‌ها

مسئولیت این ماژول فقط یک قدمِ آخرِ زنجیره است:

    TestCaseSuite  →  Postman Collection (dict مطابق Schema v2.1)

این ماژول عمداً کارهای زیر را انجام نمی‌دهد:
  - فراخوانی LLM
  - دریافت/تحلیل سند Swagger    (وظیفه‌ی swagger_analyzer.py)
  - تولید سناریوی جدید تست       (وظیفه‌ی testcase_generator.py)
  - اجرای درخواست‌های API یا ذخیره‌ی فایل

این تبدیل کاملاً قطعی (deterministic) است: هیچ حدسی درباره‌ی رفتار API زده
نمی‌شود و فقط داده‌ی ساختاریافته‌ی موجود به قالب Postman نگاشت می‌شود.

ذخیره‌ی فایل جزو مسئولیت این ماژول نیست؛ خروجی یک dict است تا فراخواننده
(مثلاً agent.py) تصمیم بگیرد آن را چطور serialize/ذخیره کند.
"""

from __future__ import annotations

import json
import re

from src.agents.test_case_generator.testcase_generator import (
    DEFAULT_BASE_URL_VAR,
    Assertion,
    ControllerTestCases,
    SaveVariable,
    TestCase,
    TestCaseSuite,
)
from src.debug import DebugConfig

# آدرس رسمی Schema نسخه‌ی ۲.۱ کالکشن Postman
_SCHEMA_V21 = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"

# هدرِ Content-Type که برای بدنه‌ی JSON اضافه می‌شود
_CONTENT_TYPE = "Content-Type"
_CONTENT_TYPE_JSON = "application/json"

# نامِ متغیرهای سطحِ کالکشن
_BASE_URL_VAR = DEFAULT_BASE_URL_VAR
_TOKEN_VAR = "token"

# اسکوپ‌های ذخیره‌سازیِ متغیر → تابعِ متناظر در Postman
_SCOPE_SETTERS = {
    "global": "pm.globals.set",
    "collection": "pm.collectionVariables.set",
}
_DEFAULT_SCOPE = "collection"

# پارامترِ مسیر به شکلِ {name} در path
_PATH_VAR_RE = re.compile(r"\{([^{}]+)\}")

# قطعه‌ی معتبرِ یک JSON path ساده (فقط شناسه‌های ساده پشتیبانی می‌شوند)
_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostmanBuildError(RuntimeError):
    """ورودیِ ساخت کالکشن ناقص یا نامعتبر بوده است.

    مانند TestCaseGenerationError، خروجیِ خراب هرگز بی‌سروصدا تولید نمی‌شود.
    """


class PostmanBuilder:
    """TestCaseSuite را به یک dictِ Postman Collection v2.1 تبدیل می‌کند.

    استفاده:
        builder = PostmanBuilder()
        collection = builder.build(suite, base_url="https://api.example.com",
                                   collection_name="Generated API Tests")
    """

    name = "postman_builder"

    def __init__(self, debug_config: DebugConfig | None = None) -> None:
        self._log = (debug_config or DebugConfig.off()).get_logger(self.name)

    # ── API عمومی ────────────────────────────────────────────────────────────

    def build(
        self,
        suite: TestCaseSuite,
        base_url: str,
        collection_name: str = "Generated API Tests",
        extra_variables: list[dict] | None = None,
    ) -> dict:
        """suite را به یک Postman Collection (dict) تبدیل می‌کند.

        پارامترها:
            suite            : خروجیِ TestCaseGenerator
            base_url         : base URLِ مشتق‌شده از Swagger (مقدارِ متغیر baseUrl)
            collection_name  : نامِ کالکشن
            extra_variables  : متغیرهای اضافیِ سطحِ کالکشن به شکلِ
                               [{"key": ..., "value": ...}] — برای سناریوهایی که
                               چند منبعِ Swagger (چند سرویس) دارند و هر کدام
                               base URLِ خودشان را می‌خواهند. متغیرهایی که کلیدشان
                               از قبل تعریف شده باشد نادیده گرفته می‌شوند.

        پرتاب می‌کند:
            PostmanBuildError : suite بدونِ controller، base_url خالی، یا یک
                                تست‌کیس فاقدِ method/path باشد.
        """
        controllers = suite.get("controllers") if isinstance(suite, dict) else None
        if not isinstance(controllers, list) or not controllers:
            raise PostmanBuildError(
                "suite has no controllers — nothing to build a Postman collection from."
            )

        base_url = (base_url or "").strip()
        if not base_url:
            raise PostmanBuildError(
                "base_url is empty — the collection needs a baseUrl value."
            )

        self._log.info(
            "شروع ساخت کالکشن",
            collection_name=collection_name,
            controllers=len(controllers),
        )

        items = [self._build_folder(controller) for controller in controllers]

        variables = [
            {"key": _BASE_URL_VAR, "value": base_url.rstrip("/")},
            {"key": _TOKEN_VAR, "value": ""},
        ]
        variables.extend(self._extra_variables(extra_variables, variables))

        collection = {
            "info": {
                "name": collection_name,
                "schema": _SCHEMA_V21,
            },
            "variable": variables,
            "item": items,
        }

        total = sum(len(folder["item"]) for folder in items)
        self._log.info("ساخت کالکشن کامل شد", folders=len(items), requests=total)
        return collection

    @staticmethod
    def _extra_variables(
        extra_variables: list[dict] | None, existing: list[dict]
    ) -> list[dict]:
        """متغیرهای اضافی را تمیز و بدونِ تکرار برمی‌گرداند."""
        if not isinstance(extra_variables, list):
            return []

        seen = {str(v.get("key")) for v in existing}
        result: list[dict] = []
        for variable in extra_variables:
            if not isinstance(variable, dict):
                continue
            key = str(variable.get("key") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append({"key": key, "value": str(variable.get("value") or "")})
        return result

    # ── سطحِ folder / controller ───────────────────────────────────────────────

    def _build_folder(self, controller: ControllerTestCases) -> dict:
        """یک controller را به یک folderِ Postman تبدیل می‌کند."""
        if not isinstance(controller, dict):
            raise PostmanBuildError(
                f"expected a controller object, got {type(controller).__name__}."
            )

        name = controller.get("name") or "default"
        cases = controller.get("test_cases")
        if not isinstance(cases, list):
            raise PostmanBuildError(f"controller '{name}' has no test_cases array.")

        return {
            "name": name,
            "item": [self._build_request(case) for case in cases],
        }

    # ── سطحِ request / test case ────────────────────────────────────────────────

    def _build_request(self, case: TestCase) -> dict:
        """یک TestCase را به یک requestِ Postman تبدیل می‌کند."""
        if not isinstance(case, dict):
            raise PostmanBuildError(
                f"expected a test case object, got {type(case).__name__}."
            )

        name = case.get("name")
        method = case.get("method")
        path = case.get("path")
        if not name:
            raise PostmanBuildError("a test case is missing its 'name'.")
        if not isinstance(method, str) or not method.strip():
            raise PostmanBuildError(f"test case '{name}' is missing its HTTP 'method'.")
        if not isinstance(path, str) or not path.strip():
            raise PostmanBuildError(f"test case '{name}' is missing its 'path'.")

        body = case.get("body")
        has_body = isinstance(body, dict)

        request: dict = {
            "method": method.upper(),
            "header": self._build_headers(case.get("headers") or {}, has_body),
            "url": self._build_url(
                path,
                case.get("path_params") or {},
                case.get("query_params") or {},
                str(case.get("base_url_var") or _BASE_URL_VAR),
            ),
        }

        description = self._build_description(case)
        if description:
            request["description"] = description

        if has_body:
            request["body"] = self._build_body(body)

        item: dict = {"name": name, "request": request}

        events = self._build_events(
            case.get("assertions") or [], case.get("save_variables") or []
        )
        if events:
            item["event"] = events

        return item

    # ── هدرها ────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_headers(headers: dict, has_body: bool) -> list[dict]:
        """dictِ هدرها را به لیستِ Postman تبدیل می‌کند و از تکرار جلوگیری می‌کند.

        اگر بدنه‌ی JSON وجود داشته باشد و Content-Type از قبل نیامده باشد، آن را
        اضافه می‌کند. مقدارِ {{token}} دست‌نخورده باقی می‌ماند.
        """
        result: list[dict] = []
        seen: set[str] = set()
        for key, value in headers.items():
            lower = str(key).lower()
            if lower in seen:  # از هدرِ تکراری جلوگیری می‌شود
                continue
            seen.add(lower)
            result.append({"key": str(key), "value": str(value)})

        if has_body and _CONTENT_TYPE.lower() not in seen:
            result.append({"key": _CONTENT_TYPE, "value": _CONTENT_TYPE_JSON})

        return result

    # ── URL ────────────────────────────────────────────────────────────────────

    def _build_url(
        self,
        path: str,
        path_params: dict,
        query_params: dict,
        base_url_var: str = _BASE_URL_VAR,
    ) -> dict:
        """path را به آبجکتِ URLِ Postman (با {{baseUrl}}) تبدیل می‌کند.

        پارامترهای مسیر به شکلِ {name} به «:name» تبدیل می‌شوند و مقدارشان از
        path_params برداشته می‌شود. query_params به entryهای query نگاشت می‌شود؛
        هیچ پارامترِ مستندنشده‌ای اضافه نمی‌شود.

        base_url_var نامِ متغیرِ base URLِ این درخواست است؛ وقتی یک سناریو چند
        منبعِ Swagger دارد، هر قدم می‌تواند میزبانِ خودش را داشته باشد.
        """
        # {name} → :name
        colon_path = _PATH_VAR_RE.sub(lambda m: f":{m.group(1)}", path)

        segments = [seg for seg in colon_path.split("/") if seg != ""]

        query = [
            {"key": str(key), "value": self._stringify(value)}
            for key, value in query_params.items()
        ]

        # متغیرهای مسیر بر اساسِ نام‌های داخلِ path اصلی
        variables = []
        for match in _PATH_VAR_RE.finditer(path):
            var_name = match.group(1)
            variables.append(
                {"key": var_name, "value": self._stringify(path_params.get(var_name, ""))}
            )

        host_var = "{{" + (base_url_var or _BASE_URL_VAR) + "}}"
        raw = host_var
        if segments:
            raw += "/" + "/".join(segments)
        if query:
            raw += "?" + "&".join(f"{q['key']}={q['value']}" for q in query)

        url: dict = {
            "raw": raw,
            "host": [host_var],
            "path": segments,
        }
        if query:
            url["query"] = query
        if variables:
            url["variable"] = variables
        return url

    # ── بدنه ────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_body(body: dict) -> dict:
        """بدنه‌ی JSON را به قالبِ raw موردِ انتظارِ Postman تبدیل می‌کند."""
        return {
            "mode": "raw",
            "raw": json.dumps(body, ensure_ascii=False, indent=2),
            "options": {"raw": {"language": "json"}},
        }

    # ── توضیحات ──────────────────────────────────────────────────────────────

    @staticmethod
    def _build_description(case: TestCase) -> str:
        """description و assumptions را به متنِ توضیحاتِ request تبدیل می‌کند."""
        case_type = str(case.get("type", "")).lower()
        label = "Positive" if case_type == "positive" else (
            "Negative" if case_type == "negative" else "Test"
        )

        lines = [f"**{label} test case**"]

        description = str(case.get("description", "")).strip()
        if description:
            lines.append(description)

        assumptions = case.get("assumptions") or []
        if isinstance(assumptions, list) and assumptions:
            lines.append("Assumptions:")
            lines.extend(f"- {str(a)}" for a in assumptions)

        return "\n\n".join(lines)

    # ── اسکریپت‌های تست (assertions + save_variables) ───────────────────────────

    def _build_events(
        self,
        assertions: list[Assertion],
        save_variables: list[SaveVariable],
    ) -> list[dict]:
        """assertions و save_variables را به یک eventِ «test» تبدیل می‌کند."""
        exec_lines: list[str] = []

        # آیا به بدنه‌ی JSONِ پاسخ نیاز داریم؟ (برای field-assertion یا save)
        needs_json = any(
            self._json_path_to_accessor(sv.get("json_path", "")) is not None
            for sv in save_variables
            if isinstance(sv, dict)
        ) or any(
            isinstance(a, dict)
            and a.get("type") != "status_code"
            and self._json_path_to_accessor(a.get("json_path", "")) is not None
            for a in assertions
        )

        if needs_json:
            exec_lines.append("let jsonData;")
            exec_lines.append("try { jsonData = pm.response.json(); }")
            exec_lines.append("catch (e) { jsonData = {}; }")
            exec_lines.append("")

        for assertion in assertions:
            exec_lines.extend(self._assertion_lines(assertion))

        for save in save_variables:
            exec_lines.extend(self._save_lines(save))

        # اگر هیچ خطِ معناداری تولید نشد، event هم نمی‌سازیم
        if not any(line.strip() for line in exec_lines):
            return []

        return [
            {
                "listen": "test",
                "script": {"type": "text/javascript", "exec": exec_lines},
            }
        ]

    def _assertion_lines(self, assertion: Assertion) -> list[str]:
        """یک assertion را به خطوطِ اسکریپتِ Postman تبدیل می‌کند.

        فقط assertionهایی که در تست‌کیس وجود دارند تبدیل می‌شوند؛ هیچ assertionِ
        جدیدی اختراع نمی‌شود. اگر یک assertion قابلِ تبدیلِ امن نباشد، نادیده
        گرفته می‌شود.
        """
        if not isinstance(assertion, dict):
            return []

        a_type = assertion.get("type")

        if a_type == "status_code":
            expected = assertion.get("expected")
            if not isinstance(expected, int) or isinstance(expected, bool):
                return []
            return [
                f'pm.test("Status code is {expected}", function () {{',
                f"    pm.response.to.have.status({expected});",
                "});",
                "",
            ]

        # assertionِ مبتنی بر فیلدِ پاسخ (json_path)
        accessor = self._json_path_to_accessor(assertion.get("json_path", ""))
        if accessor is None:
            return []

        json_path = assertion.get("json_path", "")
        if "expected" in assertion:
            literal = json.dumps(assertion.get("expected"), ensure_ascii=False)
            title = self._js_string(f"{json_path} equals {assertion.get('expected')}")
            return [
                f"pm.test({title}, function () {{",
                f"    pm.expect({accessor}).to.eql({literal});",
                "});",
                "",
            ]

        title = self._js_string(f"Response has {json_path}")
        return [
            f"pm.test({title}, function () {{",
            f"    pm.expect({accessor}).to.not.be.undefined;",
            "});",
            "",
        ]

    def _save_lines(self, save: SaveVariable) -> list[str]:
        """یک save_variable را به یک خطِ ذخیره‌سازیِ متغیر تبدیل می‌کند.

        اسکوپ تعیین می‌کند کدام API استفاده شود:
            global     → pm.globals.set(...)
            collection → pm.collectionVariables.set(...)   (پیش‌فرض)
        """
        if not isinstance(save, dict):
            return []
        variable = save.get("variable")
        accessor = self._json_path_to_accessor(save.get("json_path", ""))
        if not variable or accessor is None:
            # مسیرِ نامعتبر → به‌جای تولیدِ JSِ خراب، از آن می‌گذریم
            return []
        scope = str(save.get("scope") or _DEFAULT_SCOPE).strip().lower()
        setter = _SCOPE_SETTERS.get(scope, _SCOPE_SETTERS[_DEFAULT_SCOPE])
        return [f"{setter}({self._js_string(str(variable))}, {accessor});"]

    # ── کمکی‌ها ──────────────────────────────────────────────────────────────

    @staticmethod
    def _json_path_to_accessor(json_path: object) -> str | None:
        """یک JSON path ساده را به دسترسیِ JS روی jsonData تبدیل می‌کند.

        نمونه‌ها:
            "$.id"        → "jsonData.id"
            "$.data.id"   → "jsonData.data.id"
            "$.result.id" → "jsonData.result.id"

        اگر مسیر خالی یا شاملِ قطعه‌ای غیرِ ساده باشد (مثلِ [] یا کاراکترهای
        خاص)، None برمی‌گرداند تا JSِ نامعتبر تولید نشود.
        """
        if not isinstance(json_path, str):
            return None
        path = json_path.strip()
        if not path:
            return None
        # پیشوندِ ریشه را حذف کن: "$." یا "$"
        if path.startswith("$"):
            path = path[1:]
        path = path.lstrip(".")
        if not path:
            return None

        segments = path.split(".")
        accessor = "jsonData"
        for segment in segments:
            if not _SEGMENT_RE.match(segment):
                return None
            accessor += f".{segment}"
        return accessor

    @staticmethod
    def _js_string(text: str) -> str:
        """یک رشته را به یک literalِ رشته‌ایِ امنِ JavaScript تبدیل می‌کند."""
        return json.dumps(text, ensure_ascii=False)

    @staticmethod
    def _stringify(value: object) -> str:
        """مقدارِ query/path را به رشته تبدیل می‌کند (bool به‌صورتِ true/false)."""
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)
