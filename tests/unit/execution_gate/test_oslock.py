"""
tests/unit/execution_gate/test_oslock.py — قفلِ واقعیِ سیستم‌عامل (بدونِ mock)

مهم‌ترین فرضِ Windows این‌جا واقعاً سنجیده می‌شود: دو handleِ جدا روی همان فایل در همان
پروسه نمی‌توانند هر دو بایتِ صفر را قفل کنند. روی Linux همان تست flock را می‌سنجد.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from src.execution_gate import GateUnavailable, _oslock
from src.execution_gate._oslock import OsLock

WINDOWS = sys.platform == "win32"


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "gate.lock"


def test_the_platform_has_a_real_lock_implementation():
    assert _oslock.PLATFORM == ("windows" if WINDOWS else "posix")


class TestSameProcess:
    def test_a_second_handle_in_the_same_process_cannot_lock(self, lock_path):
        first, second = OsLock(lock_path), OsLock(lock_path)
        assert first.try_acquire()
        try:
            assert not second.try_acquire()
            assert not second.held
        finally:
            first.release()
        assert second.try_acquire()
        second.release()

    def test_many_handles_in_the_same_process_never_share_the_lock(self, lock_path):
        owner = OsLock(lock_path)
        assert owner.try_acquire()
        try:
            others = [OsLock(lock_path) for _ in range(5)]
            assert [other.try_acquire() for other in others] == [False] * 5
        finally:
            owner.release()

    def test_a_held_lock_object_cannot_be_acquired_twice(self, lock_path):
        lock = OsLock(lock_path)
        assert lock.try_acquire()
        try:
            with pytest.raises(RuntimeError):
                lock.try_acquire()
        finally:
            lock.release()

    def test_a_non_owner_release_does_not_free_the_owner_lock(self, lock_path):
        owner, other = OsLock(lock_path), OsLock(lock_path)
        assert owner.try_acquire()
        try:
            assert not other.try_acquire()
            other.release()  # هیچ اثری: other هیچ handle یا قفلی ندارد
            assert not OsLock(lock_path).try_acquire()
            assert owner.verify()
        finally:
            owner.release()

    def test_release_is_idempotent(self, lock_path):
        lock = OsLock(lock_path)
        assert lock.try_acquire()
        lock.release()
        lock.release()
        assert not lock.held and not lock.verify()


class TestLockFile:
    def test_the_lock_file_is_stable_and_never_rewritten(self, lock_path):
        lock_path.write_bytes(b"keep")
        before = os.stat(lock_path)
        for _ in range(3):
            lock = OsLock(lock_path)
            assert lock.try_acquire()
            lock.release()
        after = os.stat(lock_path)
        assert lock_path.read_bytes() == b"keep"
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)

    def test_the_handle_is_not_inheritable(self, lock_path):
        lock = OsLock(lock_path)
        assert lock.try_acquire()
        try:
            assert os.get_inheritable(lock._file.fileno()) is False
        finally:
            lock.release()

    def test_a_directory_at_the_lock_path_fails_closed(self, lock_path):
        lock_path.mkdir()
        with pytest.raises(GateUnavailable, match="not a regular file"):
            OsLock(lock_path).try_acquire()

    def test_an_unsupported_platform_fails_closed(self, lock_path, monkeypatch):
        monkeypatch.setattr(_oslock, "PLATFORM", None)
        with pytest.raises(GateUnavailable, match="not supported"):
            OsLock(lock_path).try_acquire()
        assert not lock_path.exists()

    def test_an_unexpected_lock_error_fails_closed(self, lock_path, monkeypatch):
        def broken(handle):
            raise _oslock._unavailable("cannot be locked", OSError(5, "I/O error"))

        monkeypatch.setattr(_oslock, "_lock", broken)
        with pytest.raises(GateUnavailable, match="cannot be locked"):
            OsLock(lock_path).try_acquire()

    def test_an_unidentifiable_file_fails_closed_and_unlocks(self, lock_path, monkeypatch):
        def unidentifiable(handle, path):
            raise GateUnavailable("cannot be identified")

        monkeypatch.setattr(_oslock, "_identity", unidentifiable)
        with pytest.raises(GateUnavailable):
            OsLock(lock_path).try_acquire()
        monkeypatch.undo()
        lock = OsLock(lock_path)
        assert lock.try_acquire()  # قفلِ نیمه‌کاره آزاد شده بود
        lock.release()


@pytest.mark.skipif(not WINDOWS, reason="Windows-only file semantics")
class TestWindows:
    def test_a_held_lock_file_cannot_be_deleted(self, lock_path):
        lock = OsLock(lock_path)
        assert lock.try_acquire()
        try:
            with pytest.raises(PermissionError):
                os.remove(lock_path)
        finally:
            lock.release()
        assert lock_path.exists()


@pytest.mark.skipif(WINDOWS, reason="POSIX-only flock and fork semantics")
class TestPosix:
    def test_a_replaced_lock_file_is_detected(self, lock_path):
        lock = OsLock(lock_path)
        assert lock.try_acquire()
        try:
            os.remove(lock_path)
            lock_path.write_bytes(b"")
            assert not lock.verify()
        finally:
            lock.release()

    def test_a_forked_child_does_not_keep_the_lock(self, lock_path):
        lock = OsLock(lock_path)
        assert lock.try_acquire()
        pid = os.fork()
        if pid == 0:  # pragma: no cover — در فرزند اجرا می‌شود
            os._exit(0 if not lock.held else 1)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        try:
            assert lock.verify()
            assert not OsLock(lock_path).try_acquire()  # بستن در فرزند قفلِ والد را آزاد نکرد
        finally:
            lock.release()
        other = OsLock(lock_path)
        assert other.try_acquire()
        other.release()
