"""One row per award, filtered and paged in the database.

The window and its export both call this. A date or amount that cannot be
read stays in the result: dropping it would hide a row the operator can see.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from .loose import loose_date, loose_number
from .models import (
    ENRICHMENT_STATUSES,
    OUTREACH_FAILED,
    OUTREACH_SENT,
    STATUS_DONE,
    Award,
    Outreach,
    Tender,
    Vendor,
)

PAGE_SIZE = 50

COLUMNS: tuple[tuple[str, str, str, bool], ...] = (
    # key, label, group, evidence (PDF clue, not a mail address)
    ("tender_id", "Tender", "Tender", False),
    ("title", "Title", "Tender", False),
    ("organisation", "Organisation", "Tender", False),
    ("status", "Status", "Tender", False),
    ("contract_date", "Contract date", "Tender", False),
    ("contract_value", "Contract value", "Tender", False),
    ("scraped_at", "Scraped", "Tender", False),
    ("bid_number", "Bid", "Award", False),
    ("rank", "Rank", "Award", False),
    ("quoted_value", "Quoted", "Award", False),
    ("awarded_value", "Awarded", "Award", False),
    ("awarded_currency", "Currency", "Award", False),
    ("work_title", "Work title", "Award", False),
    ("vendor_id", "Vendor", "Vendor", False),
    ("name_raw", "Name", "Vendor", False),
    ("legal_form", "Legal form", "Vendor", False),
    ("city", "City", "Vendor", False),
    ("state", "State", "Vendor", False),
    ("buyer_hint", "Buyer", "Vendor", False),
    ("source", "Source", "Vendor", False),
    ("pdf_email", "PDF email", "PDF evidence", True),
    ("pdf_phone", "PDF phone", "PDF evidence", True),
    ("gstin", "GSTIN", "PDF evidence", True),
    ("pdf_files", "Files", "PDF evidence", True),
    ("email", "Email", "Contact", False),
    ("phone", "Phone", "Contact", False),
    ("enrichment_status", "Enrichment", "Contact", False),
    ("enriched_at", "Enriched", "Contact", False),
    ("outreach_status", "Outreach", "Outreach", False),
    ("last_attempt_at", "Last attempt", "Outreach", False),
    ("attempts", "Attempts", "Outreach", False),
    ("transport", "Transport", "Outreach", False),
    ("error", "Error", "Outreach", False),
)

#: GSTIN and file names are read from the tender JSON, not from a column.
FILE_COLUMNS = frozenset({"gstin", "pdf_files"})
SORTABLE = tuple(key for key, _, _, _ in COLUMNS if key not in FILE_COLUMNS)
_SORT_KEYS = frozenset(SORTABLE)


@dataclass(frozen=True)
class AwardQuery:
    """What the operator typed. Empty strings mean "no filter"."""

    text: str = ""
    tender_status: str = ""
    enrichment_status: str = ""
    outreach_status: str = ""
    source: str = ""
    state: str = ""
    organisation: str = ""
    date_from: str = ""
    date_to: str = ""
    value_min: str = ""
    value_max: str = ""
    sort: str = "tender_id"
    direction: str = "asc"
    page: int = 1

    def normalized(self) -> AwardQuery:
        sort = self.sort if self.sort in _SORT_KEYS else "tender_id"
        direction = "desc" if self.direction == "desc" else "asc"
        page = self.page if self.page >= 1 else 1
        enrichment = (
            self.enrichment_status
            if self.enrichment_status in ENRICHMENT_STATUSES
            else ""
        )
        outreach = (
            self.outreach_status
            if self.outreach_status in {OUTREACH_SENT, OUTREACH_FAILED, "none"}
            else ""
        )
        return replace(
            self,
            sort=sort,
            direction=direction,
            page=page,
            enrichment_status=enrichment,
            outreach_status=outreach,
        )

    def is_filtered(self) -> bool:
        return any(
            (
                self.text,
                self.tender_status,
                self.enrichment_status,
                self.outreach_status,
                self.source,
                self.state,
                self.organisation,
                self.date_from,
                self.date_to,
                self.value_min,
                self.value_max,
            )
        )


@dataclass(frozen=True)
class ViewCounts:
    tenders: int
    awards: int
    vendors: int
    enriched: int
    mailable: int
    sent: int
    phone_only: int


@dataclass
class AwardView:
    rows: list[dict[str, Any]]
    total: int
    page: int
    pages: int
    page_size: int
    counts: ViewCounts
    sort: str
    direction: str

    @property
    def start(self) -> int:
        if not self.rows:
            return 0
        return (self.page - 1) * self.page_size + 1

    @property
    def end(self) -> int:
        if not self.rows:
            return 0
        return (self.page - 1) * self.page_size + len(self.rows)


def choices(session: Session) -> dict[str, list[str]]:
    """Values for the pickers, taken from the rows that exist."""
    tender_status = [
        value
        for value in session.scalars(select(Tender.status).distinct().order_by(Tender.status))
        if value
    ]
    source = [
        value
        for value in session.scalars(select(Vendor.source).distinct().order_by(Vendor.source))
        if value
    ]
    return {
        "tender_status": tender_status,
        "source": source,
        "enrichment_status": list(ENRICHMENT_STATUSES),
        "outreach_status": [OUTREACH_SENT, OUTREACH_FAILED, "none"],
    }


def award_view(
    session: Session, query: AwardQuery, *, page_size: int | None = PAGE_SIZE
) -> AwardView:
    """One page of award rows, plus the counts for that same filter.

    ``page_size is None`` returns every matching row. That is the export.
    """
    query = query.normalized()
    latest = _latest_outreach()
    attempts = _attempt_counts()
    total, counts = _totals(session, query, latest)
    if page_size is None:
        size = total
        page = 1
        pages = 1
        limit = None
        offset = 0
    else:
        size = page_size
        pages = max(1, math.ceil(total / page_size)) if total else 1
        page = min(query.page, pages)
        limit = page_size
        offset = (page - 1) * page_size

    columns = _row_columns(latest, attempts)
    statement = _joined(
        select(*columns.values()),
        latest,
    ).outerjoin(attempts, attempts.c.vendor_id == Vendor.vendor_id)
    statement = _filtered(statement, query, latest)
    statement = statement.order_by(*_order(query, latest, attempts))
    if limit is not None:
        statement = statement.limit(limit).offset(offset)

    rows = [dict(zip(columns, row, strict=True)) for row in session.execute(statement)]
    for row in rows:
        row.setdefault("gstin", "")
        row.setdefault("pdf_files", "")
    return AwardView(
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        page_size=size,
        counts=counts,
        sort=query.sort,
        direction=query.direction,
    )


def _latest_outreach():
    return (
        select(
            Outreach.vendor_id.label("vendor_id"),
            Outreach.status.label("outreach_status"),
            Outreach.sent_at.label("last_attempt_at"),
            Outreach.transport.label("transport"),
            Outreach.error.label("error"),
            func.row_number()
            .over(
                partition_by=Outreach.vendor_id,
                order_by=(Outreach.sent_at.desc(), Outreach.outreach_id.desc()),
            )
            .label("rn"),
        )
    ).subquery("outreach_ranked")


def _attempt_counts():
    return (
        select(
            Outreach.vendor_id.label("vendor_id"),
            func.count().label("attempts"),
        )
        .group_by(Outreach.vendor_id)
        .subquery("outreach_attempts")
    )


def _row_columns(latest, attempts) -> dict[str, Any]:
    return {
        "tender_id": Tender.tender_id,
        "title": Tender.title,
        "organisation": Tender.organisation,
        "status": Tender.status,
        "contract_date": Tender.contract_date,
        "contract_value": Tender.contract_value,
        "scraped_at": Tender.scraped_at,
        "bid_number": Award.bid_number,
        "rank": Award.rank,
        "quoted_value": Award.quoted_value,
        "awarded_value": Award.awarded_value,
        "awarded_currency": Award.awarded_currency,
        "work_title": Award.work_title,
        "vendor_id": Vendor.vendor_id,
        "name_raw": Vendor.name_raw,
        "legal_form": Vendor.legal_form,
        "city": Vendor.city,
        "state": Vendor.state,
        "buyer_hint": Vendor.buyer_hint,
        "source": Vendor.source,
        "pdf_email": Vendor.pdf_email,
        "pdf_phone": Vendor.pdf_phone,
        "email": Vendor.email,
        "phone": Vendor.phone,
        "enrichment_status": Vendor.enrichment_status,
        "enriched_at": Vendor.enriched_at,
        "outreach_status": latest.c.outreach_status,
        "last_attempt_at": latest.c.last_attempt_at,
        "attempts": attempts.c.attempts,
        "transport": latest.c.transport,
        "error": latest.c.error,
        "json_path": Tender.json_path,
    }


def _joined(statement, latest):
    return (
        statement.select_from(Award)
        .join(Tender, Tender.tender_id == Award.tender_id)
        .join(Vendor, Vendor.vendor_id == Award.vendor_id)
        .outerjoin(
            latest,
            and_(latest.c.vendor_id == Vendor.vendor_id, latest.c.rn == 1),
        )
    )


def _filtered(statement, query: AwardQuery, latest):
    if query.text:
        pattern = _like(query.text)
        statement = statement.where(
            or_(
                _contains(Tender.tender_id, pattern),
                _contains(Tender.title, pattern),
                _contains(Tender.organisation, pattern),
                _contains(Award.work_title, pattern),
                _contains(Vendor.name_raw, pattern),
                _contains(Vendor.city, pattern),
                _contains(Vendor.state, pattern),
                _contains(Vendor.email, pattern),
                _contains(Vendor.phone, pattern),
            )
        )
    if query.tender_status:
        statement = statement.where(Tender.status == query.tender_status)
    if query.enrichment_status:
        statement = statement.where(Vendor.enrichment_status == query.enrichment_status)
    if query.outreach_status == "none":
        statement = statement.where(latest.c.outreach_status.is_(None))
    elif query.outreach_status:
        statement = statement.where(latest.c.outreach_status == query.outreach_status)
    if query.source:
        statement = statement.where(Vendor.source == query.source)
    if query.state:
        statement = statement.where(_contains(Vendor.state, _like(query.state)))
    if query.organisation:
        statement = statement.where(_contains(Tender.organisation, _like(query.organisation)))
    statement = _range(
        statement,
        func.loose_date(Tender.contract_date),
        loose_date(query.date_from),
        loose_date(query.date_to),
    )
    statement = _range(
        statement,
        func.loose_number(Tender.contract_value),
        loose_number(query.value_min),
        loose_number(query.value_max),
    )
    return statement


def _range(statement, column, low, high):
    """Keep a row whose value cannot be parsed, whatever the bounds are."""
    if low is None and high is None:
        return statement
    bounds = []
    if low is not None:
        bounds.append(column >= low)
    if high is not None:
        bounds.append(column <= high)
    return statement.where(or_(column.is_(None), and_(*bounds)))


def _totals(session: Session, query: AwardQuery, latest) -> tuple[int, ViewCounts]:
    has_email = Vendor.email.is_not(None) & (Vendor.email != "")
    has_phone = Vendor.phone.is_not(None) & (Vendor.phone != "")
    sent_ids = select(Outreach.vendor_id).where(Outreach.status == OUTREACH_SENT)
    statement = _joined(
        select(
            func.count(),
            func.count(func.distinct(Award.tender_id)),
            func.count(func.distinct(Vendor.vendor_id)),
            func.count(
                func.distinct(case((Vendor.enrichment_status == STATUS_DONE, Vendor.vendor_id)))
            ),
            func.count(func.distinct(case((has_email, Vendor.vendor_id)))),
            func.count(func.distinct(case((Vendor.vendor_id.in_(sent_ids), Vendor.vendor_id)))),
            func.count(func.distinct(case((and_(~has_email, has_phone), Vendor.vendor_id)))),
        ),
        latest,
    )
    statement = _filtered(statement, query, latest)
    awards, tenders, vendors, enriched, mailable, sent, phone_only = session.execute(
        statement
    ).one()
    return int(awards or 0), ViewCounts(
        tenders=int(tenders or 0),
        awards=int(awards or 0),
        vendors=int(vendors or 0),
        enriched=int(enriched or 0),
        mailable=int(mailable or 0),
        sent=int(sent or 0),
        phone_only=int(phone_only or 0),
    )


def _order(query: AwardQuery, latest, attempts):
    columns = _row_columns(latest, attempts)
    column = columns[query.sort]
    primary = column.desc() if query.direction == "desc" else column.asc()
    return (primary, Award.tender_id.asc(), Award.bid_number.asc())


def _contains(column, pattern: str):
    return func.lower(column).like(pattern, escape="\\")


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped.lower()}%"
