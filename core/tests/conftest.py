from __future__ import annotations

import pytest

from pipeline_core.db import dispose_engines


@pytest.fixture(autouse=True)
def _dispose_engines():
    """Engines are cached per path; Windows will not delete a file still open."""
    yield
    dispose_engines()
