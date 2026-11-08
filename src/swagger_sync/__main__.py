"""
python -m src.swagger_sync — ورودیِ مستقلِ همگام‌سازیِ Swagger (بدونِ Streamlit)

    run                     اجرای واقعی؛ تا موفقیتِ همه‌ی منابع retry می‌کند
    run --if-due            همان، فقط اگر از آخرین زمانِ روزانه‌ی برنامه‌ریزی‌شده اجرای موفقی
                            ثبت نشده باشد (Task Scheduler همین را صدا می‌زند: روزانه و هنگامِ logon)
    run --check             GET و اعتبارسنجیِ واقعی؛ هیچ snapshotی عوض نمی‌شود
    run --check --probe-https   به‌علاوه‌ی بررسیِ نسخه‌ی https ِ هر نشانی
    run --dry-run           بدونِ شبکه و بدونِ هیچ نوشتنی
    install-task / update-task / uninstall-task / status   (src/swagger_sync/task.py)

کدهای خروج: 0 موفق، غیرفعال یا «لازم نیست»؛ 1 شکستِ بخشی؛ 2 شکستِ همه؛ 3 پیکربندی؛
4 اجرای دیگری در جریان؛ 5 قطع‌شده.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from src.swagger_settings import REPO_ROOT, SwaggerSettingsError, load_sync_settings
from src.swagger_sync import job as sync_job
from src.swagger_sync import report as sync_report

REPORT_NAMES = {sync_job.MODE_RUN: "last_run.json", sync_job.MODE_CHECK: "last_check.json"}
DEFAULT_LOG_DIR = REPO_ROOT / "logs" / "swagger_sync"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.swagger_sync")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="synchronise the Swagger snapshots")
    modes = run.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="no network and no writes")
    modes.add_argument("--check", action="store_true", help="real GET and validation, no writes")
    run.add_argument("--probe-https", action="store_true", help="with --check: also try https://")
    run.add_argument(
        "--if-due",
        action="store_true",
        help="run only if no successful run was recorded since the last daily time",
    )
    commands.add_parser("status", help="show the last run and the scheduled task")
    commands.add_parser("install-task", help="register the daily Windows scheduled task")
    commands.add_parser("update-task", help="re-register the task with the current settings")
    commands.add_parser("uninstall-task", help="remove the Windows scheduled task")
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.probe_https and not args.check:
        print("--probe-https requires --check.", file=sys.stderr)
        return sync_job.EXIT_CONFIG
    if args.if_due and (args.check or args.dry_run):
        print("--if-due is available only for a real run.", file=sys.stderr)
        return sync_job.EXIT_CONFIG
    mode = (
        sync_job.MODE_DRY_RUN
        if args.dry_run
        else sync_job.MODE_CHECK
        if args.check
        else sync_job.MODE_RUN
    )
    try:
        settings = load_sync_settings()
    except SwaggerSettingsError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        if mode != sync_job.MODE_DRY_RUN:
            # زیرِ pythonw ِ Task Scheduler کنسولی نیست: خطا در گزارشِ مسیرِ پیش‌فرض ثبت می‌شود
            failed = sync_job.RunReport(
                mode=mode,
                started_at=sync_job._utc_now(),
                finished_at=sync_job._utc_now(),
                result=sync_job.RESULT_CONFIG_ERROR,
                exit_code=sync_job.EXIT_CONFIG,
                error=str(exc),
            )
            try:
                sync_report.write_report(DEFAULT_LOG_DIR / REPORT_NAMES[mode], failed.to_dict())
            except OSError:
                pass
        return sync_job.EXIT_CONFIG
    # --dry-run هیچ فایلی نمی‌نویسد: نه log، نه گزارش
    dry = mode == sync_job.MODE_DRY_RUN
    logger = sync_report.setup_logging(None if dry else settings.log_dir, settings.log_backups)
    try:
        report_path = None if dry else settings.log_dir / REPORT_NAMES[mode]
        if args.if_due:
            last = sync_report.read_report(settings.log_dir / REPORT_NAMES[sync_job.MODE_RUN])
            reason = sync_job.due_reason(last, settings.daily_time, datetime.now().astimezone())
            if reason is None:
                logger.info(
                    "Swagger sync not due: the last successful run finished at %s.",
                    (last or {}).get("finished_at"),
                )
                return sync_job.EXIT_SUCCESS
            logger.info("Swagger sync due: %s.", reason)

        def progress(report: sync_job.RunReport) -> None:
            sync_report.write_report(report_path, report.to_dict())

        result = sync_job.SyncJob(settings, logger=logger, progress=None if dry else progress).run(
            mode, probe_https=args.probe_https
        )
        if dry:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        elif result.result != sync_job.RESULT_BUSY:  # گزارشِ اجرای در جریان را بازنویسی نکن
            try:
                sync_report.write_report(report_path, result.to_dict())
            except OSError as exc:
                logger.error("The final report could not be written (%s).", type(exc).__name__)
        return result.exit_code
    finally:
        sync_report.close_logging()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "run":
        return _run(args)
    from src.swagger_sync import task

    return task.main(args.command)


if __name__ == "__main__":
    sys.exit(main())
