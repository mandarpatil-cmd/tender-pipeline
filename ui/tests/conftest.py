from __future__ import annotations

import pytest

from pipeline_core.db import dispose_engines


@pytest.fixture(autouse=True)
def _dispose_engines():
    """Engines are cached per path; Windows will not delete a file still open."""
    yield
    dispose_engines()


@pytest.fixture(autouse=True)
def _isolated_files(tmp_path, monkeypatch):
    """No test may open the real database or rewrite the real previews folder."""
    monkeypatch.setenv("PIPELINE_DB", str(tmp_path / "default.sqlite3"))
    from ops_ui.runs import load_campaign

    campaign = load_campaign()
    monkeypatch.setattr(campaign, "PREVIEW_DIR", tmp_path / "previews")
