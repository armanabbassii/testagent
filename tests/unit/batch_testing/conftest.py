"""
tests/unit/batch_testing/conftest.py — Gate ِ مشترکِ اجرا در پوشه‌ی موقتِ هر تست

هر BatchRunner که gate_factory نگرفته از _default_gate استفاده می‌کند؛ این‌جا آن به پوشه‌ی
tmp ِ همان تست برده می‌شود تا هیچ تستی data/execution_gate/ ِ واقعی را نسازد یا قفل نکند.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.batch_testing import runner as runner_module
from src.execution_gate import ExecutionGate


@pytest.fixture
def gate_dir(tmp_path: Path) -> Path:
    return tmp_path / "execution_gate"


@pytest.fixture(autouse=True)
def _isolated_execution_gate(monkeypatch, gate_dir: Path) -> None:
    monkeypatch.setattr(runner_module, "_default_gate", lambda: ExecutionGate(gate_dir))
