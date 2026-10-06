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

  * قدم ۱ همیشه باز است؛ قدمِ n فقط با «تأییدِ قدمِ n-1» باز می‌شود. پرش به
    قدمی که پیش‌نیازش وجود ندارد ممکن نیست.
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

from dataclasses import dataclass, field
from typing import Any

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
INPUT_COLLECTION_NAME = "collection_name"

INPUT_LABELS: dict[str, str] = {
    INPUT_TASK_DESCRIPTION: "Task Description",
    INPUT_DEVELOPED_SERVICES: "Developed Services",
    INPUT_SWAGGER_SOURCES: "Swagger / OpenAPI sources",
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
_OWN_INPUTS: dict[int, tuple[str, ...]] = {
    1: (INPUT_TASK_DESCRIPTION, INPUT_DEVELOPED_SERVICES),
    2: (INPUT_SWAGGER_SOURCES,),
    4: (INPUT_COLLECTION_NAME,),
}

# زیرمجموعه‌ی اجباریِ همان ورودی‌ها — بقیه اختیاری‌اند.
_REQUIRED_INPUTS: dict[int, tuple[str, ...]] = {
    1: (INPUT_TASK_DESCRIPTION,),
    2: (INPUT_SWAGGER_SOURCES,),
}

# ── وضعیت‌های نمایشیِ هر قدم ─────────────────────────────────────────────────
STATUS_COMPLETED = "completed"
STATUS_CURRENT = "current"
STATUS_AVAILABLE = "available"
STATUS_STALE = "stale"
STATUS_LOCKED = "locked"

STATUS_MARKERS: dict[str, str] = {
    STATUS_COMPLETED: "✅",
    STATUS_CURRENT: "🔵",
    STATUS_AVAILABLE: "⚪",
    STATUS_STALE: "⚠️",
    STATUS_LOCKED: "🔒",
}

STATUS_LABELS: dict[str, str] = {
    STATUS_COMPLETED: "done",
    STATUS_CURRENT: "you are here",
    STATUS_AVAILABLE: "ready to run",
    STATUS_STALE: "out of date",
    STATUS_LOCKED: "locked",
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
    notice: str = ""

    # ── پرسش‌های پایه ────────────────────────────────────────────────────────
    def result(self, step: int) -> Any:
        """نتیجه‌ی یک قدم (یا None اگر اجرا نشده باشد)."""
        return self.results.get(step)

    def has_result(self, step: int) -> bool:
        return step in self.results

    def is_approved(self, step: int) -> bool:
        return step in self.approved

    def is_stale(self, step: int) -> bool:
        return step in self.stale

    def error(self, step: int) -> str:
        return self.errors.get(step, "")

    def input_value(self, name: str) -> str:
        return self.inputs.get(name, "")

    # ── قفل، وضعیت و ناوبری ─────────────────────────────────────────────────
    def is_unlocked(self, step: int) -> bool:
        """قدم ۱ همیشه باز است؛ بقیه فقط با تأییدِ قدمِ قبلی باز می‌شوند."""
        if not 1 <= step <= STEP_COUNT:
            return False
        if step == 1:
            return True
        return (step - 1) in self.approved

    def status(self, step: int) -> str:
        """وضعیتِ نمایشیِ قدم — کهنه > قفل > جاری > تمام‌شده > آماده."""
        if not 1 <= step <= STEP_COUNT:
            return STATUS_LOCKED
        if self.is_stale(step):
            return STATUS_STALE
        if not self.is_unlocked(step):
            return STATUS_LOCKED
        if step == self.current_step:
            return STATUS_CURRENT
        if self.has_result(step) and self.is_approved(step):
            return STATUS_COMPLETED
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
        self.errors.pop(step, None)
        return True

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
        self.notice = ""

    def _invalidate_after(self, step: int) -> list[int]:
        """نتایج/تأییدهای بعد از step را کهنه می‌کند — بدونِ پاک‌کردنِ آن‌ها."""
        invalidated: list[int] = []
        for later in range(step + 1, STEP_COUNT + 1):
            if self.has_result(later) or later in self.approved:
                self.stale.add(later)
                self.approved.discard(later)
                invalidated.append(later)
        return invalidated


def run_label(state: WorkflowState, step: int) -> str:
    """برچسبِ دکمه‌ی اجرا — بعد از خطا همان دکمه به «تلاش دوباره» تبدیل می‌شود."""
    if state.error(step):
        return f"Retry Step {step} — {STEP_TITLES[step]}"
    return f"Run Step {step} — {STEP_TITLES[step]}"


def progress_text(state: WorkflowState) -> str:
    """«Step 2 of 5 · 1 of 5 approved» برای نوارِ کنار."""
    return (
        f"Step {state.current_step} of {STEP_COUNT} · "
        f"{state.completed_count()} of {STEP_COUNT} approved"
    )


__all__ = [
    "INPUT_COLLECTION_NAME",
    "INPUT_DEVELOPED_SERVICES",
    "INPUT_LABELS",
    "INPUT_SWAGGER_SOURCES",
    "INPUT_TASK_DESCRIPTION",
    "RESULT_ARGUMENT",
    "STATUS_AVAILABLE",
    "STATUS_COMPLETED",
    "STATUS_CURRENT",
    "STATUS_LABELS",
    "STATUS_LOCKED",
    "STATUS_MARKERS",
    "STATUS_STALE",
    "STEP_COUNT",
    "STEP_ICONS",
    "STEP_TITLES",
    "WorkflowState",
    "approve_label",
    "error_retry_message",
    "invalidated_message",
    "progress_text",
    "propagated_steps",
    "run_label",
    "stale_message",
]
