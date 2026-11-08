"""
src/swagger_sync/report.py — logging و گزارشِ JSON ِ هر اجرا

  * log: logging ِ استاندارد، RotatingFileHandler در SWAGGER_SYNC_LOG_DIR (پیش‌فرض logs/swagger_sync،
    در .gitignore) + کنسول. فقط یک اجرای job در هر لحظه (قفلِ اجرا)، پس rotation روی Windows با
    فایلِ بازِ پروسه‌ی دیگری درگیر نمی‌شود.
  * گزارش: last_run.json (اجرای واقعی) یا last_check.json (--check)، با نوشتنِ اتمی.

هیچ بدنه‌ی پاسخ، header، توکن یا credential ثبت نمی‌شود — فقط نشانی، کدِ HTTP، نوعِ خطا با پیامِ
ثابتِ هسته، تعدادِ تلاش، اندازه، تعدادِ operation و پیشوندِ SHA-256.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOGGER_NAME = "swagger_sync"
LOG_FILE = "swagger_sync.log"
LOG_MAX_BYTES = 1_000_000
_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def setup_logging(log_dir: Path | None, backups: int, *, console: bool = True) -> logging.Logger:
    """logger ِ job؛ log_dir ِ None یعنی هیچ فایلی (حالتِ --dry-run)."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(_FORMAT)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=backups,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    if console and sys.stderr is not None:  # زیرِ pythonw ِ Task Scheduler stderr وجود ندارد
        stream = logging.StreamHandler(sys.stderr)  # stdout برای JSON ِ --dry-run آزاد می‌ماند
        stream.setFormatter(formatter)
        logger.addHandler(stream)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def close_logging() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def write_report(path: Path, report: dict[str, Any]) -> None:
    """JSON ِ گزارش با نوشتنِ اتمی (فایلِ موقت در همان پوشه + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def read_report(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


__all__ = [
    "LOGGER_NAME",
    "LOG_FILE",
    "close_logging",
    "read_report",
    "setup_logging",
    "write_report",
]
