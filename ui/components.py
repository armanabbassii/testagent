"""
ui/components.py — تکه‌های نمایشیِ مشترکِ قدم‌ها داخلِ ویزارد

هر قدم در ویزارد ورودی‌هایش را از وضعیتِ جریان می‌گیرد، نه از کپی/پیستِ
JSON. این ماژول همان نمایشِ مشترکِ آن ورودی‌هاست: به کاربر نشان می‌دهد چه
چیزی خودکار از قدم‌های قبلی رسیده و امکانِ دیدنِ خودِ JSON را هم می‌دهد.

هیچ منطقِ کسب‌وکاری اینجا نیست — فقط نمایش.
"""

from __future__ import annotations

import streamlit as st

from ui.workflow import (
    STATUS_AUTO_COMPLETED,
    STATUS_AVAILABLE,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_CURRENT,
    STATUS_FAILED,
    STATUS_LABELS,
    STATUS_LOCKED,
    STATUS_RUNNING,
    STATUS_STALE,
    STEP_TITLES,
    WorkflowState,
    propagated_steps,
)

# ── نمایشِ وضعیتِ قدم‌ها ────────────────────────────────────────────────────
# فقط ظاهرِ همان وضعیت‌های ui/workflow.py (WorkflowState.status)؛ هیچ وضعیتی
# این‌جا ساخته یا حدس زده نمی‌شود.
STATUS_ICONS: dict[str, str] = {
    STATUS_COMPLETED: ":material/check_circle:",
    STATUS_AUTO_COMPLETED: ":material/task_alt:",
    STATUS_CURRENT: ":material/radio_button_checked:",
    STATUS_AVAILABLE: ":material/radio_button_unchecked:",
    STATUS_STALE: ":material/history:",
    STATUS_LOCKED: ":material/lock:",
    STATUS_RUNNING: ":material/progress_activity:",
    STATUS_FAILED: ":material/error:",
    STATUS_BLOCKED: ":material/block:",
}

# رنگ‌های st.badge و متنِ رنگیِ Markdown.
STATUS_COLORS: dict[str, str] = {
    STATUS_COMPLETED: "green",
    STATUS_AUTO_COMPLETED: "blue",
    STATUS_CURRENT: "primary",
    STATUS_AVAILABLE: "gray",
    STATUS_STALE: "orange",
    STATUS_LOCKED: "gray",
    STATUS_RUNNING: "blue",
    STATUS_FAILED: "red",
    STATUS_BLOCKED: "red",
}

STEP_SYMBOLS: dict[int, str] = {
    1: ":material/assignment:",
    2: ":material/api:",
    3: ":material/account_tree:",
    4: ":material/inventory_2:",
    5: ":material/fact_check:",
}


# ── رنگِ دکمه‌های اجرا ──────────────────────────────────────────────────────
# تمِ Streamlit فقط یک رنگِ اصلی (primaryColor، فیروزه‌ای) دارد که دکمه‌ی تأیید
# و بقیه‌ی دکمه‌های primary هم از آن استفاده می‌کنند. دکمه‌های اجرا رنگِ خودشان
# را دارند تا با سبزِ «تأیید/تمام‌شده» اشتباه نشوند — با همان روشِ مستند:
# کلیدِ ویجت → کلاسِ «st-key-<key>». فقط همین دکمه‌ها هدف‌اند؛ اندازه، فاصله و
# چیدمان دست نمی‌خورد و حالتِ غیرفعال همان ظاهرِ پیش‌فرضِ تم را دارد.
RUN_ALL_KEY = "workflow_run_all"


def run_step_key(step: int) -> str:
    """کلیدِ دکمه‌ی «Run Step N» — همان کلاسی که رنگِ آبی را می‌گیرد."""
    return f"workflow_run_{step}"


# آبی برای «Run Step»: قاب و متنِ آبی روی پس‌زمینه‌ی کم‌رنگ (کمتر برجسته).
# بنفش برای «Run All»: پُر، متنِ سفید (کنتراست ۵٫۷:۱) — دکمه‌ی اصلی.
_EXECUTION_BUTTON_CSS = """
<style>
{run_step} {{
  background-color: rgba(59, 130, 246, 0.14);
  border-color: #3B82F6;
  color: #DBEAFE;
}}
{run_step_hover} {{
  background-color: rgba(59, 130, 246, 0.26);
  border-color: #60A5FA;
  color: #FFFFFF;
}}
{run_step_active} {{
  background-color: rgba(59, 130, 246, 0.36);
  border-color: #93C5FD;
}}
{run_step_focus} {{
  outline: 2px solid #60A5FA;
  outline-offset: 2px;
}}
{run_all} {{
  background-color: #7C3AED;
  border-color: #7C3AED;
  color: #FFFFFF;
  font-weight: 600;
}}
{run_all_hover} {{
  background-color: #6D28D9;
  border-color: #8B5CF6;
  color: #FFFFFF;
}}
{run_all_active} {{
  background-color: #5B21B6;
  border-color: #5B21B6;
}}
{run_all_focus} {{
  outline: 2px solid #A78BFA;
  outline-offset: 2px;
}}
</style>
"""


def _selectors(keys: list[str], state: str = "") -> str:
    # :not(:disabled) — دکمه‌ی غیرفعال همان ظاهرِ پیش‌فرضِ تم را نگه می‌دارد.
    return ",\n".join(f".st-key-{key} button:not(:disabled){state}" for key in keys)


def execution_button_css(step_count: int) -> str:
    """CSSِ رنگِ دکمه‌های اجرا — فقط برای کلیدهای run_step_key و RUN_ALL_KEY."""
    steps = [run_step_key(step) for step in range(1, step_count + 1)]
    return _EXECUTION_BUTTON_CSS.format(
        run_step=_selectors(steps),
        run_step_hover=_selectors(steps, ":hover"),
        run_step_active=_selectors(steps, ":active"),
        run_step_focus=_selectors(steps, ":focus-visible"),
        run_all=_selectors([RUN_ALL_KEY]),
        run_all_hover=_selectors([RUN_ALL_KEY], ":hover"),
        run_all_active=_selectors([RUN_ALL_KEY], ":active"),
        run_all_focus=_selectors([RUN_ALL_KEY], ":focus-visible"),
    )


def apply_execution_button_styles(step_count: int) -> None:
    """رنگِ دکمه‌های اجرا را یک بار برای کلِ صفحه اعمال می‌کند (بدونِ جای‌گرفتن در صفحه)."""
    st.html(execution_button_css(step_count))


def status_badge(status: str) -> None:
    """برچسبِ رنگیِ وضعیتِ یک قدم (done، out of date، locked، …)."""
    st.badge(STATUS_LABELS[status], icon=STATUS_ICONS[status], color=STATUS_COLORS[status])


def status_line(step: int, status: str) -> str:
    """یک خطِ Markdown برای وضعیتِ یک قدم — آیکونِ رنگی، عنوان و برچسب."""
    color = STATUS_COLORS[status]
    return (
        f":{color}[{STATUS_ICONS[status]}] **Step {step}** · {STEP_TITLES[step]} "
        f"— :{color}[{STATUS_LABELS[status]}]"
    )


# درخواستِ Run All بینِ دو اجرای اسکریپت: کلیک فقط این پرچم را می‌گذارد و
# صفحه دوباره کشیده می‌شود؛ اجرای بعد پرچم را برمی‌دارد و جریان را اجرا می‌کند
# (همان الگوی دومرحله‌ایِ دکمه‌های Update در ui/swagger_refresh.py).
_RUN_ALL_REQUEST_KEY = "_workflow_run_all_requested"


def request_run_all() -> None:
    """Run All را برای اجرای بعدیِ اسکریپت درخواست می‌کند."""
    st.session_state[_RUN_ALL_REQUEST_KEY] = True


def take_run_all_request() -> bool:
    """درخواستِ معلق را برمی‌دارد — هر کلیک دقیقاً یک بار اجرا می‌شود.

    پرچم پیش از اجرا برداشته می‌شود تا اگر اجرا قطع شد، خودبه‌خود دوباره
    شروع نشود.
    """
    return bool(st.session_state.pop(_RUN_ALL_REQUEST_KEY, False))


def render_propagated_inputs(workflow: WorkflowState, step: int) -> None:
    """نتیجه‌های قدم‌های قبلی را که خودکار به این قدم داده شده‌اند نشان می‌دهد.

    کاربر چیزی وارد نمی‌کند و چیزی کپی نمی‌کند؛ فقط می‌بیند این قدم روی چه
    ورودی‌ای اجرا می‌شود.
    """
    sources = propagated_steps(step)
    if not sources:
        return

    labels = ", ".join(f"Step {source} result" for source in sources)
    st.caption(
        f"Input for this step: {labels} — taken from the workflow automatically. "
        f"Nothing to paste."
    )

    for source in sources:
        with st.expander(
            f"Step {source} result (input to Step {step})", icon=":material/data_object:"
        ):
            st.json(workflow.result(source))
