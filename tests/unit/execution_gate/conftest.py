"""
tests/unit/execution_gate/conftest.py — پوشه‌ی موقتِ Gate برای هر تست و پروسه‌های فرزند

هیچ تستی data/execution_gate/ ِ واقعی یا پایگاه‌های Batch/Box را نمی‌بیند.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from src.execution_gate import ExecutionGate
from tests.unit.execution_gate.support import Child


@pytest.fixture
def gate_dir(tmp_path: Path) -> Path:
    return tmp_path / "gate"


@pytest.fixture
def gate(gate_dir: Path) -> ExecutionGate:
    return ExecutionGate(gate_dir)


@pytest.fixture
def spawn(gate_dir: Path) -> Iterator:
    """پروسه‌های فرزند؛ در پایانِ تست همه کشته و جمع می‌شوند (هیچ پروسه‌ی یتیمی نمی‌ماند)."""
    children: list[Child] = []

    def start(mode: str) -> Child:
        gate_dir.mkdir(parents=True, exist_ok=True)
        child = Child(gate_dir, mode)
        children.append(child)
        return child

    yield start
    for child in children:
        child.close()
