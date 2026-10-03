"""Read-only questions the window asks. The answers live in pipeline_core."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.queries import (
    backfill_tender_documents,
    contact_breakdown,
    pipeline_funnel,
)
from sqlalchemy.exc import DatabaseError, OperationalError

#: Shown when creating the schema is the way forward.
INIT_HINT = "uv run pipeline-db init"

#: First 16 bytes of every SQLite file. Opening anything else can create a database.
_SQLITE_HEADER = b"SQLite format 3\x00"


@dataclass(frozen=True)
class HomeSnapshot:
    """The count strip. `ready` is false until the shared database can be read."""

    path: Path
    ready: bool
    funnel: tuple[tuple[str, int, str], ...] = ()
    phone_only: int = 0
    message: str = ""


def load_home(path: Path) -> HomeSnapshot:
    """Counts for the home strip, from the same queries `pipeline-db status` prints.

    Does not create the database. A missing file stays missing, and an empty or
    non-SQLite file is not opened — opening one would turn it into a database.
    """
    if not path.is_file():
        return _unread(path, f"No database yet. From the repo root, run: {INIT_HINT}")

    kind = _file_kind(path)
    if kind == "empty":
        return _unread(path, f"This file is empty. From the repo root, run: {INIT_HINT}")
    if kind == "other":
        return _unread(
            path,
            f"This file is not a database. Remove it, then run: {INIT_HINT}",
        )
    if kind == "unreadable":
        return _unread(path, "The database could not be read. Nothing was changed.")

    ensure_schema(engine(path))
    backfill_tender_documents(path)
    try:
        with session(engine(path)) as current:
            funnel = tuple(pipeline_funnel(current))
            phone_only = contact_breakdown(current)["phone_only"]
    except OperationalError as exc:
        return _unread(path, _operational_message(exc))
    except DatabaseError:
        return _unread(
            path, "This file could not be read as a database. Nothing was changed."
        )

    return HomeSnapshot(path=path, ready=True, funnel=funnel, phone_only=phone_only)


def _unread(path: Path, message: str) -> HomeSnapshot:
    return HomeSnapshot(path=path, ready=False, message=message)


def _file_kind(path: Path) -> str:
    """'empty', 'sqlite', 'other', or 'unreadable'. Reads the header only."""
    try:
        with path.open("rb") as handle:
            head = handle.read(len(_SQLITE_HEADER))
    except OSError:
        return "unreadable"
    if head == b"":
        return "empty"
    if head == _SQLITE_HEADER:
        return "sqlite"
    return "other"


def _operational_message(exc: OperationalError) -> str:
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return "The database is busy. Wait for the other run to finish, then reload."
    if "no such table" in text:
        return f"This file has no pipeline tables. From the repo root, run: {INIT_HINT}"
    return "The database could not be read. Nothing was changed."
