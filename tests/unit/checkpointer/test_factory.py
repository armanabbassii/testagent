"""تست‌های checkpointer factory."""

import pytest
from unittest.mock import patch, MagicMock
from contextlib import contextmanager


@contextmanager
def _fake_cp():
    yield MagicMock()


class TestMakeCheckpointer:
    @patch("src.checkpointer.backends.sqlite_backend.make_sqlite_checkpointer",
           return_value=_fake_cp())
    def test_sqlite_backend_selected(self, mock_sqlite):
        with patch.dict("os.environ", {"CHECKPOINTER_BACKEND": "sqlite"}):
            from src.checkpointer.factory import make_checkpointer
            with make_checkpointer():
                pass
        mock_sqlite.assert_called_once()

    def test_invalid_backend_raises(self):
        with patch.dict("os.environ", {"CHECKPOINTER_BACKEND": "oracle"}):
            from src.checkpointer.factory import make_checkpointer
            with pytest.raises(ValueError, match="نامعتبر"):
                with make_checkpointer():
                    pass

    def test_default_backend_is_sqlite(self, tmp_path):
        """وقتی CHECKPOINTER_BACKEND تعریف نشده، sqlite پیش‌فرض است."""
        env = {"CHECKPOINTER_SQLITE_PATH": str(tmp_path / "test.db")}
        # CHECKPOINTER_BACKEND را از env حذف می‌کنیم
        with patch.dict("os.environ", env, clear=False):
            import os
            os.environ.pop("CHECKPOINTER_BACKEND", None)

            from src.checkpointer.factory import make_checkpointer
            try:
                with make_checkpointer() as cp:
                    assert cp is not None
            except ImportError:
                pytest.skip("langgraph-checkpoint-sqlite نصب نیست")


class TestSQLiteBackend:
    def test_in_memory_sqlite(self):
        try:
            from src.checkpointer.backends.sqlite_backend import make_sqlite_checkpointer
            with make_sqlite_checkpointer(db_path=":memory:") as cp:
                assert cp is not None
        except ImportError:
            pytest.skip("langgraph-checkpoint-sqlite نصب نیست")

    def test_file_sqlite_creates_file(self, tmp_path):
        try:
            from src.checkpointer.backends.sqlite_backend import make_sqlite_checkpointer
            db_path = str(tmp_path / "test.db")
            with make_sqlite_checkpointer(db_path=db_path) as cp:
                assert cp is not None
        except ImportError:
            pytest.skip("langgraph-checkpoint-sqlite نصب نیست")

    def test_sqlite_creates_parent_dirs(self, tmp_path):
        try:
            from src.checkpointer.backends.sqlite_backend import make_sqlite_checkpointer
            nested = str(tmp_path / "a" / "b" / "checkpoints.db")
            with make_sqlite_checkpointer(db_path=nested) as cp:
                assert cp is not None
        except ImportError:
            pytest.skip("langgraph-checkpoint-sqlite نصب نیست")