"""Values every route reads from the running app."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from pipeline_core import settings


def database_path(request: Request) -> Path:
    """The shared database, or the throwaway file a test stored on the app."""
    if request.app.state.database is not None:
        return request.app.state.database
    return settings.db_path()
