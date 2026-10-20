"""The schema, and the migration that adopts the scraper's existing database."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import inspect

from pipeline_core.db import drop_all, engine, ensure_schema, session, table_counts
from pipeline_core.models import TABLES, Tender, Vendor

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


def test_adding_origin_labels_old_tenders_once(tmp_path):
    """Old rows get a best-guess origin. The rest of the source stays unknown."""
    path = tmp_path / "aoc.sqlite3"
    _legacy_db(path)
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO tenders (tender_id, scraped_at) VALUES (?, '2026-10-09T00:00:00+00:00')",
        [("2026_SVNIT_919473_1",), ("MANUAL-AB12CD34",)],
    )
    conn.commit()
    conn.close()
    bind = engine(path)

    report = ensure_schema(bind)

    assert "tenders.origin" in report.added_columns
    assert "tenders.listing_serial" in report.added_columns
    with session(bind) as current:
        portal = current.get(Tender, "2026_SVNIT_919473_1")
        typed = current.get(Tender, "MANUAL-AB12CD34")
        assert portal.origin == "eprocure.gov.in"
        assert typed.origin == "manual"
        assert portal.search_mode is None
        assert portal.listing_page is None
        assert portal.scrape_job_id is None
        portal.origin = None

    ensure_schema(bind)
    with session(bind) as current:
        assert current.get(Tender, "2026_SVNIT_919473_1").origin is None


def test_adding_source_url_links_old_model_contacts_once(tmp_path):
    """Addresses saved before the source columns existed are linked to their answer."""
    from pipeline_core.db import dispose_engines
    from pipeline_core.models import VendorContact
    from pipeline_core.queries import record_llm_run, upsert_vendor

    path = tmp_path / "fresh.sqlite3"
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Traced Ltd")
        run_id = record_llm_run(
            current,
            vendor_id=vendor_id,
            model="test",
            prompt_version="contact-v1",
            input_json="{}",
            output_json=(
                '{"email": "found@co.example", "phone": null, '
                '"source": "https://co.example/about"}'
            ),
            status="ok",
        )
        current.add(
            VendorContact(
                vendor_id=vendor_id,
                channel="email",
                value="found@co.example",
                source="model",
                valid=1,
            )
        )
    dispose_engines()
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE vendor_contacts DROP COLUMN source_url")
    connection.execute("ALTER TABLE vendor_contacts DROP COLUMN llm_run_id")
    connection.execute("ALTER TABLE llm_runs DROP COLUMN job_id")
    connection.commit()
    connection.close()

    bind = engine(path)
    report = ensure_schema(bind)

    assert "vendor_contacts.source_url" in report.added_columns
    assert "llm_runs.job_id" in report.added_columns
    with session(bind) as current:
        row = current.query(VendorContact).one()
        assert row.source_url == "https://co.example/about"
        assert row.llm_run_id == run_id
        row.llm_run_id = None
        row.source_url = None

    assert "vendor_contacts.source_url" not in ensure_schema(bind).added_columns
    with session(bind) as current:
        assert current.query(VendorContact).one().source_url is None


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


def test_adopting_job_tenders_and_outreach_runs_backfills_them_once(tmp_path):
    """A database from before run history: PDF downloads stamped as scrape runs."""
    import json

    from pipeline_core.models import JobTender, Outreach, UiJob
    from pipeline_core.queries import record_outreach, replace_awards, upsert_vendor

    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    with session(bind) as current:
        for job_id, stage, params, started, finished in (
            (9, "scrape", {}, "2026-10-01T00:00:00", "2026-10-01T01:00:00"),
            (14, "pdfs", {"tender_id": "T-SCRAPED"}, "2026-10-02T00:00:00", "2026-10-02T00:10:00"),
            (29, "pdfs", {"tender_id": "T-TERMINAL"}, "2026-10-03T00:00:00", "2026-10-03T00:10:00"),
            (43, "outreach", {"awards": ["1:T-SCRAPED"], "send": True},
             "2026-10-04T00:00:00", "2026-10-04T00:10:00"),
            (44, "outreach", {"awards": ["1:T-SCRAPED"], "send": False},
             "2026-10-05T00:00:00", "2026-10-05T00:10:00"),
        ):
            current.add(UiJob(job_id=job_id, stage=stage, state="done", params_json=json.dumps(params),
                              log="", started_at=started, finished_at=finished))
        vendor_id = upsert_vendor(current, name_raw="Roads Ltd")
        for tender_id, job_id in (("T-SCRAPED", 9), ("T-TERMINAL", 29)):
            current.add(Tender(tender_id=tender_id, scraped_at="2026-10-01", scrape_job_id=job_id))
            current.flush()
            replace_awards(current, tender_id, [{"bid_number": "1", "vendor_id": vendor_id,
                                                  "bidder_name": "Roads Ltd"}])
        inside = record_outreach(current, vendor_id=vendor_id, tender_id="T-SCRAPED",
                                 email="a@roads.example", subject="s", transport="gmail", status="sent")
        outside = record_outreach(current, vendor_id=vendor_id, tender_id="T-SCRAPED",
                                  email="b@roads.example", subject="s", transport="gmail", status="sent")
        current.get(Outreach, inside).sent_at = "2026-10-04T00:05:00"
        current.get(Outreach, outside).sent_at = "2026-10-06T00:00:00"
    with bind.begin() as conn:
        conn.exec_driver_sql("DROP TABLE job_tenders")
        conn.exec_driver_sql("DROP INDEX idx_outreach_job")
        conn.exec_driver_sql("ALTER TABLE outreach DROP COLUMN job_id")

    report = ensure_schema(bind)
    assert "job_tenders" in report.created_tables
    assert "outreach.job_id" in report.added_columns
    with session(bind) as current:
        runs = {(row.job_id, row.tender_id) for row in current.query(JobTender)}
        assert runs == {(9, "T-SCRAPED"), (14, "T-SCRAPED"), (29, "T-TERMINAL")}
        assert current.get(Tender, "T-SCRAPED").scrape_job_id == 9
        # Job 29 was a PDF download, not the scrape that saved the tender.
        assert current.get(Tender, "T-TERMINAL").scrape_job_id is None
        assert current.get(Outreach, inside).job_id == 43
        assert current.get(Outreach, outside).job_id is None

    again = ensure_schema(bind)
    assert not again.changed
    with session(bind) as current:
        assert current.query(JobTender).count() == 3
