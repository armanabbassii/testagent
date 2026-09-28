"""
swagger_analyzer.py — تحلیل مستندات Swagger/OpenAPI

این ماژول یک URL از مستندات Swagger (چه صفحه‌ی swagger-ui و چه فایل خام
api-docs) را می‌گیرد، spec خام JSON را resolve و دریافت می‌کند و آن را به یک
لیست ساختاریافته از Endpoint ها تبدیل می‌کند.

خروجی این ماژول ورودی postman_builder است.

نکته‌ی معماری:
    این ماژول از `requests` استفاده می‌کند (مثل GitLabClient) چون با یک سرویس
    HTTP معمولی صحبت می‌کند، نه با LLM/Embedding. قانونِ «فقط از LLMClient/
    EmbeddingClient استفاده کن» فقط مربوط به ارتباط با سرویس‌های AI است.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs

import requests

# متدهای HTTP معتبر که در یک path item ممکن است ظاهر شوند
_HTTP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options", "trace")

# محل‌های استانداردی که یک اپلیکیشن springdoc/OpenAPI ممکن است spec را در آن سِرو کند
_SPEC_CANDIDATES = ("/v3/api-docs", "/v2/api-docs", "/openapi.json", "/swagger.json")

# پارامترِ مسیر به شکلِ {name} — برای مقایسه‌ی مسیرها بدونِ وابستگی به نامِ پارامتر
_PATH_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")


# ── مدل داده ────────────────────────────────────────────────────────────────

@dataclass
class ParamSpec:
    """یک پارامتر درخواست (query / path / header / cookie)."""
    name: str
    location: str            # "query" | "path" | "header" | "cookie"
    required: bool = False
    schema_type: str = "string"
    example: object = None
    description: str = ""


@dataclass
class EndpointSpec:
    """یک عملیات API (ترکیب method + path)."""
    method: str                                  # GET / POST / ...
    path: str                                    # /api/v1/vouchers
    operation_id: str = ""
    summary: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    params: list[ParamSpec] = field(default_factory=list)
    request_body: dict | None = None             # نمونه‌ی body (dict آماده‌ی ارسال)
    request_body_required: bool = False
    request_body_required_fields: list[str] = field(default_factory=list)
    request_content_type: str = "application/json"
    required_headers: list[str] = field(default_factory=list)
    success_status: int = 200                    # اولین کد 2xx تعریف‌شده
    error_statuses: list[int] = field(default_factory=list)
    response_fields: list[str] = field(default_factory=list)
    security: bool = False                        # آیا این عملیات auth می‌خواهد؟

    @property
    def controller(self) -> str:
        """گروه/کنترلری که این endpoint زیر آن قرار می‌گیرد (اولین tag)."""
        return self.tags[0] if self.tags else "default"


@dataclass
class ApiSpec:
    """نتیجه‌ی کامل تحلیل یک سند Swagger."""
    title: str
    version: str
    base_url: str
    endpoints: list[EndpointSpec]
    global_security: bool = False                 # آیا کل API به‌صورت پیش‌فرض auth دارد؟

    def by_controller(self) -> dict[str, list[EndpointSpec]]:
        """endpoint ها را بر اساس controller گروه‌بندی می‌کند (مرتب‌شده)."""
        groups: dict[str, list[EndpointSpec]] = {}
        for ep in self.endpoints:
            groups.setdefault(ep.controller, []).append(ep)
        return {k: groups[k] for k in sorted(groups)}


# ── تحلیل‌گر ─────────────────────────────────────────────────────────────────

class SwaggerAnalyzer:
    """spec را resolve/fetch کرده و به ساختار EndpointSpec تبدیل می‌کند."""

    def __init__(self, timeout: int = 30, session: requests.Session | None = None) -> None:
        self._timeout = timeout
        self._session = session or requests.Session()

    # ---- API عمومی -----------------------------------------------------------

    def analyze(self, swagger_url: str) -> ApiSpec:
        """ورودی: هر URL از Swagger. خروجی: ApiSpec ساختاریافته."""
        spec_url = self._resolve_spec_url(swagger_url)
        spec = self._fetch_json(spec_url)
        return self.parse_spec(spec, source_url=spec_url)

    def parse_spec(self, spec: dict, source_url: str = "") -> ApiSpec:
        """یک dict از OpenAPI (v2 یا v3) را به ApiSpec تبدیل می‌کند.

        این متد جدا از شبکه است تا بشود آن را با یک spec آماده هم تست/استفاده کرد.
        """
        info = spec.get("info", {})
        components = spec.get("components", {})
        # v3: components.schemas — v2: definitions
        schemas = components.get("schemas") or spec.get("definitions") or {}

        base_url = self._derive_base_url(spec, source_url)
        global_security = bool(spec.get("security"))

        endpoints: list[EndpointSpec] = []
        for path, path_item in (spec.get("paths") or {}).items():
            if not isinstance(path_item, dict):
                continue
            # پارامترهای مشترک در سطح path
            shared_params = path_item.get("parameters", [])
            for method, operation in path_item.items():
                if method.lower() not in _HTTP_METHODS:
                    continue
                if not isinstance(operation, dict):
                    continue
                endpoints.append(
                    self._parse_operation(
                        method=method.upper(),
                        path=path,
                        operation=operation,
                        shared_params=shared_params,
                        schemas=schemas,
                        global_security=global_security,
                    )
                )

        return ApiSpec(
            title=info.get("title", "API"),
            version=info.get("version", "1.0.0"),
            base_url=base_url,
            endpoints=endpoints,
            global_security=global_security,
        )

    # ---- resolve کردن URL خام spec ------------------------------------------

    def _resolve_spec_url(self, swagger_url: str) -> str:
        parsed = urlparse(swagger_url)
        path = parsed.path or ""

        if path.endswith(".json") or "/api-docs" in path or "/v3/api-docs" in path:
            return swagger_url

        primary_name = self._extract_primary_name(parsed.query)

        config_url = self._swagger_config_url(swagger_url)
        grouped = self._try_swagger_config(config_url, primary_name)
        if grouped:
            return grouped

        root = f"{parsed.scheme}://{parsed.netloc}"
        context_path = self._derive_context_path(path)
        candidate_bases = [root + context_path, root] if context_path else [root]

        for base in candidate_bases:
            if primary_name:
                for candidate in _SPEC_CANDIDATES:
                    url = urljoin(base + "/", f"{candidate.lstrip('/')}/{primary_name}")
                    if self._looks_like_json(url):
                        return url
            for candidate in _SPEC_CANDIDATES:
                url = urljoin(base + "/", candidate.lstrip("/"))
                if self._looks_like_json(url):
                    return url

        fallback_root = root + context_path if context_path else root
        return urljoin(fallback_root + "/", "v3/api-docs")
    @staticmethod
    def _extract_primary_name(query: str) -> str | None:
        qs = parse_qs(query)
        for key in ("urls.primaryName", "urls.primaryname", "configUrl"):
            if key in qs and qs[key]:
                return qs[key][0]
        return None

    @staticmethod
    def _derive_context_path(path: str) -> str:
        """context-path سرویس را از روی مسیر صفحه‌ی swagger-ui حدس می‌زند.

        مثال: /api/swagger-ui/index.html → /api
        """
        marker = "/swagger-ui"
        idx = path.find(marker)
        if idx > 0:
            return path[:idx]
        return ""

    @staticmethod
    def _swagger_config_url(swagger_url: str) -> str:
        """آدرس swagger-config را نسبت به صفحه‌ی swagger-ui می‌سازد."""
        parsed = urlparse(swagger_url)
        # مسیر دایرکتوریِ index.html
        base_dir = parsed.path.rsplit("/", 1)[0] if "/" in parsed.path else ""
        new_path = f"{base_dir}/swagger-config".replace("//", "/")
        return urlunparse((parsed.scheme, parsed.netloc, new_path, "", "", ""))

    def _try_swagger_config(self, config_url: str, primary_name: str | None) -> str | None:
        """swagger-config را می‌خواند و URL گروه مناسب را برمی‌گرداند."""
        try:
            data = self._fetch_json(config_url)
        except Exception:
            return None

        urls = data.get("urls")
        base = config_url.rsplit("/", 1)[0]

        # springdoc گاهی یک url تکی می‌دهد و گاهی لیستی از گروه‌ها
        if isinstance(urls, list) and urls:
            chosen = None
            if primary_name:
                chosen = next((u for u in urls if u.get("name") == primary_name), None)
            chosen = chosen or urls[0]
            url = chosen.get("url", "")
            return urljoin(base + "/", url.lstrip("/")) if url else None

        single = data.get("url")
        if single:
            return urljoin(base + "/", single.lstrip("/"))
        return None

    # ---- کمکی‌های شبکه -------------------------------------------------------

    def _fetch_json(self, url: str) -> dict:
        resp = self._session.get(url, timeout=self._timeout, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()

    def _looks_like_json(self, url: str) -> bool:
        try:
            resp = self._session.get(url, timeout=self._timeout, headers={"Accept": "application/json"})
            if resp.status_code != 200:
                return False
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return True
            resp.json()  # اگر parse شد یعنی JSON است
            return True
        except Exception:
            return False

    # ---- تبدیل عملیات به EndpointSpec ---------------------------------------

    @staticmethod
    def _derive_base_url(spec: dict, source_url: str) -> str:
        """base URL را از spec (servers / host+basePath) یا از source_url می‌سازد."""
        servers = spec.get("servers")
        if isinstance(servers, list) and servers and servers[0].get("url"):
            server_url = servers[0]["url"]
            if server_url.startswith("http"):
                return server_url.rstrip("/")
            # server نسبی است → با میزبانِ source ترکیب می‌شود
            if source_url:
                p = urlparse(source_url)
                return f"{p.scheme}://{p.netloc}{server_url}".rstrip("/")

        # OpenAPI v2: host + basePath + schemes
        host = spec.get("host")
        if host:
            scheme = (spec.get("schemes") or ["https"])[0]
            base_path = spec.get("basePath", "")
            return f"{scheme}://{host}{base_path}".rstrip("/")

        # fallback: ریشه‌ی سرورِ source_url
        if source_url:
            p = urlparse(source_url)
            return f"{p.scheme}://{p.netloc}"
        return ""

    def _parse_operation(
        self,
        method: str,
        path: str,
        operation: dict,
        shared_params: list,
        schemas: dict,
        global_security: bool,
    ) -> EndpointSpec:
        raw_params = list(shared_params) + list(operation.get("parameters", []))
        params: list[ParamSpec] = []
        required_headers: list[str] = []
        for p in raw_params:
            if not isinstance(p, dict):
                continue
            # v2 پارامتر body را داخل parameters می‌گذارد؛ آن را جدا مدیریت می‌کنیم
            if p.get("in") == "body":
                continue
            schema = p.get("schema", {})
            spec_param = ParamSpec(
                name=p.get("name", ""),
                location=p.get("in", "query"),
                required=bool(p.get("required", False)),
                schema_type=schema.get("type") or p.get("type") or "string",
                example=p.get("example", schema.get("example")),
                description=p.get("description", ""),
            )
            params.append(spec_param)
            if spec_param.location == "header" and spec_param.required:
                required_headers.append(spec_param.name)

        request_body, body_required, content_type, body_required_fields = (
            self._parse_request_body(operation, raw_params, schemas)
        )
        success_status, error_statuses, response_fields = self._parse_responses(
            operation, schemas
        )

        # امنیت: یا در سطح عملیات تعریف شده یا از سطح global ارث می‌برد
        op_security = operation.get("security")
        if op_security is None:
            security = global_security
        else:
            security = bool(op_security)

        return EndpointSpec(
            method=method,
            path=path,
            operation_id=operation.get("operationId", ""),
            summary=operation.get("summary", ""),
            description=operation.get("description", ""),
            tags=list(operation.get("tags", []) or []),
            params=params,
            request_body=request_body,
            request_body_required=body_required,
            request_body_required_fields=body_required_fields,
            request_content_type=content_type,
            required_headers=required_headers,
            success_status=success_status,
            error_statuses=error_statuses,
            response_fields=response_fields,
            security=security,
        )

    def _parse_request_body(
        self, operation: dict, raw_params: list, schemas: dict
    ) -> tuple[dict | None, bool, str, list[str]]:
        """نمونه‌ی body را از requestBody (v3) یا پارامترِ in=body (v2) می‌سازد.

        خروجی: (نمونه‌ی body، اجباری بودنِ body، content type، فیلدهای اجباریِ body)

        فیلدهای اجباری از `required` همان schema خوانده می‌شوند تا سناریوی منفیِ
        «فیلد اجباریِ جاافتاده» واقعاً یک فیلدِ اجباری را حذف کند، نه یک فیلدِ دلخواه.
        """
        # OpenAPI v3
        rb = operation.get("requestBody")
        if isinstance(rb, dict):
            content = rb.get("content", {})
            # ترجیح با application/json
            content_type = "application/json" if "application/json" in content else (
                next(iter(content), "application/json")
            )
            media = content.get(content_type, {})
            schema = media.get("schema", {})
            example = media.get("example")
            sample = example if example is not None else self._sample_from_schema(schema, schemas)
            return (
                sample,
                bool(rb.get("required", False)),
                content_type,
                self._required_fields(schema, schemas),
            )

        # OpenAPI v2 (body در parameters)
        for p in raw_params:
            if isinstance(p, dict) and p.get("in") == "body":
                schema = p.get("schema", {})
                sample = self._sample_from_schema(schema, schemas)
                return (
                    sample,
                    bool(p.get("required", False)),
                    "application/json",
                    self._required_fields(schema, schemas),
                )

        return None, False, "application/json", []

    def _required_fields(self, schema: dict, schemas: dict) -> list[str]:
        """نامِ فیلدهای اجباریِ سطحِ اولِ یک schema بدنه را برمی‌گرداند."""
        resolved = self._resolve_ref(schema, schemas) if isinstance(schema, dict) else {}
        if not isinstance(resolved, dict):
            return []

        required = resolved.get("required")
        names = [str(n) for n in required if isinstance(n, str)] if isinstance(required, list) else []

        # allOf: فیلدهای اجباری می‌توانند در قطعاتِ ترکیب‌شده تعریف شده باشند
        for sub in resolved.get("allOf") or []:
            if isinstance(sub, dict):
                names.extend(self._required_fields(sub, schemas))

        # حفظِ ترتیب، بدونِ تکرار
        seen: set[str] = set()
        return [n for n in names if not (n in seen or seen.add(n))]

    def _parse_responses(
        self, operation: dict, schemas: dict
    ) -> tuple[int, list[int], list[str]]:
        """کد موفقیت، کدهای خطا و فیلدهای پاسخِ موفق را استخراج می‌کند."""
        responses = operation.get("responses", {}) or {}
        success_status = 200
        error_statuses: list[int] = []
        response_fields: list[str] = []

        codes: list[int] = []
        for code in responses:
            try:
                codes.append(int(code))
            except (ValueError, TypeError):
                continue

        success_codes = sorted(c for c in codes if 200 <= c < 300)
        if success_codes:
            success_status = success_codes[0]
        error_statuses = sorted(c for c in codes if c >= 400)

        # فیلدهای پاسخِ موفق (سطح اول) برای ساخت تست‌های validation
        success_key = str(success_status) if str(success_status) in responses else next(
            (k for k in responses if k.startswith("2")), None
        )
        if success_key:
            schema = self._response_schema(responses[success_key])
            resolved = self._resolve_ref(schema, schemas) if schema else {}
            props = resolved.get("properties") if isinstance(resolved, dict) else None
            if isinstance(props, dict):
                response_fields = list(props.keys())

        return success_status, error_statuses, response_fields

    @staticmethod
    def _response_schema(response_obj: dict) -> dict:
        if not isinstance(response_obj, dict):
            return {}
        # v3
        content = response_obj.get("content", {})
        if isinstance(content, dict) and content:
            media = content.get("application/json") or next(iter(content.values()), {})
            return media.get("schema", {}) if isinstance(media, dict) else {}
        # v2
        return response_obj.get("schema", {})

    # ---- نمونه‌سازی از schema ------------------------------------------------

    def _sample_from_schema(self, schema: dict, schemas: dict, _depth: int = 0) -> object:
        """از روی یک JSON schema یک نمونه‌ی داده‌ی معتبر می‌سازد.

        از $ref پیروی می‌کند و برای جلوگیری از حلقه‌ی بی‌نهایت عمق را محدود می‌کند.
        """
        if not isinstance(schema, dict) or _depth > 6:
            return {}

        schema = self._resolve_ref(schema, schemas, _depth)

        if "example" in schema:
            return schema["example"]
        if "default" in schema:
            return schema["default"]
        if "enum" in schema and schema["enum"]:
            return schema["enum"][0]

        # ترکیب‌های allOf/oneOf/anyOf
        for combiner in ("allOf", "oneOf", "anyOf"):
            if combiner in schema and schema[combiner]:
                merged: dict = {}
                for sub in schema[combiner]:
                    sample = self._sample_from_schema(sub, schemas, _depth + 1)
                    if isinstance(sample, dict):
                        merged.update(sample)
                if merged or combiner != "allOf":
                    return merged or self._sample_from_schema(schema[combiner][0], schemas, _depth + 1)

        stype = schema.get("type")
        if stype == "object" or "properties" in schema:
            result: dict = {}
            for name, prop in (schema.get("properties") or {}).items():
                result[name] = self._sample_from_schema(prop, schemas, _depth + 1)
            return result
        if stype == "array":
            item = self._sample_from_schema(schema.get("items", {}), schemas, _depth + 1)
            return [item]

        return self._primitive_sample(schema, stype)

    @staticmethod
    def _primitive_sample(schema: dict, stype: str | None) -> object:
        fmt = schema.get("format", "")
        if stype in ("integer", "number"):
            return 0
        if stype == "boolean":
            return True
        # string با فرمت‌های رایج
        if fmt == "date-time":
            return "2024-01-01T00:00:00Z"
        if fmt == "date":
            return "2024-01-01"
        if fmt in ("uuid",):
            return "00000000-0000-0000-0000-000000000000"
        if fmt == "email":
            return "user@example.com"
        return "string"

    @staticmethod
    def _resolve_ref(schema: dict, schemas: dict, _depth: int = 0) -> dict:
        """اگر schema یک $ref باشد، تعریف واقعی را از components برمی‌گرداند."""
        seen = 0
        while isinstance(schema, dict) and "$ref" in schema and seen < 10:
            ref = schema["$ref"]
            key = ref.rsplit("/", 1)[-1]
            resolved = schemas.get(key)
            if not isinstance(resolved, dict):
                return {}
            schema = resolved
            seen += 1
        return schema if isinstance(schema, dict) else {}


def parse_swagger_fragment(swagger_url: str) -> tuple[str | None, str | None]:
    """(controller_tag, operation_id) را از فرگمنت URL صفحه‌ی swagger-ui استخراج می‌کند.

    فرگمنت (بعد از #) هرگز به سرور فرستاده نمی‌شود؛ این تابع صرفاً روی رشته‌ی
    خودِ URL کار می‌کند.

    مثال‌ها:
        .../index.html#/voucher-admin-controller
            -> ("voucher-admin-controller", None)
        .../index.html#/voucher-admin-controller/getVoucherDetails
            -> ("voucher-admin-controller", "getVoucherDetails")
    """
    fragment = urlparse(swagger_url).fragment
    parts = [p for p in fragment.split("/") if p]
    if not parts:
        return None, None
    controller = parts[0]
    operation_id = parts[1] if len(parts) > 1 else None
    return controller, operation_id


def filter_endpoints(
    endpoints: list[EndpointSpec],
    controller: str | None,
    operation_id: str | None,
) -> list[EndpointSpec]:
    """endpoint ها را بر اساس controller/operationِ استخراج‌شده از فرگمنت فیلتر می‌کند.

    اگر controller داده نشده باشد، لیست بدون تغییر برمی‌گردد.
    """
    if not controller:
        return endpoints
    filtered = [ep for ep in endpoints if controller in ep.tags]
    if operation_id:
        filtered = [ep for ep in filtered if ep.operation_id == operation_id]
    return filtered


def strip_ui_fragment(swagger_url: str) -> str:
    """فرگمنتِ صفحه‌ی swagger-ui را از URL حذف می‌کند.

    فرگمنت (بعد از #) یک شناسه‌ی داخلِ صفحه‌ی UI است، نه بخشی از آدرسِ سند. برای
    گرفتنِ خودِ سند هیچ نقشی ندارد و حذفش از سردرگمی جلوگیری می‌کند.
    """
    parsed = urlparse(swagger_url or "")
    return urlunparse(parsed._replace(fragment=""))


def _normalize_path(path: str) -> str:
    """مسیر را برای مقایسه نرمال می‌کند: اسلشِ ابتدایی، حذفِ اسلشِ انتهایی و
    یکسان‌سازیِ نامِ پارامترهای مسیر ({id} و {voucherId} یکی در نظر گرفته می‌شوند).
    """
    normalized = (path or "").strip()
    if not normalized:
        return ""
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    normalized = _PATH_PLACEHOLDER_RE.sub("{}", normalized)
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized


def _segments(path: str) -> list[str]:
    return [seg for seg in path.split("/") if seg]


def _tail_match(spec_path: str, wanted_path: str) -> bool:
    """آیا یکی از دو مسیر پسوندِ (segment به segment) دیگری است؟

    این حالت وقتی پیش می‌آید که سناریو مسیر را بدونِ context-path سرویس نوشته
    باشد (یا برعکس، مسیر را با context-path نوشته باشد).
    """
    spec_segments = _segments(spec_path)
    wanted_segments = _segments(wanted_path)
    if not spec_segments or not wanted_segments:
        return False
    if len(spec_segments) >= len(wanted_segments):
        return spec_segments[-len(wanted_segments):] == wanted_segments
    return wanted_segments[-len(spec_segments):] == spec_segments


def find_endpoint(
    endpoints: list[EndpointSpec],
    method: str,
    path: str,
    operation_id: str = "",
) -> EndpointSpec | None:
    """عملیاتِ خواسته‌شده را بینِ endpoint های یک spec پیدا می‌کند.

    سناریو «جریان» را توصیف می‌کند و ممکن است عملیات را همان‌طور که در صفحه‌ی
    swagger-ui دیده می‌شود بنویسد. یعنی چیزی که در YAML به‌عنوان `path` آمده
    همیشه مسیرِ واقعیِ HTTP نیست؛ می‌تواند شکلِ فرگمنتِ UI باشد:

        /voucher-admin-controller/defineAmountVoucher
         └── tag ────────────────┘└── operationId ──┘

    به همین دلیل چند راهبردِ تطبیق به ترتیبِ دقت امتحان می‌شوند و فقط وقتی نتیجه
    داده می‌شود که دقیقاً یک endpoint تطبیق کند — تطبیقِ مبهم مثل «پیدا نشد»
    برخورد می‌شود تا عملیاتِ اشتباهی تست نشود.

    برمی‌گرداند:
        EndpointSpec در صورتِ تطبیقِ یکتا، در غیر این صورت None.
    """
    wanted_method = (method or "").strip().upper()
    pool = [ep for ep in endpoints if ep.method.upper() == wanted_method]
    if not pool:
        return None

    raw_path = (path or "").strip()
    wanted_path = _normalize_path(raw_path)
    segments = _segments(wanted_path)

    def unique(matches: list[EndpointSpec]) -> EndpointSpec | None:
        return matches[0] if len(matches) == 1 else None

    strategies: list[list[EndpointSpec]] = []

    # ۱. operationId که سناریو صریحاً داده است — دقیق‌ترین شناسه
    if operation_id:
        strategies.append([ep for ep in pool if ep.operation_id == operation_id])

    # ۲. تطبیقِ عینِ مسیر
    strategies.append([ep for ep in pool if ep.path == raw_path])

    # ۳. تطبیقِ نرمال‌شده‌ی مسیر
    strategies.append([ep for ep in pool if _normalize_path(ep.path) == wanted_path])

    # ۴. شکلِ فرگمنتِ swagger-ui: /<tag>/<operationId>
    if len(segments) == 2:
        tag, candidate_op = segments
        strategies.append(
            [ep for ep in pool if ep.operation_id == candidate_op and tag in ep.tags]
        )

    # ۵. آخرین قطعه‌ی مسیر به‌عنوان operationId
    if segments:
        strategies.append([ep for ep in pool if ep.operation_id == segments[-1]])

    # ۶. پسوندِ مسیر (context-path در یک طرف نیامده باشد)
    strategies.append([ep for ep in pool if _tail_match(ep.path, wanted_path)])

    for matches in strategies:
        found = unique(matches)
        if found is not None:
            return found

    return None


def describe_endpoints(endpoints: list[EndpointSpec], limit: int = 20) -> str:
    """فهرستِ خوانا از endpoint های موجود — برای پیام‌های خطا."""
    lines = [
        f"  - {ep.method} {ep.path}" + (f"  (operationId: {ep.operation_id})" if ep.operation_id else "")
        for ep in endpoints[:limit]
    ]
    if len(endpoints) > limit:
        lines.append(f"  - ... and {len(endpoints) - limit} more")
    return "\n".join(lines)