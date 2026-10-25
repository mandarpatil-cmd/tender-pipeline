import json
from pathlib import Path

from pipeline_core.db import session
from pipeline_core.models import Award, Tender, Vendor

from stage1_scrape.domain.models import BidRow, ListingRow, ScrapeSource, TenderRecord
from stage1_scrape.persist.store import (
    Store,
    backfill_blank_status,
    listing_status,
    serial_number,
)


def test_store_roundtrip(tmp_path):
    store = Store(tmp_path)
    listing = ListingRow(
        serial="1",
        tender_id="2026_ORG_1",
        title_and_ref="Cable / REF",
        organisation_chain="Org",
        tender_stage="AOC",
        status="AOC",
        status_page_url="https://eprocure.gov.in/eprocure/app?sp=S",
    )
    record = TenderRecord(listing=listing, aoc={"Contract Date": "20-Jul-2026"})
    store.save_tender(record)
    assert "2026_ORG_1" in store.known_ids()
    assert not (tmp_path / "json").exists()


def test_store_writes_vendor_and_award(tmp_path):
    import sqlite3

    store = Store(tmp_path)
    listing = ListingRow(
        serial="1",
        tender_id="2026_WBNPI_911698_1",
        title_and_ref="Fans / REF",
        organisation_chain=(
            "National Project Implementation Unit - World Bank Tenders"
            "||Visvesvaraya National Institute of Technology Nagpur"
        ),
        tender_stage="AOC",
        status="",
        status_page_url="https://eprocure.gov.in/eprocure/app?sp=S",
    )
    awarded = BidRow(
        serial="1",
        bid_number="3432381",
        bidder_name="Kanta enterprises",
        submitted_date="",
        status="",
        remarks="",
        status_updated_on="",
        value="59,935",
        rank=None,
        currency="INR",
    )
    financial = BidRow(
        serial="2",
        bid_number="3432381",
        bidder_name="Kanta enterprises",
        submitted_date="",
        status="",
        remarks="",
        status_updated_on="",
        value="59934.56",
        rank="L1",
    )
    record = TenderRecord(
        listing=listing,
        financial_bids=[financial],
        awarded_bids=[awarded],
        aoc={"Contract Date": "30-Jul-2026", "Total Contract Value": "INR 59,935"},
    )
    store.save_tender(record)
    with sqlite3.connect(store.db_path) as conn:
        vendor = conn.execute(
            "SELECT name_raw, legal_form, city, state, enrichment_status FROM vendors"
        ).fetchone()
        award = conn.execute(
            "SELECT bidder_name, rank, quoted_value, awarded_value, awarded_currency FROM awards"
        ).fetchone()
        status = conn.execute("SELECT status FROM tenders").fetchone()
    assert vendor == ("Kanta enterprises", "trade", "Nagpur", "Maharashtra", "pending")
    assert award == ("Kanta enterprises", "L1", 5993456, 5993500, "INR")
    assert status == ("AOC",)


def test_blank_status_is_filled_from_saved_json(tmp_path):
    import json
    import sqlite3

    store = Store(tmp_path)
    listing = ListingRow(
        serial="1",
        tender_id="2026_ORG_2",
        title_and_ref="Cable / REF",
        organisation_chain="Org",
        tender_stage="AOC",
        status="",
        status_page_url="https://eprocure.gov.in/eprocure/app?sp=S",
    )
    store.save_tender(TenderRecord(listing=listing))
    document = tmp_path / "json" / "2026_ORG_2.json"
    document.parent.mkdir()
    document.write_text(
        json.dumps({"listing": {"status": "", "tender_stage": "AOC"}}),
        encoding="utf-8",
    )
    relative_path = Path("json") / document.name
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE tenders SET status = '', json_path = ?", (str(relative_path),))
        conn.commit()

    assert backfill_blank_status(store.db_path, tmp_path) == 1
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT status FROM tenders").fetchone() == ("AOC",)

    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE tenders SET status = 'awarded'")
        conn.commit()
    assert backfill_blank_status(store.db_path, tmp_path) == 0
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT status FROM tenders").fetchone() == ("awarded",)


def _pdf_contact_record(tender_id: str, *bidders: str) -> TenderRecord:
    listing = ListingRow(
        serial="1",
        tender_id=tender_id,
        title_and_ref="Meters / REF",
        organisation_chain="NPIU||Visvesvaraya National Institute of Technology Nagpur",
        tender_stage="AOC",
        status="",
        status_page_url="https://eprocure.gov.in/eprocure/app?sp=S",
    )
    awarded = [
        BidRow(
            serial=str(i),
            bid_number=str(i),
            bidder_name=name,
            submitted_date="",
            status="",
            remarks="",
            status_updated_on="",
            value="1",
            currency="INR",
        )
        for i, name in enumerate(bidders, start=1)
    ]
    return TenderRecord(listing=listing, awarded_bids=awarded)


FAKE_EXTRACT = [
    {
        "filename": "workorder.pdf",
        "text": "Email: sunrisebuilders@example.com  Mob.: 9123456780",
        "emails": ["sunrisebuilders@example.com"],
        "phones": ["9123456780"],
        "gstins": [],
    }
]


def test_work_order_contacts_are_stored_as_evidence(tmp_path):
    """The scraper already holds these. Throwing them away costs a paid lookup."""
    from pipeline_core.models import TenderDocument, Vendor
    from pipeline_core.db import session

    store = Store(tmp_path)
    record = _pdf_contact_record("2026_A_1", "Sunrise Builders")
    record.pdf_extracts = FAKE_EXTRACT
    store.save_tender(record)

    with session(store.engine) as current:
        vendor = current.query(Vendor).one()
        assert vendor.pdf_email == "sunrisebuilders@example.com"
        assert vendor.pdf_phone == "9123456780"
        # evidence only -- the researched contact block stays stage 2's to fill
        assert vendor.email is None
        assert vendor.enrichment_status == "pending"
        document = current.query(TenderDocument).one()
        assert document.filename == "workorder.pdf"
        assert document.emails == "sunrisebuilders@example.com"
        assert document.phones == "9123456780"


def test_contacts_are_not_attributed_when_a_tender_had_several_winners(tmp_path):
    """One work order names one firm. Guessing whose it is would mail the wrong company."""
    from pipeline_core.models import Vendor
    from pipeline_core.db import session

    store = Store(tmp_path)
    record = _pdf_contact_record("2026_A_1", "Sunrise Builders", "Other Ltd")
    record.pdf_extracts = FAKE_EXTRACT
    store.save_tender(record)

    with session(store.engine) as current:
        assert [v.pdf_email for v in current.query(Vendor).all()] == [None, None]


def test_reading_the_pdfs_again_replaces_that_tender_s_clues(tmp_path):
    """A clue the files no longer hold must not stay on the company, or be mailed."""
    from pipeline_core.models import VendorContact

    store = Store(tmp_path)
    record = _pdf_contact_record("2026_A_1", "Sunrise Builders")
    record.pdf_extracts = FAKE_EXTRACT
    store.save_tender(record)
    other = _pdf_contact_record("2026_B_1", "Sunrise Builders")
    other.pdf_extracts = [
        {"filename": "LOA.pdf", "emails": ["office@sunrise.example"], "phones": [], "gstins": []}
    ]
    store.save_tender(other)

    again = _pdf_contact_record("2026_A_1", "Sunrise Builders")
    store.save_tender(again, replace_documents=True)

    with session(store.engine) as current:
        rows = {
            (row.tender_id, row.channel, row.value)
            for row in current.query(VendorContact).filter(VendorContact.source == "pdf")
        }
        assert rows == {("2026_B_1", "email", "office@sunrise.example")}
        vendor = current.query(Vendor).one()
        assert vendor.pdf_email == "office@sunrise.example"
        assert vendor.pdf_phone is None


def test_a_save_without_new_pdfs_keeps_the_tender_s_clues(tmp_path):
    from pipeline_core.models import VendorContact

    store = Store(tmp_path)
    record = _pdf_contact_record("2026_A_1", "Sunrise Builders")
    record.pdf_extracts = FAKE_EXTRACT
    store.save_tender(record)
    store.save_tender(_pdf_contact_record("2026_A_1", "Sunrise Builders"))

    with session(store.engine) as current:
        assert current.query(VendorContact).filter(VendorContact.source == "pdf").count() == 2


def test_a_dash_before_a_pdf_email_is_not_stored(tmp_path):
    from pipeline_core.models import TenderDocument, VendorContact

    store = Store(tmp_path)
    record = _pdf_contact_record("2026_A_1", "Sunrise Builders")
    record.pdf_extracts = [
        {"filename": "LOA.pdf", "emails": ["-sunrise@example.com"], "phones": [], "gstins": []}
    ]
    store.save_tender(record)

    with session(store.engine) as current:
        assert current.query(TenderDocument).one().emails == "sunrise@example.com"
        row = current.query(VendorContact).filter(VendorContact.channel == "email").one()
        assert (row.value, row.valid, row.tender_id) == ("sunrise@example.com", 1, "2026_A_1")
        assert current.query(Vendor).one().pdf_email == "sunrise@example.com"


def test_backfill_copies_json_extracts_once(tmp_path):
    import json
    import sqlite3

    from pipeline_core.queries import backfill_tender_documents

    store = Store(tmp_path)
    listing = ListingRow(
        serial="1",
        tender_id="2026_ORG_9",
        title_and_ref="Cable / REF",
        organisation_chain="Org",
        tender_stage="AOC",
        status="AOC",
        status_page_url="https://eprocure.gov.in/eprocure/app?sp=S",
    )
    store.save_tender(TenderRecord(listing=listing))
    document = tmp_path / "kept.json"
    document.write_text(
        json.dumps(
            {
                "pdf_extracts": [
                    {
                        "filename": "work-order.pdf",
                        "emails": ["clue@example.com"],
                        "phones": [],
                        "gstins": ["27ABCDE1234F1Z5"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE tenders SET json_path = ?", (str(document),))
        conn.commit()

    assert backfill_tender_documents(store.db_path) == 1
    assert backfill_tender_documents(store.db_path) == 0
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT filename, emails, gstins FROM tender_documents"
        ).fetchone()
    assert row == ("work-order.pdf", "clue@example.com", "27ABCDE1234F1Z5")


def _sourced(page: int | None = 3, **record) -> TenderRecord:
    listing = ListingRow(
        serial="25.",
        tender_id="2026_MoRTH_7_1",
        title_and_ref="Road works / REF-7",
        organisation_chain="Ministry of Road Transport and Highways||P2 Delhi - MoRTH",
        tender_stage="AOC",
        status="",
        status_page_url="https://eprocure.gov.in/eprocure/app?sp=S",
        page=page,
    )
    return TenderRecord(listing=listing, **record)


_AWARDED_SEARCH = ScrapeSource(
    search_mode="awarded",
    search_fields={"tenderStatus": "6", "fromDate": "01/01/2026"},
    job_id=9,
)


def test_save_tender_records_where_the_tender_came_from(tmp_path):
    store = Store(tmp_path)
    store.save_tender(_sourced(), source=_AWARDED_SEARCH)

    with session(store.engine) as current:
        tender = current.get(Tender, "2026_MoRTH_7_1")
        assert tender.origin == "eprocure.gov.in"
        assert tender.search_mode == "awarded"
        assert json.loads(tender.search_fields) == {"fromDate": "01/01/2026", "tenderStatus": "6"}
        assert tender.listing_page == 3
        assert tender.listing_serial == 25
        assert tender.scrape_job_id == 9


def test_a_save_without_a_search_keeps_the_recorded_source(tmp_path):
    """A save that names no search keeps the search already recorded."""
    store = Store(tmp_path)
    store.save_tender(_sourced(), source=_AWARDED_SEARCH)
    store.save_tender(_sourced(page=None))

    with session(store.engine) as current:
        tender = current.get(Tender, "2026_MoRTH_7_1")
        assert tender.origin == "eprocure.gov.in"
        assert tender.search_mode == "awarded"
        assert tender.listing_page == 3
        assert tender.listing_serial == 25
        assert tender.scrape_job_id == 9


_PDF_DOWNLOAD = ScrapeSource(
    search_mode="tender_id",
    search_fields={"tenderId": "2026_MoRTH_7_1"},
    job_id=40,
)


def _runs(store: Store, tender_id: str = "2026_MoRTH_7_1") -> set[int]:
    from pipeline_core.models import JobTender

    with session(store.engine) as current:
        return {
            row.job_id
            for row in current.query(JobTender).filter(JobTender.tender_id == tender_id)
        }


def test_the_first_run_that_saved_a_tender_keeps_the_id(tmp_path):
    store = Store(tmp_path)
    store.save_tender(_sourced(), source=_AWARDED_SEARCH)
    terminal = ScrapeSource(search_mode="awarded", search_fields={"tenderStatus": "6"})
    store.save_tender(_sourced(page=1), source=terminal)
    store.save_tender(_sourced(page=2), source=_PDF_DOWNLOAD)

    with session(store.engine) as current:
        tender = current.get(Tender, "2026_MoRTH_7_1")
        assert tender.listing_page == 1
        assert tender.scrape_job_id == 9
    assert _runs(store) == {9, 40}


def test_a_pdf_download_leaves_the_scrape_source_alone(tmp_path):
    """Download PDFs searches by tender id only to reach the files. It is not the source."""
    store = Store(tmp_path)
    store.save_tender(_sourced(), scraped_at="2026-10-01T00:00:00+00:00", source=_AWARDED_SEARCH)
    store.save_tender(
        _sourced(page=1), scraped_at="2026-10-20T00:00:00+00:00", source=_PDF_DOWNLOAD
    )

    with session(store.engine) as current:
        tender = current.get(Tender, "2026_MoRTH_7_1")
        assert tender.scrape_job_id == 9
        assert tender.search_mode == "awarded"
        assert json.loads(tender.search_fields) == {"fromDate": "01/01/2026", "tenderStatus": "6"}
        assert tender.listing_page == 3
        assert tender.listing_serial == 25
        assert tender.scraped_at == "2026-10-01T00:00:00+00:00"
    assert _runs(store) == {9, 40}


def test_a_pdf_download_never_becomes_the_run_of_a_terminal_scrape(tmp_path):
    store = Store(tmp_path)
    terminal = ScrapeSource(search_mode="awarded", search_fields={"tenderStatus": "6"})
    store.save_tender(_sourced(), source=terminal)
    store.save_tender(_sourced(page=1), source=_PDF_DOWNLOAD)

    with session(store.engine) as current:
        tender = current.get(Tender, "2026_MoRTH_7_1")
        assert tender.scrape_job_id is None
        assert tender.search_mode == "awarded"
    assert _runs(store) == {40}


def test_a_refresh_scrape_keeps_the_first_run_and_is_listed(tmp_path):
    store = Store(tmp_path)
    store.save_tender(_sourced(), source=_AWARDED_SEARCH)
    refresh = ScrapeSource(search_mode="awarded", search_fields={"tenderStatus": "6"}, job_id=12)
    store.save_tender(_sourced(page=4), source=refresh)

    with session(store.engine) as current:
        tender = current.get(Tender, "2026_MoRTH_7_1")
        assert tender.scrape_job_id == 9
        assert tender.listing_page == 4
    assert _runs(store) == {9, 12}


def test_award_takes_its_quote_by_bid_number_and_falls_back_to_the_contract_value(tmp_path):
    awarded = BidRow("1", "77", "Road Builders Pvt Ltd", "", "", "", "", value=None)
    other_quote = BidRow("1", "76", "Other Co", "", "", "", "", value="1,00,000.00", rank="L2")
    winning_quote = BidRow(
        "2", "77", "Road Builders Pvt Ltd", "", "", "", "", value="90,000.50", rank="L1"
    )
    record = _sourced(
        financial_bids=[other_quote, winning_quote],
        awarded_bids=[awarded],
        aoc={"Total Contract Value": "INR 95,000.00"},
    )
    store = Store(tmp_path)
    store.save_tender(record)

    with session(store.engine) as current:
        award = current.query(Award).one()
        vendor = current.query(Vendor).one()
        assert award.bid_number == "77"
        assert award.rank == "L1"
        assert award.quoted_value == 9000050
        assert award.awarded_value == 9500000
        assert award.awarded_currency == "INR"
        assert award.work_title == "Road works / REF-7"
        # The buyer's office, read from the organisation chain, not the company's.
        assert (vendor.city, vendor.state) == ("Delhi", "Delhi")


def test_listing_status_uses_the_stage_when_the_cell_is_blank():
    assert listing_status("", "AOC") == "AOC"
    assert listing_status(None, " AOC ") == "AOC"
    assert listing_status("Awarded", "AOC") == "Awarded"
    assert listing_status("", "") == ""


def test_serial_number_reads_the_portal_s_no():
    assert serial_number("1.") == 1
    assert serial_number(" 5001. ") == 5001
    assert serial_number("12") == 12
    assert serial_number("") is None
    assert serial_number(None) is None
    assert serial_number("S.No") is None

def test_a_pdf_download_does_not_put_an_old_send_back_in_the_queue(tmp_path):
    """An older company-level send covers tenders scraped before it. A download must not move that."""
    from pipeline_core.models import OUTREACH_SENT
    from pipeline_core.queries import mail_messages, mark_enriched, record_outreach

    store = Store(tmp_path)
    record = _pdf_contact_record("2026_A_1", "Sunrise Builders")
    store.save_tender(record, scraped_at="2026-10-01T00:00:00+00:00", source=_AWARDED_SEARCH)
    with session(store.engine) as current:
        vendor_id = current.query(Vendor).one().vendor_id
        mark_enriched(current, vendor_id, email="office@sunrise.example", phone=None)
        record_outreach(current, vendor_id=vendor_id, email="office@sunrise.example",
                        subject="s", transport="gmail", status=OUTREACH_SENT)
        assert mail_messages(current) == []

    again = _pdf_contact_record("2026_A_1", "Sunrise Builders")
    again.pdf_extracts = FAKE_EXTRACT
    download = ScrapeSource(search_mode="tender_id", search_fields={"tenderId": "2026_A_1"}, job_id=40)
    store.save_tender(again, scraped_at="2099-01-01T00:00:00+00:00", source=download)

    with session(store.engine) as current:
        assert current.get(Tender, "2026_A_1").scraped_at == "2026-10-01T00:00:00+00:00"
        assert mail_messages(current) == []
