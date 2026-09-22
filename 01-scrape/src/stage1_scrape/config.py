from __future__ import annotations

"""Portal URLs, form ids, and parse constants for eprocure.gov.in."""

BASE_URL = "https://eprocure.gov.in"
APP_PATH = "/eprocure/app"
APP_URL = f"{BASE_URL}{APP_PATH}"
SEARCH_PAGE_URL = f"{APP_URL}?page=WebTenderStatusLists&service=page"

SEARCH_FORM_ID = "frmSearchFilter"

# CONFIRMED: AOC is option value 6 on #tenderStatus, not the label "AOC".
TENDER_STATUS_AOC = "6"

TENDER_STATUS_LABELS = {
    "0": "-Select-",
    "1": "To Be Opened Tenders",
    "2": "Technical Bid Opening",
    "3": "Technical Evaluation",
    "4": "Financial Bid Opening",
    "5": "Financial Evaluation",
    "6": "AOC",
    "7": "Retender",
    "8": "Cancelled",
    "9": "Concluded",
}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

VIEW_STATUS_TITLE = "View Tender Status"
STAGE_SUMMARY_TITLE = "View the all stage summary Details"

BIDS_LIST_HEADING = "Bids List"
FINANCIAL_EVAL_HEADING = "Financial Evaluation Bid List"
AWARDED_BIDS_HEADING = "Awarded Bids List"

PDF_MAGIC = b"%PDF"

DEFAULT_DELAY_SECONDS = 1.5
DEFAULT_TIMEOUT_SECONDS = 45.0
