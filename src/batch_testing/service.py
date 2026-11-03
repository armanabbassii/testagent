"""
src/batch_testing/service.py — ساختِ batch و میزبانیِ runner برای UI (Milestone 4.6)

    service = get_service()                    # یکی برای هر پروسه و هر پایگاه
    batch = create_batch(store, discovery, excluded={"PODIUM-7"})
    service.start(batch.id)                    # BatchRunner در threadِ پس‌زمینه

ساختِ batch:
  * فقط issueهای واجدِ شرایطِ کشف (همان معیارهای discovery)، به ترتیبِ کشف، منهای
    issueهایی که کاربر کنار گذاشته؛ batchِ خالی ساخته نمی‌شود.
  * Environment (dev) و Globals از همان مسیرهای پیکربندیِ Newman؛ باید موجود و
    خواندنی و JSONِ معتبر باشند. SHA-256ِ بایت‌های خام (همان قراردادِ runner.file_sha256)
    ثبت می‌شود. محتوا هرگز نمایش داده یا نوشته نمی‌شود و فایل‌ها تغییر نمی‌کنند.

میزبانیِ runner:
  * Streamlit اسکریپت را با هر تعامل دوباره اجرا می‌کند؛ پس runner در صفحه اجرا
    نمی‌شود. BatchService در سطحِ پروسه (نه session) زنده است، حداکثر یک threadِ
    runner دارد و مرجعِ همان BatchRunner را برای کنترلِ درون‌پروسه‌ای نگه می‌دارد.
  * rerun یا refreshِ صفحه همان سرویس را می‌گیرد و چیزی را دوباره شروع نمی‌کند؛
    وضعیتِ معتبر همیشه از SQLite خوانده می‌شود. leaseِ BatchStore همچنان مانعِ
    runnerِ دوم (حتی در پروسه‌ی دیگر) است.
  * PAUSE/CANCELِ batchِ RUNNING از راهِ request_actionِ پایگاه؛ لغوِ retry با
    request_cancel()ِ همان runner (فقط در پروسه‌ای که retry را اجرا می‌کند). batchِ
    CREATED / PAUSED / INTERRUPTED بدونِ runner و در یک تراکنش لغو می‌شود
    (BatchStore.cancel_inactive_batch)؛ leaseِ کهنه پیش از آن با بازیابی آزاد می‌شود.

فرضِ میزبانی — یک پروسه‌ی سرورِ Streamlit:
  * همه‌ی sessionهای مرورگر (هر تب، هر کاربر) در همان پروسه‌اند و همین یک
    BatchService را می‌بینند (رجیستریِ سطحِ ماژول، کلید: مسیرِ پایگاه).
  * با چند پروسه‌ی سرور روی یک پایگاه، هر پروسه سرویسِ خودش را دارد. درستی حفظ
    می‌شود — lease و ایندکس‌های یکتای SQLite فقط یک runner را در کلِ پایگاه
    می‌گذارند، و Pause/Cancelِ batchِ RUNNING از هر پروسه کار می‌کند چون از راهِ
    پایگاه است — ولی لغوِ retry فقط در پروسه‌ای ممکن است که runner را دارد؛ پروسه‌ی
    دیگر این را صریحاً رد می‌کند و ادعای لغو نمی‌کند.
  * هماهنگیِ توزیع‌شده (صفِ مشترک، لغوِ retry از راهِ پایگاه) پیاده نشده است.
  * بازیابیِ leaseهای کهنه (recover_stale) یک بار هنگامِ ساختِ سرویس؛ هیچ issueی
    خودکار دوباره اجرا نمی‌شود — Resume همیشه کارِ صریحِ کاربر است.
  * threadِ runner daemon است: اگر سرور وسطِ اجرا بسته شود، lease کهنه می‌شود و
    بازیابیِ بعدی batch را INTERRUPTED می‌کند.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from src.batch_testing.discovery import Candidate, DiscoveryResult
from src.batch_testing.models import (
    ACTION_CANCEL,
    ACTION_PAUSE,
    BATCH_CREATED,
    BATCH_FINAL,
    BATCH_INTERRUPTED,
    BATCH_PAUSED,
    BATCH_RUNNING,
    EXEC_RUNNING,
    Batch,
    InvalidTransitionError,
    IssueSnapshot,
    NewBatch,
)
from src.batch_testing.recovery import STALE_AFTER_S, RecoveryReport, recover_stale
from src.batch_testing.runner import RESULT_FAILED, RESULT_REFUSED, BatchRunner, RunnerResult
from src.batch_testing.storage import (
    DEFAULT_ARTIFACT_ROOT,
    DEFAULT_DB_PATH,
    BatchStore,
    NotFoundError,
    OwnershipError,
    StorageError,
)
from src.batch_testing.summary import HEALTH_LIVE, runner_health
from src.test_execution.newman import DEV_ENVIRONMENT_PATH, WORKSPACE_GLOBALS_PATH

MODE_START = "start"
MODE_RESUME = "resume"
MODE_RETRY = "retry"

# تا این مدت منتظرِ گرفتنِ lease یا ردِ درخواست می‌مانیم تا پاسخِ دکمه فوری باشد
LAUNCH_WAIT_S = 3.0


# ── ساختِ batch ──────────────────────────────────────────────────────────────
class ConfigurationError(Exception):
    """Environment یا Globals برای ساختِ batch قابلِ استفاده نیست — پیامِ ثابت، بی‌محتوا."""


@dataclass(frozen=True)
class ConfigurationSnapshot:
    environment_path: str
    environment_sha256: str
    globals_path: str
    globals_sha256: str


_INVALID = object()


def _read(path: Path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _parse(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return _INVALID


def _hash_postman_file(label: str, path: Path) -> str:
    """SHA-256ِ بایت‌های خام پس از اعتبارسنجی؛ هیچ بخشی از محتوا در پیامِ خطا نمی‌آید.

    خطا بیرون از بلوکِ except ساخته می‌شود تا استثنای decode (که repr آن بایت‌های
    فایل را دارد) حتی به‌عنوانِ __context__ همراهش نباشد.
    """
    raw = _read(path)
    if raw is None:
        raise ConfigurationError(f"The {label} file is missing or unreadable.")
    document = _parse(raw)
    if document is _INVALID:
        raise ConfigurationError(f"The {label} file is not valid JSON.")
    if not isinstance(document, dict):
        raise ConfigurationError(f"The {label} file is not a Postman {label} document.")
    return hashlib.sha256(raw).hexdigest()


def configuration_snapshot(
    environment_path: Path | str = DEV_ENVIRONMENT_PATH,
    globals_path: Path | str = WORKSPACE_GLOBALS_PATH,
) -> ConfigurationSnapshot:
    """مسیرها (از پیکربندیِ مخزن، نه مسیرِ ثابتِ ماشین) و هشِ هر دو فایل — فقط‌خواندنی."""
    environment, globals_ = Path(environment_path).resolve(), Path(globals_path).resolve()
    return ConfigurationSnapshot(
        environment_path=str(environment),
        environment_sha256=_hash_postman_file("environment", environment),
        globals_path=str(globals_),
        globals_sha256=_hash_postman_file("globals", globals_),
    )


def _candidate_record(candidate: Candidate) -> dict:
    """ردیابیِ کشف: فقط فیلدهای ساختاری، بدونِ متنِ آزادِ Jira."""
    return {
        "key": candidate.key,
        "issue_type": candidate.issue_type,
        "status": candidate.status_name,
        "labels": list(candidate.labels),
        "parent_key": candidate.parent_key,
        "in_active_sprint": candidate.in_active_sprint,
        # هر دلیل با نامِ شرطش (status / column / label)
        "reasons": [
            {"criterion": criterion, "reason": reason}
            for criterion, reason in zip(candidate.failed_criteria, candidate.reasons, strict=True)
        ],
    }


def build_new_batch(
    result: DiscoveryResult,
    excluded: Iterable[str] = (),
    *,
    configuration: ConfigurationSnapshot | None = None,
    artifact_root: Path | str = DEFAULT_ARTIFACT_ROOT,
) -> NewBatch:
    """NewBatch از نتیجه‌ی کشف و کنارگذاشته‌های کاربر — ترتیبِ کشف منجمد می‌شود.

    ValueError اگر کنارگذاشته‌ای واجدِ شرایط نباشد یا هیچ issueی نماند؛
    ConfigurationError اگر Environment یا Globals قابلِ استفاده نباشد.
    """
    eligible = result.eligible
    removed = set(excluded)
    if removed - {candidate.key for candidate in eligible}:
        raise ValueError("Only eligible issues can be excluded from a batch.")
    selected = [candidate for candidate in eligible if candidate.key not in removed]
    if not selected:
        raise ValueError("Select at least one eligible issue for the batch.")
    configuration = configuration or configuration_snapshot()
    context = result.context
    return NewBatch(
        board_id=context.board_id,
        board_name=context.board_name,
        board_type=context.board_type,
        column_name=context.column_name,
        criteria={
            "column": result.criteria.column,
            "status": result.criteria.status,
            "label": result.criteria.label,
        },
        discovery={
            "eligible": [candidate.key for candidate in eligible],
            "excluded": [_candidate_record(candidate) for candidate in result.excluded],
            "user_excluded": [candidate.key for candidate in eligible if candidate.key in removed],
            "selected": [candidate.key for candidate in selected],
            "active_sprint_ids": list(context.active_sprint_ids or ()),
        },
        issues=tuple(
            IssueSnapshot(
                issue_key=candidate.key,
                summary=candidate.summary,
                issue_type=candidate.issue_type,
                status_name=candidate.status_name,
                labels=candidate.labels,
                parent_key=candidate.parent_key,
                eligibility={"reasons": [], "in_active_sprint": candidate.in_active_sprint},
            )
            for candidate in selected
        ),
        environment_path=configuration.environment_path,
        environment_sha256=configuration.environment_sha256,
        globals_path=configuration.globals_path,
        globals_sha256=configuration.globals_sha256,
        artifact_root=str(Path(artifact_root).resolve()),
    )


def create_batch(
    store: BatchStore,
    result: DiscoveryResult,
    excluded: Iterable[str] = (),
    **options,
) -> Batch:
    """build_new_batch + ذخیره در یک تراکنش (همه یا هیچ)."""
    return store.create_batch(build_new_batch(result, excluded, **options))


# ── میزبانیِ runner ──────────────────────────────────────────────────────────
RunnerFactory = Callable[[Path], BatchRunner]
StoreFactory = Callable[[Path], BatchStore]
Recover = Callable[[BatchStore], list[RecoveryReport]]


@dataclass
class BatchJob:
    """یک بارِ اجرای runner در threadِ سرویس."""

    batch_id: str
    mode: str
    issue_key: str
    runner: BatchRunner
    thread: threading.Thread | None = None
    result: RunnerResult | None = None
    # کاربر لغو را درخواست کرد؛ نتیجه فقط وقتی «لغو شد» است که runner تأیید کند
    cancel_requested: bool = False
    started_at: float = field(default_factory=time.monotonic)

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
    job: BatchJob | None = None


class BatchService:
    """حداکثر یک runner برای هر پروسه؛ کنترل‌ها همیشه به همان نمونه‌ی runner می‌رسند."""

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        *,
        runner_factory: RunnerFactory = BatchRunner,
        store_factory: StoreFactory = BatchStore.open,
        recover: Recover = recover_stale,
        launch_wait: float = LAUNCH_WAIT_S,
        stale_after: float = STALE_AFTER_S,
    ) -> None:
        self.db_path = Path(db_path)
        self._runner_factory = runner_factory
        self._store_factory = store_factory
        self._recover = recover
        self._launch_wait = launch_wait
        self.stale_after = stale_after
        self._lock = threading.Lock()
        self._job: BatchJob | None = None
        self._recovered = False
        self.recovery_reports: list[RecoveryReport] = []
        self.recovery_error = ""

    def open_store(self) -> BatchStore:
        """اتصالِ تازه برای threadِ فراخواننده (اتصالِ sqlite3 بینِ threadها مشترک نیست)."""
        return self._store_factory(self.db_path)

    # ── بازیابی ─────────────────────────────────────────────────────────────
    def ensure_recovered(self) -> list[RecoveryReport]:
        """بازیابیِ آغازین — فقط بارِ اول برای این سرویس، نه با هر rerun."""
        with self._lock:
            if self._recovered:
                return self.recovery_reports
            self._recovered = True
        return self.recover()

    def recover(self) -> list[RecoveryReport]:
        """بازیابیِ صریح و idempotent؛ leaseِ سالم دزدیده نمی‌شود و چیزی اجرا نمی‌شود."""
        try:
            with self.open_store() as store:
                reports = self._recover(store)
        except (StorageError, OSError) as exc:
            self.recovery_error = f"Stale-run recovery failed ({type(exc).__name__})."
            return []
        self.recovery_error = ""
        self.recovery_reports = reports
        return reports

    # ── وضعیتِ runner ───────────────────────────────────────────────────────
    @property
    def job(self) -> BatchJob | None:
        """runnerِ جاری یا آخرین runnerِ این پروسه."""
        return self._job

    @property
    def busy(self) -> bool:
        job = self._job
        return job is not None and job.running

    def job_for(self, batch_id: str) -> BatchJob | None:
        job = self._job
        return job if job is not None and job.batch_id == batch_id else None

    def wait(self, timeout: float | None = None) -> bool:
        """منتظرِ پایانِ runnerِ جاری؛ True یعنی هیچ runnerی در حالِ اجرا نیست."""
        job = self._job
        if job is not None and job.thread is not None:
            job.thread.join(timeout)
        return not self.busy

    # ── اجرا ────────────────────────────────────────────────────────────────
    def start(self, batch_id: str) -> ActionResult:
        return self._launch(batch_id, MODE_START)

    def resume(self, batch_id: str) -> ActionResult:
        return self._launch(batch_id, MODE_RESUME)

    def retry(self, batch_id: str, issue_key: str) -> ActionResult:
        return self._launch(batch_id, MODE_RETRY, issue_key)

    def _launch(self, batch_id: str, mode: str, issue_key: str = "") -> ActionResult:
        with self._lock:
            current = self._job
            if current is not None and current.running:
                return ActionResult(
                    False, "A batch run is already in progress in this application.", current
                )
            runner = self._runner_factory(self.db_path)
            job = BatchJob(batch_id, mode, issue_key, runner)
            job.thread = threading.Thread(
                target=self._run,
                args=(job,),
                name=f"batch-runner-host-{batch_id[:8]}",
                daemon=True,
            )
            self._job = job
            job.thread.start()
        self._await_launch(job)
        if job.result is not None and job.result.status == RESULT_REFUSED:
            return ActionResult(False, job.result.error or "The runner refused the request.", job)
        return ActionResult(True, _LAUNCHED[mode], job)

    @staticmethod
    def _run(job: BatchJob) -> None:
        method = {
            MODE_START: lambda: job.runner.start(job.batch_id),
            MODE_RESUME: lambda: job.runner.resume(job.batch_id),
            MODE_RETRY: lambda: job.runner.retry(job.batch_id, job.issue_key),
        }[job.mode]
        try:
            job.result = method()
        except Exception as exc:  # noqa: BLE001 — threadِ پس‌زمینه بی‌صدا نمی‌میرد
            job.result = RunnerResult(
                job.batch_id,
                RESULT_FAILED,
                error=f"The runner failed unexpectedly ({type(exc).__name__}).",
            )

    def _await_launch(self, job: BatchJob) -> None:
        """تا گرفتنِ lease (یا پایان/ردِ runner) — فقط خواندن از پایگاه."""
        deadline = time.monotonic() + self._launch_wait
        try:
            with self.open_store() as store:
                while job.running and time.monotonic() < deadline:
                    try:
                        if store.get_batch(job.batch_id).owner_token == job.runner.token:
                            return
                    except NotFoundError:
                        pass
                    time.sleep(0.02)
        except (StorageError, OSError):
            return

    # ── کنترل ───────────────────────────────────────────────────────────────
    def pause(self, batch_id: str) -> ActionResult:
        """PAUSE از راهِ پایگاه؛ issueِ جاری کامل تمام می‌شود."""
        try:
            with self.open_store() as store:
                batch = store.get_batch(batch_id)
                if batch.status != BATCH_RUNNING:
                    return ActionResult(False, f"A {batch.status} batch cannot be paused.")
                if runner_health(batch, stale_after=self.stale_after) != HEALTH_LIVE:
                    return ActionResult(
                        False, "No live runner holds this batch, so it cannot receive a pause."
                    )
                store.request_action(batch_id, ACTION_PAUSE)
        except (InvalidTransitionError, NotFoundError) as exc:
            return ActionResult(False, str(exc))
        return ActionResult(
            True, "Pause requested. The current issue finishes before the batch pauses."
        )

    def cancel(self, batch_id: str) -> ActionResult:
        """RUNNING: CANCELِ پایگاه (runner تأیید می‌کند)؛ retry: request_cancel()ِ همان runner؛
        CREATED / PAUSED / INTERRUPTED: لغوِ مستقیم و اتمی بدونِ runner (_cancel_inactive)."""
        job = self._job
        if job is not None and job.running and job.batch_id == batch_id and job.mode == MODE_RETRY:
            if not job.runner.request_cancel():
                return ActionResult(False, "The retry is not running any more.", job)
            job.cancel_requested = True
            return ActionResult(
                True, "Cancellation requested. Waiting for the runner to confirm it.", job
            )
        try:
            with self.open_store() as store:
                batch = store.get_batch(batch_id)
                if batch.status in BATCH_FINAL:
                    if batch.owner_token:
                        return ActionResult(
                            False,
                            "A retry of this batch is running in another application process; "
                            "it can only be cancelled there.",
                        )
                    return ActionResult(False, f"The batch is already {batch.status}.")
                if batch.status in _INACTIVE:
                    return self._cancel_inactive(store, batch)
                store.request_action(batch_id, ACTION_CANCEL)
        except (InvalidTransitionError, NotFoundError) as exc:
            return ActionResult(False, str(exc))
        if job is not None and job.running and job.batch_id == batch_id:
            job.cancel_requested = True
        return ActionResult(True, "Cancellation requested. Waiting for the runner to confirm it.")

    def _cancel_inactive(self, store: BatchStore, batch: Batch) -> ActionResult:
        """بدونِ resume: QUEUED → CANCELLED و batch → CANCELLED در یک تراکنش؛ نتیجه‌های
        پایانی (PASS…INTERRUPTED) و artifactها دست نمی‌خورند و تلاشی ساخته نمی‌شود."""
        if batch.owner_token:
            if runner_health(batch, stale_after=self.stale_after) == HEALTH_LIVE:
                return ActionResult(
                    False, "A runner currently holds this batch; it cannot be cancelled now."
                )
            # leaseِ کهنه: همان بازیابیِ اتمی (take_over_lease + آزادسازی)، نه حذفِ مستقیم
            self.recover()
            batch = store.get_batch(batch.id)
            if batch.owner_token:
                return ActionResult(False, "The batch is still held by a runner; try again.")
        leftover = leftover_running(store, batch)
        if leftover:
            return ActionResult(False, leftover_running_message(leftover))
        try:
            batch, cancelled = store.cancel_inactive_batch(batch.id, expected=batch.status)
        except OwnershipError:
            return ActionResult(False, "A runner took over this batch; it was not cancelled.")
        except InvalidTransitionError as exc:
            # اجرای RUNNINGی که بینِ بررسیِ بالا و تراکنش پیدا شد — همان پیامِ راهنما
            leftover = leftover_running(store, store.get_batch(batch.id))
            return ActionResult(False, leftover_running_message(leftover) if leftover else str(exc))
        return ActionResult(
            True,
            f"The batch was cancelled. {len(cancelled)} queued issue(s) were cancelled; "
            "finished results and artifacts are kept.",
        )


_INACTIVE = (BATCH_CREATED, BATCH_PAUSED, BATCH_INTERRUPTED)


def leftover_running(store: BatchStore, batch: Batch) -> list[str]:
    """کلیدِ issueهایی که batchِ ایستاده (PAUSED/INTERRUPTED) هنوز اجرای RUNNINGِ آن‌ها را
    دارد — باقی‌مانده‌ی runnerی که پیش از بستنِ اجرا متوقف شد. این‌جا هیچ‌وقت پایانی
    نمی‌شوند؛ فقط Resume (با leaseِ خودش) آن‌ها را INTERRUPTED می‌کند."""
    if batch.status not in _INACTIVE:
        return []
    return [
        execution.issue_key
        for execution in store.executions(batch.id)
        if execution.status == EXEC_RUNNING
    ]


def leftover_running_message(keys: list[str]) -> str:
    return (
        f"The batch was not cancelled: {', '.join(keys)} is still marked RUNNING by a run that "
        "stopped before finishing it, and a running execution is never overwritten from here. "
        "Use Resume: it first takes this batch's lease (so no runner can be using it), marks "
        "that run INTERRUPTED with its results and artifacts kept, and continues the batch. "
        "You can then Pause or Cancel it; the issue in progress finishes first."
    )


_LAUNCHED = {
    MODE_START: "The batch run started.",
    MODE_RESUME: "The batch run resumed.",
    MODE_RETRY: "The retry started.",
}

# ── یک سرویس برای هر پروسه و هر پایگاه ───────────────────────────────────────
_services: dict[Path, BatchService] = {}
_services_lock = threading.Lock()


def get_service(db_path: Path | str = DEFAULT_DB_PATH, **options) -> BatchService:
    """همان سرویس برای همه‌ی sessionها و rerunهای Streamlit؛ بارِ اول بازیابیِ آغازین."""
    key = Path(db_path).resolve()
    with _services_lock:
        service = _services.get(key)
        if service is None:
            service = _services[key] = BatchService(key, **options)
    service.ensure_recovered()
    return service


def register_service(service: BatchService) -> BatchService:
    """جایگزینیِ سرویسِ یک پایگاه (برای تست با runner/executorِ ساختگی)."""
    with _services_lock:
        _services[service.db_path.resolve()] = service
    return service


def reset_services() -> None:
    with _services_lock:
        _services.clear()


__all__ = [
    "LAUNCH_WAIT_S",
    "MODE_RESUME",
    "MODE_RETRY",
    "MODE_START",
    "ActionResult",
    "BatchJob",
    "BatchService",
    "ConfigurationError",
    "ConfigurationSnapshot",
    "build_new_batch",
    "configuration_snapshot",
    "create_batch",
    "get_service",
    "leftover_running",
    "leftover_running_message",
    "register_service",
    "reset_services",
]
