"""
src/execution_gate/_oslock.py — قفلِ انحصاری و غیرمسدودکننده‌ی سیستم‌عامل روی فایلِ ثابتِ قفل

این قفل مرجعِ انحصارِ متقابل است؛ پایگاهِ داده فقط ثبت می‌کند چه کسی آن را دارد.

  * Windows: msvcrt.locking(LK_NBLCK) روی بایتِ صفر. قفلِ ناحیه‌ای مالِ همان handle است:
    handleِ دومِ همان فایل — حتی در همان پروسه — نمی‌تواند همان بایت را قفل کند (تست‌ها
    این را واقعاً بررسی می‌کنند). LK_LOCK به کار نمی‌رود چون خودش ده بار تلاشِ دوباره می‌کند.
  * POSIX: fcntl.flock(LOCK_EX | LOCK_NB). flock مالِ «open file description» است، پس دو
    open ِ جدا — حتی در یک پروسه — با هم تداخل دارند (برخلافِ fcntl/lockf که مالِ پروسه‌اند).
  * سکوی دیگر: پشتیبانی نمی‌شود و GateUnavailable — هیچ جایگزینِ ناامنی نیست.

آزاد شدن: release() صریح، بستنِ handle، یا مرگِ پروسه (سیستم‌عامل همه‌ی قفل‌های پروسه را
آزاد می‌کند). handle ارث‌بردنی نیست (PEP 446) پس پروسه‌ی فرزندِ exec‌شده (مثلِ Newman) آن را
ندارد؛ روی POSIX، فرزندِ fork‌شده‌ی بی‌exec نسخه‌ی خودش را بلافاصله می‌بندد (register_at_fork)
تا قفل را پس از مرگِ والد زنده نگه ندارد — بستن در فرزند قفلِ والد را آزاد نمی‌کند.

فایلِ قفل هرگز حذف، جایگزین یا truncate نمی‌شود و چیزی در آن نوشته نمی‌شود. پس از گرفتنِ
قفل بررسی می‌شود که handle هنوز همان فایلِ روی مسیر است (dev/inode)؛ روی POSIX فایلی که
حذف و دوباره ساخته شده قفلِ جدایی دارد و این بررسی آن را می‌بیند. فقط دیسکِ محلی و یک میزبان.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
import weakref
from pathlib import Path
from typing import BinaryIO

from .errors import GateUnavailable

if sys.platform == "win32":
    import msvcrt

    PLATFORM: str | None = "windows"
else:
    try:
        import fcntl
    except ImportError:  # pragma: no cover — سکوی بی‌fcntl
        PLATFORM = None
    else:
        PLATFORM = "posix"

# خطاهایی که یعنی «قفل را دیگری دارد»؛ هر خطای دیگر یعنی وضعیت قابلِ اطمینان نیست
_BUSY_ERRNOS = frozenset(
    code
    for code in (
        errno.EACCES,
        errno.EAGAIN,
        errno.EWOULDBLOCK,
        getattr(errno, "EDEADLK", None),
        getattr(errno, "EDEADLOCK", None),
    )
    if code is not None
)
_OPEN_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOINHERIT", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)

_held: weakref.WeakSet[OsLock] = weakref.WeakSet()


def ensure_supported() -> None:
    if PLATFORM is None:
        raise GateUnavailable(
            "OS file locking for the execution gate is not supported on this platform; "
            "nothing was started."
        )


def _unavailable(what: str, exc: BaseException) -> GateUnavailable:
    return GateUnavailable(
        f"The execution gate lock file {what} ({type(exc).__name__}); nothing was started."
    )


class OsLock:
    """یک handle و قفلِ انحصاریِ آن. thread-safe؛ هر نمونه حداکثر یک بار در هر لحظه."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._file: BinaryIO | None = None
        self._identity: tuple[int, int] | None = None
        self._mutex = threading.Lock()

    @property
    def held(self) -> bool:
        return self._file is not None

    def try_acquire(self) -> bool:
        """True = قفل گرفته شد؛ False = دیگری دارد؛ GateUnavailable = نامعلوم (fail-closed)."""
        ensure_supported()
        with self._mutex:
            if self._file is not None:
                raise RuntimeError("This OS lock is already held by this object.")
            handle = _open(self.path)
            try:
                if not _lock(handle):
                    handle.close()
                    return False
                try:
                    identity = _identity(handle, self.path)
                except BaseException:
                    _unlock(handle)
                    raise
            except BaseException:
                handle.close()
                raise
            self._file, self._identity = handle, identity
            _held.add(self)
            return True

    def verify(self) -> bool:
        """این handle هنوز قفل را روی همان فایلی دارد که اکنون روی مسیر است؟"""
        with self._mutex:
            if self._file is None:
                return False
            try:
                return _identity(self._file, self.path) == self._identity
            except GateUnavailable:
                return False

    def release(self) -> None:
        """idempotent؛ فقط قفلِ همین handle را آزاد می‌کند — هیچ راهی برای آزاد کردنِ قفلِ
        handle یا پروسه‌ی دیگری نیست."""
        with self._mutex:
            handle, self._file, self._identity = self._file, None, None
            if handle is None:
                return
            _held.discard(self)
            try:
                _unlock(handle)
            except OSError:
                pass  # بستنِ handle در هر حال قفل را آزاد می‌کند
            finally:
                handle.close()


def _open(path: Path) -> BinaryIO:
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise GateUnavailable(
                "The execution gate lock path is not a regular file; nothing was started."
            )
        fd = os.open(path, _OPEN_FLAGS, 0o600)
    except GateUnavailable:
        raise
    except OSError as exc:
        raise _unavailable("cannot be opened", exc) from None
    try:
        if os.get_inheritable(fd):
            os.set_inheritable(fd, False)
        return os.fdopen(fd, "r+b", buffering=0)
    except BaseException:
        os.close(fd)
        raise


def _lock(handle: BinaryIO) -> bool:
    fd = handle.fileno()
    try:
        if PLATFORM == "windows":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _BUSY_ERRNOS:
            return False
        raise _unavailable("cannot be locked", exc) from None
    return True


def _unlock(handle: BinaryIO) -> None:
    fd = handle.fileno()
    if PLATFORM == "windows":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _identity(handle: BinaryIO, path: Path) -> tuple[int, int]:
    """(dev، inode) ِ handle — باید با فایلِ فعلیِ روی مسیر یکی باشد؛ inode ِ صفر یعنی
    سیستم‌فایل هویت را نمی‌دهد و نمی‌توان مطمئن بود."""
    try:
        opened, current = os.fstat(handle.fileno()), os.stat(path)
    except OSError as exc:
        raise _unavailable("cannot be inspected", exc) from None
    identity = (opened.st_dev, opened.st_ino)
    if opened.st_ino == 0 or identity != (current.st_dev, current.st_ino):
        raise GateUnavailable(
            "The execution gate lock file was replaced or cannot be identified; "
            "nothing was started."
        )
    return identity


def _close_in_forked_child() -> None:
    """فرزندِ fork‌شده نسخه‌ی handle را می‌بندد (بدونِ LOCK_UN): قفلِ والد می‌ماند و فرزند
    نمی‌تواند آن را پس از مرگِ والد زنده نگه دارد. mutex گرفته نمی‌شود (ممکن است در لحظه‌ی
    fork دستِ threadِ دیگری بوده باشد)."""
    for lock in list(_held):
        handle, lock._file, lock._identity = lock._file, None, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_close_in_forked_child)


__all__ = ["PLATFORM", "OsLock", "ensure_supported"]
