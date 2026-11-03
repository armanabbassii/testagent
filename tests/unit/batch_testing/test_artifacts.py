"""
تست‌های دسترسیِ امن به artifactها (Milestone 4.6).

فقط فایل‌های فهرستِ مجاز، فقط داخلِ پوشه‌ی همان تلاش در ریشه‌ی artifactِ batch؛
هر مسیرِ دیگر (پوشه‌ی خصوصیِ موقت، بیرون از ریشه، پیمایشِ «..»، پیوند) رد می‌شود.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.batch_testing import models as m
from src.batch_testing.artifacts import (
    ALLOWED_NAMES,
    execution_artifacts,
    execution_directory,
    read_artifact,
)
from src.batch_testing.storage import BatchStore
from tests.unit.batch_testing.test_storage import _new_batch

RUN_ID = "run-1"


@pytest.fixture
def store(tmp_path: Path):
    with BatchStore.open(tmp_path / "batch.sqlite3") as opened:
        yield opened


@pytest.fixture
def batch(store: BatchStore, tmp_path: Path) -> m.Batch:
    return store.create_batch(_new_batch(("DEMO-1",), artifact_root=str(tmp_path / "artifacts")))


def _publish(directory: Path, *, newman: bool = True) -> None:
    """همان چیدمانی که IssueExecutor و موتورِ Newman منتشر می‌کنند + چند فایلِ ممنوع."""
    directory.mkdir(parents=True)
    for name in ("execution_summary.json", "collection.json", "step1.json", "step2.json"):
        (directory / name).write_text(f'{{"file": "{name}"}}', encoding="utf-8")
    (directory / ".collection.json.tmp").write_text("partial", encoding="utf-8")
    (directory / "raw-newman-report.html").write_text("RAW", encoding="utf-8")
    if newman:
        run_dir = directory / "newman" / RUN_ID
        run_dir.mkdir(parents=True)
        for name in ("newman-report.html", "newman-report.json", "result.json", "stdout.log"):
            (run_dir / name).write_text(name, encoding="utf-8")


def _execution(
    store: BatchStore, batch: m.Batch, directory: Path, *, run_dir: Path | None = None, **fields
) -> m.Execution:
    execution = store.create_execution(batch.id, "DEMO-1", trigger=fields.pop("trigger", "initial"))
    store.start_execution(execution.id)
    run_dir = run_dir if run_dir is not None else directory / "newman" / RUN_ID
    return store.finish_execution(
        execution.id,
        m.EXEC_PASS,
        artifact_dir=str(directory),
        collection_path=str(directory / "collection.json"),
        newman_run_dir=str(run_dir),
        newman_result_path=str(run_dir / "result.json"),
        **fields,
    )


def _attempt_dir(batch: m.Batch, attempt: int) -> Path:
    return Path(batch.artifact_root) / batch.id / "DEMO-1" / f"execution_{attempt}"


def _available(artifacts) -> dict[str, Path]:
    return {artifact.name: artifact.path for artifact in artifacts if artifact.available}


class TestValidArtifacts:
    def test_published_files_are_listed_and_readable(self, store, batch):
        directory = _attempt_dir(batch, 1)
        _publish(directory)
        execution = _execution(store, batch, directory)
        found = _available(execution_artifacts(batch, execution))
        assert set(found) == {
            "execution_summary.json",
            "collection.json",
            "step1.json",
            "step2.json",
            "newman-report.html",
            "newman-report.json",
            "result.json",
        }
        assert read_artifact(batch, execution, "collection.json") == b'{"file": "collection.json"}'
        assert read_artifact(batch, execution, "newman-report.html") == b"newman-report.html"

    def test_a_missing_file_is_reported_unavailable(self, store, batch):
        directory = _attempt_dir(batch, 1)
        _publish(directory, newman=False)
        execution = _execution(store, batch, directory, run_dir=Path(""))
        artifacts = {artifact.name: artifact for artifact in execution_artifacts(batch, execution)}
        assert not artifacts["step5.json"].available
        assert not artifacts["newman-report.html"].available
        assert artifacts["newman-report.html"].reason == "Not produced for this attempt."
        with pytest.raises(FileNotFoundError):
            read_artifact(batch, execution, "newman-report.html")

    def test_previous_attempts_keep_their_own_artifacts(self, store, batch):
        first_dir, second_dir = _attempt_dir(batch, 1), _attempt_dir(batch, 2)
        _publish(first_dir)
        first = _execution(store, batch, first_dir)
        _publish(second_dir)
        (second_dir / "collection.json").write_text("second", encoding="utf-8")
        second = _execution(store, batch, second_dir, trigger="retry")
        assert read_artifact(batch, first, "collection.json") == b'{"file": "collection.json"}'
        assert read_artifact(batch, second, "collection.json") == b"second"
        assert _available(execution_artifacts(batch, first))["result.json"].is_relative_to(
            first_dir.resolve()
        )


class TestRejectedArtifacts:
    def test_unlisted_files_are_never_exposed(self, store, batch):
        directory = _attempt_dir(batch, 1)
        _publish(directory)
        execution = _execution(store, batch, directory)
        names = {artifact.name for artifact in execution_artifacts(batch, execution)}
        assert names <= ALLOWED_NAMES
        for name in (
            "raw-newman-report.html",
            ".collection.json.tmp",
            "stdout.log",
            "../execution_1/collection.json",
            str(directory / "collection.json"),
        ):
            with pytest.raises(PermissionError):
                read_artifact(batch, execution, name)

    def test_a_folder_outside_the_batch_root_is_rejected(self, store, batch, tmp_path):
        outside = tmp_path / "elsewhere" / batch.id / "DEMO-1" / "execution_1"
        _publish(outside)
        execution = _execution(store, batch, outside)
        artifacts = execution_artifacts(batch, execution)
        assert not any(artifact.available for artifact in artifacts)
        assert artifacts[0].reason.startswith("Rejected")
        with pytest.raises(FileNotFoundError):
            read_artifact(batch, execution, "collection.json")

    def test_a_private_temporary_newman_folder_is_rejected(self, store, batch, tmp_path):
        directory = _attempt_dir(batch, 1)
        _publish(directory, newman=False)
        private = tmp_path / "private-newman" / RUN_ID  # پوشه‌ی خصوصیِ موتور، بیرون از تلاش
        private.mkdir(parents=True)
        (private / "newman-report.html").write_text("RAW REPORT", encoding="utf-8")
        execution = _execution(store, batch, directory, run_dir=private)
        found = _available(execution_artifacts(batch, execution))
        assert "newman-report.html" not in found and "collection.json" in found

    def test_another_attempts_folder_is_rejected(self, store, batch):
        first_dir = _attempt_dir(batch, 1)
        _publish(first_dir)
        _execution(store, batch, first_dir)
        # تلاشِ ۲ که به پوشه‌ی تلاشِ ۱ اشاره می‌کند (یا با «..» به آن می‌رسد)
        sneaky = first_dir.parent / "execution_2" / ".." / "execution_1"
        second = _execution(store, batch, sneaky, trigger="retry")
        assert execution_directory(batch, second) is None
        assert not any(artifact.available for artifact in execution_artifacts(batch, second))

    def test_a_link_out_of_the_attempt_folder_is_rejected(self, store, batch, tmp_path):
        directory = _attempt_dir(batch, 1)
        _publish(directory)
        secret = tmp_path / "secret.json"
        secret.write_text("SECRET", encoding="utf-8")
        (directory / "step3.json").unlink(missing_ok=True)
        try:
            os.symlink(secret, directory / "step3.json")
        except (OSError, NotImplementedError):
            pytest.skip("Creating symbolic links is not permitted on this machine.")
        execution = _execution(store, batch, directory)
        artifacts = {artifact.name: artifact for artifact in execution_artifacts(batch, execution)}
        assert not artifacts["step3.json"].available
        assert artifacts["step3.json"].reason.startswith("Rejected")
