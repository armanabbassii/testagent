"""
tests/unit/swagger_sync/test_import_independence.py — Swagger بدونِ تنظیماتِ LLM؛ import ِ lazy

هر بررسی در یک پروسه‌ی Python ِ جدا اجرا می‌شود، با محیطی که هیچ متغیرِ LLM/Embedding ندارد و
ENV_FILE ِ آن به فایلی ناموجود (یا یک فایلِ ساختگیِ موقت) اشاره می‌کند — .env ِ واقعی هرگز بار
نمی‌شود. هیچ درخواستِ شبکه‌ای.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LLM_KEYS = (
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL",
    "EMBEDDING_API_KEY",
    "CLIENT_ID",
)


def _environment(tmp_path: Path, env_file: Path | None = None, **extra: str) -> dict[str, str]:
    environment = {
        k: v
        for k, v in os.environ.items()
        if k not in LLM_KEYS and not k.startswith(("SWAGGER_", "LLM_", "EMBEDDING_"))
    }
    environment["ENV_FILE"] = str(env_file or tmp_path / "missing.env")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.update(extra)
    return environment


def _run(code: str, environment: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_swagger_modules_import_without_any_llm_setting(tmp_path):
    result = _run(
        """
        import sys
        import src.swagger_settings as s
        import src.agents.test_case_generator.swagger_snapshots as snaps
        import src.swagger_sync.job, src.swagger_sync.task, src.swagger_sync.__main__
        assert s.SWAGGER_CONNECT_TIMEOUT_SECONDS == 10.0
        assert s.SWAGGER_READ_TIMEOUT_SECONDS == 120.0
        assert snaps.SWAGGER_SOURCES
        heavy = [m for m in ("src.config", "src.llm_client", "src.embedding_client",
                             "src.agents.graph", "src.agents.base_agent", "langgraph")
                 if m in sys.modules]
        assert heavy == [], heavy
        print("ok")
        """,
        _environment(tmp_path),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "ok"


def test_importing_src_still_loads_the_env_file(tmp_path):
    env_file = tmp_path / "fake.env"
    env_file.write_text("SWAGGER_SYNC_TEST_MARKER=loaded\n", encoding="utf-8")
    result = _run(
        """
        import os, src
        print(os.environ.get("SWAGGER_SYNC_TEST_MARKER"))
        """,
        _environment(tmp_path, env_file),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "loaded"


def test_lazy_exports_keep_their_public_names(tmp_path):
    placeholders = {k: "placeholder" for k in LLM_KEYS}
    placeholders.update(
        LLM_BASE_URL="http://127.0.0.1:9/", EMBEDDING_BASE_URL="http://127.0.0.1:9/"
    )
    result = _run(
        """
        import sys
        import src, src.agents
        assert "src.llm_client" not in sys.modules
        from src import LLMClient, EmbeddingClient, config
        from src.llm_client import LLMClient as direct
        assert LLMClient is direct and EmbeddingClient.__name__ == "EmbeddingClient"
        assert config.SWAGGER_READ_TIMEOUT_SECONDS == 120.0
        from src.agents import build_graph, AgentState, BaseAgent, HITLHandler
        from src.agents import MemoryLoaderNode, MemorySaverNode
        from src.agents.graph import build_graph as graph_direct
        assert build_graph is graph_direct
        for name in src.__all__:
            assert name in dir(src)
        for name in src.agents.__all__:
            assert name in dir(src.agents)
        for module, missing in ((src, "nope"), (src.agents, "nope")):
            try:
                getattr(module, missing)
            except AttributeError:
                pass
            else:
                raise AssertionError("unknown attribute did not raise")
        print("ok")
        """,
        _environment(tmp_path, **placeholders),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "ok"


def test_config_still_requires_llm_settings(tmp_path):
    result = _run("import src.config", _environment(tmp_path))
    assert result.returncode != 0 and "LLM_BASE_URL" in result.stderr


def test_invalid_base_url_fails_closed(tmp_path):
    result = _run(
        "import src.swagger_settings",
        _environment(tmp_path, SWAGGER_API_DOCS_BASE_URL="ftp://example.invalid/x"),
    )
    assert result.returncode != 0 and "SWAGGER_API_DOCS_BASE_URL" in result.stderr
    assert "example.invalid" not in result.stderr.split("SwaggerSettingsError")[-1]
