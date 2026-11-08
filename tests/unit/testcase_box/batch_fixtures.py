"""
ساختِ artifactِ Batch برای تست‌های M5 — با همان ArtifactWriter، ExecutionOutcome،
sanitize_collection و sanitize_step_result که Batch به کار می‌برد، تا fixture دقیقاً هم‌شکلِ
خروجیِ واقعی باشد. فقط داخلِ tmp_path.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from src.agents.test_case_generator.postman_builder import PostmanBuilder
from src.batch_testing.executor import (
    ArtifactWriter,
    ExecutionOutcome,
    sanitize_collection,
    sanitize_step_result,
)

BATCH_ID = "0123456789abcdef0123456789abcdef"
ISSUE = "DEMO-1"


def generator_case(case_id: str, *, method="GET", path="/voucher/{id}", host="base_url", **extra):
    case = {
        "name": f"{case_id} — a readable title",
        "type": "positive",
        "method": method,
        "path": path,
        "description": "free text",
        "headers": {},
        "query_params": {},
        "path_params": {"id": "{{voucherId}}"} if "{id}" in path else {},
        "body": None,
        "expected_status": 200,
        "assertions": [{"type": "status_code", "expected": 200}],
        "save_variables": [],
        "assumptions": [],
        "base_url_var": host,
        "base_path": "/api",
        "required_variables": [],
    }
    case.update(extra)
    return case


def default_cases() -> list[dict]:
    """TC-001 تولیدکننده‌ی voucherId است، TC-002 مصرف‌کننده، TC-003 مستقل روی VDI."""
    return [
        generator_case(
            "TC-001",
            method="POST",
            path="/voucher",
            body={"name": "a"},
            save_variables=[{"variable": "voucherId", "json_path": "$.id"}],
        ),
        generator_case(
            "TC-002",
            required_variables=[
                {"variable": "voucherId", "scope": "collection", "producer": "TC-001"}
            ],
        ),
        generator_case("TC-003", path="/health", host="base_url_vdi"),
    ]


def build_collection(cases: list[dict], issue=ISSUE, attempt=1) -> dict:
    collection = PostmanBuilder().build(
        suite={"controllers": [{"name": "C", "test_cases": cases}]}, collection_name="x"
    )
    return sanitize_collection(collection, issue, attempt)


def step_results(cases: list[dict]) -> dict[int, dict]:
    ids = [case["name"].split(" ")[0] for case in cases]
    return {
        1: {"test_cases": [{"id": i, "title": f"Original title {i}"} for i in ids]},
        2: {
            "mappings": [
                {
                    "test_case_id": i,
                    "api": {"method": "GET", "path": "/x", "operation_id": f"op{n}"},
                }
                for n, i in enumerate(ids)
            ]
        },
        3: {
            "execution_order": [
                {"test_case_id": i, "order": n, "depends_on": ["TC-001"] if i == "TC-002" else []}
                for n, i in enumerate(ids)
            ],
            "data_dependencies": [
                {
                    "variable_name": "voucherId",
                    "source": {"test_case_id": "TC-001", "location": "body", "path": "id"},
                    "targets": [{"test_case_id": "TC-002", "location": "path", "parameter": "id"}],
                }
            ],
        },
        4: {
            "requests": [
                {
                    "test_case_id": i,
                    "method": case["method"],
                    "path": case["path"],
                    "host_variable": case["base_url_var"],
                    "host_basis": "default",
                    "base_path": "/api",
                }
                for i, case in zip(ids, cases, strict=True)
            ],
            "unresolved": [],
        },
        5: {"review": {"status": "ready", "issues": []}},
    }


def write_attempt(
    root: Path,
    *,
    batch_id: str = BATCH_ID,
    issue: str = ISSUE,
    attempt: int = 1,
    cases: list[dict] | None = None,
    collection: dict | None = None,
    steps: dict[int, dict] | None = None,
    skip_steps: tuple[int, ...] = (),
    status: str = "PASS",
    phase: str | None = None,
    newman_executed: bool | None = None,
    summary: Any = "full",
    summary_update: dict | None = None,
    write_collection: bool = True,
) -> Path:
    """یک تلاش به همان ترتیبِ IssueExecutor: قدم‌ها ← collection ← خلاصه."""
    cases = default_cases() if cases is None else cases
    writer = ArtifactWriter.create(root, batch_id, issue, attempt)
    outcome = ExecutionOutcome(status=status, artifact_dir=str(writer.directory))
    outcome.phase = phase or ("done" if status in ("PASS", "FAIL") else "newman")
    outcome.newman_executed = (
        newman_executed if newman_executed is not None else status in ("PASS", "FAIL")
    )
    outcome.task_source = {
        "issue_key": issue,
        "source": "jira",
        "task_description_sha256": "a" * 64,
    }
    for step, result in (steps if steps is not None else step_results(cases)).items():
        if step in skip_steps:
            continue
        name = f"step{step}.json"
        outcome.step_artifacts[name] = str(
            writer.write_json(name, sanitize_step_result(step, result), set())
        )
    if write_collection:
        document = collection if collection is not None else build_collection(cases, issue, attempt)
        outcome.collection_path = str(writer.write_json("collection.json", document, set()))
    if summary == "full":
        document = {
            "batch_id": batch_id,
            "issue_key": issue,
            "execution_id": "exec-1",
            "attempt": attempt,
            **asdict(outcome),
        }
        document.update(summary_update or {})
        writer.write_json("execution_summary.json", document, set())
    elif summary == "minimal":
        writer.write_json(
            "execution_summary.json",
            {"execution_id": "exec-1", "status": status, "error_kind": "x"},
            set(),
        )
    return writer.directory


def tree_state(root: Path) -> dict[str, tuple[bytes, int]]:
    """بایت‌ها و mtime ِ هر فایل و پوشه — برای اثباتِ فقط‌خواندنی بودن."""
    state = {}
    for current, directories, files in os.walk(root):
        for name in directories + files:
            path = Path(current) / name
            relative = str(path.relative_to(root))
            data = path.read_bytes() if path.is_file() else b"<dir>"
            state[relative] = (data, path.stat().st_mtime_ns)
    return state
