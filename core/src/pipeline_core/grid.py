"""One row per award, filtered and paged in the database.

The window and its export both call this. Amounts are integer hundredths and
calendar dates are ISO text, so a filter compares the column. A blank date
stays in a date range. A blank amount does not stay in a value range.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, false, func, or_, select
from sqlalchemy.orm import Session

from .loose import loose_date
from .money import format_amount, parse_money
from .models import (
    ENRICHMENT_STATUSES,
    OUTREACH_FAILED,
    OUTREACH_SENT,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NOT_FOUND,
    STATUS_PENDING,
    Award,
    Outreach,
    Tender,
    Vendor,
)

PAGE_SIZE = 50

#: 28 states and 8 union territories. The state filter matches these exactly.
INDIA_STATES: tuple[str, ...] = (
    "Andhra Pradesh",
    "Arunachal Pradesh",
    "Assam",
    "Bihar",
    "Chhattisgarh",
    "Goa",
    "Gujarat",
    "Haryana",
    "Himachal Pradesh",
    "Jharkhand",
    "Karnataka",
    "Kerala",
    "Madhya Pradesh",
    "Maharashtra",
    "Manipur",
    "Meghalaya",
    "Mizoram",
    "Nagaland",
    "Odisha",
    "Punjab",
    "Rajasthan",
    "Sikkim",
    "Tamil Nadu",
    "Telangana",
    "Tripura",
    "Uttar Pradesh",
    "Uttarakhand",
    "West Bengal",
    "Andaman and Nicobar Islands",
    "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi",
    "Jammu and Kashmir",
    "Ladakh",
    "Lakshadweep",
    "Puducherry",
)

COLUMNS: tuple[tuple[str, str, str, bool], ...] = (
    # key, label, group, evidence (PDF clue, not a mail address)
    ("tender_id", "Tender", "Tender", False),
    ("title", "Title", "Tender", False),
    ("organisation", "Organisation", "Tender", False),
    ("status", "Status", "Tender", False),
    ("contract_date", "Contract date", "Tender", False),
    ("contract_value", "Contract value", "Tender", False),
    ("contract_currency", "Contract currency", "Tender", False),
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
SEARCH_FIELDS = ("name", "vendor_id", "city")


def _status_set(value: object) -> tuple[str, ...]:
    """Keep each real enrichment status once. A string or a list both work."""
    if value is None:
        return ()
    if isinstance(value, str):
        parts = [value] if value else []
    else:
        parts = list(value)
    seen: list[str] = []
    for item in parts:
        text = str(item).strip()
        if text in ENRICHMENT_STATUSES and text not in seen:
            seen.append(text)
    return tuple(seen)


def _search_set(value: object) -> tuple[str, ...]:
    """Keep each search field once. Empty means every column."""
    if value is None:
        return ()
    if isinstance(value, str):
        parts = [value] if value else []
    else:
        parts = list(value)
    seen: list[str] = []
    for item in parts:
        text = str(item).strip()
        if text in SEARCH_FIELDS and text not in seen:
            seen.append(text)
    return tuple(seen)


@dataclass(frozen=True)
class AwardQuery:
    """What the operator typed. Empty strings mean "no filter"."""

    text: str = ""
    tender_status: str = ""
    enrichment_status: tuple[str, ...] = ()
    outreach_status: str = ""
    source: str = ""
    state: str = ""
    organisation: str = ""
    date_from: str = ""
    date_to: str = ""
    scraped_from: str = ""
    scraped_to: str = ""
    value_min: str = ""
    value_max: str = ""
    #: "" any, "yes" has an email, "no" does not. Same meaning as the mailable count.
    mailable: str = ""
    #: "" any, "yes" both PDF email and PDF phone are non-empty.
    pdf_contact: str = ""
    #: Empty means search every text column. Otherwise name, vendor_id, and/or city.
    search_in: tuple[str, ...] = ()
    sort: str = "tender_id"
    direction: str = "asc"
    page: int = 1

    def normalized(self) -> AwardQuery:
        sort = self.sort if self.sort in _SORT_KEYS else "tender_id"
        direction = "desc" if self.direction == "desc" else "asc"
        page = self.page if self.page >= 1 else 1
        enrichment = _status_set(self.enrichment_status)
        outreach = (
            self.outreach_status
            if self.outreach_status in {OUTREACH_SENT, OUTREACH_FAILED, "none"}
            else ""
        )
        mailable = self.mailable if self.mailable in {"yes", "no"} else ""
        pdf_contact = "yes" if self.pdf_contact == "yes" else ""
        search_in = _search_set(self.search_in)
        scraped_from = _iso_day(self.scraped_from)
        scraped_to = _iso_day(self.scraped_to)
        if scraped_from and scraped_to and scraped_from > scraped_to:
            scraped_from, scraped_to = scraped_to, scraped_from
        return replace(
            self,
            sort=sort,
            direction=direction,
            page=page,
            enrichment_status=enrichment,
            outreach_status=outreach,
            mailable=mailable,
            pdf_contact=pdf_contact,
            search_in=search_in,
            scraped_from=scraped_from,
            scraped_to=scraped_to,
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
                self.scraped_from,
                self.scraped_to,
                self.value_min,
                self.value_max,
                self.mailable,
                self.pdf_contact,
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
    pending: int = 0
    not_found: int = 0
    failed: int = 0


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
    known = {name.casefold() for name in INDIA_STATES}
    stored = [
        value
        for value in session.scalars(select(Vendor.state).distinct())
        if value and value.casefold() not in known
    ]
    return {
        "tender_status": tender_status,
        "source": source,
        "state": [*INDIA_STATES, *sorted(stored, key=str.casefold)],
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
    specific, legacy = _outreach_ranks()
    total, counts = _totals(session, query, specific, legacy)
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

    columns = _row_columns(specific, legacy)
    statement = _joined(select(*columns.values()), specific, legacy)
    statement = _filtered(statement, query, specific, legacy)
    statement = statement.order_by(*_order(query, specific, legacy))
    if limit is not None:
        statement = statement.limit(limit).offset(offset)

    rows = [_shown(dict(zip(columns, row, strict=True))) for row in session.execute(statement)]
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


def filtered_vendor_ids(session: Session, query: AwardQuery) -> list[int]:
    """Distinct companies in this filter, on every page. One winner is one id."""
    query = query.normalized()
    specific, legacy = _outreach_ranks()
    statement = _joined(select(Vendor.vendor_id), specific, legacy)
    statement = _filtered(statement, query, specific, legacy).distinct().order_by(
        Vendor.vendor_id
    )
    return [int(value) for value in session.scalars(statement)]


def filtered_award_keys(session: Session, query: AwardQuery) -> list[tuple[int, str]]:
    """Every award in this filter, on every page. One company on two tenders is two."""
    query = query.normalized()
    specific, legacy = _outreach_ranks()
    statement = _joined(
        select(Vendor.vendor_id, Award.tender_id), specific, legacy
    )
    statement = (
        _filtered(statement, query, specific, legacy)
        .distinct()
        .order_by(Award.tender_id, Vendor.vendor_id)
    )
    return [(int(vendor_id), tender_id) for vendor_id, tender_id in session.execute(statement)]


def _outreach_ranks():
    """Latest attempt that names a tender, and the latest company-level attempt."""
    specific = (
        select(
            Outreach.vendor_id.label("vendor_id"),
            Outreach.tender_id.label("tender_id"),
            Outreach.status.label("status"),
            Outreach.sent_at.label("sent_at"),
            Outreach.transport.label("transport"),
            Outreach.error.label("error"),
            func.row_number()
            .over(
                partition_by=(Outreach.vendor_id, Outreach.tender_id),
                order_by=(Outreach.sent_at.desc(), Outreach.outreach_id.desc()),
            )
            .label("rn"),
        )
        .where(Outreach.tender_id.is_not(None))
        .subquery("outreach_for_tender")
    )
    legacy = (
        select(
            Outreach.vendor_id.label("vendor_id"),
            Outreach.status.label("status"),
            Outreach.sent_at.label("sent_at"),
            Outreach.transport.label("transport"),
            Outreach.error.label("error"),
            func.row_number()
            .over(
                partition_by=Outreach.vendor_id,
                order_by=(Outreach.sent_at.desc(), Outreach.outreach_id.desc()),
            )
            .label("rn"),
        )
        .where(Outreach.tender_id.is_(None))
        .subquery("outreach_for_company")
    )
    return specific, legacy


def _prefer(specific, legacy, field: str):
    """The later of the tender attempt and a company-level attempt that covers it."""
    spec = getattr(specific.c, field)
    old = getattr(legacy.c, field)
    return case(
        (specific.c.sent_at.is_(None), old),
        (legacy.c.sent_at.is_(None), spec),
        (specific.c.sent_at >= legacy.c.sent_at, spec),
        else_=old,
    )


def _attempt_total():
    named = (
        select(func.count())
        .select_from(Outreach)
        .where(
            Outreach.vendor_id == Vendor.vendor_id,
            Outreach.tender_id == Award.tender_id,
        )
        .scalar_subquery()
    )
    covered = (
        select(func.count())
        .select_from(Outreach)
        .where(
            Outreach.vendor_id == Vendor.vendor_id,
            Outreach.tender_id.is_(None),
            Outreach.sent_at.is_not(None),
            Tender.scraped_at <= Outreach.sent_at,
        )
        .scalar_subquery()
    )
    return named + covered


def _row_columns(specific, legacy) -> dict[str, Any]:
    return {
        "tender_id": Tender.tender_id,
        "title": Tender.title,
        "organisation": Tender.organisation,
        "status": Tender.status,
        "contract_date": Tender.contract_date,
        "contract_value": Tender.contract_value,
        "contract_currency": Tender.contract_currency,
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
        "outreach_status": _prefer(specific, legacy, "status"),
        "last_attempt_at": _prefer(specific, legacy, "sent_at"),
        "attempts": _attempt_total(),
        "transport": _prefer(specific, legacy, "transport"),
        "error": _prefer(specific, legacy, "error"),
        "json_path": Tender.json_path,
    }


def _joined(statement, specific, legacy):
    return (
        statement.select_from(Award)
        .join(Tender, Tender.tender_id == Award.tender_id)
        .join(Vendor, Vendor.vendor_id == Award.vendor_id)
        .outerjoin(
            specific,
            and_(
                specific.c.vendor_id == Vendor.vendor_id,
                specific.c.tender_id == Award.tender_id,
                specific.c.rn == 1,
            ),
        )
        .outerjoin(
            legacy,
            and_(
                legacy.c.vendor_id == Vendor.vendor_id,
                legacy.c.rn == 1,
                Tender.scraped_at <= legacy.c.sent_at,
            ),
        )
    )


def _filtered(statement, query: AwardQuery, specific, legacy):
    if query.text:
        statement = statement.where(_search_clause(query))
    if query.tender_status:
        statement = statement.where(Tender.status == query.tender_status)
    if query.enrichment_status:
        statement = statement.where(
            Vendor.enrichment_status.in_(query.enrichment_status)
        )
    status = _prefer(specific, legacy, "status")
    if query.outreach_status == "none":
        statement = statement.where(status.is_(None))
    elif query.outreach_status:
        statement = statement.where(status == query.outreach_status)
    if query.source:
        statement = statement.where(Vendor.source == query.source)
    if query.state:
        statement = statement.where(func.lower(Vendor.state) == query.state.strip().lower())
    has_email = Vendor.email.is_not(None) & (Vendor.email != "")
    if query.mailable == "yes":
        statement = statement.where(has_email)
    elif query.mailable == "no":
        statement = statement.where(~has_email)
    if query.pdf_contact == "yes":
        has_pdf_email = Vendor.pdf_email.is_not(None) & (Vendor.pdf_email != "")
        has_pdf_phone = Vendor.pdf_phone.is_not(None) & (Vendor.pdf_phone != "")
        statement = statement.where(has_pdf_email & has_pdf_phone)
    if query.organisation:
        statement = statement.where(_contains(Tender.organisation, _like(query.organisation)))
    statement = _range(
        statement,
        Tender.contract_date,
        loose_date(query.date_from),
        loose_date(query.date_to),
    )
    statement = _range(
        statement,
        Tender.contract_value,
        _money_bound(query.value_min),
        _money_bound(query.value_max),
        keep_unparsed=False,
    )
    scraped_day = func.substr(Tender.scraped_at, 1, 10)
    if query.scraped_from:
        statement = statement.where(scraped_day >= query.scraped_from)
    if query.scraped_to:
        statement = statement.where(scraped_day <= query.scraped_to)
    return statement


def _iso_day(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def _range(statement, column, low, high, *, keep_unparsed: bool = True):
    """Apply inclusive bounds.

    A contract date that cannot be parsed stays in the list. A contract value
    that cannot be parsed does not: a high minimum is only known amounts.
    """
    if low is None and high is None:
        return statement
    bounds = []
    if low is not None:
        bounds.append(column >= low)
    if high is not None:
        bounds.append(column <= high)
    matched = and_(*bounds)
    if keep_unparsed:
        return statement.where(or_(column.is_(None), matched))
    return statement.where(matched)


def _totals(session: Session, query: AwardQuery, specific, legacy) -> tuple[int, ViewCounts]:
    has_email = Vendor.email.is_not(None) & (Vendor.email != "")
    has_phone = Vendor.phone.is_not(None) & (Vendor.phone != "")
    sent_award = _prefer(specific, legacy, "status") == OUTREACH_SENT
    statement = _joined(
        select(
            func.count(),
            func.count(func.distinct(Award.tender_id)),
            func.count(func.distinct(Vendor.vendor_id)),
            func.count(
                func.distinct(case((Vendor.enrichment_status == STATUS_DONE, Vendor.vendor_id)))
            ),
            func.count(func.distinct(case((has_email, Vendor.vendor_id)))),
            func.sum(case((sent_award, 1), else_=0)),
            func.count(func.distinct(case((and_(~has_email, has_phone), Vendor.vendor_id)))),
            func.sum(case((Vendor.enrichment_status == STATUS_PENDING, 1), else_=0)),
            func.sum(case((Vendor.enrichment_status == STATUS_NOT_FOUND, 1), else_=0)),
            func.sum(case((Vendor.enrichment_status == STATUS_FAILED, 1), else_=0)),
        ),
        specific,
        legacy,
    )
    statement = _filtered(statement, query, specific, legacy)
    awards, tenders, vendors, enriched, mailable, sent, phone_only, pending, not_found, failed = (
        session.execute(statement).one()
    )
    return int(awards or 0), ViewCounts(
        tenders=int(tenders or 0),
        awards=int(awards or 0),
        vendors=int(vendors or 0),
        enriched=int(enriched or 0),
        mailable=int(mailable or 0),
        sent=int(sent or 0),
        phone_only=int(phone_only or 0),
        pending=int(pending or 0),
        not_found=int(not_found or 0),
        failed=int(failed or 0),
    )


def _shown(row: dict) -> dict:
    """Hundredths become a number. The currency stays in its own column."""
    for key in ("contract_value", "quoted_value", "awarded_value"):
        row[key] = format_amount(row.get(key))
    return row


def _money_bound(text: str) -> int | None:
    """A typed rupee amount, as hundredths. ``1000000`` is ten lakh rupees."""
    if not text:
        return None
    hundredths, _currency = parse_money(text)
    return hundredths


def _order(query: AwardQuery, specific, legacy):
    columns = _row_columns(specific, legacy)
    column = columns[query.sort]
    primary = column.desc() if query.direction == "desc" else column.asc()
    blank_last = case((column.is_(None), 1), else_=0)
    return (blank_last.asc(), primary, Award.tender_id.asc(), Award.bid_number.asc())


def _search_clause(query: AwardQuery):
    """Any searches every text column. A chosen field searches only that column."""
    pattern = _like(query.text)
    everything = (
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
    if not query.search_in:
        return or_(*everything)
    clauses = []
    if "name" in query.search_in:
        clauses.append(_contains(Vendor.name_raw, pattern))
    if "city" in query.search_in:
        clauses.append(_contains(Vendor.city, pattern))
    if "vendor_id" in query.search_in:
        typed = query.text.strip()
        if typed.isdigit():
            clauses.append(Vendor.vendor_id == int(typed))
        else:
            clauses.append(false())
    return or_(*clauses)


def _contains(column, pattern: str):
    return func.lower(column).like(pattern, escape="\\")


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped.lower()}%"
