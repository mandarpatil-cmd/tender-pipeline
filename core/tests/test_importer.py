"""Reading companies out of the BidEasy workbooks and stage 2's old outputs."""

from __future__ import annotations

import pytest

from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.importer import ImportError_, import_workbook, read_companies
from pipeline_core.models import STATUS_DONE, STATUS_PENDING, SOURCE_BIDEASY, Vendor
from pipeline_core.queries import mark_enriched, pending_vendors, upsert_vendor

pd = pytest.importorskip("pandas")
pytest.importorskip("openpyxl")


@pytest.fixture
def bind(tmp_path):
    eng = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(eng)
    return eng


def _write(path, frame, *, banner_rows: int = 0):
    """Write a sheet, optionally behind BidEasy's banner rows."""
    if not banner_rows:
        frame.to_excel(path, index=False)
        return path
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, startrow=banner_rows)
    return path


def test_reads_the_probable_bidders_layout(tmp_path):
    """Two banner rows, and the company lives in 'Probable Bidder'."""
    frame = pd.DataFrame(
        {
            "Tender Ref": ["T1", "T2"],
            "Work / Title": ["Fans", "Cables"],
            "Probable Bidder": ["Acme Infra Pvt Ltd", "Bravo Traders"],
        }
    )
    path = _write(tmp_path / "bidders.xlsx", frame, banner_rows=2)

    companies, rows_read, skipped = read_companies(path)

    assert {c.name_raw for c in companies} == {"Acme Infra Pvt Ltd", "Bravo Traders"}
    assert rows_read == 2
    assert skipped == 0


def test_reads_the_awardee_layout(tmp_path):
    frame = pd.DataFrame(
        {
            "Tender ref. No.": ["T1"],
            "Project Title": ["Roadworks"],
            "Corporate Name / Awardee": ["Charlie Constructions Ltd"],
        }
    )
    path = _write(tmp_path / "awardees.xlsx", frame)

    companies, _, _ = read_companies(path)

    assert [c.name_raw for c in companies] == ["Charlie Constructions Ltd"]


def test_deduplicates_and_keeps_the_first_contact_it_sees(tmp_path):
    """These sheets hold roughly twice as many rows as distinct companies."""
    frame = pd.DataFrame(
        {
            "probable_bidder": ["Acme Infra", "Acme  Infra.", "Acme Infra"],
            "email": [None, "hello@acme.example", "later@acme.example"],
            "phone": [None, None, "+91 22 1234"],
        }
    )
    path = _write(tmp_path / "dupes.xlsx", frame)

    companies, rows_read, _ = read_companies(path)

    assert rows_read == 3
    assert len(companies) == 1
    assert companies[0].email == "hello@acme.example"
    assert companies[0].phone == "+91 22 1234"


def test_unreadable_layout_is_reported_not_guessed(tmp_path):
    frame = pd.DataFrame({"something": ["else"], "entirely": ["different"]})
    path = _write(tmp_path / "wrong.xlsx", frame)

    with pytest.raises(ImportError_, match="no company column"):
        read_companies(path)


def test_import_marks_rows_with_an_email_as_done(tmp_path, bind):
    frame = pd.DataFrame(
        {
            "probable_bidder": ["Has Email Ltd", "No Email Ltd"],
            "email": ["hi@has.example", None],
        }
    )
    path = _write(tmp_path / "mixed.xlsx", frame)

    with session(bind) as s:
        report = import_workbook(s, path)

    assert report.inserted == 2
    assert report.with_email == 1

    with session(bind) as s:
        rows = {v.name_raw: v for v in s.query(Vendor).all()}
        assert rows["Has Email Ltd"].enrichment_status == STATUS_DONE
        assert rows["Has Email Ltd"].email == "hi@has.example"
        assert rows["Has Email Ltd"].source == SOURCE_BIDEASY
        assert rows["No Email Ltd"].enrichment_status == STATUS_PENDING
        # Only the one without an address is work for stage 2.
        assert [v.name_raw for v in pending_vendors(s)] == ["No Email Ltd"]


def test_import_never_overwrites_what_stage_2_already_found(tmp_path, bind):
    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Acme Infra")
        mark_enriched(s, vendor_id, email="researched@acme.example", phone=None)

    frame = pd.DataFrame(
        {"probable_bidder": ["Acme  Infra"], "email": ["spreadsheet@acme.example"]}
    )
    path = _write(tmp_path / "clash.xlsx", frame)

    with session(bind) as s:
        report = import_workbook(s, path)

    assert report.inserted == 0
    assert report.unchanged == 1
    with session(bind) as s:
        assert s.get(Vendor, vendor_id).email == "researched@acme.example"


def test_import_fills_in_a_vendor_that_has_no_email_yet(tmp_path, bind):
    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Acme Infra")

    frame = pd.DataFrame(
        {"probable_bidder": ["Acme Infra"], "email": ["spreadsheet@acme.example"]}
    )
    path = _write(tmp_path / "fill.xlsx", frame)

    with session(bind) as s:
        report = import_workbook(s, path)

    assert report.updated == 1
    with session(bind) as s:
        vendor = s.get(Vendor, vendor_id)
        assert vendor.email == "spreadsheet@acme.example"
        assert vendor.enrichment_status == STATUS_DONE


def test_dry_run_writes_nothing(tmp_path, bind):
    frame = pd.DataFrame({"probable_bidder": ["Acme Infra", "Bravo Traders"]})
    path = _write(tmp_path / "dry.xlsx", frame)

    with session(bind) as s:
        report = import_workbook(s, path, dry_run=True)
        s.rollback()

    assert report.inserted == 2
    with session(bind) as s:
        assert s.query(Vendor).count() == 0
