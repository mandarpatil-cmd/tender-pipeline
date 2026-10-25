"""Engine, session, and the only place the schema is created or changed.

Stages run one after another in practice, so WAL and `busy_timeout` are
belt-and-braces rather than a concurrency design.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, select, func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import settings
from .models import TABLES, Base

#: Seconds a blocked writer waits before raising "database is locked".
BUSY_TIMEOUT_MS = 5000

#: Copies kept per label in ``backups/``. Older ones with that label are deleted.
BACKUP_KEEP = 10

#: One schema change at a time in this process. Two requests may arrive together.
_SCHEMA_LOCK = threading.Lock()

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
        from .emailcheck import is_buyer_email, is_email
        from .loose import loose_date, loose_number

        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            if foreign_keys:
                cursor.execute("PRAGMA foreign_keys=ON")
            dbapi_connection.create_function("loose_date", 1, loose_date)
            dbapi_connection.create_function("loose_number", 1, loose_number)
            # The mail queue's SQL and its Python must judge an address the same way.
            dbapi_connection.create_function(
                "pc_is_email", 1, lambda value: int(is_email(value)), deterministic=True
            )
            dbapi_connection.create_function(
                "pc_is_buyer_email",
                1,
                lambda value: int(is_buyer_email(value)),
                deterministic=True,
            )
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
    #: Tables rebuilt to turn text amounts into hundredths: ``tenders 915 rows``.
    converted: list[str] = field(default_factory=list)
    #: Data fixes from ``migrations`` that ran: ``2026-10-contacts-v1 (12 rows)``.
    migrated: list[str] = field(default_factory=list)
    #: The checked copy written before anything changed. Empty when nothing did.
    backup: str = ""

    @property
    def changed(self) -> bool:
        return bool(
            self.created_tables
            or self.added_columns
            or self.created_indexes
            or self.converted
            or self.migrated
        )


class SchemaTooNew(RuntimeError):
    """The file was last brought up to date by a newer release than this code."""


def create_all(bind: Engine | None = None) -> None:
    Base.metadata.create_all(bind or engine())


def drop_all(bind: Engine | None = None) -> None:
    Base.metadata.drop_all(bind or engine())


def ensure_schema(bind: Engine | None = None) -> SchemaReport:
    """Bring a database up to the models, old or brand new. Safe to call on every page.

    When the file is already up to date this only reads. Otherwise a checked
    copy goes to ``backups/`` first, then:

    - missing tables, nullable columns and indexes are added. `create_all` only
      creates *missing tables*, so columns and indexes on existing ones are
      added here. A new NOT NULL column needs a backfill and is refused;
    - a file that still holds text amounts (``INR 179,853.24``) has ``tenders``
      and ``awards`` rebuilt with every row kept and each amount converted;
    - each data fix in ``migrations.MIGRATIONS`` that this file has not run
      runs once and is recorded.

    The schema step itself never rewrites stored rows. A database that a newer
    release has already updated raises `SchemaTooNew` and is not touched.
    """
    eng = bind or engine()
    with _SCHEMA_LOCK:
        return _ensure_schema(eng)


def _ensure_schema(eng: Engine) -> SchemaReport:
    from .migrations import SCHEMA_VERSION, apply_pending, pending

    report = SchemaReport()
    stored = _user_version(eng)
    if stored > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"This database was updated by a newer version of the program "
            f"(version {stored}; this one knows up to {SCHEMA_VERSION}). "
            "Run update.bat to get the latest code. Nothing was changed."
        )

    existing = set(inspect(eng).get_table_names())
    text_amounts = bool(existing) and _has_text_amounts(eng)
    if existing and (text_amounts or _schema_behind(eng) or pending(eng)):
        path = _sqlite_path(eng)
        if path is not None and path.is_file():
            report.backup = str(rolling_backup(path, "auto"))
    if text_amounts:
        report.backup, report.converted = _convert_text_amounts(eng, report.backup)

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

    from .queries import (
        backfill_job_tenders,
        backfill_model_contact_sources,
        backfill_outreach_jobs,
        backfill_tender_origin,
    )

    # Each of these fills a column or table the step above just added, once.
    with session(eng) as current:
        if "tenders.origin" in report.added_columns or report.converted:
            backfill_tender_origin(current)
        if "job_tenders" in report.created_tables:
            backfill_job_tenders(current)
        if "outreach.job_id" in report.added_columns:
            backfill_outreach_jobs(current)

    report.migrated = [
        f"{item.migration_id} ({count} rows)" for item, count in apply_pending(eng)
    ]
    # Needs the rows the contacts migration copies in, so it runs after it.
    if (
        "vendor_contacts.source_url" in report.added_columns
        or "vendor_contacts" in report.created_tables
    ):
        with session(eng) as current:
            backfill_model_contact_sources(current)

    if stored < SCHEMA_VERSION:
        with eng.begin() as conn:
            conn.exec_driver_sql(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
    return report


def _user_version(eng: Engine) -> int:
    with eng.connect() as conn:
        return int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)


def _schema_behind(eng: Engine) -> bool:
    """True when a table, column or index the models declare is missing."""
    inspector = inspect(eng)
    names = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in names:
            return True
        columns = {col["name"] for col in inspector.get_columns(table.name)}
        if any(column.name not in columns for column in table.columns):
            return True
        indexes = {idx["name"] for idx in inspector.get_indexes(table.name)}
        if any(index.name not in indexes for index in table.indexes):
            return True
    return False


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


def backup_copy(path: Path, label: str = "bak", *, folder: Path | None = None) -> Path:
    """A consistent, checked copy of a database: ``<folder>/<name>.<label>-<time>``.

    ``folder`` defaults to the database's own directory. SQLite's own backup
    reads the write-ahead log too, so the copy holds every committed row even
    while the file has a ``-wal`` beside it. A copy never overwrites an earlier
    one, and one that fails ``PRAGMA integrity_check`` raises.
    """
    path = Path(path)
    base = Path(folder) if folder is not None else path.parent
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{path.name}.{label}-{stamp}"
    # Above every serial already used this second, so a copy is never named as
    # older than one taken before it, even after retention removed the first.
    same_second = re.compile(rf"^{re.escape(name)}(?:-(\d+))?$")
    taken = [
        int(found.group(1) or 1)
        for found in (same_second.match(item.name) for item in base.iterdir())
        if found
    ]
    target = base / (f"{name}-{max(taken) + 1}" if taken else name)
    source = sqlite3.connect(path)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
        verdict = copy.execute("PRAGMA integrity_check").fetchone()
    finally:
        copy.close()
        source.close()
    if not target.is_file():
        raise RuntimeError(f"Could not copy {path} to {target}.")
    if verdict != ("ok",):
        raise RuntimeError(
            f"The copy {target} failed SQLite's integrity check "
            f"({verdict[0] if verdict else 'no answer'}). The database was not changed."
        )
    return target


def backup_dir(path: Path) -> Path:
    """``backups/`` beside the database. Gitignored with the rest of ``data/``."""
    return Path(path).parent / "backups"


def rolling_backup(path: Path, label: str) -> Path:
    """A checked copy in `backup_dir`. Keeps the newest `BACKUP_KEEP` with this label."""
    path = Path(path)
    target = backup_copy(path, label, folder=backup_dir(path))
    named = re.compile(rf"^{re.escape(path.name)}\.{re.escape(label)}-(\d{{8}}-\d{{6}})(?:-(\d+))?$")
    copies = []
    for item in backup_dir(path).iterdir():
        found = named.match(item.name)
        if found:
            # Oldest first: by time, then by the serial a same-second copy got.
            copies.append(((found.group(1), int(found.group(2) or 1)), item))
    copies.sort()
    for _key, old in copies[:-BACKUP_KEEP]:
        for item in (old, old.with_name(old.name + "-wal"), old.with_name(old.name + "-shm")):
            item.unlink(missing_ok=True)
    return target


def _convert_text_amounts(eng: Engine, backup: str = "") -> tuple[str, list[str]]:
    """Rebuild ``tenders`` and ``awards`` with amounts as hundredths. Every row is kept.

    ``backup`` is a copy already taken; without one, one is written first.
    Returns the backup and one line per rebuilt table. A date
    the reader cannot parse stays as written; an amount it cannot parse is
    left empty and counted, since an integer column cannot hold the text.
    """
    from sqlalchemy.schema import CreateIndex, CreateTable

    from .models import Award, Tender

    path = _sqlite_path(eng)
    if path is None:
        drop_all(eng)
        create_all(eng)
        return "", []
    if not backup:
        backup = str(backup_copy(path))
    eng.dispose()
    con = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    con.row_factory = sqlite3.Row
    done: list[str] = []
    try:
        # Child tables keep naming "tenders"; the rebuilt table takes that name back.
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("PRAGMA legacy_alter_table=ON")
        con.execute("BEGIN IMMEDIATE")
        present = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, convert in ((Tender.__table__, _tender_row), (Award.__table__, _award_row)):
            if table.name not in present:
                continue
            rows = [dict(row) for row in con.execute(f'SELECT * FROM "{table.name}"')]
            for (index,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (table.name,),
            ).fetchall():
                con.execute(f'DROP INDEX "{index}"')
            old = f"_old_{table.name}"
            con.execute(f'ALTER TABLE "{table.name}" RENAME TO "{old}"')
            con.execute(str(CreateTable(table).compile(eng)))
            for index in table.indexes:
                con.execute(str(CreateIndex(index).compile(eng)))
            names = [column.name for column in table.columns]
            unread = 0
            for row in rows:
                values, missed = convert(row)
                unread += missed
                kept = [name for name in names if name in values]
                con.execute(
                    f'INSERT INTO "{table.name}" ({", ".join(kept)}) '
                    f'VALUES ({", ".join("?" for _ in kept)})',
                    [values[name] for name in kept],
                )
            con.execute(f'DROP TABLE "{old}"')
            note = f"{table.name} {len(rows)} rows"
            if unread:
                note += f", {unread} amount(s) unreadable and left empty"
            done.append(note)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.execute("PRAGMA legacy_alter_table=OFF")
        con.execute("PRAGMA foreign_keys=ON")
        con.close()
    return backup, done


def _hundredths(value: object) -> tuple[int | None, str | None, int]:
    """``(hundredths, currency, unreadable)`` for one stored amount, text or not."""
    from .money import parse_money

    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None, 0
    if isinstance(value, int):
        return value, None, 0
    amount, code = parse_money(str(value))
    return amount, code, 0 if amount is not None else 1


def _tender_row(row: dict) -> tuple[dict, int]:
    from .money import iso_day

    values = dict(row)
    raw_day = row.get("contract_date")
    values["contract_date"] = iso_day(raw_day) or (raw_day or None)
    amount, code, unread = _hundredths(row.get("contract_value"))
    explicit = str(row.get("contract_currency") or "").strip() or None
    values["contract_value"] = amount
    values["contract_currency"] = explicit or code or ("INR" if amount is not None else None)
    return values, unread


def _award_row(row: dict) -> tuple[dict, int]:
    values = dict(row)
    values["bid_number"] = row.get("bid_number") or ""
    quoted, _quoted_code, missed_quote = _hundredths(row.get("quoted_value"))
    awarded, code, missed_award = _hundredths(row.get("awarded_value"))
    explicit = str(row.get("awarded_currency") or "").strip() or None
    values["quoted_value"] = quoted
    values["awarded_value"] = awarded
    values["awarded_currency"] = explicit or code
    return values, missed_quote + missed_award


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
