
"""
test_case_generator/agent.py — ایجنت تولید تست‌کیس از Swagger یا سناریو

این ایجنت فقط orchestration زنجیره‌ی موجود را انجام می‌دهد و منطقی را که در
ماژول‌های دیگر پیاده شده تکرار نمی‌کند. دو حالتِ ورودی پشتیبانی می‌شود:

    حالتِ سناریو (وقتی کاربر مسیرِ یک فایل YAML بدهد):
        Scenario YAML
            → ScenarioParser    → Scenario
            → SwaggerAnalyzer   → ApiSpec برای هر منبعِ Swagger
            → TestCaseGenerator → TestCaseSuite (به ترتیبِ قدم‌های سناریو)
            → PostmanBuilder    → Postman Collection (dict)
            → ذخیره‌ی JSON روی دیسک

    حالتِ Swagger (رفتارِ قبلی، بدونِ تغییر):
        URL کاربر
            → SwaggerAnalyzer   → ApiSpec
            → TestCaseGenerator → TestCaseSuite
            → PostmanBuilder    → Postman Collection (dict)
            → ذخیره‌ی JSON روی دیسک

    → خلاصه در قالب AIMessage

تشخیصِ ورودی: اول سناریو، بعد URLِ Swagger. اگر کاربر صریحاً یک سناریو داده
باشد و آن سناریو خراب باشد، خطای سناریو برگردانده می‌شود و بی‌سروصدا به حالتِ
Swagger برگشت داده نمی‌شود.

نکته:
    خودِ ایجنت هیچ‌وقت مستقیماً LLM را صدا نمی‌زند؛ تولید تست‌کیس وظیفه‌ی
    TestCaseGenerator است. ساخت ساختار Postman هم وظیفه‌ی PostmanBuilder است و
    خواندن/اعتبارسنجیِ YAML وظیفه‌ی ScenarioParser. تنها کارِ اضافه‌ی این فایل،
    serialize و ذخیره‌ی dictِ کالکشن است.

محدودیتِ ایمنی:
    این ایجنت هیچ API اپلیکیشنی را اجرا نمی‌کند. تنها درخواستِ شبکه‌ای که زده
    می‌شود، دریافتِ خودِ سندِ Swagger/OpenAPI است — یعنی تحلیلِ مستندات، نه
    اجرای تست.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from langchain_core.messages import AIMessage

from src.agents.base_agent import BaseAgent
from src.agents.state import AgentState
from src.agents.test_case_generator.postman_builder import (
    PostmanBuilder,
    PostmanBuildError,
)
from src.agents.test_case_generator.scenario_parser import (
    Scenario,
    ScenarioError,
    ScenarioParser,
    extract_scenario_path,
)
from src.agents.test_case_generator.swagger_analyzer import (
    ApiSpec,
    SwaggerAnalyzer,
    describe_endpoints,
    find_endpoint,
    parse_swagger_fragment,
    filter_endpoints,
    strip_ui_fragment,
)
from src.agents.test_case_generator.testcase_generator import (
    DEFAULT_BASE_URL_VAR,
    ResolvedStep,
    TestCaseGenerationError,
    TestCaseGenerator,
    TestCaseSuite,
)
from src.debug import DebugConfig

_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"

# دایرکتوری خروجی — فقط برای artifactهای همین ایجنت
_OUTPUT_DIR = Path(__file__).parent / "output"

# اولین URL موجود در پیام کاربر
_URL_PATTERN = re.compile(r"https?://\S+")

# کاراکترهای مجاز در نامِ امنِ فایل
_UNSAFE_FILENAME_RE = re.compile(r"[^a-z0-9]+")


def _extract_swagger_url(text: str) -> str:
    """اولین URL را از پیام کاربر بیرون می‌کشد."""
    match = _URL_PATTERN.search(text or "")
    if not match:
        return ""
    # علائم نگارشی چسبیده به انتهای URL حذف می‌شوند
    return match.group(0).rstrip(".,;:)\"'>")


def _safe_filename(title: str) -> str:
    """عنوانِ API را به یک اسلاگِ امنِ فایل تبدیل می‌کند.

    مثال: "Admin API" → "admin_api". فقط حروف کوچک، ارقام و «_» باقی می‌ماند.
    اگر عنوان چیزی برای اسلاگ‌کردن نداشته باشد، به "api" برمی‌گردد.
    """
    slug = _UNSAFE_FILENAME_RE.sub("_", (title or "").strip().lower()).strip("_")
    return slug or "api"


def _base_url_var(source_name: str, single_source: bool) -> str:
    """نامِ متغیرِ base URL برای یک منبعِ Swagger.

    وقتی سناریو فقط یک منبع دارد، همان `baseUrl`ِ همیشگی استفاده می‌شود تا
    کالکشن با حالتِ Swagger یکسان بماند. با چند منبع، هر سرویس متغیرِ خودش را
    می‌گیرد (مثلاً baseUrl_admin).
    """
    if single_source:
        return DEFAULT_BASE_URL_VAR
    return f"{DEFAULT_BASE_URL_VAR}_{_safe_filename(source_name)}"


class TestCaseGeneratorAgent(BaseAgent):
    """از یک URL سواگر یا یک سناریوی YAML، یک Postman Collection می‌سازد."""

    name = "test_case_generator"

    def __init__(
        self,
        debug_config: DebugConfig | None = None,
        analyzer: SwaggerAnalyzer | None = None,
        testcase_generator: TestCaseGenerator | None = None,
        postman_builder: PostmanBuilder | None = None,
        scenario_parser: ScenarioParser | None = None,
    ) -> None:
        super().__init__(
            name=self.name,
            system_prompt=_PROMPT_PATH.read_text(encoding="utf-8"),
        )
        debug_config = debug_config or DebugConfig.off()
        self._log = debug_config.get_logger(self.name)
        self._analyzer = analyzer or SwaggerAnalyzer()
        self._testcase_generator = testcase_generator or TestCaseGenerator(debug_config)
        self._postman_builder = postman_builder or PostmanBuilder(debug_config)
        self._scenario_parser = scenario_parser or ScenarioParser()

    def run(self, state: AgentState) -> dict:
        """ورودی را تشخیص می‌دهد و به حالتِ مناسب واگذار می‌کند.

        اولویتِ تشخیص:
            ۱. مسیرِ یک فایلِ سناریو (YAML) در پیام کاربر → حالتِ سناریو
            ۲. یک URL از Swagger/OpenAPI                  → حالتِ Swagger
            ۳. هیچ‌کدام                                    → پیامِ خطای روشن

        اگر کاربر صریحاً سناریو داده باشد، هرگز بی‌سروصدا به حالتِ Swagger
        برگشت داده نمی‌شود — خطای سناریو گزارش می‌شود.
        """
        message = self._last_human_message(state)

        scenario_path = extract_scenario_path(message)
        if scenario_path:
            self._log.info(
                "ورودیِ سناریو تشخیص داده شد",
                user_id=state["user_id"],
                scenario_path=scenario_path,
            )
            return self._run_scenario_mode(scenario_path, state)

        return self._run_swagger_mode(message, state)

    # ── حالتِ Swagger (رفتارِ قبلی) ─────────────────────────────────────────────

    def _run_swagger_mode(self, message: str, state: AgentState) -> dict:
        self._log.info("شروع تولید تست‌کیس", user_id=state["user_id"])

        # ── ۱. دریافت URL ─────────────────────────────────────────────────────
        swagger_url = _extract_swagger_url(message)
        if not swagger_url:
            self._log.warning("هیچ URL سواگری در پیام کاربر پیدا نشد")
            return self._reply(
                "No Swagger/OpenAPI URL or scenario file was found in your message.\n\n"
                "Send either:\n"
                "- the URL of the Swagger UI page or the raw api-docs JSON "
                "(for example: https://host/swagger-ui/index.html), or\n"
                "- the path of a scenario YAML file "
                "(for example: scenarios/voucher_creation_flow.yaml)."
            )

        # ── ۲. تحلیل Swagger ───────────────────────────────────────────────────
        self._log.debug("سواگر در حال تحلیل", swagger_url=swagger_url)
        try:
            spec = self._analyzer.analyze(swagger_url)
        except Exception as exc:  # شبکه، 404، JSON نامعتبر، spec ناقص و ...
            self._log.error(
                "تحلیل سواگر شکست خورد",
                swagger_url=swagger_url,
                error=f"{type(exc).__name__}: {exc}",
            )
            return self._reply(
                f"Swagger analysis failed for `{swagger_url}`.\n\n"
                f"Reason: {type(exc).__name__}: {exc}\n\n"
                "Please make sure the URL is reachable and points to a valid "
                "Swagger/OpenAPI document."
            )

        # ── ۲.۵. فیلتر بر اساس کنترلر/عملیاتی که در فرگمنت URL مشخص شده ──────────
        controller, operation_id = parse_swagger_fragment(swagger_url)
        if controller:
            before = len(spec.endpoints)
            spec.endpoints = filter_endpoints(spec.endpoints, controller, operation_id)
            self._log.info(
                "فیلتر بر اساس فرگمنت اعمال شد",
                controller=controller,
                operation_id=operation_id,
                before=before,
                after=len(spec.endpoints),
            )

        if not spec.endpoints:
            self._log.warning("هیچ endpoint ای در spec پیدا نشد", swagger_url=swagger_url)
            return self._reply(
                f"The Swagger document at `{swagger_url}` was fetched successfully "
                "but contains no endpoints matching the selected controller/operation, "
                "so there is nothing to generate test cases for."
            )

        self._log.info(
            "تحلیل سواگر کامل شد",
            title=spec.title,
            base_url=spec.base_url,
            endpoint_count=len(spec.endpoints),
        )

        # ── ۳. تولید تست‌کیس ساختاریافته ────────────────────────────────────────
        self._log.info("شروع تولید تست‌کیس ساختاریافته", title=spec.title)
        try:
            suite = self._testcase_generator.generate(
                spec=spec,
                user_id=state["user_id"],
            )
        except TestCaseGenerationError as exc:
            self._log.error("تولید تست‌کیس شکست خورد", error=str(exc))
            return self._reply(
                f"Test case generation failed for `{spec.title}`.\n\n"
                f"Reason: {exc}\n\n"
                "The model output did not match the required structure. "
                "Please try again."
            )
        self._log.info("تولید تست‌کیس ساختاریافته کامل شد")

        # ── ۴. ساخت Postman Collection ──────────────────────────────────────────
        self._log.info("شروع ساخت Postman Collection")
        try:
            collection = self._postman_builder.build(
                suite=suite,
                base_url=spec.base_url,
                collection_name=f"{spec.title} API Tests",
            )
        except PostmanBuildError as exc:
            self._log.error("ساخت کالکشن شکست خورد", error=str(exc))
            return self._reply(
                f"Building the Postman collection failed for `{spec.title}`.\n\n"
                f"Reason: {exc}"
            )
        self._log.info("ساخت Postman Collection کامل شد")

        # ── ۵. ذخیره‌ی فایل ─────────────────────────────────────────────────────
        try:
            output_path = self._save_collection(collection, spec.title)
        except OSError as exc:
            self._log.error("ذخیره‌ی فایل شکست خورد", error=f"{type(exc).__name__}: {exc}")
            return self._reply(
                f"The Postman collection was built but could not be saved.\n\n"
                f"Reason: {type(exc).__name__}: {exc}"
            )
        self._log.info("فایل خروجی ذخیره شد", output_path=str(output_path))

        # ── ۶. خلاصه ────────────────────────────────────────────────────────────
        return self._reply(
            self._summary(spec, suite, output_path)
        )

    # ── حالتِ سناریو ──────────────────────────────────────────────────────────

    def _run_scenario_mode(self, scenario_path: str, state: AgentState) -> dict:
        """از یک فایلِ سناریو، یک کالکشنِ Postman می‌سازد."""
        # ── ۱. خواندن و اعتبارسنجیِ سناریو ───────────────────────────────────
        try:
            scenario = self._scenario_parser.parse_file(scenario_path)
        except ScenarioError as exc:
            # کاربر صریحاً سناریو خواسته است → به حالتِ Swagger برنمی‌گردیم
            self._log.error("خواندنِ سناریو شکست خورد", error=str(exc))
            return self._reply(
                f"The scenario file `{scenario_path}` could not be used.\n\n{exc}"
            )

        self._log.info(
            "سناریو خوانده شد",
            scenario=scenario.name,
            sources=len(scenario.swagger_sources),
            steps=len(scenario.steps),
        )

        # ── ۲. تحلیلِ منابعِ Swagger ───────────────────────────────────────────
        try:
            specs = self._analyze_sources(scenario)
        except Exception as exc:  # شبکه، 404، JSON نامعتبر، spec ناقص و ...
            self._log.error(
                "تحلیلِ منبعِ Swaggerِ سناریو شکست خورد",
                error=f"{type(exc).__name__}: {exc}",
            )
            return self._reply(
                f"Swagger analysis failed for scenario `{scenario.name}`.\n\n"
                f"Reason: {type(exc).__name__}: {exc}\n\n"
                "Please make sure every URL under `swagger_sources` is reachable "
                "and points to a valid Swagger/OpenAPI document."
            )

        # ── ۳. گره‌زدنِ هر قدم به عملیاتِ واقعیِ Swagger ─────────────────────────
        steps, unresolved = self._resolve_steps(scenario, specs)
        if unresolved:
            self._log.error("قدم‌های غیرقابلِ resolve", count=len(unresolved))
            return self._reply(
                f"Scenario `{scenario.name}` could not be mapped onto the Swagger "
                "documents.\n\n" + "\n\n".join(unresolved)
            )

        # ── ۴. تولید تست‌کیس ساختاریافته ──────────────────────────────────────
        try:
            suite = self._testcase_generator.generate_for_scenario(
                scenario=scenario,
                steps=steps,
                user_id=state["user_id"],
            )
        except (TestCaseGenerationError, ValueError) as exc:
            self._log.error("تولید تست‌کیسِ سناریو شکست خورد", error=str(exc))
            return self._reply(
                f"Test case generation failed for scenario `{scenario.name}`.\n\n"
                f"Reason: {exc}"
            )

        # ── ۵. ساخت Postman Collection ────────────────────────────────────────
        base_url, extra_variables = self._collection_variables(scenario, specs)
        try:
            collection = self._postman_builder.build(
                suite=suite,
                base_url=base_url,
                collection_name=scenario.name,
                extra_variables=extra_variables,
            )
        except PostmanBuildError as exc:
            self._log.error("ساخت کالکشنِ سناریو شکست خورد", error=str(exc))
            return self._reply(
                f"Building the Postman collection failed for scenario "
                f"`{scenario.name}`.\n\nReason: {exc}"
            )

        # ── ۶. ذخیره‌ی فایل ───────────────────────────────────────────────────
        try:
            output_path = self._save_collection(collection, scenario.name)
        except OSError as exc:
            self._log.error("ذخیره‌ی فایل شکست خورد", error=f"{type(exc).__name__}: {exc}")
            return self._reply(
                "The Postman collection was built but could not be saved.\n\n"
                f"Reason: {type(exc).__name__}: {exc}"
            )
        self._log.info("فایل خروجی ذخیره شد", output_path=str(output_path))

        # ── ۷. خلاصه ──────────────────────────────────────────────────────────
        return self._reply(self._scenario_summary(scenario, steps, suite, output_path))

    def _analyze_sources(self, scenario: Scenario) -> dict[str, ApiSpec]:
        """هر منبعِ Swaggerِ سناریو را یک بار تحلیل می‌کند.

        منابعی که به یک URL اشاره می‌کنند فقط یک بار از شبکه گرفته می‌شوند.
        فرگمنتِ صفحه‌ی swagger-ui حذف می‌شود؛ در حالتِ سناریو فیلترِ فرگمنت اعمال
        نمی‌شود چون هر قدم خودش عملیاتش را مشخص می‌کند.
        """
        by_url: dict[str, ApiSpec] = {}
        specs: dict[str, ApiSpec] = {}
        for source in scenario.swagger_sources:
            url = strip_ui_fragment(source.url)
            if url not in by_url:
                self._log.debug(
                    "تحلیلِ منبعِ Swagger", source=source.name, swagger_url=url
                )
                by_url[url] = self._analyzer.analyze(url)
                self._log.info(
                    "منبعِ Swagger تحلیل شد",
                    source=source.name,
                    title=by_url[url].title,
                    endpoint_count=len(by_url[url].endpoints),
                )
            specs[source.name] = by_url[url]
        return specs

    def _resolve_steps(
        self, scenario: Scenario, specs: dict[str, ApiSpec]
    ) -> tuple[list[ResolvedStep], list[str]]:
        """هر قدم را به یک EndpointSpec واقعی گره می‌زند.

        برمی‌گرداند: (قدم‌های resolve شده، پیام‌های خطا برای قدم‌های ناموفق)
        """
        single_source = len(specs) == 1
        resolved: list[ResolvedStep] = []
        errors: list[str] = []

        for index, step in enumerate(scenario.steps):
            spec = specs.get(step.swagger)
            if spec is None:
                # ScenarioParser این را از قبل گرفته است؛ محضِ احتیاط
                errors.append(
                    f"steps[{index}] ('{step.name}') references unknown swagger source "
                    f"'{step.swagger}'."
                )
                continue

            endpoint = find_endpoint(
                spec.endpoints,
                step.operation.method,
                step.operation.path,
                step.operation.operation_id,
            )
            if endpoint is None:
                errors.append(
                    f"steps[{index}] ('{step.name}'): no operation "
                    f"{step.operation.method} {step.operation.path} was found in "
                    f"swagger source '{step.swagger}'.\n"
                    f"Available operations:\n{describe_endpoints(spec.endpoints)}"
                )
                continue

            self._log.debug(
                "قدم به عملیات گره خورد",
                step=step.name,
                method=endpoint.method,
                path=endpoint.path,
            )
            resolved.append(
                ResolvedStep(
                    step=step,
                    endpoint=endpoint,
                    source_name=step.swagger,
                    base_url=spec.base_url,
                    base_url_var=_base_url_var(step.swagger, single_source),
                )
            )

        return resolved, errors

    @staticmethod
    def _collection_variables(
        scenario: Scenario, specs: dict[str, ApiSpec]
    ) -> tuple[str, list[dict]]:
        """(مقدارِ baseUrl، متغیرهای اضافیِ کالکشن) را برمی‌گرداند.

        با یک منبع، فقط baseUrl تعریف می‌شود. با چند منبع، علاوه بر baseUrl (که
        به منبعِ اول اشاره می‌کند) برای هر منبع یک متغیرِ اختصاصی ساخته می‌شود.
        """
        single_source = len(specs) == 1
        first = scenario.swagger_sources[0].name if scenario.swagger_sources else ""
        base_url = specs[first].base_url if first in specs else ""

        if single_source:
            return base_url, []

        extras = [
            {
                "key": _base_url_var(source.name, single_source=False),
                "value": (specs[source.name].base_url or "").rstrip("/"),
            }
            for source in scenario.swagger_sources
            if source.name in specs
        ]
        return base_url, extras

    # ── کمکی‌ها ───────────────────────────────────────────────────────────────

    def _save_collection(self, collection: dict, title: str) -> Path:
        """کالکشن را با نامی امن در دایرکتوری خروجی ذخیره می‌کند.

        برای جلوگیری از بازنویسیِ یک فایلِ نامرتبط، اگر نامِ پایه از قبل وجود
        داشته باشد یک timestamp به آن اضافه می‌شود.
        """
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        base = _safe_filename(title)
        output_path = _OUTPUT_DIR / f"{base}.postman_collection.json"
        if output_path.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = _OUTPUT_DIR / f"{base}_{stamp}.postman_collection.json"

        with output_path.open("w", encoding="utf-8") as f:
            json.dump(collection, f, ensure_ascii=False, indent=2)

        return output_path

    @staticmethod
    def _count_cases(suite: TestCaseSuite) -> tuple[int, int, int]:
        """(کل، مثبت، منفی) تست‌کیس‌ها را می‌شمارد."""
        total = 0
        positive = 0
        for controller in suite["controllers"]:
            for case in controller["test_cases"]:
                total += 1
                if case["type"] == "positive":
                    positive += 1
        return total, positive, total - positive

    def _summary(self, spec, suite: TestCaseSuite, output_path: Path) -> str:
        """خلاصه‌ی نتیجه را برای کاربر می‌سازد (بدونِ محتوای JSON)."""
        total, positive, negative = self._count_cases(suite)
        return (
            "Postman collection generated successfully.\n\n"
            f"API: {spec.title}\n"
            f"Endpoints: {len(spec.endpoints)}\n"
            f"Test cases: {total}\n"
            f"Positive: {positive}\n"
            f"Negative: {negative}\n\n"
            f"Output:\n{output_path}"
        )

    def _scenario_summary(
        self,
        scenario: Scenario,
        steps: list[ResolvedStep],
        suite: TestCaseSuite,
        output_path: Path,
    ) -> str:
        """خلاصه‌ی حالتِ سناریو — ترتیبِ قدم‌ها و متغیرهای استخراج‌شده."""
        total, positive, negative = self._count_cases(suite)

        lines = [
            "Postman collection generated successfully from the scenario.\n",
            f"Scenario: {scenario.name}",
            f"Swagger sources: {', '.join(scenario.source_names) or '-'}",
            f"Steps: {len(steps)}",
            f"Test cases: {total}",
            f"Positive: {positive}",
            f"Negative: {negative}",
            "",
            "Flow:",
        ]
        lines.extend(
            f"  {index}. {resolved.step.name} — "
            f"{resolved.endpoint.method} {resolved.endpoint.path}"
            for index, resolved in enumerate(steps, start=1)
        )

        extracted = scenario.extracted_variables()
        if extracted:
            lines.append("")
            lines.append("Variables extracted from responses:")
            lines.extend(
                f"  {{{{{extract.variable}}}}} ← {extract.json_path} ({extract.scope})"
                for extract in extracted
            )

        lines.append("")
        lines.append(f"Output:\n{output_path}")
        return "\n".join(lines)

    def _reply(self, content: str) -> dict:
        return {"messages": [AIMessage(content=content, name=self.name)]}

