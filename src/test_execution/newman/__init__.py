"""
src/test_execution/newman — موتورِ قابلِ استفاده‌ی مجددِ اجرای Newman

    from src.test_execution.newman import NewmanExecutionEngine, NewmanRunRequest

    result = NewmanExecutionEngine().run(NewmanRunRequest.for_dev(collection, output_dir))
    result.status  # PASS | FAIL | ERROR

مستقل از Streamlit، Batch و Jira. environment و globals فقط خوانده می‌شوند.
"""

from src.test_execution.newman.engine import HTMLEXTRA_OPTIONS, NewmanExecutionEngine
from src.test_execution.newman.models import (
    DEV_ENVIRONMENT_PATH,
    NO_ASSERTIONS_WARNING,
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
    WORKSPACE_GLOBALS_PATH,
    NewmanOptions,
    NewmanRunRequest,
    NewmanRunResult,
    RequestResult,
)

__all__ = [
    "DEV_ENVIRONMENT_PATH",
    "HTMLEXTRA_OPTIONS",
    "NO_ASSERTIONS_WARNING",
    "STATUS_ERROR",
    "STATUS_FAIL",
    "STATUS_PASS",
    "WORKSPACE_GLOBALS_PATH",
    "NewmanExecutionEngine",
    "NewmanOptions",
    "NewmanRunRequest",
    "NewmanRunResult",
    "RequestResult",
]
