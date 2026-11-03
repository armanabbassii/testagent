"""
locator.py — پیدا کردنِ Node، Newman و reporterِ htmlextra

Newman در ریشه‌ی مخزن نصب است (package.json: newman 6.2.3، htmlextra 1.23.1).
موتور خودِ ماژولِ Newman را از طریقِ runner.js با `node` اجرا می‌کند — نه
newman.cmd، تا نقل‌قولِ cmd.exe و فرزندانِ رهاشده در کار نباشد.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.test_execution.newman.models import (
    ERROR_NEWMAN_NOT_FOUND,
    ERROR_NODE_NOT_FOUND,
    ERROR_REPORTER_NOT_FOUND,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_MODULES = REPO_ROOT / "node_modules"
RUNNER_SCRIPT = Path(__file__).resolve().with_name("runner.js")


@dataclass(frozen=True)
class NewmanInstallation:
    node: str = ""
    node_version: str = ""
    newman_module: str = ""
    newman_version: str = ""
    htmlextra_version: str = ""
    runner: str = str(RUNNER_SCRIPT)
    error_kind: str = ""
    error_message: str = ""

    @property
    def ok(self) -> bool:
        return not self.error_kind


def _package_version(directory: Path) -> str:
    try:
        return str(
            json.loads((directory / "package.json").read_text(encoding="utf-8")).get("version")
            or ""
        )
    except (OSError, ValueError):
        return ""


def locate(
    node_executable: str | None = None, node_modules: Path = NODE_MODULES
) -> NewmanInstallation:
    node = node_executable or shutil.which("node") or ""
    if not node:
        return NewmanInstallation(
            error_kind=ERROR_NODE_NOT_FOUND, error_message="Node.js was not found on PATH."
        )
    try:
        version = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return NewmanInstallation(
            error_kind=ERROR_NODE_NOT_FOUND, error_message="Node.js could not be started."
        )

    newman_dir = Path(node_modules) / "newman"
    newman_version = _package_version(newman_dir)
    if not newman_version or not (newman_dir / "index.js").is_file():
        return NewmanInstallation(
            node=node,
            node_version=version,
            error_kind=ERROR_NEWMAN_NOT_FOUND,
            error_message="Newman is not installed in node_modules (run 'npm install' at the repository root).",
        )
    htmlextra_version = _package_version(Path(node_modules) / "newman-reporter-htmlextra")
    if not htmlextra_version:
        return NewmanInstallation(
            node=node,
            node_version=version,
            newman_module=str(newman_dir),
            newman_version=newman_version,
            error_kind=ERROR_REPORTER_NOT_FOUND,
            error_message="newman-reporter-htmlextra is not installed in node_modules.",
        )
    return NewmanInstallation(
        node=node,
        node_version=version,
        newman_module=str(newman_dir),
        newman_version=newman_version,
        htmlextra_version=htmlextra_version,
    )


__all__ = ["NODE_MODULES", "REPO_ROOT", "RUNNER_SCRIPT", "NewmanInstallation", "locate"]
