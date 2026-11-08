"""
src/testcase_box/run_service.py — میزبانیِ اجرای Box برای UI (M7.6/M7.7)

    service = get_run_service(db_path)        # یکی برای هر پروسه و هر پایگاه
    plan = service.plan_new([3, 1], RunProfile(HostIntent.NORMAL))
    result = service.start(plan)              # BoxRunner در threadِ پس‌زمینه
    service.cancel(run_id)                    # درخواستِ لغو از راهِ پایگاه
    service.plan_rerun(run_id)                # همان نسخه‌های سنجاق‌شده، پروفایلِ اصلی
    service.recover(run_id, expected_generation=...)

Streamlit اسکریپت را با هر تعامل دوباره اجرا می‌کند؛ پس runner در صفحه اجرا نمی‌شود (همان الگوی
BatchService): این سرویس در سطحِ پروسه زنده است، حداکثر یک threadِ runner دارد و وضعیتِ معتبر
همیشه از SQLite خوانده می‌شود. rerun ِ صفحه هیچ چیزی را دوباره شروع نمی‌کند. همه‌ی قاعده‌ها
(برنامه‌ریزی، Gate، lease، بی‌اسکریپت، Newman) مالِ ماژول‌های خودشان‌اند؛ این‌جا فقط ترکیب است.

مرزِ وابستگی: این تنها ماژولِ Box (غیر از UI) است که runner را import می‌کند؛ خودش به Newman، Gate
یا auth import ندارد. مقدارِ متغیرهای Environment/Globals فقط در read_variable_profile خوانده و
بلافاصله دور ریخته می‌شود؛ فقط نام‌ها در برنامه می‌مانند.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.testcase_box import recovery as box_recovery
from src.testcase_box import runner as box_runner
from src.testcase_box.db import DEFAULT_DB_PATH, StorageError
from src.testcase_box.models import RUN_QUEUED, RUN_RUNNING
from src.testcase_box.profiles import ExecutionProfile, read_variable_profile
from src.testcase_box.run_planning import RunPlan, RunPlanningError, plan_new_run, plan_rerun
from src.testcase_box.run_profile import RunProfile, RunProfileError
from src.testcase_box.runner import BoxRunner, BoxRunResult
from src.testcase_box.storage import BoxStore

LAUNCH_WAIT_S = 15.0
VARIABLE_PROFILE_ID = "dev"


@dataclass
class BoxJob:
    plan_fingerprint: str
    runner: BoxRunner
    thread: threading.Thread | None = None
    result: BoxRunResult | None = None
    cancel_requested: bool = False

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def run_id(self) -> int | None:
        if self.result is not None and self.result.run_id is not None:
            return self.result.run_id
        return self.runner.current_run_id


@dataclass(frozen=True)
class ServiceResult:
    ok: bool
    message: str
    run_id: int | None = None
    refusal: str = ""
    result: BoxRunResult | None = field(default=None, repr=False)


class PlanningProblem(Exception):
    """برنامه ساخته نشد — پیامِ امن برای کاربر (هیچ مقدارِ متغیر یا محتوای case)."""


class BoxRunService:
    """حداکثر یک اجرای Box برای هر پروسه؛ اجرای هم‌زمان با Batch را خودِ Gate رد می‌کند."""

    def __init__(
        self,
        store_path: Path | str = DEFAULT_DB_PATH,
        *,
        environment_path: Path | str | None = None,
        globals_path: Path | str | None = None,
        gate_factory: Callable[[], Any] | None = None,
        artifact_root: Path | str | None = None,
        stale_after: float = box_recovery.STALE_AFTER_S,
        runner_options: dict[str, Any] | None = None,
        launch_wait: float = LAUNCH_WAIT_S,
    ) -> None:
        self.store_path = Path(store_path)
        self.environment_path = Path(environment_path or box_runner.DEV_ENVIRONMENT_PATH)
        self.globals_path = Path(globals_path or box_runner.WORKSPACE_GLOBALS_PATH)
        self.artifact_root = Path(artifact_root or box_runner.DEFAULT_ARTIFACT_ROOT)
        self._gate_factory = gate_factory
        self.stale_after = stale_after
        self._runner_options = dict(runner_options or {})
        self._launch_wait = launch_wait
        self._lock = threading.Lock()
        self._job: BoxJob | None = None

    # ── برنامه‌ریزی ─────────────────────────────────────────────────────────
    def variables(self) -> ExecutionProfile:
        try:
            return read_variable_profile(
                VARIABLE_PROFILE_ID, self.environment_path, self.globals_path
            )
        except ValueError as exc:
            raise PlanningProblem(str(exc)) from None

    def plan_new(self, case_ids: list[int], profile: RunProfile) -> RunPlan:
        variables = self.variables()
        try:
            with BoxStore.open(self.store_path) as store:
                return plan_new_run(store, case_ids, profile=profile, variables=variables)
        except RunPlanningError as exc:
            raise PlanningProblem(str(exc)) from None

    def source_profile(self, run_id: int) -> RunProfile:
        """پروفایلِ ثبت‌شده‌ی runِ مبدأ — پیش‌فرضِ rerun."""
        with BoxStore.open(self.store_path) as store:
            snapshot = store.get_run(run_id).metadata.get("profile")
        try:
            return RunProfile.from_dict(snapshot)
        except RunProfileError:
            raise PlanningProblem(
                "The source run has no valid execution profile; choose a profile for the rerun."
            ) from None

    def plan_rerun(self, run_id: int, profile: RunProfile | None = None) -> RunPlan:
        profile = profile or self.source_profile(run_id)
        variables = self.variables()
        try:
            with BoxStore.open(self.store_path) as store:
                return plan_rerun(store, run_id, profile=profile, variables=variables)
        except RunPlanningError as exc:
            raise PlanningProblem(str(exc)) from None

    # ── اجرا ────────────────────────────────────────────────────────────────
    def _new_runner(self) -> BoxRunner:
        options = {
            "environment_path": self.environment_path,
            "globals_path": self.globals_path,
            "artifact_root": self.artifact_root,
            **self._runner_options,
        }
        if self._gate_factory is not None:
            options.setdefault("gate_factory", self._gate_factory)
        return BoxRunner(self.store_path, **options)

    @property
    def job(self) -> BoxJob | None:
        return self._job

    @property
    def busy(self) -> bool:
        job = self._job
        return job is not None and job.running

    def start(self, plan: RunPlan) -> ServiceResult:
        with self._lock:
            current = self._job
            if current is not None and current.running:
                return ServiceResult(
                    False,
                    "A TestCase Box run is already in progress in this application.",
                    current.run_id,
                    refusal="ALREADY_RUNNING",
                )
            job = BoxJob(plan.fingerprint, self._new_runner())
            job.thread = threading.Thread(
                target=self._run, args=(job, plan), name="box-run-host", daemon=True
            )
            self._job = job
            job.thread.start()
        deadline = time.monotonic() + self._launch_wait
        while job.running and job.runner.current_run_id is None and time.monotonic() < deadline:
            time.sleep(0.02)
        result = job.result
        if result is not None and result.status == box_runner.SESSION_REFUSED:
            return ServiceResult(False, result.message, None, refusal=result.refusal, result=result)
        if job.run_id is None:
            return ServiceResult(
                False,
                "The run did not start in time; check the run history before trying again.",
                refusal="NOT_STARTED",
            )
        return ServiceResult(True, "The run started.", job.run_id, result=result)

    @staticmethod
    def _run(job: BoxJob, plan: RunPlan) -> None:
        try:
            job.result = job.runner.run(plan)
        except Exception as exc:  # noqa: BLE001 — threadِ پس‌زمینه بی‌صدا نمی‌میرد
            job.result = BoxRunResult(
                box_runner.SESSION_INTERRUPTED,
                run_id=job.runner.current_run_id,
                stop_reason="RUNNER_EXCEPTION",
                message=f"The runner failed unexpectedly ({type(exc).__name__}).",
            )

    def wait(self, timeout: float | None = None) -> bool:
        job = self._job
        if job is not None and job.thread is not None:
            job.thread.join(timeout)
        return not self.busy

    def cancel(self, run_id: int) -> ServiceResult:
        """درخواستِ لغو از راهِ پایگاه (هر session)؛ runner و موتور لغو را انجام می‌دهند."""
        try:
            with BoxStore.open(self.store_path) as store:
                run = store.get_run(run_id)
                if run.status not in (RUN_QUEUED, RUN_RUNNING):
                    return ServiceResult(False, f"The run is already {run.status}.", run_id)
                ownership = store.request_run_cancel(run_id)
        except StorageError as exc:
            return ServiceResult(False, str(exc), run_id)
        job = self._job
        if job is not None and job.run_id == run_id:
            job.cancel_requested = True
        if not ownership.leased:
            return ServiceResult(
                True,
                "Cancellation was recorded, but no runner owns this run; recover it to close it.",
                run_id,
            )
        return ServiceResult(
            True, "Cancellation requested. Waiting for the runner to confirm it.", run_id
        )

    # ── بازیابی ─────────────────────────────────────────────────────────────
    def inspect_recovery(self) -> list[box_recovery.RecoveryCandidate]:
        return box_recovery.inspect_runs(
            self.store_path, gate_factory=self._gate_factory, stale_after=self.stale_after
        )

    def recover(self, run_id: int, expected_generation: int | None = None):
        job = self._job
        if job is not None and job.running and job.run_id == run_id:
            return box_recovery.RecoveryResult(
                box_recovery.BLOCKED,
                run_id,
                box_recovery.BLOCKED_GATE_BUSY,
                message="This application is still running the run; it cannot be recovered.",
            )
        return box_recovery.recover_run(
            self.store_path,
            run_id,
            gate_factory=self._gate_factory,
            stale_after=self.stale_after,
            expected_generation=expected_generation,
        )


_services: dict[Path, BoxRunService] = {}
_services_lock = threading.Lock()


def get_run_service(store_path: Path | str = DEFAULT_DB_PATH, **options) -> BoxRunService:
    """همان سرویس برای همه‌ی sessionها و rerunهای Streamlit."""
    key = Path(store_path).resolve()
    with _services_lock:
        service = _services.get(key)
        if service is None:
            service = _services[key] = BoxRunService(key, **options)
    return service


def register_run_service(service: BoxRunService) -> BoxRunService:
    with _services_lock:
        _services[service.store_path.resolve()] = service
    return service


def reset_run_services() -> None:
    with _services_lock:
        _services.clear()


__all__ = [
    "BoxJob",
    "BoxRunService",
    "PlanningProblem",
    "ServiceResult",
    "get_run_service",
    "register_run_service",
    "reset_run_services",
]
