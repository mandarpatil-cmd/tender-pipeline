from pathlib import Path

import pytest

from stage1_scrape.config import APP_URL
from stage1_scrape.parse import (
    find_document_links,
    find_next_page,
    find_stage_summary_url,
    parse_named_table,
    parse_result_count,
    parse_results_table,
    parse_summary_page,
    parse_total_records,
    search_still_showing_form,
)


def test_parse_results_table_uses_title_not_id(html):
    rows = parse_results_table(html("results_table.html"))
    assert len(rows) == 2
    assert rows[0].tender_id == "2026_ORG_908061_1"
    assert rows[0].title_and_ref == "Supply of cable / REF-007"
    assert rows[0].organisation_chain == "Ministry / Department / Division"
    assert rows[0].status == "AOC"
    assert rows[0].status_page_url.startswith(APP_URL)
    assert "sp=SL%2FqcnYbbSTsqFv41yQn3Qw%3D%3D" in rows[0].status_page_url
    assert rows[1].tender_id == "2026_ORG_908062_1"


def test_result_count_and_next_link(html):
    page = html("results_table.html")
    assert parse_result_count(page) == (1, 10, 1502)
    nxt = find_next_page(page)
    assert nxt is not None
    assert nxt.kind == "get"
    assert "SnextToken" in nxt.url


def test_search_form_is_not_treated_as_results(html):
    assert search_still_showing_form(html("search_form.html")) is True
    assert search_still_showing_form(html("results_table.html")) is False


def test_stage_summary_link(html):
    url = find_stage_summary_url(html("status_page.html"))
    assert "page=WebTenderStatus" in url
    assert "DirectLink_0" in url


def test_summary_tables_and_documents(html):
    parsed = parse_summary_page(html("summary_page.html"))
    assert parsed["header"]["Tender ID"] == "2026_ORG_908061_1"
    assert parsed["header"]["Tender Title"] == "Supply of cable"

    bids = parsed["bids"]
    assert len(bids) == 2
    assert bids[0].bidder_name == "ACHAL ENTERPRISES"
    assert bids[0].status == "Rejected-Finance"
    assert bids[0].remarks == "L8"
    assert bids[0].bid_number == "3422879"
    assert bids[0].bid_summary_url and "sp=SbidToken" in bids[0].bid_summary_url

    financial = parsed["financial_bids"]
    assert len(financial) == 1
    assert financial[0].rank == "L1"
    assert financial[0].value == "125000.00"

    awarded = parsed["awarded_bids"]
    assert awarded[0].bidder_name == "WINNER CO"
    assert awarded[0].value == "125000.00"

    assert parsed["aoc"]["Contract Date"] == "20-Jul-2026"
    assert parsed["aoc"]["Total Contract Value"] == "125000.00"

    docs = {doc.filename: doc for doc in parsed["documents"]}
    assert "007WOLTCABLEFORSTREETLIGHTPOLE.pdf" in docs
    assert "techsummary_cable.pdf" in docs
    assert "finsummary_cable.pdf" in docs
    assert docs["007WOLTCABLEFORSTREETLIGHTPOLE.pdf"].label == "AOC document"
    assert docs["007WOLTCABLEFORSTREETLIGHTPOLE.pdf"].size == "1109.00 KB"
    assert not any(doc.filename == "3422879" for doc in parsed["documents"])


def test_live_financial_and_awarded_rows_without_td_field(html):
    parsed = parse_summary_page(html("live_eval_tables.html"))
    financial = parsed["financial_bids"]
    assert [(row.bidder_name, row.value, row.rank) for row in financial] == [
        ("DQBYDT PVT LTD", "235516.20", "L4"),
        ("Kanta enterprises", "59934.56", "L1"),
    ]
    awarded = parsed["awarded_bids"]
    assert len(awarded) == 1
    assert awarded[0].bidder_name == "Kanta enterprises"
    assert awarded[0].bid_number == "3432381"
    assert awarded[0].currency == "INR"
    assert awarded[0].value == "59,935"


def test_parse_named_table_stops_at_next_section(html):
    bids = parse_named_table(html("summary_page.html"), "Bids List")
    assert [b.bid_number for b in bids] == ["3422879", "3422880"]


def test_find_document_links_standalone(html):
    docs = find_document_links(html("summary_page.html"))
    assert {d.filename for d in docs} == {
        "007WOLTCABLEFORSTREETLIGHTPOLE.pdf",
        "techsummary_cable.pdf",
        "finsummary_cable.pdf",
    }


def test_parse_total_records_reads_the_banner_this_portal_prints():
    """GePNIC prints 'Total records: N', not the '1 - 10 Records of N' form."""
    assert parse_total_records("<b>Total records: 159019&nbsp;</b>") == 159019
    assert parse_total_records("Total records: 1,502") == 1502
    assert parse_total_records("total  RECORDS :  42") == 42


def test_parse_total_records_returns_none_when_absent():
    assert parse_total_records("<td>no banner here</td>") is None
    assert parse_total_records("") is None


def test_parse_total_records_handles_the_real_listing_page():
    """The count is the only signal that a search filter applied.

    The listing table carries no date column, so a filtered page and an
    unfiltered one are indistinguishable without this number. This reads the
    real page a live run saved -- gitignored, so absent in a fresh checkout.
    """
    page = Path(__file__).resolve().parents[1] / "data" / "debug" / "listing_page_1.html"
    if not page.is_file():
        pytest.skip("no saved listing page in this checkout")
    total = parse_total_records(page.read_text(encoding="utf-8", errors="replace"))
    # Not pinned: every run overwrites this file and the count changes with the
    # filter (~159,000 unfiltered, ~3,000 for a six-month window). What must
    # hold is that the regex still matches the portal's real markup.
    assert isinstance(total, int) and total > 0
