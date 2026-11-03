"""
src/batch_testing/artifacts.py — دسترسیِ امن به artifactهای منتشرشده‌ی یک تلاش

  * فقط فایل‌هایی که IssueExecutor و موتورِ Newman پس از پاک‌سازی منتشر می‌کنند،
    با نامِ ثابت از یک فهرستِ مجاز — هیچ مسیری از ورودیِ کاربر ساخته نمی‌شود.
  * مسیرها از رکوردِ اجرا در پایگاه می‌آیند و پیش از هر فهرست/خواندن بررسی می‌شوند:
    پوشه‌ی تلاش باید دقیقاً <artifact_root>/<batch>/<issue>/execution_<attempt>
    باشد و هر فایل (پس از resolveِ پیوندها) داخلِ همان پوشه. پوشه‌ی خصوصیِ موقتِ
    Newman و گزارش‌های خام هرگز داخلِ این پوشه نیستند، پس هیچ‌وقت در دسترس نیستند.
  * فایلِ ناموجود «در دسترس نیست» گزارش می‌شود، نه پیوندِ ساختگی.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.batch_testing.models import Batch, Execution

NEWMAN_DIR = "newman"

# (نامِ فایل، برچسب، نوعِ MIME) — ترتیبِ نمایش
_EXECUTION_FILES = (
    ("execution_summary.json", "Execution summary", "application/json"),
    ("collection.json", "Postman collection", "application/json"),
    ("step1.json", "Step 1 output (sanitized)", "application/json"),
    ("step2.json", "Step 2 output (sanitized)", "application/json"),
    ("step3.json", "Step 3 output (sanitized)", "application/json"),
    ("step4.json", "Step 4 output (sanitized)", "application/json"),
    ("step5.json", "Step 5 output (sanitized)", "application/json"),
)
_NEWMAN_FILES = (
    ("newman-report.html", "Newman HTML report (sanitized)", "text/html"),
    ("newman-report.json", "Newman JSON report (sanitized)", "application/json"),
    ("result.json", "Newman run result", "application/json"),
)
ALLOWED_NAMES = frozenset(name for name, _, _ in _EXECUTION_FILES + _NEWMAN_FILES)


@dataclass(frozen=True)
class Artifact:
    name: str
    label: str
    mime: str
    path: Path | None  # None یعنی در دسترس نیست
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.path is not None


def batch_root(batch: Batch) -> Path:
    return (Path(batch.artifact_root) / batch.id).resolve()


def _inside(path: Path, directory: Path) -> bool:
    return directory in path.parents


def execution_directory(batch: Batch, execution: Execution) -> Path | None:
    """پوشه‌ی تلاش اگر دقیقاً همان جای مورد انتظار باشد؛ وگرنه None."""
    if not execution.artifact_dir or execution.batch_id != batch.id:
        return None
    expected = batch_root(batch) / execution.issue_key / f"execution_{execution.attempt}"
    actual = Path(execution.artifact_dir).resolve()
    if actual != expected.resolve() or not _inside(actual, batch_root(batch)):
        return None
    return actual if actual.is_dir() else None


def _newman_directory(directory: Path, execution: Execution) -> Path | None:
    if not execution.newman_run_dir:
        return None
    run_dir = Path(execution.newman_run_dir).resolve()
    return run_dir if _inside(run_dir, directory / NEWMAN_DIR) and run_dir.is_dir() else None


def _file(directory: Path | None, name: str, within: Path | None) -> tuple[Path | None, str]:
    if directory is None or within is None:
        return None, "Not produced for this attempt."
    target = (directory / name).resolve()
    if not _inside(target, within) or target.name != name:
        return None, "Rejected: the file is outside the attempt's artifact folder."
    if not target.is_file():
        return None, "Not produced for this attempt."
    return target, ""


def execution_artifacts(batch: Batch, execution: Execution) -> list[Artifact]:
    """artifactهای مجازِ یک تلاش، هر کدام با مسیرِ بررسی‌شده یا دلیلِ در دسترس نبودن."""
    directory = execution_directory(batch, execution)
    if directory is None:
        reason = (
            "No artifact folder was recorded for this attempt."
            if not execution.artifact_dir
            else "Rejected: the recorded artifact folder is not inside this batch's folder."
        )
        return [
            Artifact(name, label, mime, None, reason)
            for name, label, mime in _EXECUTION_FILES + _NEWMAN_FILES
        ]
    artifacts = []
    for name, label, mime in _EXECUTION_FILES:
        path, reason = _file(directory, name, directory)
        artifacts.append(Artifact(name, label, mime, path, reason))
    newman = _newman_directory(directory, execution)
    for name, label, mime in _NEWMAN_FILES:
        path, reason = _file(newman, name, newman)
        artifacts.append(Artifact(name, label, mime, path, reason))
    return artifacts


def read_artifact(batch: Batch, execution: Execution, name: str) -> bytes:
    """بایت‌های یک artifactِ مجاز — دوباره همان بررسی‌ها، درست پیش از خواندن."""
    if name not in ALLOWED_NAMES:
        raise PermissionError("This file is not a published artifact.")
    for artifact in execution_artifacts(batch, execution):
        if artifact.name == name:
            if artifact.path is None:
                raise FileNotFoundError(artifact.reason)
            return artifact.path.read_bytes()
    raise FileNotFoundError("The artifact is unavailable.")


__all__ = [
    "ALLOWED_NAMES",
    "NEWMAN_DIR",
    "Artifact",
    "batch_root",
    "execution_artifacts",
    "execution_directory",
    "read_artifact",
]
