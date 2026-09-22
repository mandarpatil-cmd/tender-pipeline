import pytest

from stage1_scrape.domain.models import BidRow, ListingRow, TenderRecord
from stage1_scrape.export.vendor_sheet import (
    MISSING,
    VENDOR_SHEET_COLUMNS,
    cell,
    vendor_row,
    write_sheet,
    write_vendor_sheet,
)
from stage1_scrape.persist.store import Store


def _seed() -> dict:
    return {
        "vendor_id": 1,
        "name_raw": "Sunrise Builders",
        "name_norm": "SUNRISE BUILDERS",
        "legal_form": "trade",
        "city": "Nagpur",
        "state": "Maharashtra",
        "buyer_hint": "Visvesvaraya National Institute of Technology Nagpur",
        "awards": [
            {
                "tender_id": "2026_WBNPI_895142_1",
                "bid_number": "3365622",
                "rank": None,
                "awarded_value": "48,445",
                "awarded_currency": "INR",
                "contract_date": "04-Mar-2026",
                "work_title": "Energy meters",
            }
        ],
    }


def _tender(tender_id: str, bidder: str, bid_number: str) -> TenderRecord:
    return TenderRecord(
        listing=ListingRow(
            serial="1",
            tender_id=tender_id,
            title_and_ref="Meters / REF",
            organisation_chain="NPIU||Visvesvaraya National Institute of Technology Nagpur",
            tender_stage="AOC",
            status="",
            status_page_url="https://eprocure.gov.in/eprocure/app?sp=S",
        ),
        awarded_bids=[
            BidRow(
                serial="1",
                bid_number=bid_number,
                bidder_name=bidder,
                submitted_date="",
                status="",
                remarks="",
                status_updated_on="",
                value="48,445",
                currency="INR",
            )
        ],
    )


def test_cell_keeps_partial_and_uses_na_for_empty():
    assert cell("27**********1ZA") == "27**********1ZA"
    assert cell(None) == MISSING
    assert cell("") == MISSING
    assert cell([]) == MISSING
    assert cell(["a@x.com", ""]) == "a@x.com"
    assert cell(["a@x.com", "b@x.com"]) == "a@x.com; b@x.com"


def test_vendor_row_has_every_column():
    row = vendor_row(_seed())
    assert list(row) == VENDOR_SHEET_COLUMNS
    assert row["name_raw"] == "Sunrise Builders"
    assert row["tender_ids"] == "2026_WBNPI_895142_1"
    assert row["work_titles"] == "Energy meters"
    assert row["awarded_values"] == "48,445 INR"
    assert row["pdf_emails"] == MISSING


def test_the_sheet_carries_no_model_generated_columns():
    """Contacts live in the database now; this sheet is scrape facts only."""
    assert not [c for c in VENDOR_SHEET_COLUMNS if c.startswith(("llm_", "apollo_"))]
    assert len(VENDOR_SHEET_COLUMNS) == 15


def test_write_sheet_csv_and_xlsx(tmp_path):
    pandas = pytest.importorskip("pandas")
    csv_path, xlsx_path = write_sheet([vendor_row(_seed())], tmp_path)

    frame = pandas.read_csv(csv_path, dtype=str, keep_default_na=False)
    assert list(frame.columns) == VENDOR_SHEET_COLUMNS
    assert frame.loc[0, "name_raw"] == "Sunrise Builders"
    assert frame.loc[0, "pdf_gstins"] == MISSING
    assert xlsx_path.exists()


def test_write_vendor_sheet_covers_every_stored_vendor(tmp_path):
    pandas = pytest.importorskip("pandas")
    store = Store(tmp_path)
    store.save_tender(_tender("2026_WBNPI_895142_1", "Sunrise Builders", "1"))
    store.save_tender(_tender("2026_WBNPI_892549_1", "New Vendor", "2"))

    csv_path, _ = write_vendor_sheet(store, tmp_path)

    frame = pandas.read_csv(csv_path, dtype=str, keep_default_na=False)
    assert set(frame["name_raw"]) == {"Sunrise Builders", "New Vendor"}
    assert len(frame) == store.vendor_count()


def test_rewriting_the_sheet_does_not_accumulate_rows(tmp_path):
    pandas = pytest.importorskip("pandas")
    store = Store(tmp_path)
    store.save_tender(_tender("2026_WBNPI_895142_1", "Sunrise Builders", "1"))

    write_vendor_sheet(store, tmp_path)
    csv_path, _ = write_vendor_sheet(store, tmp_path)

    frame = pandas.read_csv(csv_path, dtype=str, keep_default_na=False)
    assert len(frame) == 1
