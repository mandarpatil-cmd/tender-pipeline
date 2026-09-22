from stage1_scrape.domain.enrichment import (
    infer_city_state,
    infer_legal_form,
    normalize_name,
    winner_rows,
)
from stage1_scrape.domain.models import BidRow, ListingRow, TenderRecord


def test_normalize_and_legal_form():
    assert "PVT LTD" in normalize_name("ACME TECHWORKS PRIVATE LIMITED")
    assert infer_legal_form("ACME TECHWORKS PRIVATE LIMITED") == "pvt_ltd"
    assert infer_legal_form("SHRINIVAS HOSPITALITY AND MANAGEMENT SERVICES PVT. LTD.") == "pvt_ltd"
    assert infer_legal_form("AMIT RAUT") == "person"
    assert infer_legal_form("N H COSTRUCTION") == "trade"
    assert infer_legal_form("Kanta enterprises") == "trade"
    assert infer_legal_form("KHANDELWAL AND KHANDELWAL") == "trade"


def test_city_from_vnit_chain():
    chain = (
        "National Project Implementation Unit - World Bank Tenders"
        "||Visvesvaraya National Institute of Technology Nagpur"
    )
    assert infer_city_state(chain) == ("Nagpur", "Maharashtra")


def test_winner_prefers_awarded_table():
    listing = ListingRow(
        serial="1",
        tender_id="T1",
        title_and_ref="x",
        organisation_chain="Org",
        tender_stage="AOC",
        status="",
        status_page_url="https://eprocure.gov.in/eprocure/app",
    )
    l1 = BidRow("1", "1", "A", "", "", "", "", value="1", rank="L1")
    awarded = BidRow("1", "2", "B", "", "", "", "", value="2", currency="INR")
    record = TenderRecord(listing=listing, financial_bids=[l1], awarded_bids=[awarded])
    assert winner_rows(record)[0].bidder_name == "B"
