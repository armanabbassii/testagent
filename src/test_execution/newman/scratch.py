"""
scratch.py — پوشه‌ی موقتِ خصوصی برای گزارش‌های خامِ Newman

گزارشِ خام (پیش از پاک‌سازی) فقط این‌جا نوشته می‌شود:

    <TEMP>/agents-newman-engine/run-XXXXXXXX/   ← یکی برای هر اجرا
        .newman-engine-private                  ← نشانِ مالکیتِ موتور

  * ریشه و هر پوشه‌ی اجرا با mode=0o700 ساخته می‌شوند؛ Python 3.13 روی Windows برای
    آن ACLِ محافظت‌شده می‌گذارد (فقط SYSTEM، Administrators، OWNER RIGHTS و کاربرِ
    فعلی، بدونِ ارث از %TEMP%). این فرض بررسی می‌شود (icacls)؛ اگر برقرار نباشد،
    اجرا انجام نمی‌شود.
  * در پایانِ هر اجرا پوشه حذف می‌شود. اگر پروسه‌ی Python کشته شود، برق برود یا
    سیستم ری‌استارت شود، حذف انجام نمی‌شود؛ پوشه‌ی رهاشده در اجرای بعدیِ موتور —
    وقتی از STALE_AFTER_S قدیمی‌تر باشد — پاک می‌شود. فقط پوشه‌های «run-*» داخلِ
    همین ریشه که نشانِ موتور را دارند حذف می‌شوند؛ هیچ چیزِ دیگری.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT_NAME = "agents-newman-engine"
MARKER = ".newman-engine-private"
RUN_PREFIX = "run-"
# بیشتر از سقفِ پیش‌فرضِ یک اجرا (۱۵ دقیقه) با حاشیه‌ی زیاد.
STALE_AFTER_S = 6 * 60 * 60

DEFAULT_ROOT = Path(tempfile.gettempdir()) / ROOT_NAME

# اصل‌هایی که روی پوشه‌ی خصوصی مجازند (نام‌های نامحلی‌شده‌ی icacls).
_ALLOWED_PRINCIPALS = ("NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators", "OWNER RIGHTS")
_RUN_NAME_RE = re.compile(rf"^{RUN_PREFIX}[a-z0-9_]{{8,}}$")


class PrivateDirectoryError(RuntimeError):
    """پوشه‌ی خصوصی ساخته نشد یا محدودیتِ دسترسی‌اش قابلِ تأیید نیست."""


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def acl_problems(path: Path) -> list[str]:
    """ورودی‌های ACLِ غیرمجاز یا ارث‌برده — فقط روی Windows؛ جای دیگر mode=0o700 کافی است."""
    if sys.platform != "win32":
        mode = Path(path).stat().st_mode & 0o777
        return [] if mode & 0o077 == 0 else [f"mode {oct(mode)} allows group/other access"]
    try:
        output = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True, timeout=30, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ["icacls could not be run"]
    allowed = (*_ALLOWED_PRINCIPALS, _current_user())
    problems = []
    for line in output.splitlines():
        entry = line.replace(str(path), "", 1).strip()
        if not entry or ":" not in entry or entry.startswith("Successfully processed"):
            continue
        principal, _, rights = entry.rpartition(":")
        if "(I)" in rights:
            problems.append(f"inherited entry for {principal.strip()}")
        elif principal.strip().lower() not in {name.lower() for name in allowed}:
            problems.append(f"unexpected entry for {principal.strip()}")
    return problems


def create_private_dir(root: Path = DEFAULT_ROOT) -> Path:
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=RUN_PREFIX, dir=root))
        (directory / MARKER).write_text("newman engine private scratch\n", encoding="utf-8")
    except OSError as exc:
        raise PrivateDirectoryError(
            f"the private directory cannot be created ({type(exc).__name__})."
        ) from exc
    problems = acl_problems(directory)
    if problems:
        shutil.rmtree(directory, ignore_errors=True)
        raise PrivateDirectoryError(
            "the private directory is not restricted to the current user: " + "; ".join(problems)
        )
    return directory


def remove_private_dir(directory: Path) -> bool:
    """فقط پوشه‌ی خصوصیِ موتور حذف می‌شود — هر مسیرِ دیگری نادیده گرفته می‌شود."""
    if not _is_engine_dir(directory):
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return not directory.exists()


def _is_engine_dir(directory: Path) -> bool:
    return (
        directory.is_dir()
        and not directory.is_symlink()
        and _RUN_NAME_RE.match(directory.name) is not None
        and (directory / MARKER).is_file()
    )


def sweep_stale(root: Path = DEFAULT_ROOT, max_age_s: float = STALE_AFTER_S) -> int:
    """پوشه‌های رهاشده‌ی اجراهای قطع‌شده — فقط داخلِ root، فقط با نشانِ موتور."""
    if not root.is_dir():
        return 0
    removed = 0
    now = time.time()
    for child in root.iterdir():
        try:
            if not _is_engine_dir(child) or now - child.stat().st_mtime < max_age_s:
                continue
        except OSError:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += not child.exists()
    return removed


__all__ = [
    "DEFAULT_ROOT",
    "MARKER",
    "STALE_AFTER_S",
    "PrivateDirectoryError",
    "acl_problems",
    "create_private_dir",
    "remove_private_dir",
    "sweep_stale",
]
