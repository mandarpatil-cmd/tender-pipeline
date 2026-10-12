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


_LISTING = ListingRow(
    serial="1",
    tender_id="T1",
    title_and_ref="x",
    organisation_chain="Org",
    tender_stage="AOC",
    status="",
    status_page_url="https://eprocure.gov.in/eprocure/app",
)


def test_winner_prefers_awarded_table():
    l1 = BidRow("1", "1", "A", "", "", "", "", value="1", rank="L1")
    awarded = BidRow("1", "2", "B", "", "", "", "", value="2", currency="INR")
    record = TenderRecord(listing=_LISTING, financial_bids=[l1], awarded_bids=[awarded])
    assert winner_rows(record)[0].bidder_name == "B"


def test_winner_falls_back_to_the_l1_financial_bid():
    l1 = BidRow("1", "1", "Lowest", "", "", "", "", value="1", rank="l1")
    l2 = BidRow("2", "2", "Second", "", "", "", "", value="2", rank="L2")
    accepted = BidRow("1", "3", "Accepted Co", "", "Accepted", "", "")
    record = TenderRecord(listing=_LISTING, financial_bids=[l2, l1], bids=[accepted])
    assert [row.bidder_name for row in winner_rows(record)] == ["Lowest"]


def test_winner_falls_back_to_an_accepted_bid():
    rejected = BidRow("1", "1", "Rejected Co", "", "Rejected", "", "")
    accepted = BidRow("2", "2", "Accepted Co", "", "Technically Accepted", "", "")
    record = TenderRecord(listing=_LISTING, bids=[rejected, accepted])
    assert [row.bidder_name for row in winner_rows(record)] == ["Accepted Co"]


def test_winner_falls_back_to_one_bid_with_l1_in_remarks():
    first = BidRow("1", "1", "First L1", "", "", "Declared L1", "")
    second = BidRow("2", "2", "Second L1", "", "", "also L1", "")
    record = TenderRecord(listing=_LISTING, bids=[first, second])
    assert [row.bidder_name for row in winner_rows(record)] == ["First L1"]


def test_no_winner_when_nothing_marks_one():
    plain = BidRow("1", "1", "Bidder", "", "Opened", "", "")
    record = TenderRecord(listing=_LISTING, bids=[plain])
    assert winner_rows(record) == []
