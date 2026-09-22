from __future__ import annotations

import pytest

from pipeline_core.db import dispose_engines, engine, ensure_schema


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own database, never the real pipeline one."""
    path = tmp_path / "pipeline.sqlite3"
    monkeypatch.setenv("PIPELINE_DB", str(path))
    ensure_schema(engine(path))
    yield path
    dispose_engines()


@pytest.fixture(autouse=True)
def _model_env(monkeypatch):
    """`llm_runs.model` is read from the environment even when the chain is fake."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MODEL", "google/gemini-2.5-flash")
