"""Where the pipeline lives on disk.

One database is shared by all three stages, so every stage has to agree on its
path without being told. The root is found by walking up from this file until a
directory holding the pipeline's stage folders appears.
"""

from __future__ import annotations

import os
from pathlib import Path

#: A directory is the pipeline root when it contains all of these.
ROOT_MARKERS = ("01-scrape", "core")

DB_FILENAME = "pipeline.sqlite3"


def project_root() -> Path:
    """The `tender-pipeline` directory.

    `PIPELINE_ROOT` overrides the search — set it when the package is installed
    somewhere that is not a checkout (a container, a test fixture).
    """
    override = os.environ.get("PIPELINE_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if all((parent / marker).is_dir() for marker in ROOT_MARKERS):
            return parent

    # Installed editable from <root>/core/src/pipeline_core/settings.py.
    if len(here.parents) > 3:
        return here.parents[3]
    raise RuntimeError(
        "Cannot locate the pipeline root. Set PIPELINE_ROOT to the directory "
        "holding 01-scrape/, 02-enrich/, 03-outreach/ and core/."
    )


def data_dir() -> Path:
    return project_root() / "data"


def db_path() -> Path:
    """The shared SQLite file. `PIPELINE_DB` overrides it (tests, a scratch copy)."""
    override = os.environ.get("PIPELINE_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return data_dir() / DB_FILENAME
