"""One award opened out: the tender, its awards, and that company's history."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    CHANNEL_EMAIL,
    CHANNEL_PHONE,
    CONTACT_PDF,
    Award,
    JobTender,
    LlmRun,
    Outreach,
    Tender,
    UiJob,
    Vendor,
)
from .money import format_amount, iso_day, parse_money


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
            Outreach.job_id,
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


def save_opened_award(
    session: Session,
    *,
    tender_id: str,
    bid_number: str,
    title: str,
    organisation: str,
    status: str,
    contract_date: str,
    contract_value: str,
    contract_currency: str,
    rank: str,
    quoted_value: str,
    awarded_value: str,
    awarded_currency: str,
    work_title: str,
) -> None:
    """Save the tender and the opened award. The tender id and bid stay fixed."""
    award = session.get(Award, (tender_id, bid_number))
    tender = session.get(Tender, tender_id)
    if award is None or tender is None:
        raise LookupError("No award is stored for that tender and bid.")
    tender.title = title.strip() or None
    tender.organisation = organisation.strip() or None
    tender.status = status.strip() or None
    tender.contract_date = _day(contract_date, "Contract date")
    amount, code = _amount(contract_value, "Contract value")
    tender.contract_value = amount
    tender.contract_currency = contract_currency.strip() or code
    award.rank = rank.strip() or None
    award.quoted_value = _amount(quoted_value, "Quoted value")[0]
    awarded, awarded_code = _amount(awarded_value, "Awarded value")
    award.awarded_value = awarded
    award.awarded_currency = awarded_currency.strip() or awarded_code
    award.work_title = work_title.strip() or None


def _day(value: str, label: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    day = iso_day(text)
    if day is None:
        raise ValueError(f"{label} is not a date.")
    return day


def _amount(value: str, label: str) -> tuple[int | None, str | None]:
    text = value.strip()
    if not text:
        return None, None
    amount, code = parse_money(text)
    if amount is None:
        raise ValueError(f"{label} is not an amount.")
    return amount, code


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


#: How the award page names a run's stage.
JOB_STAGE_LABELS = {
    "scrape": "Scrape",
    "pdfs": "Download PDFs",
    "enrich": "Enrich",
    "outreach": "Mail",
    "status": "Status",
}


def record_jobs(session: Session, vendor_id: int, tender_id: str) -> tuple[dict[str, Any], ...]:
    """Every window run that touched this award, newest first.

    A scrape or PDF download that saved the tender, an enrich run that looked
    the company up, and a mail run (preview or send) that named this award.
    Each links to the run opened on this one record.
    """
    did: dict[int, list[str]] = {}

    def note(job_id: int | None, text: str) -> None:
        if job_id is not None and text not in did.setdefault(int(job_id), []):
            did[int(job_id)].append(text)

    tender = session.get(Tender, tender_id)
    saved = set(
        session.scalars(select(JobTender.job_id).where(JobTender.tender_id == tender_id))
    )
    if tender is not None and tender.scrape_job_id is not None:
        saved.add(int(tender.scrape_job_id))
    stages = {
        int(job_id): stage
        for job_id, stage in session.execute(
            select(UiJob.job_id, UiJob.stage).where(UiJob.job_id.in_(saved))
        )
    }
    for job_id in saved:
        if stages.get(job_id) == "pdfs":
            note(job_id, "downloaded this tender's PDFs and read their clues")
        elif tender is not None and tender.scrape_job_id == job_id:
            note(job_id, "first saved this tender")
        else:
            note(job_id, "saved this tender again")

    looked_up: dict[int, list[str]] = {}
    for job_id, status in session.execute(
        select(LlmRun.job_id, LlmRun.status)
        .where(LlmRun.vendor_id == vendor_id, LlmRun.job_id.is_not(None))
        .order_by(LlmRun.run_id)
    ):
        looked_up.setdefault(int(job_id), []).append(status)
    for job_id, statuses in looked_up.items():
        note(job_id, "looked up the company's contact (" + ", ".join(statuses) + ")")

    pair = f"{vendor_id}:{tender_id}"
    for job_id, raw in session.execute(
        select(UiJob.job_id, UiJob.params_json).where(UiJob.stage == "outreach")
    ):
        params = _answer(raw)
        if pair not in [str(item) for item in params.get("awards") or []]:
            continue
        if params.get("preflight"):
            continue
        note(job_id, "sent mail for this award" if params.get("send") else "previewed mail for this award")
    for job_id, email, status in session.execute(
        select(Outreach.job_id, Outreach.email, Outreach.status)
        .where(
            Outreach.vendor_id == vendor_id,
            Outreach.tender_id == tender_id,
            Outreach.job_id.is_not(None),
        )
        .order_by(Outreach.outreach_id)
    ):
        note(job_id, f"{status} {email}")

    if not did:
        return ()
    jobs = {
        int(job.job_id): job
        for job in session.scalars(select(UiJob).where(UiJob.job_id.in_(list(did))))
    }
    shown = []
    for job_id in sorted(did, reverse=True):
        job = jobs.get(job_id)
        stage = job.stage if job is not None else ""
        shown.append(
            {
                "job_id": job_id,
                "stage": JOB_STAGE_LABELS.get(stage, stage or "Run"),
                "state": job.state if job is not None else "not recorded",
                "started_at": job.started_at if job is not None else "",
                "did": "; ".join(did[job_id]),
                "href": f"/jobs/{job_id}?award={vendor_id}:{tender_id}",
            }
        )
    return tuple(shown)


def tender_clues(session: Session, vendor_id: int, tender_id: str) -> list[dict[str, Any]]:
    """The PDF email and phone rows of this award, each with the files that hold it."""
    from .queries import clue_files, ordered_contacts

    shown: list[dict[str, Any]] = []
    for channel in (CHANNEL_EMAIL, CHANNEL_PHONE):
        files = clue_files(session, [tender_id], channel)
        for row in ordered_contacts(session, vendor_id, tender_id, channel=channel):
            if row.source != CONTACT_PDF or row.tender_id != tender_id:
                continue
            shown.append(
                {
                    "channel": channel,
                    "value": row.value,
                    "valid": bool(row.valid),
                    "filenames": files.get((tender_id, row.value.casefold()), ()),
                }
            )
    return shown


def vendor_documents(
    session: Session, vendor_id: int, *, except_tender: str | None = None
) -> list[dict[str, Any]]:
    """The company's other tenders and what their PDFs said. One entry per tender.

    ``attributed`` is false when the tender had several winners: its clues were
    then not given to any one company.
    """
    from .queries import documents_for_tenders

    awards = session.execute(
        select(Award.tender_id, Award.bid_number, Tender.title)
        .join(Tender, Tender.tender_id == Award.tender_id)
        .where(Award.vendor_id == vendor_id)
        .order_by(Tender.scraped_at.desc(), Award.tender_id)
    ).all()
    ids = [tender_id for tender_id, _bid, _title in awards if tender_id != except_tender]
    documents = documents_for_tenders(session, ids)
    winners = {
        tender_id: int(count)
        for tender_id, count in session.execute(
            select(Award.tender_id, func.count(func.distinct(Award.vendor_id)))
            .where(Award.tender_id.in_(ids))
            .group_by(Award.tender_id)
        )
    }
    shown = []
    for tender_id, bid_number, title in awards:
        if tender_id == except_tender or tender_id in {item["tender_id"] for item in shown}:
            continue
        shown.append(
            {
                "tender_id": tender_id,
                "bid_number": bid_number,
                "title": title or "",
                "documents": documents.get(tender_id, []),
                "attributed": winners.get(tender_id, 1) == 1,
            }
        )
    return shown


def award_addresses(session: Session, vendor_id: int, tender_id: str) -> list[dict[str, Any]]:
    """Every email and phone of the company for this award, with where it came from.

    An email also says whether a send for this award mails it, and why not.
    """
    from .queries import address_report, clue_files, ordered_contacts

    lines = address_report(session, vendor_id, tender_id)
    phones = ordered_contacts(session, vendor_id, tender_id, channel=CHANNEL_PHONE)
    phone_files = clue_files(
        session,
        (row.tender_id for row in phones if row.source == CONTACT_PDF),
        CHANNEL_PHONE,
    )
    run_ids = [line.llm_run_id for line in lines if line.llm_run_id] + [
        row.llm_run_id for row in phones if row.llm_run_id
    ]
    jobs = {}
    if run_ids:
        jobs = {
            int(run_id): job_id
            for run_id, job_id in session.execute(
                select(LlmRun.run_id, LlmRun.job_id).where(LlmRun.run_id.in_(run_ids))
            )
        }
    shown = [
        {
            "channel": CHANNEL_EMAIL,
            "value": line.value,
            "source": line.source,
            "tender_id": line.tender_id,
            "this_tender": line.tender_id == tender_id,
            "filenames": line.filenames,
            "source_url": line.source_url or "",
            "job_id": jobs.get(line.llm_run_id) if line.llm_run_id else None,
            "valid": line.valid,
            "will_mail": line.will_mail,
            "reason": line.reason,
            "contact_id": line.contact_id,
        }
        for line in lines
    ]
    for row in phones:
        shown.append(
            {
                "channel": CHANNEL_PHONE,
                "value": row.value,
                "source": row.source,
                "tender_id": row.tender_id,
                "this_tender": row.tender_id == tender_id,
                "filenames": phone_files.get((row.tender_id, row.value.casefold()), ())
                if row.tender_id
                else (),
                "source_url": row.source_url or "",
                "job_id": jobs.get(row.llm_run_id) if row.llm_run_id else None,
                "valid": bool(row.valid),
                "will_mail": False,
                "reason": "",
                "contact_id": row.contact_id,
            }
        )
    return shown
