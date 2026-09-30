"""A tender typed by hand. Identity only: an existing contact is left alone."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .models import SOURCE_MANUAL, Tender
from .queries import upsert_tender, upsert_vendor, utcnow, vendor_by_name, replace_awards


class NeedsAttach(Exception):
    """At least one company name is already stored. Ask before attaching."""

    def __init__(self, matches: list[NameMatch]) -> None:
        self.matches = matches
        super().__init__("company already stored")


@dataclass(frozen=True)
class WinnerInput:
    name: str
    bid_number: str
    rank: str = ""
    quoted_value: str = ""
    awarded_value: str = ""
    awarded_currency: str = ""
    work_title: str = ""


@dataclass(frozen=True)
class NameMatch:
    typed_name: str
    vendor_id: int
    stored_name: str


def matching_companies(session: Session, winners: list[WinnerInput]) -> list[NameMatch]:
    found: list[NameMatch] = []
    seen: set[int] = set()
    for winner in winners:
        vendor = vendor_by_name(session, winner.name)
        if vendor is None or vendor.vendor_id in seen:
            continue
        seen.add(vendor.vendor_id)
        found.append(
            NameMatch(
                typed_name=winner.name,
                vendor_id=int(vendor.vendor_id),
                stored_name=vendor.name_raw,
            )
        )
    return found


def save_manual_tender(
    session: Session,
    *,
    tender_id: str,
    title: str,
    organisation: str,
    status: str,
    contract_date: str,
    contract_value: str,
    winners: list[WinnerInput],
    attach: bool,
) -> str:
    """Write the tender and its awards. Raises NeedsAttach when a name exists."""
    if not winners:
        raise ValueError("Type at least one company.")
    bids = [winner.bid_number.strip() or "1" for winner in winners]
    if len(bids) != len(set(bids)):
        raise ValueError("Each winner needs its own bid number.")
    matches = matching_companies(session, winners)
    if matches and not attach:
        raise NeedsAttach(matches)

    chosen = (tender_id or "").strip() or _fresh_id(session)
    upsert_tender(
        session,
        tender_id=chosen,
        title=title.strip() or None,
        organisation=organisation.strip() or None,
        status=status.strip() or None,
        contract_date=contract_date.strip() or None,
        contract_value=contract_value.strip() or None,
        scraped_at=utcnow(),
    )
    rows = []
    for winner, bid in zip(winners, bids, strict=True):
        existing = vendor_by_name(session, winner.name)
        if existing is None:
            vendor_id = upsert_vendor(session, name_raw=winner.name.strip(), source=SOURCE_MANUAL)
        else:
            vendor_id = int(existing.vendor_id)
        rows.append(
            {
                "bid_number": bid,
                "vendor_id": vendor_id,
                "bidder_name": winner.name.strip(),
                "rank": winner.rank.strip() or None,
                "quoted_value": winner.quoted_value.strip() or None,
                "awarded_value": winner.awarded_value.strip() or None,
                "awarded_currency": winner.awarded_currency.strip() or None,
                "contract_date": contract_date.strip() or None,
                "contract_value": contract_value.strip() or None,
                "work_title": winner.work_title.strip() or title.strip() or None,
            }
        )
    replace_awards(session, chosen, rows)
    return chosen


def _fresh_id(session: Session) -> str:
    for _ in range(5):
        candidate = "MANUAL-" + secrets.token_hex(4).upper()
        if session.get(Tender, candidate) is None:
            return candidate
    raise RuntimeError("Could not choose a manual tender id.")
