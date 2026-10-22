"""Putting an old copy back into a database that kept working after it was emptied."""

from __future__ import annotations

import json
import sqlite3

import pytest
from sqlalchemy import text

from pipeline_core.cli import main
from pipeline_core.db import dispose_engines, engine, ensure_schema, session
from pipeline_core.merge import merge_backup
from pipeline_core.models import MailLetter, Outreach, Tender, UiJob, Vendor, VendorContact
from pipeline_core.queries import mail_messages, replace_awards, upsert_tender, upsert_vendor

#: The 7 Oct release (97cae34): text amounts, no run ids on model calls or mail.
OLD_COPY = """
CREATE TABLE tenders (
    tender_id TEXT PRIMARY KEY, title TEXT, organisation TEXT, status TEXT,
    contract_date TEXT, contract_value TEXT, json_path TEXT, scraped_at TEXT NOT NULL
);
CREATE TABLE vendors (
    vendor_id INTEGER PRIMARY KEY AUTOINCREMENT, name_raw TEXT NOT NULL,
    name_norm TEXT NOT NULL UNIQUE, legal_form TEXT, city TEXT, state TEXT,
    buyer_hint TEXT, source TEXT, pdf_email TEXT, pdf_phone TEXT, email TEXT,
    phone TEXT, enrichment_status TEXT NOT NULL DEFAULT 'pending', enriched_at TEXT,
    contact_origin TEXT
);
CREATE TABLE awards (
    tender_id TEXT NOT NULL REFERENCES tenders(tender_id), bid_number TEXT NOT NULL DEFAULT '',
    vendor_id INTEGER NOT NULL REFERENCES vendors(vendor_id), bidder_name TEXT NOT NULL,
    rank TEXT, quoted_value TEXT, awarded_value TEXT, awarded_currency TEXT,
    contract_date TEXT, contract_value TEXT, work_title TEXT,
    PRIMARY KEY (tender_id, bid_number)
);
CREATE TABLE tender_documents (
    tender_id TEXT NOT NULL REFERENCES tenders(tender_id), filename TEXT NOT NULL,
    emails TEXT, phones TEXT, gstins TEXT, PRIMARY KEY (tender_id, filename)
);
CREATE TABLE llm_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id INTEGER NOT NULL REFERENCES vendors(vendor_id),
    model TEXT, prompt_version TEXT, input_json TEXT, output_json TEXT,
    status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL
);
CREATE TABLE outreach (
    outreach_id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL REFERENCES vendors(vendor_id),
    email TEXT, subject TEXT, transport TEXT, status TEXT, error TEXT, sent_at TEXT,
    tender_id TEXT REFERENCES tenders(tender_id)
);
CREATE TABLE mail_letter (
    letter_id INTEGER PRIMARY KEY, subject TEXT NOT NULL, body TEXT NOT NULL,
    sender_name TEXT NOT NULL, sender_designation TEXT NOT NULL, sender_org TEXT NOT NULL,
    sender_mobile TEXT NOT NULL, sender_email TEXT NOT NULL, attachment_names TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE ui_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL, state TEXT NOT NULL,
    operator TEXT NOT NULL, params_json TEXT, log TEXT NOT NULL, exit_code INTEGER,
    started_at TEXT NOT NULL, finished_at TEXT
);
INSERT INTO tenders VALUES
    ('T-1', 'Cable', 'NIT', 'AOC', '21-Sep-2026', 'INR 179,853.24', NULL, '2026-09-30T00:00:00+00:00'),
    ('T-OLD', 'Fans', 'NIT', 'AOC', '01-Sep-2026', 'INR 1,000', NULL, '2026-09-30T00:00:00+00:00');
INSERT INTO vendors (vendor_id, name_raw, name_norm, email, enrichment_status, enriched_at, contact_origin)
    VALUES (1, 'Kept Ltd', 'KEPT LTD', 'hi@kept.example', 'done', '2026-10-01', 'model'),
           (2, 'Old Only Ltd', 'OLD ONLY LTD', 'only@old.example', 'done', '2026-10-01', 'typed');
INSERT INTO awards VALUES
    ('T-1', '77', 1, 'Kept Ltd', 'L1', '179853.24', '179,853.24', NULL, NULL, NULL, 'Cable'),
    ('T-OLD', '80', 2, 'Old Only Ltd', NULL, NULL, '1,000', NULL, NULL, NULL, 'Fans');
INSERT INTO tender_documents VALUES ('T-1', 'LOA.pdf', 'hi@kept.example', '', '');
INSERT INTO llm_runs (vendor_id, model, status, output_json, created_at)
    VALUES (1, 'm', 'ok', '{"email": "hi@kept.example"}', '2026-10-01T00:00:00'),
           (2, 'm', 'ok', '{"email": "only@old.example"}', '2026-10-01T00:01:00');
INSERT INTO ui_jobs (job_id, stage, state, operator, params_json, log, started_at, finished_at)
    VALUES (1, 'scrape', 'done', 'local', '{}', 'old scrape', '2026-09-30T00:00:00', '2026-09-30T01:00:00'),
           (2, 'outreach', 'done', 'local', '{"awards": ["1:T-1"], "send": true}', 'sent',
            '2026-10-02T00:00:00', '2026-10-02T01:00:00');
INSERT INTO outreach (vendor_id, email, subject, transport, status, sent_at, tender_id)
    VALUES (1, 'hi@kept.example', 'Hello', 'gmail', 'sent', '2026-10-02T00:30:00', 'T-1');
INSERT INTO mail_letter VALUES (1, 'Old subject', 'Old body', 'Mandar', 'Director', 'Org',
    '9000000000', 'me@org.example', 'pitch.pdf', '2026-10-01');
"""


def _old_copy(path):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_COPY)
    conn.commit()
    conn.close()


def _worked_after_the_wipe(path) -> None:
    """The emptied database, used today: ids start again from 1."""
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        current.add(UiJob(job_id=1, stage="scrape", state="done", params_json="{}",
                          log="today", started_at="2026-10-20T10:00:00"))
        new_today = upsert_vendor(current, name_raw="New Today Ltd")
        kept = upsert_vendor(current, name_raw="Kept Ltd")
        assert (new_today, kept) == (1, 2)
        upsert_tender(current, tender_id="T-1", title="Cable (rescraped)", scraped_at="2026-10-20T10:05:00",
                      scrape_job_id=1)
        upsert_tender(current, tender_id="T-NEW", title="Pumps", scraped_at="2026-10-20T10:06:00",
                      scrape_job_id=1)
        replace_awards(current, "T-1", [{"bid_number": "77", "vendor_id": kept, "bidder_name": "Kept Ltd"}])
        replace_awards(current, "T-NEW", [{"bid_number": "90", "vendor_id": new_today,
                                           "bidder_name": "New Today Ltd"}])
        current.add(MailLetter(letter_id=1, subject="Seeded", body="Seeded", updated_at="2026-10-20"))
    dispose_engines()


def _counts(path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {
            name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()


def test_merge_brings_the_old_copy_back_and_keeps_todays_work(tmp_path):
    old = tmp_path / "pipeline.sqlite3.bak"
    live = tmp_path / "pipeline.sqlite3"
    _old_copy(old)
    _worked_after_the_wipe(live)
    untouched = old.read_bytes()

    report = merge_backup(live, old)

    assert old.read_bytes() == untouched
    assert report.added["vendors"] == 1
    assert report.added["tenders"] == 1
    assert report.added["ui_jobs"] == 2
    assert report.added["outreach"] == 1
    assert report.filled["vendors"] == 1
    with session(engine(live)) as current:
        vendors = {row.name_raw: row for row in current.query(Vendor)}
        assert set(vendors) == {"New Today Ltd", "Kept Ltd", "Old Only Ltd"}
        kept = vendors["Kept Ltd"]
        # Still pending today, so it takes the old lookup's answer whole.
        assert (kept.vendor_id, kept.email, kept.enrichment_status, kept.contact_origin) == (
            2, "hi@kept.example", "done", "model"
        )
        old_only = vendors["Old Only Ltd"].vendor_id

        t1 = current.get(Tender, "T-1")
        assert (t1.title, t1.scrape_job_id) == ("Cable (rescraped)", 1)
        assert current.get(Tender, "T-OLD").contract_value == 100000
        awards = {
            (award_tender, bid): vendor_id
            for award_tender, bid, vendor_id in current.execute(
                text("SELECT tender_id, bid_number, vendor_id FROM awards")
            )
        }
        assert awards == {("T-1", "77"): 2, ("T-NEW", "90"): 1, ("T-OLD", "80"): old_only}

        jobs = {row.log: row for row in current.query(UiJob)}
        assert jobs["today"].job_id == 1
        assert jobs["old scrape"].job_id != 1
        mail_job = jobs["sent"]
        assert json.loads(mail_job.params_json)["awards"] == ["2:T-1"]
        attempt = current.query(Outreach).one()
        assert (attempt.vendor_id, attempt.tender_id, attempt.job_id) == (2, "T-1", mail_job.job_id)

        assert current.get(MailLetter, 1).subject == "Old subject"
        assert current.query(VendorContact).filter(VendorContact.vendor_id == old_only).count() >= 1
        # The old send came back, so Kept Ltd is not mailed again for T-1.
        assert all(m.vendor_id != 2 for m in mail_messages(current))
    assert report.letter == "backup"
    conn = sqlite3.connect(live)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()


def test_merging_twice_adds_nothing_the_second_time(tmp_path):
    old = tmp_path / "pipeline.sqlite3.bak"
    live = tmp_path / "pipeline.sqlite3"
    _old_copy(old)
    _worked_after_the_wipe(live)

    merge_backup(live, old)
    after_first = _counts(live)
    again = merge_backup(live, old)

    assert again.added == {}
    assert _counts(live) == after_first


def test_the_live_database_is_copied_aside_before_the_merge(tmp_path):
    old = tmp_path / "pipeline.sqlite3.bak"
    live = tmp_path / "pipeline.sqlite3"
    _old_copy(old)
    _worked_after_the_wipe(live)

    report = merge_backup(live, old, letter="current")

    copy = sqlite3.connect(report.backup)
    assert copy.execute("SELECT name_raw FROM vendors ORDER BY vendor_id").fetchall() == [
        ("New Today Ltd",),
        ("Kept Ltd",),
    ]
    copy.close()
    with session(engine(live)) as current:
        assert current.get(MailLetter, 1).subject == "Seeded"


def test_a_running_job_stops_the_merge_before_anything_is_written(tmp_path):
    old = tmp_path / "pipeline.sqlite3.bak"
    live = tmp_path / "pipeline.sqlite3"
    _old_copy(old)
    _worked_after_the_wipe(live)
    with session(engine(live)) as current:
        current.add(UiJob(stage="enrich", state="running", params_json="{}", log="",
                          started_at="2026-10-20T11:00:00"))
    dispose_engines()
    before = _counts(live)

    with pytest.raises(RuntimeError, match="still going"):
        merge_backup(live, old)
    assert _counts(live) == before


def test_the_merge_command_prints_what_it_added(tmp_path, capsys):
    old = tmp_path / "pipeline.sqlite3.bak"
    live = tmp_path / "pipeline.sqlite3"
    _old_copy(old)
    _worked_after_the_wipe(live)

    assert main(["--db", str(live), "merge", str(old)]) == 0
    printed = capsys.readouterr().out
    assert "rows added from the old copy:" in printed
    assert "vendors" in printed
    assert "mail letter: the old copy one" in printed
    assert main(["--db", str(tmp_path / "missing.sqlite3"), "merge", str(old)]) == 1
