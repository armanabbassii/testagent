"""
src/testcase_box/runner.py — اجرای یک run ِ TestCase Box (M7.5)

    result = BoxRunner(store_path).run(plan)      # plan از run_planning (M7.4)

مرزِ وابستگی: این تنها ماژولِ بسته‌ی Box است که به موتورِ Newman (src.test_execution.newman)،
Execution Gate ِ مشترک (src.execution_gate) و منبعِ توکن (src.auth) وابسته است. هیچ ماژولِ دیگری
از Box این ماژول را import نمی‌کند (test_contract هر دو جهت را بررسی می‌کند). موتور تغییر نکرده
است و فقط از راهِ NewmanExecutionEngine.run(NewmanRunRequest, cancel_event) صدا زده می‌شود.

چرخه‌ی یک نشست، به این ترتیب:
  1. برنامه: نوعِ RunPlan، قابلِ ثبت بودنِ همه‌ی ورودی‌ها، دستِ کم یک ورودیِ واجد، و ساختِ
     کالکشن‌ها (prepare_collections) — شکست → REFUSED، هیچ چیزی ساخته نمی‌شود؛
  2. ریشه‌ی artifact (data/testcase_box/runs/) امن و پوشه است؛
  3. Gate ِ مشترک با kind="BOX" — تا پایانِ واقعیِ نشست نگه داشته می‌شود؛ Batch در این مدت
     شروع نمی‌شود (و برعکس)؛
  4. snapshot ِ Environment/Globals (هش)، پروفایلِ متغیرهای ساخته‌شده از همان بایت‌ها باید با
     برنامه بخواند، و برنامه‌ریزیِ دوباره باید همان fingerprint را بدهد — وگرنه REFUSED؛
  5. create_owned_run (lease ِ پایگاه، نسلِ ۱) و پوشه‌ی یکتای run؛ threadِ پایش heartbeat ِ
     lease و Gate را می‌زند و درخواستِ لغو را می‌خواند؛ start_run؛
  6. unitهای مسدود → NOT_RUN (caseها از ابتدا NOT_EXECUTABLE اند)؛ توکن یک بار؛
  7. برای هر unitِ واجد، به ترتیب: شرط‌های توقف، هشِ پیکربندی، بررسیِ کاملِ یکپارچگی (نسخه،
     اثرانگشت‌ها، سیاستِ بی‌اسکریپت، آماده‌سازیِ دوباره، کالکشن)، assert ِ Gate و lease، نوشتنِ
     collection.json، start_unit → engine.run → finish_unit؛
  8. finish_run ِ اتمی (COMPLETED / CANCELLED / INTERRUPTED با stop_reason)؛
  9. finally: توقفِ پایش و آزادسازیِ Gate — فقط اگر پایانِ پروسه‌ی Newman تأیید شده باشد.

پایانِ تأییدنشده (termination_confirmed): فقط دو حالت اثبات‌اند — پروسه شروع و reap شد
(process_id و exit_code)، یا خطای صریحِ پیش از شروع بی‌process_id. هر چیزِ دیگر — نبودِ
process_id به‌تنهایی، INTERNAL_ERROR ِ catch-all ِ موتور (مثلاً timeout ِ بستنِ درختِ پروسه)، یا
استثنای engine.run — یعنی هیچ unitِ دیگری شروع نمی‌شود، run نهایی نمی‌شود و Gate آزاد نمی‌شود — lease ِ آن در
_UNRELEASED_GATES نگه داشته می‌شود تا handle ِ قفلِ سیستم‌عامل تا پایانِ همین پروسه زنده بماند.
آزاد شدن فقط با بسته شدنِ پروسه‌ی برنامه است (runner.js با مرگِ والد خودش متوقف می‌شود).

نتیجه‌ها (R1): PASS ِ بی‌assertion در پایگاه همان PASS ِ موتور است ولی verification=UNVERIFIED
دارد و هرگز موفقیتِ تأییدشده شمرده نمی‌شود؛ در V1 (بی‌اسکریپت) همه‌ی PASSها UNVERIFIED اند.

توکن فقط در حافظه و فقط از راهِ NewmanRunRequest.access_token به موتور می‌رسد؛ در پایگاه،
فراداده، artifactها، نام‌ها، repr و پیام‌ها نیست. توکنِ مالکیت (RunLease.token) هم همین‌طور.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from src.auth import AccessToken, AuthenticationError, TokenProvider
from src.execution_gate import ExecutionGate, GateBusy, GateError, GateLease, GateLeaseLost
from src.test_execution.newman import NewmanExecutionEngine, NewmanOptions, NewmanRunRequest
from src.test_execution.newman.models import (
    DEV_ENVIRONMENT_PATH,
    ERROR_ACCESS_TOKEN_INVALID,
    ERROR_HOST_VARIABLE_EMPTY,
    ERROR_HOST_VARIABLE_IN_COLLECTION,
    ERROR_INPUT_FILE_MODIFIED,
    ERROR_INVALID_COLLECTION,
    ERROR_INVALID_ENVIRONMENT,
    ERROR_INVALID_GLOBALS,
    ERROR_INVALID_OPTIONS,
    ERROR_NETWORK,
    ERROR_NEWMAN_NOT_FOUND,
    ERROR_NODE_NOT_FOUND,
    ERROR_OUTPUT_DIRECTORY,
    ERROR_PRIVATE_DIRECTORY,
    ERROR_PROCESS_CANCELLED,
    ERROR_PROCESS_START_FAILED,
    ERROR_PROCESS_TIMEOUT,
    ERROR_REPORT_INCONSISTENT,
    ERROR_REPORT_MALFORMED,
    ERROR_REPORT_MISSING,
    ERROR_REPORTER_NOT_FOUND,
    ERROR_SANITIZATION_FAILED,
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
    WORKSPACE_GLOBALS_PATH,
    NewmanRunResult,
)
from src.test_execution.newman.redaction import (
    SanitizationError,
    variable_secrets,
    verify_clean_json,
)
from src.testcase_box.collection_preparation import (
    CollectionPreparationError,
    PreparedRun,
    PreparedUnit,
    build_unit_collection,
    prepare_collections,
    validate_unit_collection,
)
from src.testcase_box.db import REPO_ROOT, ActiveRunError, RunOwnershipError, StorageError
from src.testcase_box.models import (
    CASE_RUN_CANCELLED,
    CASE_RUN_ERROR,
    CASE_RUN_FAIL,
    CASE_RUN_NOT_EXECUTABLE,
    CASE_RUN_NOT_RUN,
    CASE_RUN_PASS,
    CASE_RUN_QUEUED,
    RUN_ACTION_CANCEL,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_INTERRUPTED,
    UNIT_CANCELLED,
    UNIT_ERROR,
    UNIT_FAIL,
    UNIT_NOT_RUN,
    UNIT_PASS,
    UNIT_QUEUED,
    UNIT_RUNNING,
    NewRun,
    NewRunCase,
    NewRunUnit,
    RunLease,
    sha256_json,
    to_json,
)
from src.testcase_box.preparation import prepare_case
from src.testcase_box.profiles import variable_profile_from_documents
from src.testcase_box.run_planning import (
    PLAN_NEW,
    PLAN_RERUN,
    PlanEntry,
    RunPlan,
    RunPlanningError,
    plan_new_run,
    plan_rerun,
    script_free_findings,
)
from src.testcase_box.run_profile import RunProfile, RunProfileError
from src.testcase_box.storage import BoxStore

DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "data" / "testcase_box" / "runs"
HEARTBEAT_INTERVAL_S = 10.0
POLL_INTERVAL_S = 1.0
STALE_AFTER_S = 60.0

# ── نتیجه‌ی نشست ────────────────────────────────────────────────────────────
SESSION_COMPLETED = "COMPLETED"
SESSION_CANCELLED = "CANCELLED"
SESSION_INTERRUPTED = "INTERRUPTED"
SESSION_REFUSED = "REFUSED"  # هیچ run ای ساخته نشد و هیچ Newman ای اجرا نشد
SESSION_TERMINATION_UNCONFIRMED = "TERMINATION_UNCONFIRMED"  # Gate عمداً آزاد نشد

# دلیلِ رد (پیش از ساختنِ run)
REFUSAL_PLAN_INVALID = "PLAN_INVALID"
REFUSAL_NOTHING_TO_EXECUTE = "NOTHING_TO_EXECUTE"
REFUSAL_PLAN_NOT_RECORDABLE = "PLAN_NOT_RECORDABLE"
REFUSAL_ARTIFACT_ROOT_UNSAFE = "ARTIFACT_ROOT_UNSAFE"
REFUSAL_GATE_BUSY = "GATE_BUSY"
REFUSAL_GATE_UNAVAILABLE = "GATE_UNAVAILABLE"
REFUSAL_CONFIGURATION_INVALID = "CONFIGURATION_INVALID"
REFUSAL_VARIABLES_CHANGED = "VARIABLES_CHANGED"
REFUSAL_PLAN_CHANGED = "PLAN_CHANGED"
REFUSAL_ACTIVE_RUN = "ACTIVE_RUN"
REFUSAL_STORAGE_FAILED = "STORAGE_FAILED"

# دلیلِ توقفِ run ِ شروع‌شده (R2/R3) — در metadata.stop_reason
STOP_USER_CANCELLED = "USER_CANCELLED"
STOP_GATE_LOST = "GATE_LOST"
STOP_GATE_UNAVAILABLE = "GATE_UNAVAILABLE"
STOP_LEASE_LOST = "LEASE_LOST"
STOP_HEARTBEAT_FAILED = "HEARTBEAT_FAILED"
STOP_CONFIGURATION_CHANGED = "CONFIGURATION_CHANGED"
STOP_AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
STOP_INTEGRITY_FAILED = "INTEGRITY_FAILED"
STOP_ENGINE_ENVIRONMENT = "ENGINE_ENVIRONMENT_ERROR"
STOP_ARTIFACT_PUBLICATION = "ARTIFACT_PUBLICATION_FAILED"
STOP_STORAGE_FAILED = "STORAGE_FAILED"
STOP_RUNNER_EXCEPTION = "RUNNER_EXCEPTION"
STOP_TERMINATION_UNCONFIRMED = "TERMINATION_UNCONFIRMED"

# طبقه‌بندیِ هر unit (metadata.classification) و درستی‌سنجی (metadata.verification)
CLASS_VERIFIED_PASS = "VERIFIED_PASS"
CLASS_UNVERIFIED = "UNVERIFIED"
CLASS_ASSERTION_FAILED = "ASSERTION_FAILED"
CLASS_CANCELLED = "CANCELLED"
CLASS_STOPPED_BY_RUNNER = "STOPPED_BY_RUNNER"
CLASS_NETWORK_ERROR = "NETWORK_ERROR"  # از جمله timeoutِ درخواست
CLASS_PROCESS_TIMEOUT = "PROCESS_TIMEOUT"
CLASS_ARTIFACT_UNSAFE = "ARTIFACT_UNSAFE"  # پاک‌سازی/نشتِ راز — هیچ گزارشی منتشر نشد
CLASS_REPORT_INVALID = "REPORT_INVALID"
CLASS_ARTIFACT_INVALID = "ARTIFACT_INVALID"
CLASS_ENGINE_ENVIRONMENT = "ENGINE_ENVIRONMENT_ERROR"
CLASS_ENGINE_ERROR = "ENGINE_ERROR"
CLASS_BLOCKED = "BLOCKED"  # در برنامه واجد نبود
CLASS_PRE_EXECUTION_BLOCKED = "PRE_EXECUTION_BLOCKED"  # بررسیِ پیش از Newman رد کرد
CLASS_NOT_RUN = "NOT_RUN"  # run پیش از رسیدن به این unit متوقف شد
VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
VERIFICATION_FAILED = "FAILED"
NOT_VERIFIED = "NOT_VERIFIED"

# خطاهای موتور که برای unitهای بعدی هم تکرار می‌شوند → توقفِ run (R3)
STOP_RUN_ERROR_KINDS = frozenset(
    {
        ERROR_NODE_NOT_FOUND,
        ERROR_NEWMAN_NOT_FOUND,
        ERROR_REPORTER_NOT_FOUND,
        ERROR_INVALID_OPTIONS,
        ERROR_INVALID_ENVIRONMENT,
        ERROR_INVALID_GLOBALS,
        ERROR_HOST_VARIABLE_EMPTY,
        ERROR_OUTPUT_DIRECTORY,
        ERROR_PRIVATE_DIRECTORY,
        ERROR_INPUT_FILE_MODIFIED,
        ERROR_ACCESS_TOKEN_INVALID,
    }
)
# خطاهایی که موتور فقط پیش از اجرای پروسه می‌دهد (engine._run پیش از _execute، و classify برای
# outcome ِ started=False). فقط همراه با process_id ِ None اثباتِ «هیچ پروسه‌ای شروع نشد» است.
# process_id فقط وقتی run_process برگردد پر می‌شود؛ پس هر نتیجه‌ی دیگری بی‌process_id — به‌ویژه
# INTERNAL_ERROR ِ catch-all ِ موتور، مثلاً پس از timeout ِ بستنِ درختِ پروسه در _kill_tree —
# مبهم است و پایانِ پروسه تأییدنشده حساب می‌شود (fail-closed).
PRE_START_ERROR_KINDS = frozenset(
    {
        ERROR_INVALID_OPTIONS,
        ERROR_ACCESS_TOKEN_INVALID,
        ERROR_NODE_NOT_FOUND,
        ERROR_NEWMAN_NOT_FOUND,
        ERROR_REPORTER_NOT_FOUND,
        ERROR_INVALID_COLLECTION,
        ERROR_INVALID_ENVIRONMENT,
        ERROR_INVALID_GLOBALS,
        ERROR_HOST_VARIABLE_IN_COLLECTION,
        ERROR_HOST_VARIABLE_EMPTY,
        ERROR_OUTPUT_DIRECTORY,
        ERROR_PRIVATE_DIRECTORY,
        ERROR_PROCESS_START_FAILED,
    }
)


def termination_confirmed(result: NewmanRunResult) -> bool:
    """آیا نتیجه‌ی موتور ثابت می‌کند هیچ پروسه‌ی Newman ای زنده نمانده است؟

    * پروسه شروع شد و reap شد: process_id و exit_code هر دو هست؛
    * پروسه هرگز شروع نشد: process_id نیست و خطا یکی از خطاهای پیش از شروع است، یا لغو پیش از
      شروع (PROCESS_CANCELLED + cancelled + بی‌process_id).
    هر چیزِ دیگر — نبودِ process_id به‌تنهایی، INTERNAL_ERROR، PASS/FAIL بی‌process_id، … — اثبات
    نیست و False برمی‌گرداند."""
    if result.process_id is not None:
        return result.exit_code is not None
    if result.status != STATUS_ERROR:
        return False
    if result.error_kind in PRE_START_ERROR_KINDS:
        return True
    return result.error_kind == ERROR_PROCESS_CANCELLED and result.cancelled


_ERROR_CLASSES = {
    ERROR_NETWORK: CLASS_NETWORK_ERROR,
    ERROR_PROCESS_TIMEOUT: CLASS_PROCESS_TIMEOUT,
    ERROR_SANITIZATION_FAILED: CLASS_ARTIFACT_UNSAFE,
    ERROR_REPORT_MISSING: CLASS_REPORT_INVALID,
    ERROR_REPORT_MALFORMED: CLASS_REPORT_INVALID,
    ERROR_REPORT_INCONSISTENT: CLASS_REPORT_INVALID,
}

# leaseهای Gate که آزادشان نکرده‌ایم چون پایانِ پروسه‌ی Newman تأیید نشد — نگه داشتنِ مرجع یعنی
# handle ِ قفلِ سیستم‌عامل تا پایانِ همین پروسه بسته نمی‌شود (هیچ‌وقت خودکار آزاد نمی‌شوند)
_UNRELEASED_GATES: list[GateLease] = []


class Engine(Protocol):
    def run(
        self, request: NewmanRunRequest, cancel_event: threading.Event | None = None
    ) -> NewmanRunResult: ...


class TokenSource(Protocol):
    def fetch(self) -> AccessToken: ...


def _default_gate() -> ExecutionGate:
    return ExecutionGate()


def _default_engine() -> Engine:
    return NewmanExecutionEngine()


def _default_token_provider() -> TokenSource:
    return TokenProvider()


# ── رکوردهای نتیجه (بی‌توکن) ─────────────────────────────────────────────────


@dataclass(frozen=True)
class UnitOutcome:
    unit_number: int
    case_id: int
    case_version_id: int
    status: str
    classification: str
    verification: str


@dataclass(frozen=True)
class BoxRunResult:
    status: str
    run_id: int | None = None
    refusal: str = ""
    stop_reason: str = ""
    message: str = ""
    units: tuple[UnitOutcome, ...] = ()
    cleanup_errors: tuple[str, ...] = ()
    artifact_dir: str = ""

    @property
    def verified_passes(self) -> int:
        return sum(1 for unit in self.units if unit.verification == VERIFIED)

    @property
    def unverified(self) -> int:
        return sum(1 for unit in self.units if unit.verification == UNVERIFIED)


class _Refused(Exception):  # noqa: N818 — کنترلِ جریانِ داخلی
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class _Stop(Exception):  # noqa: N818 — کنترلِ جریانِ داخلی
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason, self.message = reason, message


@dataclass
class _Session:
    store: BoxStore
    plan: RunPlan
    prepared: PreparedRun
    gate: GateLease
    run_dir: Path | None = None
    lease: RunLease | None = None
    run_id: int | None = None
    token: AccessToken | None = field(default=None, repr=False)
    secrets: set[str] = field(default_factory=set, repr=False)
    config_hashes: tuple[str, str] = ("", "")
    outcomes: list[UnitOutcome] = field(default_factory=list)
    unconfirmed: bool = False
    finalized: bool = False
    cleanup_errors: list[str] = field(default_factory=list)


# ── پایش: heartbeat ِ lease و Gate، درخواستِ لغو ──────────────────────────────


class _Monitor(threading.Thread):
    """اتصالِ جدای خودش؛ هر شکست ثبت و unitِ جاری متوقف می‌شود — هیچ استثنایی گم نمی‌شود."""

    def __init__(
        self,
        open_store: Callable[[], BoxStore],
        lease: RunLease,
        gate: GateLease,
        *,
        heartbeat_interval: float,
        poll_interval: float,
        failure_after: float,
    ) -> None:
        super().__init__(name=f"box-runner-monitor-{lease.run_id}", daemon=True)
        self._open_store, self._lease, self._gate = open_store, lease, gate
        self._heartbeat_interval, self._poll_interval = heartbeat_interval, poll_interval
        self._failure_after = failure_after
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._active: threading.Event | None = None
        self.cancel_requested = threading.Event()
        self.failure: tuple[str, str] | None = None
        self.beats = 0

    def set_active(self, event: threading.Event | None) -> None:
        with self._lock:
            self._active = event
            if event is not None and (self.failure or self.cancel_requested.is_set()):
                event.set()

    def _signal(self) -> None:
        with self._lock:
            if self._active is not None:
                self._active.set()

    def _fail(self, reason: str, message: str) -> None:
        if self.failure is None:
            self.failure = (reason, message)
        self._signal()

    def halt(self) -> None:
        self._halt.set()
        self.join()

    def run(self) -> None:
        try:
            store = self._open_store()
        except Exception as exc:  # noqa: BLE001
            self._fail(STOP_HEARTBEAT_FAILED, f"The heartbeat store failed ({type(exc).__name__}).")
            return
        try:
            last_ok = next_beat = time.monotonic()
            while not self._halt.is_set():
                moment = time.monotonic()
                if moment >= next_beat:
                    try:
                        store.heartbeat_run(self._lease)
                        last_ok, next_beat = moment, moment + self._heartbeat_interval
                        self.beats += 1
                    except RunOwnershipError:
                        self._fail(STOP_LEASE_LOST, "The runner lost its run lease.")
                        return
                    except Exception as exc:  # noqa: BLE001
                        if moment - last_ok >= self._failure_after:
                            self._fail(
                                STOP_HEARTBEAT_FAILED,
                                f"The run heartbeat kept failing ({type(exc).__name__}).",
                            )
                            return
                        next_beat = moment + self._poll_interval
                    else:
                        try:
                            self._gate.heartbeat()
                        except GateLeaseLost:
                            self._fail(STOP_GATE_LOST, "The runner lost the shared execution gate.")
                            return
                        except Exception:  # noqa: BLE001 — fail-closed
                            self._fail(
                                STOP_GATE_UNAVAILABLE,
                                "The shared execution gate could not be confirmed.",
                            )
                            return
                try:
                    ownership = store.run_ownership(self._lease.run_id)
                    if ownership.requested_action == RUN_ACTION_CANCEL:
                        self.cancel_requested.set()
                        self._signal()
                except Exception:  # noqa: BLE001, S110 — سلامتِ پایگاه را heartbeat می‌سنجد
                    pass
                self._halt.wait(self._poll_interval)
        finally:
            store.close()


# ── runner ───────────────────────────────────────────────────────────────────


def _profile_valid(profile: object) -> bool:
    """از خودِ فیلدها دوباره ساخته می‌شود (نه از to_dict که bail/insecure را ثابت می‌نویسد)؛
    هر مقدارِ دست‌کاری‌شده — مثلاً insecure=True — رد می‌شود."""
    if not isinstance(profile, RunProfile):
        return False
    try:
        rebuilt = RunProfile(
            host_intent=profile.host_intent,
            request_timeout_ms=profile.request_timeout_ms,
            process_timeout_s=profile.process_timeout_s,
            bail=profile.bail,
            insecure=profile.insecure,
        )
    except (RunProfileError, TypeError):
        return False
    return rebuilt == profile


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


class BoxRunner:
    """یک نمونه = یک نشست در هر لحظه. همه‌ی وابستگی‌ها برای تست قابلِ جایگزینی‌اند."""

    def __init__(
        self,
        store_path: Path | str,
        *,
        gate_factory: Callable[[], ExecutionGate] | None = None,
        engine: Engine | None = None,
        token_provider: TokenSource | None = None,
        environment_path: Path | str = DEV_ENVIRONMENT_PATH,
        globals_path: Path | str = WORKSPACE_GLOBALS_PATH,
        artifact_root: Path | str | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
        poll_interval: float = POLL_INTERVAL_S,
        stale_after: float = STALE_AFTER_S,
    ) -> None:
        self.store_path = Path(store_path)
        self._gate_factory = gate_factory
        self._engine = engine
        self._token_provider = token_provider
        self.environment_path = Path(environment_path)
        self.globals_path = Path(globals_path)
        self.artifact_root = Path(artifact_root) if artifact_root is not None else None
        self._heartbeat_interval = heartbeat_interval
        self._poll_interval = poll_interval
        self._failure_after = stale_after / 2
        self._busy = threading.Lock()
        self._monitor: _Monitor | None = None
        # شناسه‌ی runی که همین نشست ساخت (نه توکن) — برای میزبانِ پس‌زمینه (run_service)
        self.current_run_id: int | None = None

    # ── ورودی ───────────────────────────────────────────────────────────────
    def run(self, plan: RunPlan) -> BoxRunResult:
        if not self._busy.acquire(blocking=False):
            return BoxRunResult(
                SESSION_REFUSED, refusal=REFUSAL_GATE_BUSY, message="This runner is busy."
            )
        try:
            self.current_run_id = None
            return self._run(plan)
        finally:
            self._busy.release()

    def _run(self, plan: RunPlan) -> BoxRunResult:
        try:
            prepared = self._check_plan(plan)
            root = self._check_artifact_root()
        except _Refused as refused:
            return BoxRunResult(SESSION_REFUSED, refusal=refused.code, message=refused.message)
        gate = self._gate_factory or _default_gate
        try:
            gate_lease = gate().acquire("BOX", f"box-plan-{plan.fingerprint[:16]}")
        except GateBusy as exc:
            return BoxRunResult(SESSION_REFUSED, refusal=REFUSAL_GATE_BUSY, message=str(exc))
        except GateError as exc:
            return BoxRunResult(SESSION_REFUSED, refusal=REFUSAL_GATE_UNAVAILABLE, message=str(exc))
        session: _Session | None = None
        result: BoxRunResult | None = None
        try:
            store = BoxStore.open(self.store_path)
            session = _Session(store=store, plan=plan, prepared=prepared, gate=gate_lease)
            result = self._session(session, root)
            return result
        except _Refused as refused:
            result = BoxRunResult(SESSION_REFUSED, refusal=refused.code, message=refused.message)
            return result
        except (StorageError, sqlite3.Error) as exc:
            result = BoxRunResult(
                SESSION_REFUSED,
                refusal=REFUSAL_STORAGE_FAILED,
                message=f"The TestCase Box library could not be opened ({type(exc).__name__}).",
            )
            return result
        finally:
            unconfirmed = session is not None and session.unconfirmed
            notes = self._release_gate(gate_lease, unconfirmed)
            if session is not None:
                session.store.close()
            if notes and result is not None:
                object.__setattr__(result, "cleanup_errors", (*result.cleanup_errors, *notes))

    @staticmethod
    def _release_gate(gate_lease: GateLease, unconfirmed: bool) -> list[str]:
        if unconfirmed:
            _UNRELEASED_GATES.append(gate_lease)  # عمداً آزاد نمی‌شود
            return [
                "The shared execution gate was kept: termination of the Newman process was not "
                "confirmed. It is released when this application process exits."
            ]
        try:
            outcome = gate_lease.release()
        except Exception as exc:  # noqa: BLE001 — قفلِ سیستم‌عامل در release آزاد می‌شود
            return [f"The shared execution gate release failed ({type(exc).__name__})."]
        if outcome.value == "RECORD_NOT_CLEARED":
            return ["The shared execution gate owner record could not be cleared."]
        return []

    # ── بررسی‌های پیش از Gate ────────────────────────────────────────────────
    @staticmethod
    def _check_plan(plan: RunPlan) -> PreparedRun:
        if not isinstance(plan, RunPlan):
            raise _Refused(REFUSAL_PLAN_INVALID, "A RunPlan is required.")
        if not _profile_valid(plan.profile):
            raise _Refused(REFUSAL_PLAN_INVALID, "The run profile is not valid.")
        unrecordable = [
            entry.position
            for entry in plan.entries
            if entry.case_id is None
            or entry.version_id is None
            or any(f.code in ("SELECTION_DUPLICATE", "CASE_NOT_FOUND") for f in entry.blocking)
        ]
        if unrecordable:
            raise _Refused(
                REFUSAL_PLAN_NOT_RECORDABLE,
                "Selected entries cannot be recorded in a run (positions "
                + ", ".join(str(p) for p in unrecordable)
                + "); fix the selection. Nothing was started.",
            )
        if not plan.executable:
            raise _Refused(REFUSAL_NOTHING_TO_EXECUTE, "No selected test case can be executed.")
        try:
            return prepare_collections(plan)
        except CollectionPreparationError as exc:
            raise _Refused(
                REFUSAL_PLAN_INVALID, f"Collection preparation failed ({exc.code})."
            ) from None

    def _check_artifact_root(self) -> Path:
        root = self.artifact_root if self.artifact_root is not None else DEFAULT_ARTIFACT_ROOT
        try:
            if root.is_symlink() or (root.exists() and not root.is_dir()):
                raise _Refused(
                    REFUSAL_ARTIFACT_ROOT_UNSAFE, "The run artifact root is not a directory."
                )
            root.mkdir(parents=True, exist_ok=True)
            return root.resolve()
        except OSError as exc:
            raise _Refused(
                REFUSAL_ARTIFACT_ROOT_UNSAFE,
                f"The run artifact root cannot be used ({type(exc).__name__}).",
            ) from None

    # ── نشست ────────────────────────────────────────────────────────────────
    def _read_configuration(self) -> tuple[tuple[str, str], dict, dict]:
        documents, hashes = [], []
        for label, path in (("environment", self.environment_path), ("globals", self.globals_path)):
            try:
                raw = Path(path).read_bytes()
                document = json.loads(raw.decode("utf-8-sig"))
            except (OSError, UnicodeDecodeError, ValueError):
                raise _Refused(
                    REFUSAL_CONFIGURATION_INVALID, f"The Postman {label} file cannot be read."
                ) from None
            hashes.append(hashlib.sha256(raw).hexdigest())
            documents.append(document)
        return (hashes[0], hashes[1]), documents[0], documents[1]

    def _session(self, session: _Session, root: Path) -> BoxRunResult:
        plan, store = session.plan, session.store
        session.config_hashes, environment, globals_document = self._read_configuration()
        try:
            variables = variable_profile_from_documents(
                plan.variables.profile_id, environment, globals_document
            )
        except ValueError:
            raise _Refused(
                REFUSAL_CONFIGURATION_INVALID, "The Postman variables are malformed."
            ) from None
        if (variables.resolved_variables, variables.empty_variables) != (
            plan.variables.resolved_variables,
            plan.variables.empty_variables,
        ):
            raise _Refused(
                REFUSAL_VARIABLES_CHANGED,
                "The Environment/Globals variables differ from the plan; plan the run again.",
            )
        session.secrets = variable_secrets(environment) | variable_secrets(globals_document)
        self._check_replan(store, plan)

        try:
            run, lease = store.create_owned_run(
                self._new_run(plan),
                pid=os.getpid(),
                host=socket.gethostname(),
            )
        except ActiveRunError as exc:
            raise _Refused(REFUSAL_ACTIVE_RUN, str(exc)) from None
        session.run_id, session.lease = run.id, lease
        self.current_run_id = run.id
        try:
            session.run_dir = self._create_run_dir(root, run.id)
        except OSError as exc:
            self._finalize(
                session,
                RUN_INTERRUPTED,
                STOP_ARTIFACT_PUBLICATION,
                f"The run directory could not be created ({type(exc).__name__}).",
            )
            return self._result(
                session,
                SESSION_INTERRUPTED,
                STOP_ARTIFACT_PUBLICATION,
                "The run directory could not be created.",
            )

        monitor = _Monitor(
            lambda: BoxStore.open(self.store_path),
            lease,
            session.gate,
            heartbeat_interval=self._heartbeat_interval,
            poll_interval=self._poll_interval,
            failure_after=self._failure_after,
        )
        self._monitor = monitor
        monitor.start()
        try:
            return self._execute(session, monitor)
        finally:
            self._monitor = None
            monitor.halt()
            session.token = None

    def _check_replan(self, store: BoxStore, plan: RunPlan) -> None:
        try:
            if plan.kind == PLAN_RERUN:
                fresh = plan_rerun(
                    store, plan.rerun_of_run_id, profile=plan.profile, variables=plan.variables
                )
            elif plan.kind == PLAN_NEW:
                fresh = plan_new_run(
                    store,
                    [entry.case_id for entry in plan.entries],
                    profile=plan.profile,
                    variables=plan.variables,
                )
            else:
                raise _Refused(REFUSAL_PLAN_INVALID, "Unknown plan kind.")
        except RunPlanningError:
            raise _Refused(REFUSAL_PLAN_CHANGED, "The plan can no longer be reproduced.") from None
        if fresh.fingerprint != plan.fingerprint:
            raise _Refused(
                REFUSAL_PLAN_CHANGED,
                "The library changed since the run was planned; plan the run again.",
            )

    @staticmethod
    def _new_run(plan: RunPlan) -> NewRun:
        units = tuple(
            NewRunUnit(
                entry.unit_number,
                (
                    NewRunCase(
                        entry.case_id,
                        entry.version_id,
                        1,
                        status=CASE_RUN_QUEUED if entry.eligible else CASE_RUN_NOT_EXECUTABLE,
                        result_metadata={}
                        if entry.eligible
                        else {"blocking": sorted({f.code for f in entry.blocking})},
                    ),
                ),
                metadata={
                    "case_version_id": entry.version_id,
                    "prepared_fingerprint": entry.prepared_fingerprint,
                },
            )
            for entry in plan.entries
        )
        return NewRun(
            units=units,
            retry_of_run_id=plan.rerun_of_run_id,
            metadata={
                "plan_fingerprint": plan.fingerprint,
                "plan_kind": plan.kind,
                "profile": plan.profile.to_dict(),
                "variables": {
                    "profile_id": plan.variables.profile_id,
                    "resolved": sorted(plan.variables.resolved_variables),
                    "empty": sorted(plan.variables.empty_variables),
                },
            },
        )

    @staticmethod
    def _create_run_dir(root: Path, run_id: int) -> Path:
        directory = root / f"run_{run_id:06d}_{_now_stamp()}_{uuid.uuid4().hex[:8]}"
        directory.mkdir(exist_ok=False)  # هرگز بازنویسی نمی‌شود
        resolved = directory.resolve()
        if resolved.parent != root:
            raise OSError("The run directory escapes the artifact root.")
        return resolved

    # ── اجرا ────────────────────────────────────────────────────────────────
    def _execute(self, session: _Session, monitor: _Monitor) -> BoxRunResult:
        store, lease = session.store, session.lease
        try:
            store.start_run(
                lease,
                metadata={
                    **store.get_run(session.run_id).metadata,
                    "artifact_dir": session.run_dir.name,
                    "environment_sha256": session.config_hashes[0],
                    "globals_sha256": session.config_hashes[1],
                },
            )
            units = {u.unit_number: u for u in store.run_units(session.run_id)}
            for entry in session.plan.blocked_entries:
                unit = units[entry.unit_number]
                store.finish_unit(
                    lease,
                    unit.id,
                    UNIT_NOT_RUN,
                    unit_metadata={**unit.metadata, "classification": CLASS_BLOCKED},
                )
                session.outcomes.append(
                    UnitOutcome(
                        entry.unit_number,
                        entry.case_id,
                        entry.version_id,
                        UNIT_NOT_RUN,
                        CLASS_BLOCKED,
                        NOT_VERIFIED,
                    )
                )
            self._authenticate(session)
            for prepared in session.prepared.units:
                self._check_stop(session, monitor)
                entry = next(
                    e for e in session.plan.entries if e.unit_number == prepared.unit_number
                )
                self._run_unit(session, monitor, units[prepared.unit_number].id, entry, prepared)
            # پس از آخرین unit، وضعیتِ پایانی از نتیجه‌های واقعیِ unitها می‌آید: لغو یا تغییرِ
            # پیکربندی‌ای که فقط پس از پایانِ همه‌ی کار دیده شود runِ کامل را برچسب نمی‌زند، ولی
            # unitی که وسطِ اجرا متوقف شد یعنی کار کامل نشده است.
            self._check_completion(session, monitor)
        except _Stop as stop:
            return self._stopped(session, stop)
        except RunOwnershipError:
            return self._stopped(session, _Stop(STOP_LEASE_LOST, "The runner lost its run lease."))
        except (StorageError, sqlite3.Error) as exc:  # BoxStore فقط IntegrityError را می‌پوشاند
            return self._stopped(
                session,
                _Stop(STOP_STORAGE_FAILED, f"Persisting the run failed ({type(exc).__name__})."),
            )
        except Exception as exc:  # noqa: BLE001 — هیچ نتیجه‌ی ساختگی؛ run متوقف و ثبت می‌شود
            return self._stopped(
                session,
                _Stop(
                    STOP_RUNNER_EXCEPTION, f"The runner failed unexpectedly ({type(exc).__name__})."
                ),
            )
        if not self._finalize(session, RUN_COMPLETED, "", ""):
            return self._result(
                session, SESSION_INTERRUPTED, STOP_STORAGE_FAILED, "The run could not be finalized."
            )
        return self._result(session, SESSION_COMPLETED, "", "The run finished.")

    @staticmethod
    def _check_completion(session: _Session, monitor: _Monitor) -> None:
        """COMPLETED فقط وقتی همه‌ی unitهای برنامه نتیجه‌ی ثبت‌شده دارند و هیچ unitِ واجدی اجرانشده
        (NOT_RUN) یا وسطِ اجرا متوقف‌شده (CANCELLED) نیست. unitِ متوقف‌شده یعنی توقفِ واقعی:
        شکستِ پایش → همان دلیل (INTERRUPTED)، درخواستِ کاربر → CANCELLED، بی‌دلیل → INTERRUPTED.
        درخواستی که فقط پس از پایانِ واقعیِ همه‌ی unitها رسیده هیچ اثری ندارد."""
        recorded = {outcome.unit_number: outcome for outcome in session.outcomes}
        planned = {entry.unit_number for entry in session.plan.entries}
        executed = [recorded.get(unit.unit_number) for unit in session.prepared.units]
        if planned - set(recorded) or any(
            outcome is None or outcome.status == UNIT_NOT_RUN for outcome in executed
        ):
            raise _Stop(
                STOP_RUNNER_EXCEPTION,
                "Not every planned unit has a recorded outcome; the run is not complete.",
            )
        if any(outcome.status == UNIT_CANCELLED for outcome in executed):
            if monitor.failure:
                raise _Stop(*monitor.failure)
            if monitor.cancel_requested.is_set():
                raise _Stop(STOP_USER_CANCELLED, "The run was cancelled.")
            raise _Stop(
                STOP_RUNNER_EXCEPTION, "A unit was cancelled without a cancellation request."
            )

    def _authenticate(self, session: _Session) -> None:
        provider = self._token_provider or _default_token_provider()
        try:
            session.token = provider.fetch()
        except AuthenticationError as exc:
            raise _Stop(STOP_AUTHENTICATION_FAILED, str(exc)) from None  # پیامِ ثابت و بی‌راز
        session.secrets |= {session.token.value, f"Bearer {session.token.value}"}

    def _check_stop(self, session: _Session, monitor: _Monitor) -> None:
        if monitor.failure:
            raise _Stop(*monitor.failure)
        if monitor.cancel_requested.is_set():
            raise _Stop(STOP_USER_CANCELLED, "The run was cancelled.")
        try:
            hashes = (_sha256_file(self.environment_path), _sha256_file(self.globals_path))
        except OSError:
            hashes = ("", "")
        if hashes != session.config_hashes:
            raise _Stop(
                STOP_CONFIGURATION_CHANGED,
                "The Environment or Globals file changed during the run.",
            )

    def _verify_unit(self, session: _Session, entry: PlanEntry, prepared: PreparedUnit) -> dict:
        """بررسیِ کاملِ یکپارچگی درست پیش از Newman — هر شکست _Stop(INTEGRITY_FAILED)."""
        store, plan = session.store, session.plan

        def fail(message: str) -> None:
            raise _Stop(STOP_INTEGRITY_FAILED, f"Unit {entry.unit_number}: {message}")

        if not _profile_valid(plan.profile):
            fail("the run profile is no longer valid.")
        version = store.get_version(entry.version_id)
        if version.case_id != entry.case_id or prepared.case_version_id != version.id:
            fail("the pinned version does not belong to the planned case.")
        if (
            sha256_json(version.payload) != version.fingerprint
            or version.fingerprint != entry.version_fingerprint
        ):
            fail("the pinned version fingerprint changed.")
        if script_free_findings(version.payload, version.script_fingerprint):
            fail("the pinned version is not script-free.")
        again = prepare_case(
            version,
            host_intent=plan.profile.host_intent,
            profile=plan.variables,
            approval_state=store.script_approval_state(version.id),
        )
        if not again.prepared or again.prepared_fingerprint != entry.prepared_fingerprint:
            fail("the prepared request changed.")
        fresh = replace(entry, prepared_payload=again.prepared_payload)
        try:
            collection = build_unit_collection(fresh)
            validate_unit_collection(collection, fresh)
        except CollectionPreparationError as exc:
            fail(f"the collection was rejected ({exc.code}).")
        if sha256_json(collection) != prepared.collection_fingerprint:
            fail("the collection fingerprint changed.")
        if len(collection["item"]) != 1:
            fail("a unit must hold exactly one request.")
        try:
            verify_clean_json(collection, session.secrets, "the unit collection")
        except SanitizationError:
            fail("the collection contains a secret value.")
        return collection

    def _run_unit(
        self,
        session: _Session,
        monitor: _Monitor,
        unit_id: int,
        entry: PlanEntry,
        prepared: PreparedUnit,
    ) -> None:
        store, lease = session.store, session.lease
        try:
            collection = self._verify_unit(session, entry, prepared)
        except _Stop:
            unit = store.get_run_unit(unit_id)
            store.finish_unit(
                lease,
                unit_id,
                UNIT_NOT_RUN,
                case_status=CASE_RUN_NOT_RUN,
                unit_metadata={**unit.metadata, "classification": CLASS_PRE_EXECUTION_BLOCKED},
                case_metadata={"classification": CLASS_PRE_EXECUTION_BLOCKED},
            )
            session.outcomes.append(
                UnitOutcome(
                    entry.unit_number,
                    entry.case_id,
                    entry.version_id,
                    UNIT_NOT_RUN,
                    CLASS_PRE_EXECUTION_BLOCKED,
                    NOT_VERIFIED,
                )
            )
            raise
        unit_dir = session.run_dir / f"unit_{entry.unit_number:03d}"
        try:
            unit_dir.mkdir(exist_ok=False)
            collection_path = unit_dir / "collection.json"
            with open(collection_path, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(to_json(collection))
        except OSError as exc:
            raise _Stop(
                STOP_ARTIFACT_PUBLICATION,
                f"Unit {entry.unit_number}: the collection could not be written ({type(exc).__name__}).",
            ) from None
        # حصارِ پیش از Newman: Gate و lease هنوز مالِ همین نشست‌اند
        try:
            session.gate.assert_held()
        except GateLeaseLost:
            raise _Stop(STOP_GATE_LOST, "The runner lost the shared execution gate.") from None
        except GateError:
            raise _Stop(
                STOP_GATE_UNAVAILABLE, "The shared execution gate could not be confirmed."
            ) from None
        if not store.owns_run(lease):
            raise _Stop(STOP_LEASE_LOST, "The runner lost its run lease.")
        self._check_stop(session, monitor)
        store.start_unit(lease, unit_id)

        cancel_event = threading.Event()
        monitor.set_active(cancel_event)
        engine = self._engine or _default_engine()
        request = NewmanRunRequest(
            collection_path=collection_path,
            environment_path=self.environment_path,
            globals_path=self.globals_path,
            output_dir=unit_dir,
            options=NewmanOptions(**session.plan.profile.newman_option_values()),
            access_token=session.token.value if session.token is not None else None,
        )
        try:
            result = engine.run(request, cancel_event=cancel_event)
        except BaseException:
            session.unconfirmed = True  # پروسه ممکن است زنده باشد — Gate نگه داشته می‌شود
            raise _Stop(
                STOP_TERMINATION_UNCONFIRMED, "The Newman engine was interrupted."
            ) from None
        finally:
            monitor.set_active(None)
        if not termination_confirmed(result):
            # هیچ unitِ دیگری شروع نمی‌شود، run نهایی نمی‌شود و Gate قرنطینه می‌ماند
            session.unconfirmed = True
            raise _Stop(
                STOP_TERMINATION_UNCONFIRMED, "Termination of the Newman process was not confirmed."
            )
        runner_stopped = cancel_event.is_set() and not monitor.cancel_requested.is_set()
        self._record(session, unit_id, entry, unit_dir, result, runner_stopped)
        if result.status == STATUS_ERROR and result.error_kind in STOP_RUN_ERROR_KINDS:
            raise _Stop(STOP_ENGINE_ENVIRONMENT, f"The Newman engine reported {result.error_kind}.")

    @staticmethod
    def _artifacts(unit_dir: Path, result: NewmanRunResult) -> tuple[dict[str, str], bool]:
        """فقط مسیرهای نسبیِ فایل‌های منتشرشده‌ی موتور داخلِ پوشه‌ی همین unit."""
        root = unit_dir.resolve()
        found: dict[str, str] = {}
        for name in (
            "run_dir",
            "result_json",
            "json_report",
            "html_report",
            "stdout_log",
            "stderr_log",
        ):
            value = getattr(result.artifacts, name, "")
            if not value:
                continue
            path = Path(value).resolve()
            if root not in path.parents or not path.exists():
                return {}, False
            found[name] = path.relative_to(root.parent).as_posix()
        return found, bool(found.get("run_dir")) and bool(found.get("result_json"))

    def _record(
        self,
        session: _Session,
        unit_id: int,
        entry: PlanEntry,
        unit_dir: Path,
        result: NewmanRunResult,
        runner_stopped: bool,
    ) -> None:
        artifacts, complete = self._artifacts(unit_dir, result)
        cancelled = result.cancelled or result.error_kind == ERROR_PROCESS_CANCELLED
        if cancelled:
            status, case_status = UNIT_CANCELLED, CASE_RUN_CANCELLED
            classification = CLASS_STOPPED_BY_RUNNER if runner_stopped else CLASS_CANCELLED
            verification = NOT_VERIFIED
        elif not complete:
            status, case_status, classification, verification = (
                UNIT_ERROR,
                CASE_RUN_ERROR,
                CLASS_ARTIFACT_INVALID,
                NOT_VERIFIED,
            )
        elif result.status == STATUS_PASS:
            status, case_status = UNIT_PASS, CASE_RUN_PASS
            classification, verification = (
                (CLASS_UNVERIFIED, UNVERIFIED)
                if result.no_assertions
                else (CLASS_VERIFIED_PASS, VERIFIED)
            )
        elif result.status == STATUS_FAIL:
            status, case_status, classification, verification = (
                UNIT_FAIL,
                CASE_RUN_FAIL,
                CLASS_ASSERTION_FAILED,
                VERIFICATION_FAILED,
            )
        else:
            status, case_status, verification = UNIT_ERROR, CASE_RUN_ERROR, NOT_VERIFIED
            classification = (
                CLASS_ENGINE_ENVIRONMENT
                if result.error_kind in STOP_RUN_ERROR_KINDS
                else _ERROR_CLASSES.get(result.error_kind, CLASS_ENGINE_ERROR)
            )
        stats = result.stats
        newman = {
            "run_id": result.run_id,
            "status": result.status,
            "error_kind": result.error_kind,
            "no_assertions": result.no_assertions,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "requests_executed": stats.requests_executed,
            "assertions_total": stats.assertions_total,
            "assertions_failed": stats.assertions_failed,
        }
        http = [r.status_code for r in result.requests if isinstance(r.status_code, int)]
        common = {
            "classification": classification,
            "verification": verification,
            "http_status": http,
        }
        unit = session.store.get_run_unit(unit_id)
        unit_metadata = {**unit.metadata, **common, "newman": newman, "artifacts": artifacts}
        try:
            verify_clean_json(unit_metadata, session.secrets, "the unit metadata")
        except SanitizationError:
            unit_metadata = {
                **unit.metadata,
                "classification": CLASS_ARTIFACT_UNSAFE,
                "verification": NOT_VERIFIED,
            }
            common = {"classification": CLASS_ARTIFACT_UNSAFE, "verification": NOT_VERIFIED}
            status, case_status = UNIT_ERROR, CASE_RUN_ERROR
            classification, verification = CLASS_ARTIFACT_UNSAFE, NOT_VERIFIED
        if not session.store.owns_run(session.lease):  # پیش از انتشارِ نتیجه
            raise _Stop(STOP_LEASE_LOST, "The runner lost its run lease.")
        session.store.finish_unit(
            session.lease,
            unit_id,
            status,
            case_status=case_status,
            unit_metadata=unit_metadata,
            case_metadata=common,
        )
        session.outcomes.append(
            UnitOutcome(
                entry.unit_number,
                entry.case_id,
                entry.version_id,
                status,
                classification,
                verification,
            )
        )

    # ── پایان ───────────────────────────────────────────────────────────────
    def _stopped(self, session: _Session, stop: _Stop) -> BoxRunResult:
        if stop.reason == STOP_TERMINATION_UNCONFIRMED:
            # run نهایی نمی‌شود: پروسه ممکن است هنوز اجرا شود
            return self._result(session, SESSION_TERMINATION_UNCONFIRMED, stop.reason, stop.message)
        if stop.reason == STOP_LEASE_LOST:
            return self._result(
                session,
                SESSION_INTERRUPTED,
                stop.reason,
                stop.message + " The run was not finalized by this runner.",
            )
        target, session_status = (
            (RUN_CANCELLED, SESSION_CANCELLED)
            if stop.reason == STOP_USER_CANCELLED
            else (RUN_INTERRUPTED, SESSION_INTERRUPTED)
        )
        if not self._finalize(session, target, stop.reason, stop.message):
            return self._result(
                session,
                SESSION_INTERRUPTED,
                stop.reason,
                stop.message + " The run could not be finalized.",
            )
        return self._result(session, session_status, stop.reason, stop.message)

    def _finalize(self, session: _Session, status: str, reason: str, message: str) -> bool:
        store, lease = session.store, session.lease
        cancelled = status == RUN_CANCELLED
        try:
            for unit in store.run_units(session.run_id):
                if unit.status == UNIT_RUNNING:  # نباید بماند؛ اگر ماند، خطای فنی است نه نتیجه
                    store.finish_unit(
                        lease,
                        unit.id,
                        UNIT_ERROR,
                        case_status=CASE_RUN_ERROR,
                        unit_metadata={**unit.metadata, "classification": CLASS_ENGINE_ERROR},
                    )
            queued = [u for u in store.run_units(session.run_id) if u.status == UNIT_QUEUED]
            for unit in queued:
                if not any(o.unit_number == unit.unit_number for o in session.outcomes):
                    case = store.run_cases(session.run_id, unit_id=unit.id)[0]
                    session.outcomes.append(
                        UnitOutcome(
                            unit.unit_number,
                            case.case_id,
                            case.case_version_id,
                            UNIT_CANCELLED if cancelled else UNIT_NOT_RUN,
                            CLASS_CANCELLED if cancelled else CLASS_NOT_RUN,
                            NOT_VERIFIED,
                        )
                    )
            run = store.get_run(session.run_id)
            summary = {
                "units": len(session.plan.entries),
                "verified_pass": sum(1 for o in session.outcomes if o.verification == VERIFIED),
                "unverified": sum(1 for o in session.outcomes if o.verification == UNVERIFIED),
                "failed": sum(1 for o in session.outcomes if o.status == UNIT_FAIL),
                "errors": sum(1 for o in session.outcomes if o.status == UNIT_ERROR),
                "not_run": sum(1 for o in session.outcomes if o.status == UNIT_NOT_RUN),
                "cancelled": sum(1 for o in session.outcomes if o.status == UNIT_CANCELLED),
            }
            metadata = {**run.metadata, "summary": summary}
            if reason:
                metadata.update(stop_reason=reason, stop_message=message)
            store.finish_run(
                lease,
                status,
                metadata=metadata,
                queued_unit_status=UNIT_CANCELLED if cancelled else UNIT_NOT_RUN,
                open_case_status=CASE_RUN_CANCELLED if cancelled else CASE_RUN_NOT_RUN,
                child_metadata={"classification": CLASS_CANCELLED if cancelled else CLASS_NOT_RUN}
                | ({"stop_reason": reason} if reason else {}),
            )
        except (StorageError, sqlite3.Error, ValueError) as exc:
            session.cleanup_errors.append(f"The run could not be finalized ({type(exc).__name__}).")
            return False
        session.finalized = True
        return True

    @staticmethod
    def _result(session: _Session, status: str, reason: str, message: str) -> BoxRunResult:
        return BoxRunResult(
            status=status,
            run_id=session.run_id,
            stop_reason=reason,
            message=message,
            units=tuple(sorted(session.outcomes, key=lambda o: o.unit_number)),
            cleanup_errors=tuple(session.cleanup_errors),
            artifact_dir=session.run_dir.name if session.run_dir is not None else "",
        )


__all__ = [
    "DEFAULT_ARTIFACT_ROOT",
    "BoxRunResult",
    "BoxRunner",
    "UnitOutcome",
]
