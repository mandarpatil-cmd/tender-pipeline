"""Data fixes that run once per database, in order, and are recorded.

``db.ensure_schema`` only ever adds tables, columns and indexes. Anything that
rewrites stored rows lives here instead, so a new release changes a client's
data once, right after a backup, and never again on a page load.

To change stored rows in a release, append an entry to ``MIGRATIONS`` and raise
``SCHEMA_VERSION``. Never edit, rename or reorder an entry that has shipped: a
database that already ran it will not run it again, and one that did not must
get exactly what the others got.

A rule change in ``emailcheck`` does not touch stored rows by itself. The mail
queue judges addresses with the current rule as it reads them. Add an entry
here only when stored rows must change too, such as ``vendor_contacts.valid``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import DataMigration

#: Written to ``PRAGMA user_version``. Raise it with every release that adds a
#: migration or a schema change older code must not open.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Migration:
    migration_id: str
    summary: str
    apply: Callable[[Session], int]


def _contacts_v1(current: Session) -> int:
    from .queries import backfill_vendor_contacts, clean_stored_contacts

    return clean_stored_contacts(current) + backfill_vendor_contacts(current)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        "2026-10-contacts-v1",
        "clean stored emails, re-judge each address, copy company contacts into vendor_contacts",
        _contacts_v1,
    ),
)


def pending(eng: Engine) -> list[Migration]:
    """Entries this database has not run, in order."""
    if "data_migrations" not in set(inspect(eng).get_table_names()):
        return list(MIGRATIONS)
    from .db import session

    with session(eng) as current:
        done = set(current.scalars(select(DataMigration.migration_id)))
    return [item for item in MIGRATIONS if item.migration_id not in done]


def apply_pending(eng: Engine) -> list[tuple[Migration, int]]:
    """Run each pending entry in its own transaction. Returns what ran and its row count.

    The record is written first, so the transaction holds the write lock before
    the fix reads anything. A second process that ran the same entry meanwhile
    makes the insert fail, and this one skips it.
    """
    from .db import session
    from .queries import utcnow

    ran: list[tuple[Migration, int]] = []
    for item in pending(eng):
        with session(eng) as current:
            record = DataMigration(migration_id=item.migration_id, applied_at=utcnow())
            current.add(record)
            try:
                current.flush()
            except IntegrityError:
                current.rollback()
                continue
            # A failure here rolls back the record too, so the entry runs again next time.
            record.rows_changed = int(item.apply(current) or 0)
            ran.append((item, record.rows_changed))
    return ran
