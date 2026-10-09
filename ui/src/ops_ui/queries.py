"""Read-only questions the window asks. The answers live in pipeline_core."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.db import BUSY_TIMEOUT_MS, engine, ensure_schema, session
from pipeline_core.models import STATUS_PENDING, TABLES, Tender, Vendor
from pipeline_core.queries import backfill_tender_documents, outreach_targets
from sqlalchemy import func, select
from sqlalchemy.exc import DatabaseError, OperationalError

#: Shown when creating the schema is the way forward.
INIT_HINT = "uv run pipeline-db init"

#: First 16 bytes of every SQLite file. Opening anything else can create a database.
_SQLITE_HEADER = b"SQLite format 3\x00"

#: Tables that mean this file is already a pipeline database.
_PIPELINE_TABLES = frozenset(table.__tablename__ for table in TABLES)


@dataclass(frozen=True)
class HomeSnapshot:
    """The count strip. `ready` is false until the shared database can be read."""

    path: Path
    ready: bool
    message: str = ""
    tenders: int = 0
    companies: int = 0
    waiting: int = 0
    to_mail: int = 0


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

    try:
        names = _table_names(path)
    except sqlite3.OperationalError as exc:
        return _unread(path, _operational_message(exc))
    except sqlite3.DatabaseError:
        return _unread(
            path, "This file could not be read as a database. Nothing was changed."
        )
    if names.isdisjoint(_PIPELINE_TABLES):
        return _unread(
            path,
            f"This file has no pipeline tables. From the repo root, run: {INIT_HINT}",
        )

    try:
        ensure_schema(engine(path))
        backfill_tender_documents(path)
        with session(engine(path)) as current:
            tenders = int(current.scalar(select(func.count()).select_from(Tender)) or 0)
            companies = int(current.scalar(select(func.count()).select_from(Vendor)) or 0)
            waiting = int(
                current.scalar(
                    select(func.count())
                    .select_from(Vendor)
                    .where(Vendor.enrichment_status == STATUS_PENDING)
                )
                or 0
            )
            to_mail = len(outreach_targets(current))
    except OperationalError as exc:
        return _unread(path, _operational_message(exc))
    except DatabaseError:
        return _unread(
            path, "This file could not be read as a database. Nothing was changed."
        )

    return HomeSnapshot(
        path=path,
        ready=True,
        tenders=tenders,
        companies=companies,
        waiting=waiting,
        to_mail=to_mail,
    )


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


def _table_names(path: Path) -> set[str]:
    """User table names. Read-only, so a foreign file is not switched to WAL."""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    timeout = BUSY_TIMEOUT_MS / 1000
    connection = sqlite3.connect(uri, uri=True, timeout=timeout)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        return {name for (name,) in rows}
    finally:
        connection.close()


def _operational_message(exc: Exception) -> str:
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return "The database is busy. Wait for the other run to finish, then reload."
    if "no such table" in text:
        return f"This file has no pipeline tables. From the repo root, run: {INIT_HINT}"
    return "The database could not be read. Nothing was changed."
