from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ListingRow:
    serial: str
    tender_id: str
    title_and_ref: str
    organisation_chain: str
    tender_stage: str
    status: str
    status_page_url: str
    #: 1-based listing page this row was read from. The parser cannot know it.
    page: int | None = None


#: ``ScrapeSource.search_mode``: the portal search a run sent.
SEARCH_AWARDED = "awarded"
SEARCH_ORGANISATION = "organisation"
#: A PDF download. It finds one tender to reach its files; it is not a scrape.
SEARCH_TENDER_ID = "tender_id"


@dataclass(frozen=True)
class ScrapeSource:
    """The portal search a run sent, written onto every tender it saves."""

    search_mode: str
    #: Portal form fields, without the captcha.
    search_fields: dict[str, str] = field(default_factory=dict)
    #: ``ui_jobs.job_id`` when the window started the run.
    job_id: int | None = None


@dataclass(frozen=True)
class BidRow:
    serial: str
    bid_number: str
    bidder_name: str
    submitted_date: str
    status: str
    remarks: str
    status_updated_on: str
    bid_summary_url: str | None = None
    value: str | None = None
    rank: str | None = None
    currency: str | None = None


@dataclass(frozen=True)
class DocumentLink:
    label: str
    filename: str
    url: str
    size: str | None = None


@dataclass(frozen=True)
class NextPage:
    kind: Literal["get", "post"]
    url: str | None = None
    note: str = ""


@dataclass
class TenderRecord:
    listing: ListingRow
    header: dict[str, str] = field(default_factory=dict)
    stage_summary_url: str | None = None
    bids: list[BidRow] = field(default_factory=list)
    financial_bids: list[BidRow] = field(default_factory=list)
    awarded_bids: list[BidRow] = field(default_factory=list)
    bid_opening: dict[str, str] = field(default_factory=dict)
    technical_eval: dict[str, str] = field(default_factory=dict)
    finance_eval: dict[str, str] = field(default_factory=dict)
    aoc: dict[str, str] = field(default_factory=dict)
    documents: list[DocumentLink] = field(default_factory=list)
    downloaded_files: list[str] = field(default_factory=list)
    pdf_extracts: list[dict[str, Any]] = field(default_factory=list)
