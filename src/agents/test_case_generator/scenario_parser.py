"""
test_case_generator/scenario_parser.py — خواندن و اعتبارسنجی فایل سناریو (YAML)

مسئولیت این ماژول فقط یک قدم از زنجیره است:

    مسیرِ فایل YAML  →  خواندن  →  parse  →  اعتبارسنجی  →  Scenario

سناریو «جریانِ کسب‌وکاریِ درخواست‌شده» را توصیف می‌کند: چه قدم‌هایی، به چه ترتیبی،
با چه انتظاری، و چه متغیرهایی از پاسخ استخراج شوند.

این ماژول عمداً کارهای زیر را انجام نمی‌دهد:
  - دریافت یا تحلیل سند Swagger      (وظیفه‌ی swagger_analyzer.py)
  - تولید تست‌کیس                     (وظیفه‌ی testcase_generator.py)
  - ساخت Postman Collection          (وظیفه‌ی postman_builder.py)
  - ذخیره‌ی فایل یا اجرای درخواست API

سناریو هیچ‌وقت «منبعِ حقیقتِ فنی» نیست؛ جزئیاتِ فنیِ API همیشه از Swagger می‌آید.
سناریو فقط می‌گوید چه جریانی خواسته شده است.

اگر فایل پیدا نشود، YAML خراب باشد، یا ساختار سناریو نامعتبر باشد، یکی از
خطاهای ScenarioError پرتاب می‌شود — هرگز بی‌سروصدا به حالتِ Swagger برنمی‌گردیم.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# ── ثابت‌ها ──────────────────────────────────────────────────────────────────

# متدهای HTTP معتبر برای operation یک step
VALID_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

# دامنه‌ی متغیرهای Postman که extract می‌تواند بسازد
VALID_SCOPES = ("global", "collection")

# تنها منبعِ پشتیبانی‌شده برای extract در این نسخه
VALID_EXTRACT_SOURCES = ("response",)

# انواعِ شناخته‌شده‌ی سناریوی منفی — تایپو باید خطا بدهد، نه تستِ اشتباه
KNOWN_NEGATIVE_TYPES = (
    "unauthorized",
    "forbidden",
    "missing_required_field",
    "invalid_request",
    "invalid_body",
    "invalid_parameter",
    "not_found",
    "duplicate",
    "validation_error",
)

# محل‌هایی که یک step می‌تواند برایشان مقدار تعیین کند
VALID_PARAM_LOCATIONS = ("path", "query", "header", "body")

# محدوده‌ی معتبرِ یک کد وضعیت HTTP
_MIN_STATUS, _MAX_STATUS = 100, 599

# مسیرِ فایل سناریو داخلِ پیامِ کاربر (هر توکنی که به .yaml/.yml ختم شود)
_SCENARIO_PATH_RE = re.compile(r"[^\s'\"`<>|]+\.ya?ml\b", re.IGNORECASE)

# ریشه‌هایی که یک مسیر نسبی نسبت به آن‌ها هم امتحان می‌شود
_AGENT_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = _AGENT_DIR / "scenarios"
_PROJECT_ROOT = _AGENT_DIR.parents[2]  # src/agents/test_case_generator → ریشه‌ی پروژه


# ── خطاها ────────────────────────────────────────────────────────────────────

class ScenarioError(RuntimeError):
    """خطای پایه‌ی همه‌ی مشکلاتِ مربوط به سناریو."""


class ScenarioNotFoundError(ScenarioError):
    """فایل سناریوی خواسته‌شده پیدا نشد."""


class ScenarioParseError(ScenarioError):
    """فایل سناریو YAML معتبری نبود."""


class ScenarioValidationError(ScenarioError):
    """ساختارِ سناریو با قرارداد این ماژول نمی‌خواند."""


# ── مدل داده ────────────────────────────────────────────────────────────────

@dataclass
class ExtractSpec:
    """یک مقدار از پاسخِ موفق که باید در متغیر Postman ذخیره شود.

    نمونه‌ی YAML:
        extract:
          - from: response
            json_path: $.entity.id
            variable: voucherId
            scope: global
    """
    variable: str
    json_path: str
    scope: str = "global"          # global | collection
    source: str = "response"       # کلیدِ `from` در YAML


@dataclass
class PositiveCase:
    """سناریوی مثبتِ یک step."""
    expected_status: int | None = None
    extract: list[ExtractSpec] = field(default_factory=list)


@dataclass
class NegativeCase:
    """یک سناریوی منفیِ صریحاً تعریف‌شده روی یک step."""
    name: str
    type: str
    expected_status: int | None = None
    description: str = ""
    params: "StepParams" = field(default_factory=lambda: StepParams())


@dataclass
class StepParams:
    """مقادیری که سناریو برای یک درخواست تعیین می‌کند.

    مقادیر می‌توانند متغیر Postman باشند (مثلاً "{{voucherId}}") و باید دست‌نخورده
    تا خروجیِ نهایی منتقل شوند.
    """
    path: dict = field(default_factory=dict)
    query: dict = field(default_factory=dict)
    header: dict = field(default_factory=dict)
    body: dict = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.path or self.query or self.header or self.body)


@dataclass
class OperationRef:
    """اشاره به یک عملیاتِ API — مرجعِ نهایی همچنان خودِ Swagger است."""
    method: str
    path: str
    operation_id: str = ""


@dataclass
class ScenarioStep:
    """یک قدم از جریان — ترتیبِ قدم‌ها همان ترتیبِ اجرا در کالکشن است."""
    name: str
    swagger: str
    operation: OperationRef
    params: StepParams = field(default_factory=StepParams)
    positive: PositiveCase | None = None
    negative: list[NegativeCase] = field(default_factory=list)
    description: str = ""


@dataclass
class SwaggerSource:
    """یک سرویس/منبعِ مشخصاتِ API — نه یک API منفرد."""
    name: str
    url: str


@dataclass
class Scenario:
    """نتیجه‌ی کاملِ تحلیل یک فایل سناریو."""
    name: str
    description: str
    swagger_sources: list[SwaggerSource]
    steps: list[ScenarioStep]
    source_path: Path | None = None

    @property
    def source_names(self) -> list[str]:
        return [s.name for s in self.swagger_sources]

    def source_by_name(self, name: str) -> SwaggerSource | None:
        return next((s for s in self.swagger_sources if s.name == name), None)

    def extracted_variables(self) -> list[ExtractSpec]:
        """همه‌ی متغیرهایی که این سناریو در طول جریان می‌سازد (به ترتیبِ قدم‌ها)."""
        result: list[ExtractSpec] = []
        for step in self.steps:
            if step.positive:
                result.extend(step.positive.extract)
        return result


# ── تشخیصِ ورودی ─────────────────────────────────────────────────────────────

def extract_scenario_path(text: str) -> str:
    """اولین مسیرِ فایل YAML را از پیام کاربر بیرون می‌کشد.

    آدرس‌های http(s) عمداً نادیده گرفته می‌شوند؛ سناریو یک فایلِ محلی است و این
    ایجنت سناریو را از شبکه نمی‌خواند (فقط سندِ Swagger را می‌خواند).

    اگر چیزی پیدا نشود رشته‌ی خالی برمی‌گرداند تا فراخواننده به حالتِ Swagger برود.
    """
    for match in _SCENARIO_PATH_RE.finditer(text or ""):
        candidate = match.group(0).strip().strip("`\"'")
        candidate = candidate.rstrip(".,;:)")
        if candidate.lower().startswith(("http://", "https://")):
            continue
        if candidate:
            return candidate
    return ""


def resolve_scenario_path(raw_path: str) -> Path:
    """مسیرِ داده‌شده را به یک فایلِ موجود روی دیسک resolve می‌کند.

    مسیرهای نسبی نسبت به دایرکتوری جاری، ریشه‌ی پروژه، دایرکتوری خودِ ایجنت و
    دایرکتوری scenarios/ امتحان می‌شوند.

    پرتاب می‌کند:
        ScenarioNotFoundError : هیچ‌کدام از کاندیداها فایل نبودند
    """
    raw = (raw_path or "").strip()
    if not raw:
        raise ScenarioNotFoundError("No scenario path was provided.")

    given = Path(raw).expanduser()
    if given.is_absolute():
        candidates = [given]
    else:
        candidates = [
            Path.cwd() / given,
            _PROJECT_ROOT / given,
            _AGENT_DIR / given,
            SCENARIOS_DIR / given.name,
        ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    tried = "\n".join(f"  - {c}" for c in candidates)
    raise ScenarioNotFoundError(
        f"Scenario file '{raw}' was not found. Looked in:\n{tried}"
    )


# ── کمکی‌های اعتبارسنجی ──────────────────────────────────────────────────────

def _as_dict(value: object) -> dict | None:
    return value if isinstance(value, dict) else None


def _as_text(value: object) -> str:
    """یک مقدارِ اسکالر را به رشته‌ی تمیز تبدیل می‌کند (None → "")."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _validate_status(
    value: object, where: str, errors: list[str]
) -> int | None:
    """یک کد وضعیت HTTP اختیاری را اعتبارسنجی می‌کند."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{where}: expected an integer HTTP status, got {value!r}.")
        return None
    if not (_MIN_STATUS <= value <= _MAX_STATUS):
        errors.append(
            f"{where}: HTTP status {value} is out of range "
            f"({_MIN_STATUS}-{_MAX_STATUS})."
        )
        return None
    return value


def _parse_params(raw: object, where: str, errors: list[str]) -> StepParams:
    """بلوکِ params یک step/negative را به StepParams تبدیل می‌کند."""
    params = StepParams()
    if raw is None:
        return params

    data = _as_dict(raw)
    if data is None:
        errors.append(f"{where}: 'params' must be a mapping.")
        return params

    for location, values in data.items():
        loc = _as_text(location)
        if loc not in VALID_PARAM_LOCATIONS:
            errors.append(
                f"{where}.params: unknown location '{loc}'. "
                f"Valid locations: {', '.join(VALID_PARAM_LOCATIONS)}."
            )
            continue
        bucket = _as_dict(values)
        if bucket is None:
            errors.append(f"{where}.params.{loc}: must be a mapping of name → value.")
            continue
        setattr(params, loc, dict(bucket))

    return params


def _parse_extract(raw: object, where: str, errors: list[str]) -> list[ExtractSpec]:
    """بلوکِ extract یک سناریوی مثبت را به لیست ExtractSpec تبدیل می‌کند."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"{where}: 'extract' must be a list.")
        return []

    result: list[ExtractSpec] = []
    for index, item in enumerate(raw):
        item_where = f"{where}.extract[{index}]"
        data = _as_dict(item)
        if data is None:
            errors.append(f"{item_where}: must be a mapping.")
            continue

        variable = _as_text(data.get("variable"))
        if not variable:
            errors.append(f"{item_where}: 'variable' is required.")

        json_path = _as_text(data.get("json_path"))
        if not json_path:
            errors.append(f"{item_where}: 'json_path' is required.")

        scope = _as_text(data.get("scope")) or "global"
        if scope not in VALID_SCOPES:
            errors.append(
                f"{item_where}: 'scope' must be one of "
                f"{', '.join(VALID_SCOPES)}, got '{scope}'."
            )

        source = _as_text(data.get("from")) or "response"
        if source not in VALID_EXTRACT_SOURCES:
            errors.append(
                f"{item_where}: 'from' must be one of "
                f"{', '.join(VALID_EXTRACT_SOURCES)}, got '{source}'."
            )

        if variable and json_path:
            result.append(
                ExtractSpec(
                    variable=variable,
                    json_path=json_path,
                    scope=scope if scope in VALID_SCOPES else "global",
                    source=source if source in VALID_EXTRACT_SOURCES else "response",
                )
            )

    return result


def _parse_positive(raw: object, where: str, errors: list[str]) -> PositiveCase | None:
    """بلوکِ positive یک step را تبدیل می‌کند (اختیاری است)."""
    if raw is None:
        return None
    data = _as_dict(raw)
    if data is None:
        errors.append(f"{where}: 'positive' must be a mapping.")
        return None

    return PositiveCase(
        expected_status=_validate_status(
            data.get("expected_status"), f"{where}.positive.expected_status", errors
        ),
        extract=_parse_extract(data.get("extract"), f"{where}.positive", errors),
    )


def _parse_negatives(raw: object, where: str, errors: list[str]) -> list[NegativeCase]:
    """بلوکِ negative یک step را تبدیل می‌کند (اختیاری است)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"{where}: 'negative' must be a list.")
        return []

    result: list[NegativeCase] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        item_where = f"{where}.negative[{index}]"
        data = _as_dict(item)
        if data is None:
            errors.append(f"{item_where}: must be a mapping.")
            continue

        name = _as_text(data.get("name"))
        if not name:
            errors.append(f"{item_where}: 'name' is required.")
        elif name in seen:
            errors.append(f"{item_where}: duplicate negative case name '{name}'.")
        else:
            seen.add(name)

        case_type = _as_text(data.get("type"))
        if not case_type:
            errors.append(f"{item_where}: 'type' is required.")
        elif case_type not in KNOWN_NEGATIVE_TYPES:
            errors.append(
                f"{item_where}: unknown negative type '{case_type}'. "
                f"Known types: {', '.join(KNOWN_NEGATIVE_TYPES)}."
            )

        expected_status = _validate_status(
            data.get("expected_status"), f"{item_where}.expected_status", errors
        )

        if name and case_type:
            result.append(
                NegativeCase(
                    name=name,
                    type=case_type,
                    expected_status=expected_status,
                    description=_as_text(data.get("description")),
                    params=_parse_params(data.get("params"), item_where, errors),
                )
            )

    return result


def _parse_operation(raw: object, where: str, errors: list[str]) -> OperationRef | None:
    """بلوکِ operation یک step را تبدیل می‌کند."""
    data = _as_dict(raw)
    if data is None:
        errors.append(f"{where}: 'operation' is required and must be a mapping.")
        return None

    method = _as_text(data.get("method")).upper()
    if not method:
        errors.append(f"{where}.operation: 'method' is required.")
    elif method not in VALID_METHODS:
        errors.append(
            f"{where}.operation: '{method}' is not a valid HTTP method. "
            f"Valid methods: {', '.join(VALID_METHODS)}."
        )

    path = _as_text(data.get("path"))
    if not path:
        errors.append(f"{where}.operation: 'path' is required.")

    if not method or not path:
        return None

    return OperationRef(
        method=method,
        path=path,
        operation_id=_as_text(data.get("operation_id") or data.get("operationId")),
    )


def _parse_swagger_sources(raw: object, errors: list[str]) -> list[SwaggerSource]:
    """بلوکِ swagger_sources را تبدیل و اعتبارسنجی می‌کند."""
    if raw is None:
        errors.append("'swagger_sources' is required.")
        return []
    if not isinstance(raw, list) or not raw:
        errors.append("'swagger_sources' must be a non-empty list.")
        return []

    result: list[SwaggerSource] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"swagger_sources[{index}]"
        data = _as_dict(item)
        if data is None:
            errors.append(f"{where}: must be a mapping with 'name' and 'url'.")
            continue

        name = _as_text(data.get("name"))
        if not name:
            errors.append(f"{where}: 'name' is required.")
        elif name in seen:
            errors.append(f"{where}: duplicate swagger source name '{name}'.")
        else:
            seen.add(name)

        url = _as_text(data.get("url"))
        if not url:
            errors.append(f"{where}: 'url' is required.")

        if name and url:
            result.append(SwaggerSource(name=name, url=url))

    return result


def _parse_steps(
    raw: object, source_names: list[str], errors: list[str]
) -> list[ScenarioStep]:
    """بلوکِ steps را تبدیل و اعتبارسنجی می‌کند — ترتیب حفظ می‌شود."""
    if raw is None:
        errors.append("'steps' is required.")
        return []
    if not isinstance(raw, list) or not raw:
        errors.append("'steps' must be a non-empty list.")
        return []

    result: list[ScenarioStep] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"steps[{index}]"
        data = _as_dict(item)
        if data is None:
            errors.append(f"{where}: must be a mapping.")
            continue

        name = _as_text(data.get("name"))
        if not name:
            errors.append(f"{where}: 'name' is required.")
        else:
            where = f"{where} ('{name}')"
            if name in seen:
                errors.append(f"{where}: duplicate step name '{name}'.")
            else:
                seen.add(name)

        # ارجاع به منبعِ Swagger — اگر فقط یک منبع وجود داشته باشد اختیاری است
        swagger = _as_text(data.get("swagger"))
        if not swagger:
            if len(source_names) == 1:
                swagger = source_names[0]
            else:
                errors.append(
                    f"{where}: 'swagger' is required when the scenario declares "
                    f"more than one swagger source. "
                    f"Available sources: {', '.join(source_names) or '(none)'}."
                )
        elif source_names and swagger not in source_names:
            errors.append(
                f"{where}.swagger references unknown swagger source '{swagger}'. "
                f"Available sources: {', '.join(source_names)}."
            )

        operation = _parse_operation(data.get("operation"), where, errors)
        params = _parse_params(data.get("params"), where, errors)
        positive = _parse_positive(data.get("positive"), where, errors)
        negatives = _parse_negatives(data.get("negative"), where, errors)

        if name and swagger and operation is not None:
            result.append(
                ScenarioStep(
                    name=name,
                    swagger=swagger,
                    operation=operation,
                    params=params,
                    positive=positive,
                    negative=negatives,
                    description=_as_text(data.get("description")),
                )
            )

    return result


# ── تحلیل‌گر ─────────────────────────────────────────────────────────────────

class ScenarioParser:
    """فایل سناریو را می‌خواند، parse و اعتبارسنجی می‌کند.

    استفاده:
        parser = ScenarioParser()
        scenario = parser.parse_file("src/agents/test_case_generator/scenarios/x.yaml")
    """

    name = "scenario_parser"

    # ---- API عمومی -----------------------------------------------------------

    def parse_file(self, path: str | Path) -> Scenario:
        """یک فایل YAML را می‌خواند و Scenario معتبر برمی‌گرداند.

        پرتاب می‌کند:
            ScenarioNotFoundError   : فایل پیدا نشد
            ScenarioParseError      : YAML خراب یا غیرقابل خواندن بود
            ScenarioValidationError : ساختارِ سناریو نامعتبر بود
        """
        resolved = resolve_scenario_path(str(path))
        try:
            text = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            raise ScenarioParseError(
                f"Scenario file '{resolved}' could not be read: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        return self.parse_text(text, source_path=resolved)

    def parse_text(self, text: str, source_path: Path | None = None) -> Scenario:
        """متنِ YAML را parse و اعتبارسنجی می‌کند."""
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            where = f" in '{source_path}'" if source_path else ""
            raise ScenarioParseError(f"Invalid YAML{where}: {exc}") from exc

        if data is None:
            where = f" '{source_path}'" if source_path else ""
            raise ScenarioParseError(f"Scenario file{where} is empty.")

        return self.parse_dict(data, source_path=source_path)

    def parse_dict(self, data: object, source_path: Path | None = None) -> Scenario:
        """یک dictِ از پیش خوانده‌شده را اعتبارسنجی و به Scenario تبدیل می‌کند.

        همه‌ی تخلف‌ها جمع می‌شوند و یک‌جا گزارش داده می‌شوند تا کاربر مجبور نشود
        خطاها را یکی‌یکی رفع کند.
        """
        root = _as_dict(data)
        if root is None:
            raise ScenarioValidationError(
                "Invalid Scenario:\n"
                f"the document root must be a mapping, got {type(data).__name__}."
            )

        errors: list[str] = []

        meta = _as_dict(root.get("scenario"))
        if meta is None:
            errors.append("'scenario' section is required and must be a mapping.")
            meta = {}

        name = _as_text(meta.get("name"))
        if not name:
            errors.append("'scenario.name' is required.")

        sources = _parse_swagger_sources(root.get("swagger_sources"), errors)
        steps = _parse_steps(root.get("steps"), [s.name for s in sources], errors)

        if errors:
            where = f" ({source_path})" if source_path else ""
            raise ScenarioValidationError(
                f"Invalid Scenario{where}:\n" + "\n".join(errors)
            )

        return Scenario(
            name=name,
            description=_as_text(meta.get("description")),
            swagger_sources=sources,
            steps=steps,
            source_path=source_path,
        )
