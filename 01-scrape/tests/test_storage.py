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
    assert list(store.json_dir.glob("*.json")) == []


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


def test_reindex_keeps_document_rows_when_no_pdf_is_on_disk(tmp_path):
    import json

    from pipeline_core.db import session
    from pipeline_core.models import TenderDocument
    from stage1_scrape.app.pipeline import reindex_saved

    store = Store(tmp_path)
    record = _pdf_contact_record("2026_A_1", "Sunrise Builders")
    record.pdf_extracts = list(FAKE_EXTRACT)
    store.save_tender(record)
    payload = record.to_dict()
    payload["pdf_extracts"] = []
    (store.json_dir / "2026_A_1.json").write_text(json.dumps(payload), encoding="utf-8")

    assert reindex_saved(tmp_path) == 1
    with session(store.engine) as current:
        assert current.query(TenderDocument).count() == 1


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
    """A reindex re-reads saved HTML. It cannot know the search, so it keeps it."""
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


def test_the_last_run_that_saved_a_tender_is_the_one_recorded(tmp_path):
    store = Store(tmp_path)
    store.save_tender(_sourced(), source=_AWARDED_SEARCH)
    terminal = ScrapeSource(search_mode="awarded", search_fields={"tenderStatus": "6"})
    store.save_tender(_sourced(page=1), source=terminal)

    with session(store.engine) as current:
        tender = current.get(Tender, "2026_MoRTH_7_1")
        assert tender.listing_page == 1
        assert tender.scrape_job_id is None
        assert json.loads(tender.search_fields) == {"tenderStatus": "6"}


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