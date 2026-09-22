from pathlib import Path

import pytest

from pipeline_core.db import dispose_engines

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def html():
    def load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return load


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point every test at its own database.

    `Store()` now defaults to the pipeline's shared database. Without this a test
    run would scribble on real scraped data, so `PIPELINE_DB` is redirected for
    the whole suite rather than trusting each test to pass a path.
    """
    monkeypatch.setenv("PIPELINE_DB", str(tmp_path / "pipeline.sqlite3"))
    yield
    # Engines are cached per path, and Windows will not delete an open file.
    dispose_engines()
