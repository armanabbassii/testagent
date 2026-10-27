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
    RequiredVariable,
    SaveVariable,
    TestCase,
    TestCaseSuite,
)
from src.agents.test_case_generator.variable_dependencies import (
    JSON_PATH_RE,
    normalize_response_path,
    path_tokens,
)
from src.debug import DebugConfig

# آدرس رسمی Schema نسخه‌ی ۲.۱ کالکشن Postman
SCHEMA_V21 = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"

# هدرِ Content-Type که برای بدنه‌ی JSON اضافه می‌شود
_CONTENT_TYPE = "Content-Type"
_CONTENT_TYPE_JSON = "application/json"

# نامِ متغیرهای سطحِ کالکشن
_BASE_URL_VAR = DEFAULT_BASE_URL_VAR
_TOKEN_VAR = "token"

# قاعده‌ی سراسریِ احراز هویت: هر درخواستِ ساخته‌شده دقیقاً همین یک هدر را دارد —
# مستقل از اینکه سندِ Swagger یا نتیجه‌ی قدم‌های قبلی چیزی درباره‌ی auth بگویند.
# توکنِ واقعی هرگز ساخته نمی‌شود؛ مقدار همان متغیرِ کالکشنِ {{token}} است.
AUTHORIZATION_HEADER = "Authorization"
AUTHORIZATION_VALUE = "{{" + _TOKEN_VAR + "}}"

# منبعِ مقدار در save_variable — بدنه‌ی JSON (پیش‌فرض) یا هدرِ پاسخ
_BODY_SOURCE = "body"
_HEADER_SOURCE = "header"

# برچسبِ نوعِ تست‌کیس در توضیحاتِ request. انواعِ ناشناخته مثلِ قبل «Test» می‌شوند،
# پس رفتارِ جریانِ موجود (positive/negative) دست‌نخورده می‌ماند.
_TYPE_LABELS = {
    "positive": "Positive",
    "negative": "Negative",
    "boundary": "Boundary",
    "state_transition": "State transition",
}

# اسکوپ‌های ذخیره‌سازیِ متغیر → تابعِ متناظر در Postman
_SCOPE_SETTERS = {
    "global": "pm.globals.set",
    "collection": "pm.collectionVariables.set",
}
_SCOPE_GETTERS = {
    "global": "pm.globals.get",
    "collection": "pm.collectionVariables.get",
}
_SCOPE_UNSETTERS = {
    "global": "pm.globals.unset",
    "collection": "pm.collectionVariables.unset",
}
_DEFAULT_SCOPE = "collection"

# پارامترِ مسیر به شکلِ {name} در path
_PATH_VAR_RE = re.compile(r"\{([^{}]+)\}")

# مقداری که خودش دقیقاً یک متغیرِ Postman است، مثلِ «{{voucherId}}»
_VARIABLE_REFERENCE_RE = re.compile(r"^\{\{[^{}\s]+\}\}$")

# قطعه‌ی معتبرِ یک JSON path ساده (فقط شناسه‌های ساده پشتیبانی می‌شوند)
_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def variable_setter(scope: str) -> str:
    """تابعِ Postmanی که متغیرِ این اسکوپ را ذخیره می‌کند (مثلاً pm.globals.set).

    عمومی است تا اعتبارسنجیِ قدم ۴ و ۵ همان نگاشتِ این ماژول را بخوانند و
    اسکریپتِ ساخته‌شده را با قاعده‌ای جداگانه نسنجند.
    """
    key = (scope or "").strip().lower()
    return _SCOPE_SETTERS.get(key, _SCOPE_SETTERS[_DEFAULT_SCOPE])


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
        description: str = "",
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
            description      : توضیحاتِ سطحِ کالکشن (info.description). خالی باشد،
                               اصلاً اضافه نمی‌شود تا خروجیِ قبلی یکسان بماند.

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

        info: dict = {
            "name": collection_name,
            "schema": SCHEMA_V21,
        }
        description = (description or "").strip()
        if description:
            info["description"] = description

        collection = {
            "info": info,
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
            case.get("assertions") or [],
            case.get("save_variables") or [],
            case.get("required_variables") or [],
        )
        if events:
            item["event"] = events

        return item

    # ── هدرها ────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_headers(headers: dict, has_body: bool) -> list[dict]:
        """dictِ هدرها را به لیستِ Postman تبدیل می‌کند و از تکرار جلوگیری می‌کند.

        هدرِ «Authorization: {{token}}» همیشه اولین هدر است، بی‌قید و شرط. هر
        Authorization ای که از قبل (با هر حروف و هر مقداری) در تست‌کیس آمده باشد
        با همین هدر جایگزین می‌شود، نه تکرار — این تنها نقطه‌ای است که هر دو
        جریان (ویزارد و agent.py) از آن می‌گذرند.

        اگر بدنه‌ی JSON وجود داشته باشد و Content-Type از قبل نیامده باشد، آن را
        اضافه می‌کند. بقیه‌ی هدرها دست‌نخورده می‌مانند.
        """
        result: list[dict] = [
            {"key": AUTHORIZATION_HEADER, "value": AUTHORIZATION_VALUE, "type": "text"}
        ]
        seen: set[str] = {AUTHORIZATION_HEADER.lower()}
        for key, value in headers.items():
            lower = str(key).strip().lower()
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

        استثنا: وقتی مقدارِ یک پارامترِ مسیر خودش دقیقاً یک متغیرِ Postman باشد
        (مثلاً «{{voucherId}}» که درخواستِ قبلی ذخیره کرده)، همان ارجاع مستقیم
        در مسیر می‌نشیند — «/voucher/{{voucherId}}» به‌جای «/voucher/:id». این‌طور
        مصرفِ متغیر در خودِ URL دیده می‌شود و به جدولِ path variables وابسته نیست.

        base_url_var نامِ متغیرِ base URLِ این درخواست است؛ وقتی یک سناریو چند
        منبعِ Swagger دارد، هر قدم می‌تواند میزبانِ خودش را داشته باشد.
        """
        inlined: set[str] = set()

        def segment_for(match: re.Match) -> str:
            name = match.group(1)
            value = path_params.get(name)
            if isinstance(value, str) and _VARIABLE_REFERENCE_RE.match(value.strip()):
                inlined.add(name)
                return value.strip()
            return f":{name}"  # {name} → :name

        colon_path = _PATH_VAR_RE.sub(segment_for, path)

        segments = [seg for seg in colon_path.split("/") if seg != ""]

        query = [
            {"key": str(key), "value": self._stringify(value)}
            for key, value in query_params.items()
        ]

        # متغیرهای مسیر بر اساسِ نام‌های داخلِ path اصلی
        variables = []
        for match in _PATH_VAR_RE.finditer(path):
            var_name = match.group(1)
            if var_name in inlined:
                continue
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
        label = _TYPE_LABELS.get(case_type, "Test")

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
        required_variables: list[RequiredVariable] | None = None,
    ) -> list[dict]:
        """assertions و save_variables را به eventِ «test» و required_variables را
        به eventِ «prerequest» تبدیل می‌کند.

        هر متغیر فقط یک‌بار ذخیره می‌شود — حتی اگر save_variables (مثلاً پس از
        پردازشِ دوباره‌ی یک تست‌کیس) تکراری داشته باشد.
        """
        events: list[dict] = []
        guard_lines = self._guard_lines(required_variables or [])
        if guard_lines:
            events.append(
                {
                    "listen": "prerequest",
                    "script": {"type": "text/javascript", "exec": guard_lines},
                }
            )

        unique_saves: list[SaveVariable] = []
        seen_saves: set[tuple[str, str]] = set()
        for save in save_variables:
            if not isinstance(save, dict) or not save.get("variable"):
                continue
            key = (str(save.get("variable")), self._scope(save))
            if key in seen_saves:
                continue
            seen_saves.add(key)
            unique_saves.append(save)
        save_variables = unique_saves

        exec_lines: list[str] = []

        # آیا به بدنه‌ی JSONِ پاسخ نیاز داریم؟ (برای field-assertion یا save)
        # فقط saveهای بدنه‌محورِ ساده به jsonData نیاز دارند؛ save از هدر و saveِ
        # الزامی (که پاسخ را داخلِ pm.testِ خودش می‌خواند) نه.
        needs_json = any(
            self._save_source(sv) == _BODY_SOURCE
            and not sv.get("required")
            and self._json_path_to_accessor(sv.get("json_path", "")) is not None
            for sv in save_variables
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
            if save.get("required"):
                exec_lines.extend(self._required_save_lines(save))
            else:
                exec_lines.extend(self._save_lines(save))

        # اگر هیچ خطِ معناداری تولید نشد، eventِ test هم نمی‌سازیم
        if any(line.strip() for line in exec_lines):
            events.append(
                {
                    "listen": "test",
                    "script": {"type": "text/javascript", "exec": exec_lines},
                }
            )
        return events

    def _required_save_lines(self, save: SaveVariable) -> list[str]:
        """استخراجِ محافظت‌شده‌ی یک متغیرِ الزامی.

        ترتیبِ اسکریپت عمدی است:

          1. مقدارِ قبلیِ متغیر پاک می‌شود — اجرای ناموفقِ این درخواست نباید
             مقدارِ کهنه‌ی یک اجرای قبلی را برای مصرف‌کننده‌ها باقی بگذارد؛
          2. داخلِ یک pm.test: پاسخِ موفق (2xx)، وجودِ هر قطعه‌ی مسیر و
             غیرِخالی‌بودنِ مقدار (نه undefined، نه null، نه "") بررسی می‌شود؛
          3. فقط اگر همه‌ی بررسی‌ها رد نشوند، مقدار ذخیره می‌شود — هر expectِ
             ناموفق تابع را متوقف می‌کند و set هرگز اجرا نمی‌شود.

        مسیر یا نامِ هدرِ نامعتبر هیچ JSِ خرابی تولید نمی‌کند.
        """
        variable = str(save.get("variable") or "")
        scope = self._scope(save)
        setter = _SCOPE_SETTERS[scope]
        unsetter = _SCOPE_UNSETTERS[scope]
        variable_literal = self._js_string(variable)
        status_label = self._js_string(f"HTTP status of the response that provides {variable}")

        if self._save_source(save) == _HEADER_SOURCE:
            header = self._header_name(save.get("json_path", ""))
            if not header:
                return []
            title = self._js_string(
                f"Response header {header} is present and saved as {scope} variable "
                f"'{variable}'"
            )
            return [
                f"{unsetter}({variable_literal});",
                f"pm.test({title}, function () {{",
                f"    pm.expect(pm.response.code, {status_label}).to.be.within(200, 299);",
                f"    const value = pm.response.headers.get({self._js_string(header)});",
                f"    pm.expect(value, {self._js_string('response header ' + header)})"
                ".to.not.be.oneOf([undefined, null, \"\"]);",
                f"    {setter}({variable_literal}, value);",
                "});",
                "",
            ]

        try:
            json_path = normalize_response_path(save.get("json_path", ""))
        except ValueError:
            return []
        if not json_path or not JSON_PATH_RE.match(json_path):
            return []
        tokens = path_tokens(json_path)
        readable = json_path[2:] if json_path.startswith("$.") else json_path[1:]

        title = self._js_string(
            f"Response provides {readable} and saves it as {scope} variable "
            f"'{variable}'"
        )
        lines = [
            f"{unsetter}({variable_literal});",
            f"pm.test({title}, function () {{",
            f"    pm.expect(pm.response.code, {status_label}).to.be.within(200, 299);",
            "    const response = pm.response.json();",
        ]
        accessor = "response"
        for token in tokens[:-1]:
            accessor += token
            label = self._js_string(accessor)
            lines.append(f"    pm.expect({accessor}, {label}).to.exist;")
        accessor += tokens[-1]
        lines.extend(
            [
                f"    const value = {accessor};",
                f"    pm.expect(value, {self._js_string(accessor)})"
                ".to.not.be.oneOf([undefined, null, \"\"]);",
                f"    {setter}({variable_literal}, value);",
                "});",
                "",
            ]
        )
        return lines

    def _guard_lines(self, required_variables: list[RequiredVariable]) -> list[str]:
        """pre-request guard برای درخواستی که به متغیرِ یک درخواستِ قبلی نیاز دارد.

        اگر تولیدکننده اجرا نشده یا شکست خورده باشد (مثلاً فقط همین پوشه در
        Runner اجرا شده، یا ترتیبِ اجرا تغییر کرده)، متغیر ست نیست. در آن حالت
        یک تستِ ناموفقِ گویا ثبت می‌شود و درخواست اصلاً فرستاده نمی‌شود — به‌جای
        اینکه با «/voucher/» یا مقدارِ کهنه یک پاسخِ گمراه‌کننده بگیرد.
        ``pm.execution.skipRequest`` در Postman/Newmanِ قدیمی وجود ندارد؛ آنجا
        خطای اسکریپت همان نقش را بازی می‌کند.
        """
        lines: list[str] = []
        seen: set[tuple[str, str]] = set()
        for required in required_variables:
            if not isinstance(required, dict):
                continue
            variable = str(required.get("variable") or "").strip()
            scope = self._scope(required)
            if not variable or (variable, scope) in seen:
                continue
            seen.add((variable, scope))

            producer = str(required.get("producer") or "").strip() or "a previous request"
            message = (
                f"{scope} variable '{variable}' is not set — '{producer}' must run "
                f"successfully before this request."
            )
            title = f"Required {scope} variable '{variable}' is available"
            lines.extend(
                [
                    "{",
                    f"    const value = {_SCOPE_GETTERS[scope]}({self._js_string(variable)});",
                    '    const available = !(value === undefined || value === null || value === "");',
                    f"    pm.test({self._js_string(title)}, function () {{",
                    f"        pm.expect(available, {self._js_string(message)}).to.be.true;",
                    "    });",
                    "    if (!available) {",
                    '        if (pm.execution && typeof pm.execution.skipRequest === "function") {',
                    "            pm.execution.skipRequest();",
                    "        } else {",
                    f"            throw new Error({self._js_string(message)});",
                    "        }",
                    "    }",
                    "}",
                ]
            )
        return lines

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

        منبع (source) تعیین می‌کند مقدار از کجا خوانده شود:
            body   → jsonData.<json_path>            (پیش‌فرض، رفتارِ قبلی)
            header → pm.response.headers.get(...)    (json_path نامِ هدر است)
        """
        if not isinstance(save, dict):
            return []
        variable = save.get("variable")
        if not variable:
            return []

        setter = _SCOPE_SETTERS[self._scope(save)]
        variable_literal = self._js_string(str(variable))

        if self._save_source(save) == _HEADER_SOURCE:
            header = self._header_name(save.get("json_path", ""))
            if not header:
                # نامِ هدرِ خالی → به‌جای تولیدِ JSِ خراب، از آن می‌گذریم
                return []
            return [
                f"{setter}({variable_literal}, "
                f"pm.response.headers.get({self._js_string(header)}));"
            ]

        accessor = self._json_path_to_accessor(save.get("json_path", ""))
        if accessor is None:
            # مسیرِ نامعتبر → به‌جای تولیدِ JSِ خراب، از آن می‌گذریم
            return []
        return [f"{setter}({variable_literal}, {accessor});"]

    # ── کمکی‌ها ──────────────────────────────────────────────────────────────

    @staticmethod
    def _scope(item: dict) -> str:
        """اسکوپِ یک save/required — مقدارِ ناشناخته همان پیش‌فرض (collection) است."""
        scope = str(item.get("scope") or _DEFAULT_SCOPE).strip().lower()
        return scope if scope in _SCOPE_SETTERS else _DEFAULT_SCOPE

    @staticmethod
    def _save_source(save: dict) -> str:
        """منبعِ مقدارِ یک save_variable — «body» (پیش‌فرض) یا «header»."""
        source = str(save.get("source") or _BODY_SOURCE).strip().lower()
        return _HEADER_SOURCE if source == _HEADER_SOURCE else _BODY_SOURCE

    @staticmethod
    def _header_name(value: object) -> str:
        """نامِ هدرِ پاسخ را از مقدارِ json_path بیرون می‌کشد.

        قراردادِ قدم سوم مسیر را با «$.» می‌نویسد، بنابراین «$.X-Trace-Id» به
        «X-Trace-Id» تبدیل می‌شود. مقدارِ خالی یعنی هدرِ نامشخص.
        """
        if not isinstance(value, str):
            return ""
        name = value.strip()
        if name.startswith("$"):
            name = name[1:]
        return name.lstrip(".").strip()

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
