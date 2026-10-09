"""The schema, and the migration that adopts the scraper's existing database."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import inspect

from pipeline_core.db import drop_all, engine, ensure_schema, session, table_counts
from pipeline_core.models import TABLES, Vendor

# The scraper's hand-rolled DDL, exactly as it shipped: five reserved columns
# that nothing ever wrote, no `source`, and no `outreach` table.
LEGACY_VENDORS = """
CREATE TABLE vendors (
    vendor_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name_raw TEXT NOT NULL,
    name_norm TEXT NOT NULL UNIQUE,
    legal_form TEXT,
    city TEXT,
    state TEXT,
    buyer_hint TEXT,
    cin TEXT,
    gstin TEXT,
    domain TEXT,
    website TEXT,
    email TEXT,
    phone TEXT,
    directors_json TEXT,
    enrichment_status TEXT NOT NULL DEFAULT 'pending',
    enriched_at TEXT
)
"""
LEGACY_TENDERS = """
CREATE TABLE tenders (
    tender_id TEXT PRIMARY KEY,
    title TEXT,
    organisation TEXT,
    status TEXT,
    contract_date TEXT,
    contract_value TEXT,
    json_path TEXT,
    scraped_at TEXT NOT NULL
)
"""


def _legacy_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_TENDERS)
    conn.execute(LEGACY_VENDORS)
    conn.execute(
        "INSERT INTO vendors (name_raw, name_norm, enrichment_status) "
        "VALUES ('Kanta enterprises', 'KANTA ENTERPRISES', 'pending')"
    )
    conn.commit()
    conn.close()


def test_ensure_schema_creates_every_table(tmp_path):
    bind = engine(tmp_path / "fresh.sqlite3")
    report = ensure_schema(bind)

    assert set(report.created_tables) == {model.__tablename__ for model in TABLES}
    assert set(inspect(bind).get_table_names()) >= {m.__tablename__ for m in TABLES}
    assert all(count == 0 for count in table_counts(bind).values())


def test_ensure_schema_is_idempotent(tmp_path):
    bind = engine(tmp_path / "fresh.sqlite3")
    ensure_schema(bind)
    second = ensure_schema(bind)

    assert not second.changed
    assert second.created_tables == []
    assert second.added_columns == []


def test_ensure_schema_migrates_the_legacy_database(tmp_path):
    """create_all alone cannot do this — it never alters an existing table."""
    path = tmp_path / "aoc.sqlite3"
    _legacy_db(path)
    bind = engine(path)

    report = ensure_schema(bind)

    assert "outreach" in report.created_tables
    assert "vendors.source" in report.added_columns

    columns = {col["name"] for col in inspect(bind).get_columns("vendors")}
    assert "source" in columns
    # The reserved columns are left alone: dropped from the models, not the file.
    assert {"cin", "gstin", "directors_json"} <= columns

    # And the existing row survived untouched.
    with session(bind) as current:
        vendor = current.query(Vendor).one()
        assert vendor.name_raw == "Kanta enterprises"
        assert vendor.enrichment_status == "pending"
        assert vendor.source is None


def test_ensure_schema_creates_missing_indexes_on_existing_tables(tmp_path):
    path = tmp_path / "aoc.sqlite3"
    _legacy_db(path)
    bind = engine(path)

    ensure_schema(bind)

    names = {idx["name"] for idx in inspect(bind).get_indexes("vendors")}
    assert "idx_vendors_status" in names


def test_reset_empties_everything(tmp_path):
    bind = engine(tmp_path / "fresh.sqlite3")
    ensure_schema(bind)
    with session(bind) as current:
        current.add(Vendor(name_raw="A", name_norm="A"))

    assert table_counts(bind)["vendors"] == 1

    drop_all(bind)
    ensure_schema(bind)
    assert table_counts(bind)["vendors"] == 0


def test_wal_and_foreign_keys_are_on(tmp_path):
    bind = engine(tmp_path / "fresh.sqlite3")
    ensure_schema(bind)
    with bind.connect() as conn:
        journal = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        fks = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert str(journal).lower() == "wal"
    assert int(fks) == 1


def test_a_new_database_stores_amounts_as_integers(tmp_path):
    bind = engine(tmp_path / "fresh.sqlite3")
    ensure_schema(bind)

    tenders = {
        col["name"]: str(col["type"]).upper()
        for col in inspect(bind).get_columns("tenders")
    }
    awards = {
        col["name"]: str(col["type"]).upper()
        for col in inspect(bind).get_columns("awards")
    }
    assert "INT" in tenders["contract_value"]
    assert "contract_currency" in tenders
    assert "INT" in awards["quoted_value"]
    assert "INT" in awards["awarded_value"]
    assert "contract_date" not in awards
    assert "contract_value" not in awards


def test_text_amounts_are_copied_aside_and_the_file_starts_empty(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tenders ("
        "tender_id TEXT PRIMARY KEY, contract_value TEXT, scraped_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tenders (tender_id, contract_value, scraped_at) "
        "VALUES ('T-1', 'INR 10', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "CREATE TABLE vendors ("
        "vendor_id INTEGER PRIMARY KEY, name_raw TEXT NOT NULL, "
        "name_norm TEXT NOT NULL UNIQUE, "
        "enrichment_status TEXT NOT NULL DEFAULT 'pending')"
    )
    conn.execute("INSERT INTO vendors (name_raw, name_norm) VALUES ('Kept Ltd', 'KEPT LTD')")
    conn.commit()
    conn.close()

    bind = engine(path)
    ensure_schema(bind)

    bak = path.with_name(path.name + ".bak")
    assert bak.exists()
    old = sqlite3.connect(bak)
    assert old.execute("SELECT contract_value FROM tenders").fetchone()[0] == "INR 10"
    assert old.execute("SELECT name_raw FROM vendors").fetchone()[0] == "Kept Ltd"
    old.close()

    awards = {col["name"] for col in inspect(bind).get_columns("awards")}
    assert "contract_date" not in awards
    assert "contract_value" not in awards
    assert table_counts(bind)["tenders"] == 0
    assert table_counts(bind)["vendors"] == 0

    again = ensure_schema(bind)
    assert not again.changed
    assert table_counts(bind)["vendors"] == 0


def test_adding_a_not_null_column_is_refused(tmp_path, monkeypatch):
    """A NOT NULL column needs a backfill, so it must not be half-applied."""
    from sqlalchemy import Column, Text

    path = tmp_path / "fresh.sqlite3"
    bind = engine(path)
    ensure_schema(bind)

    table = Vendor.__table__
    extra = Column("mandatory_thing", Text, nullable=False)
    table.append_column(extra)
    try:
        with pytest.raises(RuntimeError, match="NOT NULL"):
            ensure_schema(bind)
    finally:
        table._columns.remove(extra)
