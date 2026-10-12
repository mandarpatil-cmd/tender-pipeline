"""One award opened out: the tender, its awards, and that company's history."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Award, LlmRun, Outreach, Tender, Vendor
from .money import format_amount


@dataclass(frozen=True)
class AwardDetail:
    tender: dict[str, Any]
    vendor: dict[str, Any]
    awards: tuple[dict[str, Any], ...]
    runs: tuple[dict[str, Any], ...]
    attempts: tuple[dict[str, Any], ...]
    focus_bid: str


def award_detail(session: Session, tender_id: str, bid_number: str) -> AwardDetail | None:
    """The clicked award, or None when that tender and bid are not stored."""
    award = session.get(Award, (tender_id, bid_number))
    if award is None:
        return None
    tender = session.get(Tender, tender_id)
    vendor = session.get(Vendor, award.vendor_id)
    if tender is None or vendor is None:
        return None

    award_rows = session.execute(
        select(
            Award.bid_number,
            Award.rank,
            Award.quoted_value,
            Award.awarded_value,
            Award.awarded_currency,
            Award.work_title,
            Award.vendor_id,
            Vendor.name_raw,
        )
        .join(Vendor, Vendor.vendor_id == Award.vendor_id)
        .where(Award.tender_id == tender_id)
        .order_by(Award.bid_number)
    ).mappings()

    runs = session.execute(
        select(
            LlmRun.run_id,
            LlmRun.model,
            LlmRun.prompt_version,
            LlmRun.status,
            LlmRun.error,
            LlmRun.created_at,
            LlmRun.output_json,
            LlmRun.job_id,
        )
        .where(LlmRun.vendor_id == vendor.vendor_id)
        .order_by(LlmRun.created_at.desc(), LlmRun.run_id.desc())
    ).mappings()

    attempts = session.execute(
        select(
            Outreach.outreach_id,
            Outreach.tender_id,
            Outreach.email,
            Outreach.subject,
            Outreach.transport,
            Outreach.status,
            Outreach.error,
            Outreach.sent_at,
        )
        .where(Outreach.vendor_id == vendor.vendor_id)
        .order_by(Outreach.sent_at.desc(), Outreach.outreach_id.desc())
    ).mappings()

    return AwardDetail(
        tender=_tender_dict(tender),
        vendor=_vendor_dict(vendor),
        awards=tuple(_shown_award(row) for row in award_rows),
        runs=tuple(_shown_run(row) for row in runs),
        attempts=tuple(dict(row) for row in attempts),
        focus_bid=bid_number,
    )


def _tender_dict(tender: Tender) -> dict[str, Any]:
    return {
        "tender_id": tender.tender_id,
        "title": tender.title,
        "organisation": tender.organisation,
        "status": tender.status,
        "contract_date": tender.contract_date,
        "contract_value": format_amount(tender.contract_value),
        "contract_currency": tender.contract_currency,
        "scraped_at": tender.scraped_at,
        "json_path": tender.json_path,
        "origin": tender.origin,
        "search_mode": tender.search_mode,
        "search_fields": _fields(tender.search_fields),
        "listing_page": tender.listing_page,
        "listing_serial": tender.listing_serial,
        "scrape_job_id": tender.scrape_job_id,
    }


def _fields(raw: str | None) -> dict[str, str]:
    """The stored portal fields. Text that is not a JSON object shows as nothing."""
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}


def _shown_run(row) -> dict[str, Any]:
    """One model call, with what the address rule left out of its answer."""
    from .emailcheck import usable_contact

    shown = dict(row)
    answer = _answer(row["output_json"]) if row["status"] == "ok" else {}
    shown["not_kept"] = usable_contact(
        _text(answer.get("email")), _text(answer.get("phone"))
    ).dropped
    return shown


def _answer(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _shown_award(row) -> dict[str, Any]:
    shown = dict(row)
    shown["quoted_value"] = format_amount(shown.get("quoted_value"))
    shown["awarded_value"] = format_amount(shown.get("awarded_value"))
    return shown


def _vendor_dict(vendor: Vendor) -> dict[str, Any]:
    return {
        "vendor_id": vendor.vendor_id,
        "name_raw": vendor.name_raw,
        "legal_form": vendor.legal_form,
        "city": vendor.city,
        "state": vendor.state,
        "source": vendor.source,
        "email": vendor.email,
        "phone": vendor.phone,
        "enrichment_status": vendor.enrichment_status,
        "enriched_at": vendor.enriched_at,
        "contact_origin": vendor.contact_origin,
        "pdf_email": vendor.pdf_email,
        "pdf_phone": vendor.pdf_phone,
        "do_not_mail": vendor.do_not_mail,
    }
