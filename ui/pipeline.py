"""
ui/pipeline.py — اجرای قدم‌های ویزارد بدونِ Streamlit

    execute_step(step, state)   ← دکمه‌ی «Run Step N» و Run All هر دو همین را صدا می‌زنند
    run_all(state, start=1)     ← قدم‌های start تا ۵ پشتِ سرِ هم، توقف روی اولین خطا

پیش از این ماژول، «ورودی را جمع کن → Generator را صدا بزن → نتیجه یا خطا را
ثبت کن» داخلِ handlerِ دکمه‌ی هر صفحه نوشته شده بود و بدونِ رندرِ صفحه قابلِ
اجرا نبود. حالا همان توالی فقط اینجاست و صفحه‌ها فقط spinner، پیام و rerun را
اضافه می‌کنند. هیچ منطقِ کسب‌وکاری اینجا نیست: Generatorها، پیام‌های خطا و
قواعدِ WorkflowState همان قبلی‌اند.

این ماژول عمداً Streamlit را import نمی‌کند (همان قاعده‌ی ui/workflow.py)، پس
اجرای کاملِ جریان با Generatorهای ساختگی بدونِ UI قابلِ تست است.

پذیرشِ خودکار: Run All بعد از هر نتیجه‌ی قابلِ استفاده ``auto_advance`` را صدا
می‌زند، نه ``approve`` — قدمِ بعد باز می‌شود ولی هیچ قدمی «تأییدشده توسطِ
انسان» ثبت نمی‌شود.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from src.agents.test_case_generator.api_discovery import split_sources
from src.agents.test_case_generator.api_mapping import (
    ApiMappingError,
    Step2ApiMappingGenerator,
    compact_step2_result,
)
from src.agents.test_case_generator.final_review import (
    STATUS_BLOCKED as REVIEW_BLOCKED,
)
from src.agents.test_case_generator.final_review import (
    STATUS_NEEDS_REVIEW as REVIEW_NEEDS_REVIEW,
)
from src.agents.test_case_generator.final_review import (
    STATUS_READY as REVIEW_READY,
)
from src.agents.test_case_generator.final_review import FinalReviewGenerator
from src.agents.test_case_generator.postman_generation import (
    PostmanGenerationError,
    Step4PostmanGenerator,
)
from src.agents.test_case_generator.scenario_analysis import (
    ScenarioAnalysisError,
    Step3ScenarioAnalysisGenerator,
)
from src.agents.test_case_generator.source_selection import (
    SourceSelection,
    select_sources,
)
from src.agents.test_case_generator.source_semantics import SemanticSourceAnalyzer
from src.agents.test_case_generator.swagger_snapshots import (
    SELECTION_AUTO,
    snapshot_fingerprint,
    snapshot_path,
)
from src.agents.test_case_generator.task_analysis import (
    TaskAnalysisError,
    TaskAnalysisGenerator,
)
from src.debug import DebugConfig
from src.task_sources.jira import (
    JiraClient,
    JiraError,
    JiraImport,
    JiraReferenceError,
    JiraSettings,
    import_issue,
    parse_issue_reference,
)
from src.task_sources.jira.importer import STAGE_CONFIGURATION
from ui.formatting import postman_collection
from ui.workflow import (
    INPUT_COLLECTION_NAME,
    INPUT_DEVELOPED_SERVICES,
    INPUT_JIRA_ISSUE,
    INPUT_SWAGGER_SELECTION,
    INPUT_SWAGGER_SNAPSHOTS,
    INPUT_SWAGGER_SOURCES,
    INPUT_TASK_DESCRIPTION,
    RESULT_ARGUMENT,
    STEP_COUNT,
    WorkflowState,
)

# شناسه‌ی کاربری که به‌عنوان هدر x-user-id به سرویسِ LLM فرستاده می‌شود — همان
# مقدارهای قبلیِ صفحه‌ها.
USER_IDS: dict[int, str] = {
    1: "step1-task-analysis-ui",
    2: "step2-api-mapping-ui",
    3: "step3-scenario-analysis-ui",
}

# قدم‌هایی که LLM صدا می‌زنند و مصرفِ توکنشان ثبت می‌شود.
_LLM_STEPS = (1, 2, 3)

# ── نتیجه‌ی اجرای یک قدم ────────────────────────────────────────────────────
STEP_RUNNING = "running"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"
STEP_BLOCKED = "blocked"

# ── نتیجه‌ی Run All ─────────────────────────────────────────────────────────
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_BLOCKED = "blocked"
RUN_INTERRUPTED = "interrupted"
RUN_REJECTED = "rejected"

ProgressCallback = Callable[[int, str], None]


@dataclass(frozen=True)
class StepOutcome:
    """نتیجه‌ی execute_step.

    ``recorded`` یعنی وضعیتِ جریان عوض شد (نتیجه یا خطا ثبت شد). پیش‌نیازِ
    ناقص (مثلاً توصیفِ تسکِ خالی یا نتیجه‌ی بالادستیِ غایب) ثبت نمی‌شود — همان
    رفتارِ قبلیِ دکمه‌ها — و فقط پیامش برمی‌گردد.
    """

    step: int
    status: str
    error: str = ""
    recorded: bool = True

    @property
    def ok(self) -> bool:
        return self.status == STEP_COMPLETED


@dataclass(frozen=True)
class RunOutcome:
    """نتیجه‌ی ساختاریافته‌ی run_all."""

    status: str
    start: int = 1
    completed_steps: tuple[int, ...] = ()
    failed_step: int = 0
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == RUN_SUCCEEDED

    @property
    def last_completed_step(self) -> int:
        return self.completed_steps[-1] if self.completed_steps else 0


# ── فراخوانیِ Generatorها: (نتیجه، خطا) — هیچ استثنایی بیرون نمی‌آید ─────────
# همین توابع را صفحه‌های مستقل هم (داخلِ spinner) صدا می‌زنند، پس فراخوانیِ هر
# Generator و متنِ پیام‌های خطایش فقط یک پیاده‌سازی دارد.

def run_task_analysis(
    task_description: str, developed_services: str
) -> tuple[dict | None, str | None]:
    """قدم ۱ — TaskAnalysisGenerator."""
    try:
        result = TaskAnalysisGenerator(debug_config=DebugConfig.from_env()).generate(
            task_description=task_description,
            user_id=USER_IDS[1],
            developed_services=developed_services,
        )
    except ValueError as exc:
        # ورودیِ نامعتبر — مثلاً توصیفِ تسکِ خالی که پیش از این‌جا گرفته می‌شود
        return None, f"Invalid input: {exc}"
    except TaskAnalysisError as exc:
        # خطا ممکن است بریدگیِ خروجی، JSONِ نامعتبر، یا مغایرت با قرارداد باشد؛
        # پیامِ خودِ خطا دقیق است و نباید با یک برچسبِ ثابت گمراه‌کننده شود.
        return None, f"Task analysis failed:\n\n{exc}"
    except Exception as exc:  # noqa: BLE001 — خطای شبکه/سرویس نباید UI را بترکاند
        return None, f"Task analysis failed: {type(exc).__name__}: {exc}"
    return result, None


def run_api_mapping(
    step1_result: dict,
    services_raw: str,
    snapshot_keys: list[str] | None = None,
) -> tuple[dict | None, str | None]:
    """قدم ۲ — Step2ApiMappingGenerator (snapshotها، یا URL/فایل در صفحه‌ی مستقل)."""
    try:
        result = Step2ApiMappingGenerator(debug_config=DebugConfig.from_env()).generate(
            step1_result=step1_result,
            service_sources=split_sources(services_raw),
            user_id=USER_IDS[2],
            snapshot_keys=snapshot_keys,
        )
    except ValueError as exc:
        # ورودیِ نامعتبر — مثلاً فهرستِ خالیِ سرویس‌ها
        return None, f"Invalid input: {exc}"
    except ApiMappingError as exc:
        # سندِ Swagger خوانده نشد یا نگاشت با قرارداد نخواند
        return None, f"API mapping failed:\n\n{exc}"
    except Exception as exc:  # noqa: BLE001 — خطای شبکه/سرویس نباید UI را بترکاند
        return None, f"API mapping failed: {type(exc).__name__}: {exc}"
    return result, None


def run_scenario_analysis(
    step1_result: dict, step2_result: dict
) -> tuple[dict | None, str | None]:
    """قدم ۳ — قراردادِ فشرده‌ی قدم ۲ و بعد Step3ScenarioAnalysisGenerator.

    کاتالوگِ کاملِ Swagger هرگز وارد این قدم نمی‌شود، وگرنه پرامپت به اندازه‌ای
    بزرگ می‌شود که سرویسِ LLM خطای «Request Entity Too Large» برگرداند.
    """
    try:
        contract = compact_step2_result(step2_result)
    except ApiMappingError as exc:
        return None, f"Scenario analysis failed:\n\n{exc}"

    try:
        result = Step3ScenarioAnalysisGenerator(
            debug_config=DebugConfig.from_env()
        ).generate(
            step1_result=step1_result,
            step2_result=contract,
            user_id=USER_IDS[3],
        )
    except ScenarioAnalysisError as exc:
        # ورودیِ نامعتبر یا خروجیِ ناسازگار با قرارداد
        return None, f"Scenario analysis failed:\n\n{exc}"
    except Exception as exc:  # noqa: BLE001 — خطای شبکه/سرویس نباید UI را بترکاند
        return None, f"Scenario analysis failed: {type(exc).__name__}: {exc}"
    return result, None


def run_postman_generation(
    step1_result: dict,
    step2_result: dict,
    step3_result: dict,
    collection_name: str,
) -> tuple[dict | None, str | None]:
    """قدم ۴ — Step4PostmanGenerator (قطعی، بدونِ LLM)."""
    try:
        result = Step4PostmanGenerator(
            debug_config=DebugConfig.from_env(),
            collection_name=collection_name,
        ).generate(
            step1_result=step1_result,
            step2_result=step2_result,
            step3_result=step3_result,
        )
    except PostmanGenerationError as exc:
        # ورودیِ ناسازگار یا کالکشنِ نامعتبر
        return None, f"Collection generation failed:\n\n{exc}"
    except Exception as exc:  # noqa: BLE001 — خطای غیرمنتظره نباید UI را بترکاند
        return None, f"Collection generation failed: {type(exc).__name__}: {exc}"
    return result, None


def run_final_review(
    step1_result: object,
    step2_result: object,
    step3_result: object,
    step4_result: object,
) -> tuple[dict | None, str | None]:
    """قدم ۵ — FinalReviewGenerator (قطعی، بدونِ LLM)."""
    try:
        result = FinalReviewGenerator(debug_config=DebugConfig.from_env()).generate(
            step1_result=step1_result,
            step2_result=step2_result,
            step3_result=step3_result,
            step4_result=step4_result,
        )
    except Exception as exc:  # noqa: BLE001 — خطای غیرمنتظره نباید UI را بترکاند
        return None, f"Final review failed: {type(exc).__name__}: {exc}"
    return result, None


# ── منابعِ Swagger قدم ۲ ───────────────────────────────────────────────────

def explicit_selection(state: WorkflowState) -> str:
    """انتخابِ دستیِ ذخیره‌شده در قدم ۲ (``admin,crm``) — یا "" یعنی خودکار."""
    stored = state.input_value(INPUT_SWAGGER_SELECTION).strip().lower()
    return "" if stored in ("", SELECTION_AUTO) else stored


def plan_swagger_sources(state: WorkflowState, *, semantic: bool = False) -> SourceSelection:
    """snapshotهای قدم ۲ — source_selection.select_sources روی ورودی‌های قدم ۱.

    انتخابِ دستیِ قدم ۲ جایگزین نمی‌شود (فقط با URLِ Swaggerِ صریح مقایسه
    می‌شود)؛ بدونِ آن انتخاب خودکار است و هیچ fallbackِ ثابتی ندارد — شاهدِ
    ناکافی خطای روشن است.

    ``semantic=True`` تحلیلِ معناییِ متنِ تسک (یک فراخوانیِ LLM، فقط وقتی لازم
    است) را فعال می‌کند — اجرای قدم ۲. پیش‌نمایشِ صفحه‌ی قدم ۲ آن را خاموش
    نگه می‌دارد تا هر rerun یک فراخوانیِ LLM نباشد.
    """
    analyzer = None
    if semantic:
        analyzer = SemanticSourceAnalyzer(
            user_id=USER_IDS[2], debug_config=DebugConfig.from_env()
        ).analyze
    return select_sources(
        state.input_value(INPUT_TASK_DESCRIPTION),
        state.input_value(INPUT_DEVELOPED_SERVICES),
        manual=explicit_selection(state),
        analyze=analyzer,
    )


def _log_selection(selection: SourceSelection) -> None:
    """خلاصه‌ی ساختاریافته‌ی انتخاب — بدونِ URLهای ورودی و بدونِ اعتبارنامه."""
    log = DebugConfig.from_env().get_logger("source_selection")
    if selection.error:
        log.warning("انتخابِ منبعِ Swagger ممکن نشد", **selection.to_dict())
    else:
        log.info("منابعِ Swagger انتخاب شدند", **selection.to_dict())


# ── ورودیِ تسک از Jira ─────────────────────────────────────────────────────
# یک کلاینت (یعنی یک نشستِ واردشده) برای هر تنظیماتِ Jira، در حافظه‌ی همین
# پروسه؛ دریافت‌های بعدی دوباره وارد نمی‌شوند. قفل دریافت‌های هم‌زمانِ
# نشست‌های مختلفِ Streamlit را پشتِ هم می‌کند (requests.Session امنِ چندرشته‌ای نیست).
_jira_clients: dict[JiraSettings, JiraClient] = {}
_jira_lock = threading.Lock()


def _jira_client(settings: JiraSettings) -> JiraClient:
    client = _jira_clients.get(settings)
    if client is None:
        _jira_clients.clear()  # تنظیماتِ تازه — نشستِ قبلی دیگر خرج نمی‌شود
        client = _jira_clients[settings] = JiraClient(settings)
    return client


def load_jira_task(state: WorkflowState, reference: str) -> JiraImport:
    """یک issue را دریافت و — فقط اگر موفق بود — به ورودیِ قدمِ اول تبدیل می‌کند.

    متنِ ساختاریافته‌ی TaskInput همان «Task Description» می‌شود و کلیدِ issue در
    INPUT_JIRA_ISSUE ثبت می‌شود؛ پس قدمِ اول، Run All و کهنه‌شدنِ نتیجه‌ها همان
    سازوکارِ ورودیِ دستی را دارند. دریافتِ ناموفق وضعیت را دست نمی‌زند.
    """
    with _jira_lock:
        try:
            settings = JiraSettings.from_config().validate()
        except JiraError as exc:
            result = JiraImport(reference=reference).fail(STAGE_CONFIGURATION, exc)
        else:
            result = import_issue(reference, settings=settings, client=_jira_client(settings))
    _log_jira_import(result)
    if result.ok:
        state.task_import = result
        state.set_input(INPUT_TASK_DESCRIPTION, result.task_input.to_task_description())
        state.set_input(INPUT_JIRA_ISSUE, result.issue_key)
    return result


def jira_input_problem(state: WorkflowState, reference: str) -> str:
    """چرا محتوای Jiraِ فعلی برای این کلید/نشانی قابلِ استفاده نیست — یا "".

    جلوی اجرای قدمِ اول با محتوای issueِ قبلی را می‌گیرد وقتی کاربر کلید را
    عوض کرده ولی هنوز دریافتش نکرده است.
    """
    imported = state.task_import
    try:
        key = parse_issue_reference(reference)
    except JiraReferenceError as exc:
        return str(exc)
    if imported is None or not imported.ok:
        return f"Fetch {key} from Jira before running Step 1."
    if key != imported.issue_key:
        return (
            f"The Task Description holds {imported.issue_key}, but the Jira field now says "
            f"{key}. Fetch {key} first, or switch back to {imported.issue_key}."
        )
    return ""


def _log_jira_import(result: JiraImport) -> None:
    """خلاصه‌ی دریافت — بدونِ هویت، متن، کوکی یا هیچ رازی."""
    log = DebugConfig.from_env().get_logger("jira_import")
    task = result.task_input
    details = {
        "issue_key": result.issue_key,
        "ok": result.ok,
        "stage": result.error_stage,
        "comments": len(task.comments) if task else 0,
        "attachments": [item.status for item in task.attachments] if task else [],
    }
    if result.ok:
        log.info("issueِ Jira دریافت شد", **details)
    else:
        log.warning("دریافتِ issueِ Jira ممکن نشد", error=result.error, **details)


# ── بررسیِ قابلِ استفاده بودنِ خروجی ───────────────────────────────────────

# کلیدی که هر قدم حتماً باید برگرداند تا قدمِ بعد چیزی برای مصرف داشته باشد.
_REQUIRED_SHAPE: dict[int, tuple[str, type]] = {
    1: ("test_cases", list),
    2: ("mappings", list),
    3: ("scenarios", list),
    4: ("collection", dict),
    5: ("review", dict),
}

_REVIEW_STATUSES = (REVIEW_READY, REVIEW_NEEDS_REVIEW, REVIEW_BLOCKED)


def unusable_reason(step: int, result: Any) -> str:
    """چرا نتیجه‌ای که Generator بدونِ استثنا برگردانده قابلِ استفاده نیست — یا "".

    Generatorها خودشان قرارداد را اعتبارسنجی می‌کنند؛ این فقط آخرین سدِ شکل
    است تا «برگشت بدونِ استثنا» به‌تنهایی موفقیت حساب نشود.
    """
    if not isinstance(result, dict) or not result:
        return f"Step {step} returned no result."
    key, kind = _REQUIRED_SHAPE[step]
    if not isinstance(result.get(key), kind):
        return f"Step {step} returned a result without a valid '{key}' section."
    if step == 4 and not result["collection"]:
        return "Step 4 returned an empty Postman collection."
    if step == 5:
        if result["review"].get("status") not in _REVIEW_STATUSES:
            return (
                f"Step 5 returned an unknown review status "
                f"{result['review'].get('status')!r}."
            )
        if not postman_collection(result):
            return "Step 5 did not carry the Step 4 collection in its artifacts."
    return ""


def blocked_reason(step: int, result: Any) -> str:
    """پیامِ «مسدود» اگر نتیجه خودش بگوید قابلِ استفاده نیست — یا ""."""
    if step != STEP_COUNT:
        return ""
    review = result.get("review") or {}
    if review.get("status") != REVIEW_BLOCKED:
        return ""
    issues = review.get("issues") or []
    summary = str(review.get("summary") or "").strip()
    message = (
        f"The final review is blocked by {len(issues)} issue(s). The collection is "
        "not a valid final artifact — see Step 5 for the issues."
    )
    return f"{message}\n\n{summary}" if summary else message


# ── اجرای یک قدم ────────────────────────────────────────────────────────────

_MISSING_UPSTREAM: dict[int, str] = {
    2: "The Step 1 result is not available yet. Run Step 1 and approve it first.",
    3: "The Step 1 and Step 2 results are not available yet. Run them and approve "
    "Step 2 first.",
    4: "The Step 1–3 results are not available yet. Run them and approve Step 3 first.",
    5: "The Step 1–4 results are not available yet. Run them and approve Step 4 first.",
}


def _upstream(step: int) -> list[str]:
    return [RESULT_ARGUMENT[source] for source in range(1, step)]


def _invoke(
    step: int,
    state: WorkflowState,
    payload: dict[str, Any],
    snapshot_keys: tuple[str, ...] = (),
) -> tuple[dict | None, str | None]:
    """Generatorِ همان قدم را با ورودیِ جمع‌شده صدا می‌زند → (نتیجه، خطا)."""
    upstream = [payload[name] for name in _upstream(step)]
    if step == 1:
        return run_task_analysis(
            state.input_value(INPUT_TASK_DESCRIPTION),
            state.input_value(INPUT_DEVELOPED_SERVICES),
        )
    if step == 2:
        return run_api_mapping(upstream[0], "", snapshot_keys=list(snapshot_keys))
    if step == 3:
        return run_scenario_analysis(*upstream)
    if step == 4:
        return run_postman_generation(*upstream, state.input_value(INPUT_COLLECTION_NAME))
    return run_final_review(*upstream)


def execute_step(
    step: int, state: WorkflowState, *, plan: SourceSelection | None = None
) -> StepOutcome:
    """قدمِ ``step`` را روی وضعیتِ جریان اجرا و نتیجه یا خطا را ثبت می‌کند.

    ورودی‌های دستیِ قدم (توصیفِ تسک، انتخابِ Swagger، نامِ کالکشن) باید پیش از
    این در وضعیت گذاشته شده باشند — کارِ UI یا فراخوان.

      * پیش‌نیازِ ناقص → ``recorded=False``؛ وضعیت دست نمی‌خورد (رفتارِ قبلی).
      * استثنای Generator یا خروجیِ غیرِقابلِ‌استفاده → ``record_error``.
      * قدم ۵ با status=blocked → نتیجه ثبت و «مسدود» علامت می‌خورد.
      * در غیرِ این صورت → ``record_result``.

    ``plan`` انتخابِ منابعِ Swaggerِ ازپیش‌ساخته‌ی قدم ۲ است؛ بدونِ آن همین‌جا
    (با تحلیلِ معنایی) یک بار ساخته می‌شود. مصرفِ توکنِ تحلیلِ معنایی جزوِ
    مصرفِ قدم ۲ ثبت می‌شود.

    تأیید یا پذیرشِ قدم کارِ این تابع نیست.
    """
    if not 1 <= step <= STEP_COUNT:
        raise ValueError(f"Unknown step {step}.")

    if step == 1 and not state.input_value(INPUT_TASK_DESCRIPTION).strip():
        return StepOutcome(1, STEP_FAILED, "Task Description is required.", recorded=False)

    payload = state.step_inputs(step)
    if any(name not in payload for name in _upstream(step)):
        return StepOutcome(step, STEP_FAILED, _MISSING_UPSTREAM[step], recorded=False)

    # مصرفِ توکنِ همین اجرا ثبت می‌شود (همه‌ی دسته‌ها و تلاش‌ها، و در قدم ۲
    # تحلیلِ معناییِ منابع)؛ مصرفِ اجرای قبلی جایگزین می‌شود تا دوباره شمرده نشود.
    with state.track_usage(step) if step in _LLM_STEPS else nullcontext():
        snapshot_keys: tuple[str, ...] = ()
        if step == 2:
            plan = plan or plan_swagger_sources(state, semantic=True)
            state.swagger_selection = plan
            _log_selection(plan)
            snapshot_keys = plan.keys
            if plan.error:
                state.record_error(2, plan.error)
                return StepOutcome(2, STEP_FAILED, plan.error)
            # مسیرهای خوانده‌شده و اثرانگشتشان هم ثبت می‌شوند تا «منابعِ Swagger»
            # معنای همیشگی‌اش را نگه دارد و کهنه‌شدن درست گزارش شود.
            state.set_input(
                INPUT_SWAGGER_SOURCES,
                "\n".join(str(snapshot_path(key)) for key in plan.keys),
            )
            state.set_input(INPUT_SWAGGER_SNAPSHOTS, snapshot_fingerprint(list(plan.keys)))
        result, error = _invoke(step, state, payload, snapshot_keys)

    if not error:
        error = unusable_reason(step, result)
    if error:
        state.record_error(step, error)
        return StepOutcome(step, STEP_FAILED, error)

    state.record_result(step, result)
    blocked = blocked_reason(step, result)
    if blocked:
        state.mark_blocked(step)
        return StepOutcome(step, STEP_BLOCKED, blocked)
    return StepOutcome(step, STEP_COMPLETED)


# ── Run All ─────────────────────────────────────────────────────────────────

ALREADY_RUNNING = "Run All is already in progress — wait for it to finish."


def _notify(on_progress: ProgressCallback | None, step: int, status: str) -> None:
    if on_progress is not None:
        on_progress(step, status)


def _start_error(state: WorkflowState, start: int) -> RunOutcome | None:
    """آیا قدم‌های پیش از start نتیجه‌ی جاری دارند؟ (ازسرگیری فقط روی نتیجه‌ی معتبر)"""
    if start == 1:
        if not state.input_value(INPUT_TASK_DESCRIPTION).strip():
            return RunOutcome(
                RUN_FAILED, start, failed_step=1, error="Task Description is required."
            )
        return None
    for step in range(1, start):
        if not state.has_result(step) or state.is_out_of_date(step) or state.is_blocked(step):
            return RunOutcome(
                RUN_FAILED,
                start,
                failed_step=step,
                error=(
                    f"Step {step} has no current result, so Run All cannot start at "
                    f"Step {start}. Start from Step {step} instead."
                ),
            )
    return None


def run_all(
    state: WorkflowState,
    *,
    start: int = 1,
    on_progress: ProgressCallback | None = None,
) -> RunOutcome:
    """قدم‌های ``start`` تا ۵ را پشتِ سرِ هم اجرا می‌کند.

    * همه‌چیز ترتیبی است؛ هیچ قدمِ وابسته‌ای هم‌زمان اجرا نمی‌شود.
    * پیش از شروع، نتایجِ قدمِ start به بعد «کهنه» می‌شوند تا نتیجه‌ی اجرای
      قبلی هرگز نتیجه‌ی این اجرا دیده نشود.
    * بعد از هر نتیجه‌ی قابلِ استفاده ``auto_advance`` — نه تأییدِ انسانی.
    * اولین خطا اجرا را متوقف می‌کند؛ قدم‌های بعدی اجرا نمی‌شوند و نتیجه‌ی
      قبلی‌شان کهنه می‌ماند.
    * قدم ۵ با status=blocked موفقیت نیست (``RUN_BLOCKED``).
    * اجرای دوم روی همان وضعیت تا پایانِ اولی رد می‌شود (``RUN_REJECTED``).
    * منابعِ Swagger همیشه خودکار انتخاب می‌شوند: انتخابِ دستیِ ذخیره‌شده از
      اجرای قبلیِ قدم ۲ در شروع پاک می‌شود و انتخاب در هر اجرا فقط یک بار (در
      قدم ۲، با تحلیلِ معنایی) ساخته می‌شود؛ قدم‌های ۳ تا ۵ فقط نتیجه‌ی قدم ۲ را
      می‌خوانند، پس کلِ اجرا همان مجموعه را می‌بیند.

    ``on_progress(step, status)`` پیش و پس از هر قدم صدا زده می‌شود (برای UI).
    اگر اجرا وسطِ کار با یک استثنا قطع شود (مثلاً rerunِ Streamlit)، قفل آزاد و
    ``RUN_INTERRUPTED`` ثبت می‌شود و استثنا دوباره بالا می‌رود.
    """
    if not 1 <= start <= STEP_COUNT:
        raise ValueError(f"Run All must start between Step 1 and Step {STEP_COUNT}.")
    if state.run_in_progress:
        return RunOutcome(RUN_REJECTED, start, error=ALREADY_RUNNING)

    problem = _start_error(state, start)
    if problem is not None:
        state.last_run = problem
        return problem

    state.run_in_progress = True
    state.last_run = None
    completed: list[int] = []
    outcome: RunOutcome | None = None
    try:
        # قدم‌های پیش از start نتیجه‌ی جاری دارند (بالا بررسی شد)؛ اگر هنوز کسی
        # نپذیرفته‌شان، پذیرشِ خودکار قدمِ start را باز می‌کند.
        for step in range(1, start):
            state.auto_advance(step)
        if start <= 2:
            state.set_input(INPUT_SWAGGER_SELECTION, SELECTION_AUTO)
        state.invalidate_from(start)

        for step in range(start, STEP_COUNT + 1):
            state.go_to(step)
            state.running_step = step
            _notify(on_progress, step, STEP_RUNNING)
            result = execute_step(step, state)
            state.running_step = 0
            _notify(on_progress, step, result.status)

            if result.status == STEP_FAILED:
                state.current_step = step
                outcome = RunOutcome(RUN_FAILED, start, tuple(completed), step, result.error)
                break
            if result.status == STEP_BLOCKED:
                outcome = RunOutcome(RUN_BLOCKED, start, tuple(completed), step, result.error)
                break
            state.auto_advance(step)
            completed.append(step)
        else:
            outcome = RunOutcome(RUN_SUCCEEDED, start, tuple(completed))
    finally:
        interrupted_at = state.running_step
        state.running_step = 0
        state.run_in_progress = False
        if outcome is None:
            outcome = RunOutcome(
                RUN_INTERRUPTED,
                start,
                tuple(completed),
                interrupted_at,
                "Run All was interrupted before it finished. Results of the steps "
                "that did not run are out of date — run again.",
            )
        # پیامِ «Step N was changed…» ویژه‌ی اجرای دستی است؛ نتیجه‌ی Run All خودش
        # گزارش می‌شود و قدم‌های کهنه در نوار علامت دارند.
        state.notice = ""
        state.last_run = outcome
    return outcome


__all__ = [
    "ALREADY_RUNNING",
    "RUN_BLOCKED",
    "RUN_FAILED",
    "RUN_INTERRUPTED",
    "RUN_REJECTED",
    "RUN_SUCCEEDED",
    "STEP_BLOCKED",
    "STEP_COMPLETED",
    "STEP_FAILED",
    "STEP_RUNNING",
    "USER_IDS",
    "RunOutcome",
    "StepOutcome",
    "blocked_reason",
    "execute_step",
    "explicit_selection",
    "jira_input_problem",
    "load_jira_task",
    "plan_swagger_sources",
    "run_all",
    "run_api_mapping",
    "run_final_review",
    "run_postman_generation",
    "run_scenario_analysis",
    "run_task_analysis",
    "unusable_reason",
]
