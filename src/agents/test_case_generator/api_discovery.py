"""
api_discovery.py — کشفِ API از سندِ Swagger/OpenAPI (قدم دومِ جریان HITL)

این ماژول بخشِ «تصمیمِ قطعی» قدم دوم است:

    Developed Services (URL یا فایلِ Swagger)
        → دریافت و parse سند
        → عملیات‌های واقعیِ API
        → base URL، پارامترها، بدنه‌ی درخواست، پاسخ‌ها
        → قراردادِ احراز هویت (فقط اگر خودِ سند auth بخواهد)

هیچ نگاشتی به تست‌کیس و هیچ فراخوانیِ LLM اینجا نیست. سندِ Swagger تنها منبعِ
حقیقتِ فنی است: هیچ متد/مسیر/پارامتری حدس زده نمی‌شود و هر چیزی که قابلِ
تعیین نباشد در `unresolved` گزارش می‌شود.

parse و استخراجِ عملیات‌ها در swagger_analyzer.py انجام می‌شود — همان ماژولی که
جریانِ Swagger-first موجود استفاده می‌کند — و اینجا دوباره پیاده نشده است.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.agents.test_case_generator.swagger_analyzer import (
    ApiSpec,
    EndpointSpec,
    SwaggerAnalyzer,
)

# قراردادِ احراز هویتِ شناخته‌شده‌ی پروژه. توکنِ واقعی هرگز اینجا ساخته نمی‌شود
# و هیچ درخواستی با آن ارسال نمی‌شود — فقط برای ساختِ Postman در قدمِ بعد.
AUTHORIZATION_HEADER = "Authorization"
AUTHORIZATION_VALUE = "Bearer {{token}}"

# نامِ پارامترِ مسیر به شکلِ {name} — برای تطبیقِ مسیرها بدونِ وابستگی به نام
_PATH_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")


class SwaggerLoadError(RuntimeError):
    """سندِ Swagger دریافت، خوانده یا parse نشد."""


# ── مدل داده ─────────────────────────────────────────────────────────────────

@dataclass
class DiscoveredApi:
    """یک عملیاتِ واقعیِ API که از سندِ Swagger استخراج شده است."""

    method: str
    path: str
    operation_id: str = ""
    summary: str = ""
    parameters: list[dict[str, Any]] = field(default_factory=list)
    request_body: Any = None
    responses: dict[str, Any] = field(default_factory=dict)
    requires_auth: bool = False

    @property
    def label(self) -> str:
        """نمایشِ خوانا: «GET /admin/voucher/{id}»."""
        return f"{self.method} {self.path}"

    def to_dict(self) -> dict[str, Any]:
        """شکلِ ساختاریافته برای نتیجه‌ی قدم دوم."""
        return {
            "method": self.method,
            "path": self.path,
            "operation_id": self.operation_id,
            "summary": self.summary,
            "parameters": list(self.parameters),
            "request_body": self.request_body,
            "responses": dict(self.responses),
        }


@dataclass
class DiscoveredService:
    """یک سرویسِ Swagger و همه‌ی عملیات‌های کشف‌شده‌ی آن."""

    name: str
    source_url: str
    base_url: str
    title: str = ""
    version: str = ""
    apis: list[DiscoveredApi] = field(default_factory=list)
    requires_auth: bool = False
    unresolved: list[str] = field(default_factory=list)

    @property
    def authorization(self) -> dict[str, str] | None:
        """قراردادِ احراز هویت — فقط وقتی سند واقعاً auth می‌خواهد."""
        if not self.requires_auth:
            return None
        return {"header": AUTHORIZATION_HEADER, "value": AUTHORIZATION_VALUE}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_url": self.source_url,
            "base_url": self.base_url,
            "apis": [api.to_dict() for api in self.apis],
            "authorization": self.authorization,
            "unresolved": list(self.unresolved),
        }


# ── کلیدِ تطبیقِ عملیات ──────────────────────────────────────────────────────

def operation_key(method: str, path: str) -> tuple[str, str]:
    """کلیدِ تطبیقِ یک عملیات: متدِ بزرگ + مسیرِ نرمال‌شده.

    نامِ پارامترهای مسیر نادیده گرفته می‌شود تا `{id}` و `{voucherId}` یکی
    حساب شوند؛ اسلشِ ابتدایی اضافه و اسلشِ انتهایی حذف می‌شود.
    """
    normalized = (path or "").strip()
    if normalized and not normalized.startswith("/"):
        normalized = "/" + normalized
    normalized = _PATH_PLACEHOLDER_RE.sub("{}", normalized)
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return (method or "").strip().upper(), normalized


def operation_index(services: list[DiscoveredService]) -> dict[tuple[str, str], DiscoveredApi]:
    """(متد، مسیرِ نرمال‌شده) → عملیات — برای اعتبارسنجیِ نگاشت‌ها."""
    index: dict[tuple[str, str], DiscoveredApi] = {}
    for service in services:
        for api in service.apis:
            index.setdefault(operation_key(api.method, api.path), api)
    return index


def operation_ids(services: list[DiscoveredService]) -> dict[str, list[DiscoveredApi]]:
    """operationId → عملیات‌هایی که این شناسه را دارند."""
    ids: dict[str, list[DiscoveredApi]] = {}
    for service in services:
        for api in service.apis:
            if api.operation_id:
                ids.setdefault(api.operation_id, []).append(api)
    return ids


# ── تبدیلِ EndpointSpec به مدلِ قدم دوم ──────────────────────────────────────

def _serialize_parameters(endpoint: EndpointSpec) -> list[dict[str, Any]]:
    return [
        {
            "name": param.name,
            "in": param.location,
            "required": param.required,
            "type": param.schema_type,
            "description": param.description,
        }
        for param in endpoint.params
    ]


def _serialize_responses(endpoint: EndpointSpec) -> dict[str, Any]:
    """نقشه‌ی کدِ وضعیت → فیلدهای شناخته‌شده‌ی پاسخ.

    فقط چیزی که analyzer واقعاً استخراج کرده منتشر می‌شود؛ توضیحِ پاسخ ساخته
    نمی‌شود.
    """
    responses: dict[str, Any] = {
        str(endpoint.success_status): {"fields": list(endpoint.response_fields)}
    }
    for status in endpoint.error_statuses:
        responses.setdefault(str(status), {"fields": []})
    return responses


def _to_discovered_api(endpoint: EndpointSpec) -> DiscoveredApi:
    return DiscoveredApi(
        method=endpoint.method.upper(),
        path=endpoint.path,
        operation_id=endpoint.operation_id,
        summary=endpoint.summary,
        parameters=_serialize_parameters(endpoint),
        request_body=endpoint.request_body,
        responses=_serialize_responses(endpoint),
        requires_auth=endpoint.security,
    )


def _primary_name(source_url: str) -> str:
    """نامِ گروهِ Swagger از کوئریِ `urls.primaryName` در آدرسِ صفحه‌ی UI."""
    query = parse_qs(urlparse(source_url or "").query)
    for key in ("urls.primaryName", "urls.primaryname"):
        values = query.get(key)
        if values:
            return values[0]
    return ""


def _service_name(api_spec: ApiSpec, source_url: str, fallback_name: str) -> str:
    """نامِ سرویس: نامِ صریح، سپس گروهِ Swagger، سپس عنوانِ سند، سپس میزبان."""
    if fallback_name:
        return fallback_name
    primary = _primary_name(source_url)
    if primary:
        return primary
    if api_spec.title and api_spec.title != "API":
        return api_spec.title
    return urlparse(source_url or "").netloc or "service"


def _service_from_api_spec(
    api_spec: ApiSpec, source_url: str, name: str = ""
) -> DiscoveredService:
    unresolved: list[str] = []
    if not api_spec.base_url:
        unresolved.append(
            "Base URL could not be determined from the Swagger document."
        )

    apis = [_to_discovered_api(endpoint) for endpoint in api_spec.endpoints]
    if not apis:
        unresolved.append("No API operation was found in the Swagger document.")

    return DiscoveredService(
        name=_service_name(api_spec, source_url, name),
        source_url=source_url,
        base_url=api_spec.base_url,
        title=api_spec.title,
        version=api_spec.version,
        apis=apis,
        requires_auth=api_spec.global_security
        or any(endpoint.security for endpoint in api_spec.endpoints),
        unresolved=unresolved,
    )


# ── ورودی‌های عمومی ──────────────────────────────────────────────────────────

def discover_from_spec(
    spec: dict, source_url: str = "", name: str = ""
) -> DiscoveredService:
    """یک سندِ OpenAPI/Swagger را بدونِ شبکه به DiscoveredService تبدیل می‌کند."""
    if not isinstance(spec, dict) or not spec:
        raise SwaggerLoadError("Swagger document is empty or not an object.")
    if not isinstance(spec.get("paths"), dict) or not spec["paths"]:
        raise SwaggerLoadError(
            "Swagger document has no 'paths' — no API operation can be discovered."
        )

    api_spec = SwaggerAnalyzer().parse_spec(spec, source_url=source_url)
    return _service_from_api_spec(api_spec, source_url, name)


def load_service(
    source: str,
    *,
    name: str = "",
    session: Any = None,
    timeout: int = 30,
) -> DiscoveredService:
    """یک سرویس را از URL یا فایلِ محلیِ Swagger می‌خواند و کشف را انجام می‌دهد."""
    source = (source or "").strip()
    if not source:
        raise SwaggerLoadError("Swagger source is empty.")

    local = Path(source)
    if local.exists() and local.is_file():
        try:
            spec = json.loads(local.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SwaggerLoadError(f"Swagger file could not be read: {exc}") from exc
        return discover_from_spec(spec, source_url=str(local), name=name)

    if not source.startswith(("http://", "https://")):
        raise SwaggerLoadError(
            f"Swagger source is neither a URL nor a readable file: {source}"
        )

    analyzer = SwaggerAnalyzer(timeout=timeout, session=session)
    try:
        api_spec = analyzer.analyze(source)
    except Exception as exc:  # noqa: BLE001 — خطای شبکه/سند نباید بالا بشکند
        raise SwaggerLoadError(
            f"Swagger document could not be loaded from {source}: {exc}"
        ) from exc
    return _service_from_api_spec(api_spec, source, name)


def discover_services(
    sources: list[str],
    *,
    session: Any = None,
    timeout: int = 30,
) -> tuple[list[DiscoveredService], list[str]]:
    """چند منبع را می‌خواند و (سرویس‌های کشف‌شده، خطاها) را برمی‌گرداند.

    یک منبعِ خراب کلِ قدم را متوقف نمی‌کند: خطا جمع می‌شود و بقیه‌ی منابع
    ادامه پیدا می‌کنند تا نتیجه برای بازبینیِ انسانی کامل بماند.
    """
    services: list[DiscoveredService] = []
    errors: list[str] = []
    for source in sources:
        try:
            services.append(load_service(source, session=session, timeout=timeout))
        except SwaggerLoadError as exc:
            errors.append(str(exc))
    return services, errors


def split_sources(raw: str) -> list[str]:
    """متنِ چندخطیِ سرویس‌ها را به لیستِ منبع‌های غیرِتکراری تبدیل می‌کند."""
    seen: set[str] = set()
    sources: list[str] = []
    for line in (raw or "").splitlines():
        source = line.strip()
        if source and source not in seen:
            seen.add(source)
            sources.append(source)
    return sources
