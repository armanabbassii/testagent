"""
ui/workflow.py — دفترِ وضعیتِ ویزاردِ پنج‌قدمی (بدونِ Streamlit)

این ماژول «تنها منبعِ حقیقت»ی ویزارد است: کدام قدم تأیید شده، نتیجه‌ی هر قدم
چیست، قدمِ بعدی چه زمانی باز می‌شود، وقتی نتیجه‌ی یک قدم عوض می‌شود کدام نتایجِ
پایین‌دستی بی‌اعتبار می‌شوند و چه ورودی‌هایی از قبل در جریان نگه داشته شده‌اند.

هیچ منطقِ کسب‌وکاریِ قدم‌ها اینجا نیست — نه فراخوانیِ LLM، نه کشفِ API، نه ساختِ
Postman. این ماژول فقط وضعیت را نگه می‌دارد و قواعدِ ناوبری/بی‌اعتبارسازی را
اجرا می‌کند. عمداً هیچ وابستگی‌ای به Streamlit ندارد تا همه‌ی این قواعد بدونِ
اجرای UI قابلِ تست باشند.

قواعد:

  * قدم ۱ همیشه باز است؛ قدمِ n فقط با «تأییدِ قدمِ n-1» باز می‌شود — یا با
    پذیرشِ خودکارِ Run All (auto_advance) که جدا از تأییدِ انسانی ثبت می‌شود.
    پرش به قدمی که پیش‌نیازش وجود ندارد ممکن نیست.
  * تأیید بدونِ نتیجه ممکن نیست و روی نتیجه‌ی «کهنه» هم ممکن نیست.
  * نتیجه‌ی تازه برای یک قدم (اگر با نتیجه‌ی قبلی‌اش یکی نباشد) همه‌ی نتایجِ
    قدم‌های بعدی را «کهنه» می‌کند: تأییدشان لغو می‌شود ولی خودِ نتیجه پاک
    نمی‌شود تا کاربر بتواند ببیند چه چیزی کهنه شده. تا اجرای دوباره، آن نتیجه
    نه قابلِ تأیید است و نه به قدمِ بعد پاس داده می‌شود.
  * خطای یک قدم نتیجه‌ی همان قدم را برمی‌دارد، کاربر را روی همان قدم نگه
    می‌دارد و قدمِ بعدی را باز نمی‌کند — پس «تأیید و ادامه» بعد از یک اجرای
    ناموفق ممکن نیست.
  * ورودی‌هایی که کاربر یک‌بار می‌دهد (توصیفِ تسک، منابعِ Swagger، نامِ
    کالکشن) در وضعیت می‌مانند و دوباره پرسیده نمی‌شوند. عوض‌کردنِ یک ورودی
    تا وقتی آن قدم دوباره اجرا نشود چیزی را باطل نمی‌کند، ولی ناسازگاری‌اش
    از طریقِ pending_input_changes() دیده و گزارش می‌شود.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from src.token_usage import TokenUsage, collect_usage

# تعدادِ قدم‌های جریان. مرزهای ناوبری از همین عدد مشتق می‌شوند.
STEP_COUNT = 5

STEP_TITLES: dict[int, str] = {
    1: "Task Analysis",
    2: "Swagger Analysis & API Mapping",
    3: "Scenario & Dependency Analysis",
    4: "Postman Collection Generation",
    5: "Final Review & Export",
}

STEP_ICONS: dict[int, str] = {1: "🧪", 2: "🔗", 3: "🧩", 4: "📦", 5: "✅"}

# ── ورودی‌های کاربر ──────────────────────────────────────────────────────────
INPUT_TASK_DESCRIPTION = "task_description"
INPUT_DEVELOPED_SERVICES = "developed_services"
INPUT_SWAGGER_SOURCES = "swagger_sources"
INPUT_SWAGGER_SELECTION = "swagger_selection"
INPUT_SWAGGER_SNAPSHOTS = "swagger_snapshots"
INPUT_COLLECTION_NAME = "collection_name"
# کلیدِ issueِ Jira که توصیفِ تسک از آن ساخته شده ("" یعنی توصیفِ دستی).
INPUT_JIRA_ISSUE = "jira_issue"

INPUT_LABELS: dict[str, str] = {
    INPUT_TASK_DESCRIPTION: "Task Description",
    INPUT_JIRA_ISSUE: "Jira issue",
    INPUT_DEVELOPED_SERVICES: "Developed Services",
    INPUT_SWAGGER_SOURCES: "Swagger / OpenAPI sources",
    INPUT_SWAGGER_SELECTION: "Swagger source selection",
    INPUT_SWAGGER_SNAPSHOTS: "Swagger snapshot contents",
    INPUT_COLLECTION_NAME: "Collection name",
}

# ── نامِ نتیجه‌ها در قراردادِ قدم‌ها ──────────────────────────────────────────
# همان نامی که generate(...) هر قدم به‌عنوان آرگومان می‌گیرد.
RESULT_ARGUMENT: dict[int, str] = {
    1: "step1_result",
    2: "step2_result",
    3: "step3_result",
    4: "step4_result",
}

# نتیجه‌هایی که هر قدم خودکار از قدم‌های قبلی می‌گیرد (بدونِ کپی/پیست).
_PROPAGATED: dict[int, tuple[int, ...]] = {
    2: (1,),
    3: (1, 2),
    4: (1, 2, 3),
    5: (1, 2, 3, 4),
}

# ورودی‌هایی که کاربر مستقیم وارد می‌کند و به همان قدم تعلق دارند.
# انتخابِ منبعِ Swagger هم ورودیِ خودِ قدم دوم است: عوض‌کردنش نتیجه را کهنه
# اعلام می‌کند، ولی عمداً در _REQUIRED_INPUTS نیست — انتخابگر همیشه مقدارِ
# پیش‌فرض دارد و هرگز «کم» حساب نمی‌شود.
# اثرانگشتِ محتوای snapshotها هم ورودیِ قدم دوم است: وقتی دکمه‌ی Update (یا
# ویرایشِ دستی) فایلی را بعد از اجرای نگاشت عوض کند، نتیجه‌ی فعلی متعلق به سندِ
# قدیمی گزارش می‌شود — بدونِ اجرای خودکارِ دوباره‌ی نگاشت.
# URLهای Swagger در Developed Services تعیین می‌کنند قدم دوم کدام snapshot را
# بخواند (ui/pipeline.py)، پس همین متن ورودیِ قدم دوم هم هست.
# کلیدِ issueِ Jira هم ورودیِ قدمِ اول است: issueِ دیگری یعنی نتیجه‌ی فعلی کهنه
# است، حتی اگر متنِ توصیف تصادفاً یکی باشد.
_OWN_INPUTS: dict[int, tuple[str, ...]] = {
    1: (INPUT_TASK_DESCRIPTION, INPUT_DEVELOPED_SERVICES, INPUT_JIRA_ISSUE),
    2: (
        INPUT_SWAGGER_SOURCES,
        INPUT_SWAGGER_SELECTION,
        INPUT_SWAGGER_SNAPSHOTS,
        INPUT_DEVELOPED_SERVICES,
    ),
    4: (INPUT_COLLECTION_NAME,),
}

# زیرمجموعه‌ی اجباریِ همان ورودی‌ها — بقیه اختیاری‌اند.
_REQUIRED_INPUTS: dict[int, tuple[str, ...]] = {
    1: (INPUT_TASK_DESCRIPTION,),
    2: (INPUT_SWAGGER_SOURCES,),
}

# ── وضعیت‌های نمایشیِ هر قدم ─────────────────────────────────────────────────
STATUS_COMPLETED = "completed"
# نتیجه را Run All پذیرفته و قدمِ بعد را باز کرده — هنوز هیچ انسانی تأییدش نکرده.
STATUS_AUTO_COMPLETED = "auto_completed"
STATUS_CURRENT = "current"
STATUS_AVAILABLE = "available"
STATUS_STALE = "stale"
STATUS_LOCKED = "locked"
STATUS_RUNNING = "running"
STATUS_FAILED = "failed"
# نتیجه ثبت شده ولی خودش می‌گوید قابلِ استفاده نیست (قدم ۵ با status=blocked).
STATUS_BLOCKED = "blocked"

STATUS_MARKERS: dict[str, str] = {
    STATUS_COMPLETED: "✅",
    STATUS_AUTO_COMPLETED: "☑️",
    STATUS_CURRENT: "🔵",
    STATUS_AVAILABLE: "⚪",
    STATUS_STALE: "⚠️",
    STATUS_LOCKED: "🔒",
    STATUS_RUNNING: "⏳",
    STATUS_FAILED: "❌",
    STATUS_BLOCKED: "⛔",
}

STATUS_LABELS: dict[str, str] = {
    STATUS_COMPLETED: "done",
    STATUS_AUTO_COMPLETED: "done by Run All — not reviewed",
    STATUS_CURRENT: "you are here",
    STATUS_AVAILABLE: "ready to run",
    STATUS_STALE: "out of date",
    STATUS_LOCKED: "locked",
    STATUS_RUNNING: "running",
    STATUS_FAILED: "failed",
    STATUS_BLOCKED: "blocked",
}

# نشانه‌ی «این کلید وجود ندارد» — تا None با «ثبت‌نشده» قاطی نشود.
_MISSING = object()


def _step_range(steps: list[int]) -> str:
    """«2, 3, 4, 5» → «2–5» و در غیرِ این صورت فهرستِ مرتب."""
    ordered = sorted(steps)
    if len(ordered) == 1:
        return str(ordered[0])
    if ordered == list(range(ordered[0], ordered[-1] + 1)):
        return f"{ordered[0]}–{ordered[-1]}"
    return ", ".join(str(step) for step in ordered)


def invalidated_message(changed: int, invalidated: list[int]) -> str:
    """پیامِ کهنه‌شدن — مثلاً «Step 1 was changed. Steps 2–5 need to be regenerated.»"""
    ordered = sorted(invalidated)
    if len(ordered) == 1:
        target = f"Step {ordered[0]} needs"
    else:
        target = f"Steps {_step_range(ordered)} need"
    return f"Step {changed} was changed. {target} to be regenerated."


def stale_message(step: int) -> str:
    """چرا نتیجه‌ی فعلی قابلِ تأیید نیست."""
    return (
        f"The result below is from an earlier run of Step {step} and is out of "
        f"date. Run Step {step} again before approving it."
    )


def error_retry_message(step: int) -> str:
    """بعد از خطا — کاربر روی همان قدم می‌ماند و می‌تواند دوباره تلاش کند."""
    if step >= STEP_COUNT:
        return (
            f"Step {step} did not finish. Fix the input if needed and run Step "
            f"{step} again."
        )
    return (
        f"Step {step} did not finish, so Step {step + 1} stays locked. Fix the "
        f"input if needed and run Step {step} again."
    )


def propagated_steps(step: int) -> tuple[int, ...]:
    """قدم‌هایی که نتیجه‌شان خودکار به این قدم داده می‌شود."""
    return _PROPAGATED.get(step, ())


def approve_label(step: int) -> str:
    """برچسبِ دکمه‌ی تأیید — قدمِ آخر چیزی برای ادامه دادن ندارد."""
    if step >= STEP_COUNT:
        return "Approve Step 5 — finish"
    return f"Approve & Continue to Step {step + 1}"


@dataclass
class WorkflowState:
    """وضعیتِ کاملِ ویزارد.

    این کلاس هیچ ورودی/خروجیِ Streamlit ای ندارد؛ فقط داده و قاعده است.
    """

    current_step: int = 1
    results: dict[int, Any] = field(default_factory=dict)
    approved: set[int] = field(default_factory=set)
    stale: set[int] = field(default_factory=set)
    errors: dict[int, str] = field(default_factory=dict)
    inputs: dict[str, str] = field(default_factory=dict)
    # ورودی‌هایی که هر قدم در لحظه‌ی آخرین اجرایش دیده بود.
    used_inputs: dict[int, dict[str, str]] = field(default_factory=dict)
    # مصرفِ توکنِ آخرین اجرای هر قدم — جدا از نتیجه‌های کسب‌وکاری.
    usage: dict[int, TokenUsage] = field(default_factory=dict)
    notice: str = ""
    # قدم‌هایی که Run All بعد از نتیجه‌ی قابلِ استفاده پذیرفته است. جدا از
    # approved نگه داشته می‌شود تا پیشرفتِ خودکار هرگز «تأییدِ انسانی» دیده
    # نشود؛ هر دو قدمِ بعد را باز می‌کنند.
    auto_advanced: set[int] = field(default_factory=set)
    # قدم‌هایی که نتیجه دارند ولی آن نتیجه خودش «مسدود» است (قدم ۵: blocked).
    blocked: set[int] = field(default_factory=set)
    # اجرای Run All: قدمِ در حالِ اجرا (۰ یعنی هیچ)، قفلِ هم‌پوشانی و آخرین نتیجه.
    running_step: int = 0
    run_in_progress: bool = False
    last_run: Any = None
    # آخرین انتخابِ منابعِ Swaggerِ قدم ۲ (source_selection.SourceSelection):
    # منابعِ انتخاب‌شده، شواهد و هشدارها — برای نمایش و اشکال‌زدایی، نه ورودی.
    swagger_selection: Any = None
    # آخرین issueِ Jiraِ با موفقیت دریافت‌شده (src.task_sources.jira.JiraImport):
    # برای نمایشِ وضعیتِ استخراج و تشخیصِ issueِ کهنه — متنِ خودش در
    # INPUT_TASK_DESCRIPTION است، نه این‌جا.
    task_import: Any = None

    # ── پرسش‌های پایه ────────────────────────────────────────────────────────
    def result(self, step: int) -> Any:
        """نتیجه‌ی یک قدم (یا None اگر اجرا نشده باشد)."""
        return self.results.get(step)

    def has_result(self, step: int) -> bool:
        return step in self.results

    def is_approved(self, step: int) -> bool:
        return step in self.approved

    def is_auto_advanced(self, step: int) -> bool:
        """Run All این قدم را پذیرفته و هنوز هیچ انسانی تأییدش نکرده است."""
        return step in self.auto_advanced and step not in self.approved

    def is_accepted(self, step: int) -> bool:
        """تأییدِ انسانی یا پذیرشِ Run All — هر کدام قدمِ بعد را باز می‌کند."""
        return step in self.approved or step in self.auto_advanced

    def is_blocked(self, step: int) -> bool:
        return step in self.blocked

    def is_stale(self, step: int) -> bool:
        return step in self.stale

    def error(self, step: int) -> str:
        return self.errors.get(step, "")

    def input_value(self, name: str) -> str:
        return self.inputs.get(name, "")

    # ── مصرفِ توکن ───────────────────────────────────────────────────────────
    def usage_for(self, step: int) -> TokenUsage | None:
        """مصرفِ آخرین اجرای یک قدم، یا ``None`` اگر هنوز اجرا نشده باشد."""
        return self.usage.get(step)

    def add_usage(self, step: int, usage: TokenUsage) -> None:
        """یک فراخوانیِ LLM را به مصرفِ همان قدم اضافه می‌کند.

        دسته‌ها، تلاش‌های دوباره و همه‌ی فراخوانی‌های یک اجرا این‌جا جمع
        می‌شوند. خودِ فراخوانی قبلاً در ``LLMClient.chat`` یک بار شمرده شده
        است؛ این‌جا فقط جمعِ همان گزارش‌هاست.
        """
        current = self.usage.get(step) or TokenUsage()
        self.usage[step] = current.combine(usage)

    @contextmanager
    def track_usage(self, step: int) -> Iterator[None]:
        """مصرفِ یک اجرای این قدم را ثبت می‌کند.

        سیاستِ اجرای دوباره: مصرفِ اجرای قبلیِ همین قدم جایگزین می‌شود، نه
        جمع — اجرای دوباره نباید مصرفِ قبلی را دوبار حساب کند. اگر اجرای تازه
        وسطِ کار خطا بدهد، هر چه تا همان لحظه مصرف کرده است می‌ماند (هر
        فراخوانی بلافاصله نوشته می‌شود)؛ ولی اگر هیچ فراخوانی‌ای نکرده باشد،
        عددِ اجرای قبلی به آن نسبت داده نمی‌شود.
        """
        self.usage.pop(step, None)
        with collect_usage(lambda usage: self.add_usage(step, usage)):
            yield

    # ── قفل، وضعیت و ناوبری ─────────────────────────────────────────────────
    def is_unlocked(self, step: int) -> bool:
        """قدم ۱ همیشه باز است؛ بقیه با تأیید (یا پذیرشِ Run All) قدمِ قبلی باز می‌شوند."""
        if not 1 <= step <= STEP_COUNT:
            return False
        if step == 1:
            return True
        return self.is_accepted(step - 1)

    def is_out_of_date(self, step: int) -> bool:
        """نتیجه‌ی این قدم دیگر به ورودی‌های فعلی تعلق ندارد.

        یا خودش کهنه است، یا ورودیِ خودش یا یکی از قدم‌های بالادستش بعد از
        اجرا عوض شده است — چون این نتیجه از همان نتیجه‌ی بالادستی ساخته شده.
        """
        if not self.has_result(step):
            return False
        if self.is_stale(step):
            return True
        return any(
            self.has_result(source) and self.pending_input_changes(source)
            for source in range(1, step + 1)
        )

    def status(self, step: int) -> str:
        """وضعیتِ نمایشی — در حالِ اجرا > خطا > کهنه > مسدود > قفل > جاری > تمام‌شده > آماده."""
        if not 1 <= step <= STEP_COUNT:
            return STATUS_LOCKED
        if step == self.running_step:
            return STATUS_RUNNING
        if self.error(step):
            return STATUS_FAILED
        if self.is_out_of_date(step):
            return STATUS_STALE
        if self.is_blocked(step) and self.has_result(step):
            return STATUS_BLOCKED
        if not self.is_unlocked(step):
            return STATUS_LOCKED
        if step == self.current_step:
            return STATUS_CURRENT
        if self.has_result(step) and self.is_approved(step):
            return STATUS_COMPLETED
        if self.has_result(step) and self.is_auto_advanced(step):
            return STATUS_AUTO_COMPLETED
        return STATUS_AVAILABLE

    def statuses(self) -> list[tuple[int, str]]:
        """(شماره‌ی قدم، وضعیت) برای هر پنج قدم — برای نوارِ پیشرفت."""
        return [(step, self.status(step)) for step in range(1, STEP_COUNT + 1)]

    def completed_count(self) -> int:
        """قدم‌هایی که نتیجه دارند، تأیید شده‌اند و کهنه نیستند."""
        return sum(
            1
            for step in range(1, STEP_COUNT + 1)
            if self.has_result(step) and self.is_approved(step) and not self.is_stale(step)
        )

    def auto_completed_count(self) -> int:
        """قدم‌هایی که Run All پذیرفته و هنوز کسی تأییدشان نکرده (کهنه نیستند)."""
        return sum(
            1
            for step in range(1, STEP_COUNT + 1)
            if self.has_result(step) and self.is_auto_advanced(step) and not self.is_stale(step)
        )

    def failed_steps(self) -> list[int]:
        """قدم‌هایی که آخرین اجرایشان خطا داده است."""
        return sorted(self.errors)

    def missing_inputs(self, step: int) -> list[str]:
        """برچسب‌های انسانیِ چیزهایی که برای اجرای این قدم کم است.

        نتیجه‌ی «کهنه» حساب نمی‌شود: تا قدمِ بالادستی دوباره اجرا نشود، این
        قدم نباید با داده‌ی بی‌اعتبار اجرا شود.
        """
        if not 1 <= step <= STEP_COUNT:
            return []
        missing: list[str] = []
        for name in _REQUIRED_INPUTS.get(step, ()):
            if not self.input_value(name).strip():
                missing.append(INPUT_LABELS.get(name, name))
        for source in _PROPAGATED.get(step, ()):
            if not self.has_result(source) or self.is_stale(source):
                missing.append(f"Step {source} result")
        return missing

    def can_run(self, step: int) -> bool:
        """آیا این قدم همین حالا قابلِ اجراست؟ (باز و با ورودی‌های کامل)"""
        return self.is_unlocked(step) and not self.missing_inputs(step)

    def go_to(self, step: int) -> bool:
        """رفتن به یک قدم — فقط اگر باز باشد. خروجی: موفقیت."""
        if not self.is_unlocked(step):
            return False
        self.current_step = step
        return True

    def next_step(self) -> int | None:
        """قدمِ بعدی یا None اگر روی قدمِ آخر باشیم."""
        if self.current_step >= STEP_COUNT:
            return None
        return self.current_step + 1

    # ── ورودی‌ها ────────────────────────────────────────────────────────────
    def set_input(self, name: str, value: str) -> None:
        """یک ورودیِ کاربر را در جریان نگه می‌دارد (تا دوباره پرسیده نشود)."""
        self.inputs[name] = value

    def step_inputs(self, step: int) -> dict[str, Any]:
        """ورودی‌های کاملِ یک قدم: نتیجه‌ی قدم‌های قبلی + ورودی‌های دستی خودش.

        نتیجه‌ی کهنه هرگز پاس داده نمی‌شود — وگرنه خروجیِ بی‌اعتبار دوباره
        مصرف می‌شد.
        """
        payload: dict[str, Any] = {}
        for source in _PROPAGATED.get(step, ()):
            if self.has_result(source) and not self.is_stale(source):
                payload[RESULT_ARGUMENT[source]] = self.results[source]
        for name in _OWN_INPUTS.get(step, ()):
            value = self.input_value(name)
            if value.strip():
                payload[name] = value
        return payload

    def pending_input_changes(self, step: int) -> list[str]:
        """ورودی‌هایی که بعد از آخرین اجرای این قدم عوض شده‌اند.

        نتیجه‌ی فعلی به ورودیِ قدیمی تعلق دارد؛ این فهرست همان چیزی است که
        باید به کاربر هشدار داده شود (تا بی‌خبری تأییدش نکند).
        """
        if not self.has_result(step):
            return []
        used = self.used_inputs.get(step, {})
        return [
            INPUT_LABELS.get(name, name)
            for name in _OWN_INPUTS.get(step, ())
            if self.input_value(name) != used.get(name, "")
        ]

    # ── ثبتِ نتیجه، خطا و تأیید ─────────────────────────────────────────────
    def record_result(self, step: int, result: Any) -> list[int]:
        """نتیجه‌ی یک قدم را ثبت می‌کند و در صورت تغییر، پایین‌دستی را کهنه می‌کند.

        خروجی: فهرستِ قدم‌هایی که تازه کهنه شدند. اگر نتیجه دقیقاً همان نتیجه‌ی
        قبلی باشد چیزی کهنه نمی‌شود — اجرای دوباره با ورودیِ یکسان نباید
        کارِ پایین‌دست را بی‌اعتبار کند.
        """
        previous = self.results.get(step, _MISSING)
        self.results[step] = result
        self.errors.pop(step, None)
        self.approved.discard(step)
        self.auto_advanced.discard(step)
        self.blocked.discard(step)
        self.stale.discard(step)
        self.used_inputs[step] = {name: self.input_value(name) for name in _OWN_INPUTS.get(step, ())}

        if previous is not _MISSING and previous == result:
            return []

        invalidated = self._invalidate_after(step)
        if invalidated:
            self.notice = invalidated_message(step, invalidated)
        return invalidated

    def record_error(self, step: int, message: str) -> list[int]:
        """خطای یک قدم را ثبت می‌کند.

        نتیجه‌ی همان قدم برداشته می‌شود (اجرای ناموفق نتیجه‌ی معتبری نساخته)،
        تأییدش لغو می‌شود، کاربر روی همان قدم می‌ماند و — اگر قبلاً نتیجه‌ای
        داشت — پایین‌دستی کهنه می‌شود. خودِ نتایجِ پایین‌دستی پاک نمی‌شوند.
        """
        had_result = self.has_result(step)
        self.results.pop(step, None)
        self.approved.discard(step)
        self.auto_advanced.discard(step)
        self.blocked.discard(step)
        self.stale.discard(step)
        self.used_inputs.pop(step, None)
        self.errors[step] = message
        self.current_step = step

        invalidated = self._invalidate_after(step) if had_result else []
        if invalidated:
            self.notice = invalidated_message(step, invalidated)
        return invalidated

    def approve(self, step: int) -> bool:
        """تأییدِ انسانی — فقط روی نتیجه‌ای که هست، کهنه نیست و قدمش باز است."""
        if not self.is_unlocked(step):
            return False
        if not self.has_result(step) or self.is_stale(step):
            return False
        self.approved.add(step)
        self.auto_advanced.discard(step)
        self.errors.pop(step, None)
        return True

    def auto_advance(self, step: int) -> bool:
        """پذیرشِ خودکار (Run All) — همان شرط‌های approve، ولی ثبت به‌عنوانِ خودکار.

        قدمِ بعد را باز می‌کند بی‌آنکه وانمود کند کسی نتیجه را بازبینی کرده
        است. نتیجه‌ی مسدود پذیرفته نمی‌شود.
        """
        if not self.is_unlocked(step):
            return False
        if not self.has_result(step) or self.is_stale(step) or self.is_blocked(step):
            return False
        if step not in self.approved:
            self.auto_advanced.add(step)
        self.errors.pop(step, None)
        return True

    def mark_blocked(self, step: int) -> None:
        """نتیجه‌ی ثبت‌شده‌ی این قدم خودش قابلِ استفاده نیست (مثلاً قدم ۵ blocked)."""
        if self.has_result(step):
            self.blocked.add(step)
            self.approved.discard(step)
            self.auto_advanced.discard(step)

    def invalidate_from(self, step: int) -> list[int]:
        """نتایجِ این قدم و بعدش را کهنه می‌کند — شروعِ یک اجرای تازه از این قدم.

        نتیجه‌ها پاک نمی‌شوند؛ تا اجرای تازه‌ی هر قدم، نتیجه‌ی قبلی‌اش «out of
        date» است: نه پاس داده می‌شود و نه قابلِ تأیید است.
        """
        return self._invalidate_after(step - 1)

    def approve_and_advance(self, step: int) -> bool:
        """تأیید و رفتن به قدمِ بعد — همان کاری که دکمه‌ی «تأیید و ادامه» می‌کند."""
        if not self.approve(step):
            return False
        if step < STEP_COUNT:
            self.go_to(step + 1)
        return True

    # ── پیام‌ها و بازنشانی ──────────────────────────────────────────────────
    def dismiss_notice(self) -> None:
        """پیامِ کهنه‌شدن را می‌بندد (وضعیت دست‌نخورده می‌ماند)."""
        self.notice = ""

    def reset(self) -> None:
        """کلِ جریان پاک می‌شود: نه نتیجه‌ای می‌ماند، نه ورودی‌ای."""
        self.current_step = 1
        self.results.clear()
        self.approved.clear()
        self.stale.clear()
        self.errors.clear()
        self.inputs.clear()
        self.used_inputs.clear()
        self.usage.clear()
        self.notice = ""
        self.auto_advanced.clear()
        self.blocked.clear()
        self.running_step = 0
        self.run_in_progress = False
        self.last_run = None
        self.swagger_selection = None
        self.task_import = None

    def _invalidate_after(self, step: int) -> list[int]:
        """نتایج/تأییدهای بعد از step را کهنه می‌کند — بدونِ پاک‌کردنِ آن‌ها."""
        invalidated: list[int] = []
        for later in range(step + 1, STEP_COUNT + 1):
            if self.has_result(later) or self.is_accepted(later):
                self.stale.add(later)
                self.approved.discard(later)
                self.auto_advanced.discard(later)
                invalidated.append(later)
        return invalidated


def run_label(state: WorkflowState, step: int) -> str:
    """برچسبِ دکمه‌ی اجرا — بعد از خطا همان دکمه به «تلاش دوباره» تبدیل می‌شود."""
    if state.error(step):
        return f"Retry Step {step} — {STEP_TITLES[step]}"
    return f"Run Step {step} — {STEP_TITLES[step]}"


def progress_text(state: WorkflowState) -> str:
    """«Step 2 of 5 · 1 of 5 approved» برای نوارِ کنار.

    قدم‌هایی که فقط Run All پذیرفته جدا شمرده می‌شوند — تأیید حساب نمی‌شوند.
    """
    text = (
        f"Step {state.current_step} of {STEP_COUNT} · "
        f"{state.completed_count()} of {STEP_COUNT} approved"
    )
    automatic = state.auto_completed_count()
    if automatic:
        text += f" · {automatic} completed by Run All (not reviewed)"
    return text


# ── نمایشِ مصرفِ توکن ────────────────────────────────────────────────────────

# «گزارش‌نشده» با «صفر» فرق دارد؛ «اجرا نشده» هم با هیچ‌کدام.
USAGE_NOT_AVAILABLE = "—"
USAGE_NO_CALLS = "no calls"
USAGE_NOT_REPORTED = "not reported"


def _format_tokens(value: int | None) -> str:
    """عددِ توکن با جداکننده‌ی هزارگان؛ نبودِ مقدار با خطِ تیره نشان داده می‌شود."""
    return USAGE_NOT_AVAILABLE if value is None else f"{value:,}"


def usage_totals(state: WorkflowState) -> TokenUsage | None:
    """جمعِ مصرفِ همه‌ی قدم‌ها؛ ``None`` یعنی هنوز هیچ فراخوانی‌ای ثبت نشده."""
    total = TokenUsage()
    found = False
    for usage in state.usage.values():
        if usage.calls:
            total = total.combine(usage)
            found = True
    return total if found else None


def has_cached_usage(state: WorkflowState) -> bool:
    """آیا جایی توکنِ کش‌شده گزارش شده است؟ (برای نمایشِ ستونِ جدا)"""
    return any(
        usage.cached_tokens is not None for usage in state.usage.values()
    )


def _usage_values(usage: TokenUsage | None, executed: bool) -> tuple[str, str, str]:
    """(prompt، completion، total) برای یک ردیف — با تفکیکِ حالت‌های ممکن."""
    if not executed:
        return USAGE_NOT_AVAILABLE, USAGE_NOT_AVAILABLE, USAGE_NOT_AVAILABLE
    if usage is None or usage.calls == 0:
        return USAGE_NOT_AVAILABLE, USAGE_NOT_AVAILABLE, USAGE_NO_CALLS
    if (
        usage.prompt_tokens is None
        and usage.completion_tokens is None
        and usage.total_tokens is None
    ):
        return USAGE_NOT_AVAILABLE, USAGE_NOT_AVAILABLE, USAGE_NOT_REPORTED
    return (
        _format_tokens(usage.prompt_tokens),
        _format_tokens(usage.completion_tokens),
        _format_tokens(usage.total_tokens),
    )


def usage_rows(state: WorkflowState) -> list[dict[str, str]]:
    """جدولِ مصرف: یک ردیف برای هر قدم و یک ردیفِ جمع برای همه‌ی قدم‌ها.

    حالت‌ها صریح‌اند: «—» یعنی این قدم هنوز اجرا نشده، «no calls» یعنی اجرا
    شده ولی فراخوانیِ LLM نداشته (مثلِ قدم‌های ۴ و ۵) و «not reported» یعنی
    فراخوانی بوده ولی ارائه‌دهنده usage نداده است. هیچ‌کدام با صفرِ گمراه‌کننده
    نمایش داده نمی‌شوند.
    """
    show_cached = has_cached_usage(state)
    rows: list[dict[str, str]] = []

    for step in range(1, STEP_COUNT + 1):
        usage = state.usage_for(step)
        executed = (
            state.has_result(step) or bool(state.error(step)) or usage is not None
        )
        prompt, completion, total = _usage_values(usage, executed)
        row = {
            "Step": f"Step {step}" + (" ⚠" if state.error(step) else ""),
            "Prompt": prompt,
            "Completion": completion,
            "Total": total,
        }
        if show_cached:
            row["Cached"] = _format_tokens(usage.cached_tokens if usage else None)
        rows.append(row)

    totals = usage_totals(state)
    executed = totals is not None
    prompt, completion, total = _usage_values(totals, executed)
    total_row = {
        "Step": "All steps",
        "Prompt": prompt,
        "Completion": completion,
        "Total": total,
    }
    if show_cached:
        total_row["Cached"] = _format_tokens(totals.cached_tokens if totals else None)
    rows.append(total_row)
    return rows


__all__ = [
    "INPUT_COLLECTION_NAME",
    "INPUT_DEVELOPED_SERVICES",
    "INPUT_JIRA_ISSUE",
    "INPUT_LABELS",
    "INPUT_SWAGGER_SELECTION",
    "INPUT_SWAGGER_SNAPSHOTS",
    "INPUT_SWAGGER_SOURCES",
    "INPUT_TASK_DESCRIPTION",
    "RESULT_ARGUMENT",
    "STATUS_AUTO_COMPLETED",
    "STATUS_AVAILABLE",
    "STATUS_BLOCKED",
    "STATUS_COMPLETED",
    "STATUS_CURRENT",
    "STATUS_FAILED",
    "STATUS_LABELS",
    "STATUS_LOCKED",
    "STATUS_MARKERS",
    "STATUS_RUNNING",
    "STATUS_STALE",
    "STEP_COUNT",
    "STEP_ICONS",
    "STEP_TITLES",
    "USAGE_NO_CALLS",
    "USAGE_NOT_AVAILABLE",
    "USAGE_NOT_REPORTED",
    "WorkflowState",
    "approve_label",
    "error_retry_message",
    "has_cached_usage",
    "invalidated_message",
    "progress_text",
    "propagated_steps",
    "run_label",
    "stale_message",
    "usage_rows",
    "usage_totals",
]
