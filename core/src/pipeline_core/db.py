"""Engine, session, and the only place the schema is created or changed.

Stages run one after another in practice, so WAL and `busy_timeout` are
belt-and-braces rather than a concurrency design.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, select, func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import settings
from .models import TABLES, Base

#: Seconds a blocked writer waits before raising "database is locked".
BUSY_TIMEOUT_MS = 5000

_ENGINES: dict[tuple[str, bool], Engine] = {}


def engine(path: Path | str | None = None, *, foreign_keys: bool = True) -> Engine:
    """Engine for the shared database, one per path (SQLAlchemy pools per engine)."""
    target = Path(path) if path is not None else settings.db_path()
    target = target.expanduser()
    key = (str(target), foreign_keys)
    existing = _ENGINES.get(key)
    if existing is not None:
        return existing

    target.parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(f"sqlite+pysqlite:///{target}", future=True)

    @event.listens_for(eng, "connect")
    def _on_connect(dbapi_connection: sqlite3.Connection, _record) -> None:
        from .loose import loose_date, loose_number

        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            if foreign_keys:
                cursor.execute("PRAGMA foreign_keys=ON")
            dbapi_connection.create_function("loose_date", 1, loose_date)
            dbapi_connection.create_function("loose_number", 1, loose_number)
        finally:
            cursor.close()

    _ENGINES[key] = eng
    return eng


def dispose_engines() -> None:
    """Close every pooled connection. Tests need this before deleting a file."""
    for eng in _ENGINES.values():
        eng.dispose()
    _ENGINES.clear()


def session_factory(bind: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=bind or engine(), future=True, expire_on_commit=False)


@contextmanager
def session(bind: Engine | None = None) -> Iterator[Session]:
    """Commit on clean exit, roll back on error.

    Stage 2 commits per vendor rather than per run, so open one of these per
    vendor — that is what makes the job resumable.
    """
    maker = session_factory(bind)
    current = maker()
    try:
        yield current
        current.commit()
    except Exception:
        current.rollback()
        raise
    finally:
        current.close()


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


@dataclass
class SchemaReport:
    """What `ensure_schema` actually changed. Empty everywhere means a no-op."""

    created_tables: list[str] = field(default_factory=list)
    added_columns: list[str] = field(default_factory=list)
    created_indexes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created_tables or self.added_columns or self.created_indexes)


def create_all(bind: Engine | None = None) -> None:
    Base.metadata.create_all(bind or engine())


def drop_all(bind: Engine | None = None) -> None:
    Base.metadata.drop_all(bind or engine())


def ensure_schema(bind: Engine | None = None) -> SchemaReport:
    """Bring a database up to the models, old or brand new.

    `create_all` only ever creates *missing tables* — it will not add a column to
    a table that already exists, which is exactly what adopting the scraper's
    database needs (`vendors.source`). So new columns and new indexes on
    pre-existing tables are applied here explicitly.

    Only nullable columns can be added this way; a new NOT NULL column needs a
    backfill and so is refused rather than half-applied.

    Amounts used to be text (``INR 179,853.24``). SQLite cannot change that
    column to an integer, so a file that still holds text amounts is copied to
    ``<name>.bak`` and replaced with an empty database of the current schema.
    Contacts and mail history stay in the copy.
    """
    eng = bind or engine()
    if _has_text_amounts(eng):
        _backup_and_reset(eng)
        report = SchemaReport()
        report.created_tables = sorted(inspect(eng).get_table_names())
        return report
    report = SchemaReport()

    before = set(inspect(eng).get_table_names())
    Base.metadata.create_all(eng)
    report.created_tables = sorted(set(inspect(eng).get_table_names()) - before)

    inspector = inspect(eng)
    for table in Base.metadata.sorted_tables:
        if table.name not in before:
            continue  # freshly created — columns and indexes came with it

        existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_columns:
                continue
            if not column.nullable:
                raise RuntimeError(
                    f"Cannot add NOT NULL column {table.name}.{column.name} to an "
                    "existing table without a backfill. Add it by hand, or reset."
                )
            ddl_type = column.type.compile(eng.dialect)
            with eng.begin() as conn:
                conn.exec_driver_sql(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}'
                )
            report.added_columns.append(f"{table.name}.{column.name}")

        existing_indexes = {idx["name"] for idx in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in existing_indexes:
                continue
            index.create(eng, checkfirst=True)
            report.created_indexes.append(index.name or "<unnamed>")

    from .queries import backfill_vendor_contacts

    with session(eng) as current:
        backfill_vendor_contacts(current)
    return report


def _has_text_amounts(eng: Engine) -> bool:
    """True when a stored amount is still text, such as ``INR 10``."""
    inspector = inspect(eng)
    names = set(inspector.get_table_names())
    checks: list[str] = []
    if "tenders" in names:
        columns = {col["name"] for col in inspector.get_columns("tenders")}
        if "contract_value" in columns:
            checks.append(
                "SELECT 1 FROM tenders WHERE typeof(contract_value) = 'text' LIMIT 1"
            )
    if "awards" in names:
        columns = {col["name"] for col in inspector.get_columns("awards")}
        parts = [
            f"typeof({name}) = 'text'"
            for name in ("quoted_value", "awarded_value", "contract_value")
            if name in columns
        ]
        if parts:
            checks.append(f"SELECT 1 FROM awards WHERE {' OR '.join(parts)} LIMIT 1")
    if not checks:
        return False
    with eng.connect() as conn:
        return any(conn.exec_driver_sql(sql).first() for sql in checks)


def _sqlite_path(eng: Engine) -> Path | None:
    raw = eng.url.database
    if not raw or raw == ":memory:":
        return None
    return Path(raw)


def _backup_and_reset(eng: Engine) -> None:
    """Copy the file aside, then create the empty typed schema in its place."""
    path = _sqlite_path(eng)
    if path is not None and path.exists():
        bak = Path(str(path) + ".bak")
        if not bak.exists():
            with eng.begin() as conn:
                conn.exec_driver_sql("PRAGMA wal_checkpoint(FULL)")
            shutil.copy2(path, bak)
            if not bak.exists():
                raise RuntimeError(f"Could not copy {path} to {bak}.")
    drop_all(eng)
    create_all(eng)


def table_counts(bind: Engine | None = None) -> dict[str, int]:
    """Row count per table, in pipeline order. Missing tables report -1."""
    eng = bind or engine()
    present = set(inspect(eng).get_table_names())
    counts: dict[str, int] = {}
    with session(eng) as current:
        for model in TABLES:
            name = model.__tablename__
            if name not in present:
                counts[name] = -1
                continue
            counts[name] = int(current.scalar(select(func.count()).select_from(model)) or 0)
    return counts
