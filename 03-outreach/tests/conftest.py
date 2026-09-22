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
def previews_in_tmp(tmp_path, monkeypatch):
    """Keep test previews out of the real previews/ folder."""
    import campaign

    monkeypatch.setattr(campaign, "PREVIEW_DIR", tmp_path / "previews")
