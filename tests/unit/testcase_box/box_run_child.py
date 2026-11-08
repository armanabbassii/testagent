"""
tests/unit/testcase_box/box_run_child.py — یک «پروسه‌ی برنامه» که وسطِ اجرای Box کشته می‌شود (ماژولِ تست نیست)

    python box_run_child.py <repo> <db> <gate_dir> <runs> <env> <globals> <case_id>...

BoxRunner ِ واقعی با Gate ِ واقعی (tmp) و موتورِ ساختگی که هرگز تمام نمی‌شود — هیچ Newman/Node یا
شبکه‌ای. خروجی: «PID <pid>» و سپس «ENGINE» وقتی unitِ اول به موتور رسید. تست پروسه را می‌کشد.
"""

from __future__ import annotations

import os
import sys
import time


def main() -> None:
    repo, db, gate_dir, runs, env, globals_path, *case_ids = sys.argv[1:]
    sys.path.insert(0, repo)
    from src.execution_gate import ExecutionGate
    from src.testcase_box.profiles import HostIntent, read_variable_profile
    from src.testcase_box.run_planning import plan_new_run
    from src.testcase_box.run_profile import RunProfile
    from src.testcase_box.runner import BoxRunner
    from src.testcase_box.storage import BoxStore
    from tests.unit.testcase_box.runner_fakes import FakeTokens

    print(f"PID {os.getpid()}", flush=True)

    class HangingEngine:
        def run(self, request, cancel_event=None):
            print("ENGINE", flush=True)
            while True:  # Newman ِ گیرکرده — تا کشته شدنِ پروسه
                time.sleep(1)

    variables = read_variable_profile("dev", env, globals_path)
    with BoxStore.open(db) as store:
        plan = plan_new_run(
            store,
            [int(c) for c in case_ids],
            profile=RunProfile(HostIntent.NORMAL),
            variables=variables,
        )
    BoxRunner(
        db,
        gate_factory=lambda: ExecutionGate(gate_dir),
        engine=HangingEngine(),
        token_provider=FakeTokens(),
        environment_path=env,
        globals_path=globals_path,
        artifact_root=runs,
        heartbeat_interval=0.05,
        poll_interval=0.01,
    ).run(plan)


if __name__ == "__main__":
    main()
