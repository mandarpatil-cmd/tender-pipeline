from unittest.mock import patch

from stage1_scrape.app.run import run_scrape
from stage1_scrape.domain.models import BidRow, ListingRow, TenderRecord
from stage1_scrape.persist.store import Store


def _record() -> TenderRecord:
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
        currency="INR",
    )
    return TenderRecord(listing=listing, awarded_bids=[awarded])


def test_run_scrape_scrapes_then_exports(tmp_path):
    record = _record()
    Store(tmp_path).save_tender(record)

    with patch("stage1_scrape.app.run.scrape_aoc", return_value=[record]) as scrape:
        info = run_scrape(tmp_path, captcha_solver="openrouter", skip_known=True)

    scrape.assert_called_once()
    assert scrape.call_args.kwargs["captcha_solver"] == "openrouter"
    assert info["scraped"] == 1
    assert info["tender_ids"] == ["2026_WBNPI_911698_1"]
    assert info["vendors"] == 1
    assert info["csv"].endswith("vendors.csv")
    assert (tmp_path / "exports" / "vendors.csv").is_file()


def test_run_scrape_still_exports_when_nothing_new_was_scraped(tmp_path):
    """The listing was already indexed. The sheet should still be rewritten."""
    Store(tmp_path).save_tender(_record())

    with patch("stage1_scrape.app.run.scrape_aoc", return_value=[]):
        info = run_scrape(tmp_path, max_tenders=1, skip_known=True)

    assert info["scraped"] == 0
    assert info["tender_ids"] == []
    assert info["vendors"] == 1
    assert (tmp_path / "exports" / "vendors.csv").is_file()


def test_run_scrape_does_not_import_any_llm_machinery():
    """Stage 1 stops at the portal; contacts are stage 2's job."""
    import stage1_scrape.app.run as module

    source = open(module.__file__, encoding="utf-8").read()
    for banned in ("langchain", "run_llm_source", "enrich_limit", "web_search"):
        assert banned not in source
