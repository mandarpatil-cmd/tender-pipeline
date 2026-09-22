from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "listing": asdict(self.listing),
            "header": self.header,
            "stage_summary_url": self.stage_summary_url,
            "bids": [asdict(row) for row in self.bids],
            "financial_bids": [asdict(row) for row in self.financial_bids],
            "awarded_bids": [asdict(row) for row in self.awarded_bids],
            "bid_opening": self.bid_opening,
            "technical_eval": self.technical_eval,
            "finance_eval": self.finance_eval,
            "aoc": self.aoc,
            "documents": [asdict(doc) for doc in self.documents],
            "downloaded_files": self.downloaded_files,
            "pdf_extracts": self.pdf_extracts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TenderRecord:
        listing = ListingRow(**data["listing"])
        return cls(
            listing=listing,
            header=dict(data.get("header") or {}),
            stage_summary_url=data.get("stage_summary_url"),
            bids=_rows(BidRow, data.get("bids")),
            financial_bids=_rows(BidRow, data.get("financial_bids")),
            awarded_bids=_rows(BidRow, data.get("awarded_bids")),
            bid_opening=dict(data.get("bid_opening") or {}),
            technical_eval=dict(data.get("technical_eval") or {}),
            finance_eval=dict(data.get("finance_eval") or {}),
            aoc=dict(data.get("aoc") or {}),
            documents=_rows(DocumentLink, data.get("documents")),
            downloaded_files=list(data.get("downloaded_files") or []),
            pdf_extracts=list(data.get("pdf_extracts") or []),
        )


def _rows(cls: type, items: Any) -> list:
    names = {item.name for item in fields(cls)}
    out = []
    for raw in items or []:
        payload = {key: value for key, value in raw.items() if key in names}
        out.append(cls(**payload))
    return out
