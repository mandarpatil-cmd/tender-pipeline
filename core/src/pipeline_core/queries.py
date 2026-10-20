"""The queries each stage runs, in one place.

Keeping them here rather than in the stages is what stops the three from
drifting apart again: the "work queue" and the "who is left" query each have
exactly one definition.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .emailcheck import clean_email
from .money import iso_day, parse_money
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_PHONE,
    CONTACT_MODEL,
    CONTACT_PDF,
    CONTACT_TYPED,
    ORIGIN_MANUAL,
    ORIGIN_PORTAL,
    OUTREACH_FAILED,
    OUTREACH_SENDING,
    OUTREACH_SENT,
    OUTREACH_TEST,
    SOURCE_SCRAPE,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NOT_FOUND,
    STATUS_PENDING,
    Award,
    JobTender,
    LlmRun,
    Outreach,
    Tender,
    TenderDocument,
    UiJob,
    Vendor,
    VendorContact,
)
from .naming import infer_legal_form, normalize_name


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Stage 1 — scrape
# --------------------------------------------------------------------------


def known_tender_ids(session: Session) -> set[str]:
    """The skip-list, so a re-run does not re-scrape what is already stored."""
    return set(session.scalars(select(Tender.tender_id)).all())


def last_scraped_at(session: Session) -> str | None:
    """When stage 1 last stored a tender, or None if it never has.

    Stage 1 is the only stage whose backlog cannot be counted -- what is waiting
    lives on the portal, not in here. This is the next best signal: how stale
    what we hold is.
    """
    return session.scalar(select(func.max(Tender.scraped_at)))


def upsert_tender(
    session: Session, *, keep: Iterable[str] = (), **values: Any
) -> None:
    """Insert or replace one tender. `tender_id` and `scraped_at` are required.

    A portal date and an amount string are parsed here. The row stores an ISO
    day and integer hundredths, never ``21-Sep-2026`` or ``INR 179,853.24``.

    A column named in ``keep`` keeps its stored value when it has one. The
    stored ``scrape_job_id`` is always kept: the first scrape run is the one
    that brought the tender in.
    """
    values = _stored_tender(values)
    statement = sqlite_insert(Tender).values(**values)
    kept = {*keep, "scrape_job_id"}
    assigned = {}
    for key in values:
        if key == "tender_id":
            continue
        incoming = getattr(statement.excluded, key)
        if key in kept:
            incoming = func.coalesce(getattr(Tender, key), incoming)
        assigned[key] = incoming
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[Tender.tender_id],
            set_=assigned,
        )
    )


def record_job_tender(session: Session, job_id: int | None, tender_id: str) -> None:
    """Note that this window run saved this tender. A repeat changes nothing."""
    if job_id is None or not (tender_id or "").strip():
        return
    session.execute(
        sqlite_insert(JobTender)
        .values(job_id=int(job_id), tender_id=tender_id)
        .on_conflict_do_nothing()
    )


def _job_params(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def backfill_job_tenders(session: Session) -> int:
    """Fill ``job_tenders`` from what earlier runs left. Returns rows written.

    A scrape run is known from ``tenders.scrape_job_id``, a PDF download from
    the tender id in its parameters. A PDF download that stamped itself as a
    tender's scrape run is moved here, and that run is cleared: what scrape run
    first saved the tender was not recorded.
    """
    stages = {int(job_id): stage for job_id, stage in session.execute(select(UiJob.job_id, UiJob.stage))}
    written = 0
    for tender_id, job_id in session.execute(
        select(Tender.tender_id, Tender.scrape_job_id).where(Tender.scrape_job_id.is_not(None))
    ).all():
        record_job_tender(session, job_id, tender_id)
        written += 1
        if stages.get(int(job_id), "scrape") != "scrape":
            session.query(Tender).filter(Tender.tender_id == tender_id).update(
                {Tender.scrape_job_id: None}, synchronize_session=False
            )
    known = known_tender_ids(session)
    for job_id, raw in session.execute(
        select(UiJob.job_id, UiJob.params_json).where(UiJob.stage == "pdfs")
    ).all():
        tender_id = str(_job_params(raw).get("tender_id") or "").strip()
        if tender_id in known:
            record_job_tender(session, job_id, tender_id)
            written += 1
    return written


def backfill_outreach_jobs(session: Session) -> int:
    """Stamp attempts made before ``outreach.job_id`` with the send that made them.

    An attempt belongs to a send when its company and tender were in that run's
    awards and it was written while the run was open. Returns rows stamped.
    """
    stamped = 0
    for job_id, raw, started, finished in session.execute(
        select(UiJob.job_id, UiJob.params_json, UiJob.started_at, UiJob.finished_at)
        .where(UiJob.stage == "outreach")
        .order_by(UiJob.job_id)
    ).all():
        params = _job_params(raw)
        if not params.get("send"):
            continue
        for item in params.get("awards") or []:
            vendor, separator, tender_id = str(item).partition(":")
            if not separator or not vendor.isdigit() or not tender_id:
                continue
            statement = (
                session.query(Outreach)
                .filter(
                    Outreach.job_id.is_(None),
                    Outreach.vendor_id == int(vendor),
                    Outreach.tender_id == tender_id,
                    Outreach.sent_at >= started,
                )
            )
            if finished:
                statement = statement.filter(Outreach.sent_at <= finished)
            stamped += int(
                statement.update({Outreach.job_id: int(job_id)}, synchronize_session=False)
            )
    return stamped


def backfill_tender_origin(session: Session) -> int:
    """Label tenders saved before ``origin`` existed. Returns how many changed.

    A ``MANUAL-`` id came from the window's form. Every other writer of
    ``tenders`` is the scraper. A hand-typed tender that was given a portal-style
    id is labelled portal: nothing stored can tell the two apart.
    """
    return int(
        session.query(Tender)
        .filter(Tender.origin.is_(None))
        .update(
            {
                Tender.origin: case(
                    (Tender.tender_id.like("MANUAL-%"), ORIGIN_MANUAL),
                    else_=ORIGIN_PORTAL,
                )
            },
            synchronize_session=False,
        )
    )


def upsert_vendor(
    session: Session,
    *,
    name_raw: str,
    city: str = "",
    state: str = "",
    buyer_hint: str = "",
    source: str = SOURCE_SCRAPE,
) -> int:
    """Insert a vendor, or refresh the identity columns of an existing one.

    Never touches the contact block or `do_not_mail` — `email`, `phone`,
    `enrichment_status` and `enriched_at` belong to stage 2, and the mail
    stop belongs to stage 3. City and state are only filled in when blank, so
    a better value already on the row is not overwritten.
    """
    statement = sqlite_insert(Vendor).values(
        name_raw=name_raw,
        name_norm=normalize_name(name_raw),
        legal_form=infer_legal_form(name_raw),
        city=city,
        state=state,
        buyer_hint=buyer_hint,
        source=source,
        enrichment_status=STATUS_PENDING,
    )
    excluded = statement.excluded
    result = session.execute(
        statement.on_conflict_do_update(
            index_elements=[Vendor.name_norm],
            set_={
                "city": func.coalesce(func.nullif(Vendor.city, ""), excluded.city),
                "state": func.coalesce(func.nullif(Vendor.state, ""), excluded.state),
                "buyer_hint": excluded.buyer_hint,
                "legal_form": excluded.legal_form,
                "source": func.coalesce(Vendor.source, excluded.source),
            },
        ).returning(Vendor.vendor_id)
    )
    return int(result.scalar_one())


def replace_awards(session: Session, tender_id: str, rows: Iterable[dict[str, Any]]) -> int:
    """Rewrite every award for one tender. Returns how many were written.

    Quoted and awarded amounts are parsed to hundredths. Contract date and
    contract value are not copied onto the award; the tender row holds them.
    """
    session.query(Award).filter(Award.tender_id == tender_id).delete(
        synchronize_session=False
    )
    written = 0
    for row in rows:
        session.execute(sqlite_insert(Award).values(tender_id=tender_id, **_stored_award(row)))
        written += 1
    return written


def _stored_tender(values: dict[str, Any]) -> dict[str, Any]:
    values = dict(values)
    if "contract_date" in values:
        values["contract_date"] = iso_day(values.get("contract_date"))
    if "contract_value" in values:
        explicit = str(values.get("contract_currency") or "").strip() or None
        hundredths, found = parse_money(values.get("contract_value"))
        values["contract_value"] = hundredths
        if hundredths is None:
            values["contract_currency"] = explicit
        else:
            values["contract_currency"] = explicit or found or "INR"
    return values


def _stored_award(row: dict[str, Any]) -> dict[str, Any]:
    explicit = str(row.get("awarded_currency") or "").strip() or None
    quoted, _quoted_currency = parse_money(row.get("quoted_value"))
    awarded, found = parse_money(row.get("awarded_value"))
    return {
        "bid_number": row.get("bid_number") or "",
        "vendor_id": row.get("vendor_id"),
        "bidder_name": row.get("bidder_name") or "",
        "rank": row.get("rank"),
        "quoted_value": quoted,
        "awarded_value": awarded,
        "awarded_currency": explicit or found,
        "work_title": row.get("work_title"),
    }


def join_clues(values: Iterable[Any] | None) -> str:
    """Semicolon-joined clues, in first-seen order. The shape the dashboard prints."""
    kept: list[str] = []
    for value in values or []:
        text = str(value).strip()
        if text and text not in kept:
            kept.append(text)
    return "; ".join(kept)


def split_clues(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(";") if part.strip()]


def _clue_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return split_clues(raw)
    return [str(value).strip() for value in (raw or []) if str(value).strip()]


def replace_tender_documents(
    session: Session, tender_id: str, extracts: Iterable[dict[str, Any]]
) -> int:
    """Rewrite every document row for one tender. Returns how many were written."""
    merged: dict[str, dict[str, list[str]]] = {}
    for item in extracts or []:
        filename = str(item.get("filename") or "").strip() or "document.pdf"
        bucket = merged.setdefault(
            filename,
            {"emails": [], "phones": [], "gstins": [], "text": "", "text_source": ""},
        )
        for key in ("emails", "phones", "gstins"):
            for value in _clue_list(item.get(key)):
                if key == "emails":
                    value = clean_email(value)
                if value and value not in bucket[key]:
                    bucket[key].append(value)
        if not bucket["text"] and item.get("text"):
            bucket["text"] = str(item.get("text") or "")
            bucket["text_source"] = str(item.get("text_source") or "")
    session.query(TenderDocument).filter(TenderDocument.tender_id == tender_id).delete(
        synchronize_session=False
    )
    for filename, clues in merged.items():
        session.execute(
            sqlite_insert(TenderDocument).values(
                tender_id=tender_id,
                filename=filename,
                emails=join_clues(clues["emails"]),
                phones=join_clues(clues["phones"]),
                gstins=join_clues(clues["gstins"]),
                text=clues["text"] or None,
                text_source=clues["text_source"] or None,
            )
        )
    return len(merged)


def documents_for_tenders(
    session: Session, tender_ids: Iterable[str]
) -> dict[str, list[dict[str, str]]]:
    """Document cards keyed by tender id. Strings are already semicolon-joined."""
    ids = [tender_id for tender_id in dict.fromkeys(tender_ids) if tender_id]
    if not ids:
        return {}
    rows = session.scalars(
        select(TenderDocument)
        .where(TenderDocument.tender_id.in_(ids))
        .order_by(TenderDocument.tender_id, TenderDocument.filename)
    )
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row.tender_id, []).append(
            {
                "filename": row.filename,
                "emails": row.emails or "",
                "phones": row.phones or "",
                "gstins": row.gstins or "",
            }
        )
    return grouped


def backfill_tender_documents(db_path: Path) -> int:
    """Copy `pdf_extracts` from each tender JSON into `tender_documents`.

    A tender that already has document rows is left alone. Returns how many
    tenders were filled. Missing files are skipped.
    """
    from .db import session as open_session
    from .db import engine

    filled = 0
    with open_session(engine(db_path)) as current:
        tenders = current.scalars(
            select(Tender).where(
                Tender.json_path.is_not(None),
                Tender.json_path != "",
            )
        ).all()
        for tender in tenders:
            already = current.scalar(
                select(func.count())
                .select_from(TenderDocument)
                .where(TenderDocument.tender_id == tender.tender_id)
            )
            if already:
                continue
            extracts = _extracts_from_json(tender.json_path)
            if not extracts:
                continue
            replace_tender_documents(current, tender.tender_id, extracts)
            filled += 1
    return filled


def _extracts_from_json(json_path: str | None) -> list[dict[str, Any]]:
    if not json_path:
        return []
    path = Path(json_path)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    extracts = payload.get("pdf_extracts") or []
    return list(extracts) if isinstance(extracts, list) else []


def vendor_count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(Vendor)) or 0)


# --------------------------------------------------------------------------
# Stage 2 — enrich
# --------------------------------------------------------------------------


def set_pdf_contacts(
    session: Session,
    vendor_id: int,
    *,
    email: str | None,
    phone: str | None,
) -> bool:
    """Fill the company's evidence columns. Fills blanks only.

    Returns True if anything was written. The scraper calls
    ``refresh_vendor_pdf_columns`` instead, which reads them from the PDF rows.
    These two columns are company-wide stage-2 evidence, never a tender's.
    """
    clean_email = (email or "").strip() or None
    clean_phone = (phone or "").strip() or None
    if not (clean_email or clean_phone):
        return False

    vendor = session.get(Vendor, vendor_id)
    if vendor is None:
        return False

    wrote = False
    if clean_email and not vendor.pdf_email:
        vendor.pdf_email = clean_email
        wrote = True
    if clean_phone and not vendor.pdf_phone:
        vendor.pdf_phone = clean_phone
        wrote = True
    return wrote


_SOURCE_ORDER = {CONTACT_PDF: 0, CONTACT_MODEL: 1, CONTACT_TYPED: 2}
ADDRESS_FIRST = "first"
ADDRESS_ALL = "all"
ADDRESS_CHOSEN = "chosen"


def contact_is_valid(channel: str, value: str) -> bool:
    from .emailcheck import is_email, is_phone

    if channel == CHANNEL_EMAIL:
        return is_email(value)
    if channel == CHANNEL_PHONE:
        return is_phone(value)
    return False


def add_contact(
    session: Session,
    vendor_id: int,
    *,
    channel: str,
    value: str | None,
    source: str,
    tender_id: str | None = None,
    source_url: str | None = None,
    llm_run_id: int | None = None,
) -> bool:
    """Insert one address unless this company already has that value from this source.

    A PDF address is kept per tender. An enrichment or typed address has no tender.
    ``source_url`` and ``llm_run_id`` say which model answer gave a ``model`` row.
    Returns True when a row was added.
    """
    text = (value or "").strip()
    if not text or channel not in {CHANNEL_EMAIL, CHANNEL_PHONE}:
        return False
    if source not in {CONTACT_PDF, CONTACT_MODEL, CONTACT_TYPED}:
        return False
    if channel == CHANNEL_EMAIL:
        text = clean_email(text)
        if not text:
            return False
    tender = (tender_id or "").strip() or None
    if source != CONTACT_PDF:
        tender = None
    folded = text.casefold()
    rows = session.scalars(
        select(VendorContact).where(
            VendorContact.vendor_id == vendor_id,
            VendorContact.channel == channel,
            VendorContact.source == source,
            VendorContact.tender_id == tender
            if tender
            else VendorContact.tender_id.is_(None),
        )
    ).all()
    if any(row.value.casefold() == folded for row in rows):
        return False
    session.add(
        VendorContact(
            vendor_id=vendor_id,
            tender_id=tender,
            channel=channel,
            value=text,
            source=source,
            valid=1 if contact_is_valid(channel, text) else 0,
            source_url=(source_url or "").strip() or None,
            llm_run_id=llm_run_id,
        )
    )
    session.flush()
    return True


def replace_company_contact(
    session: Session,
    vendor_id: int,
    *,
    source: str,
    channel: str,
    value: str | None,
    source_url: str | None = None,
    llm_run_id: int | None = None,
) -> None:
    """The new enrichment or typed value replaces only that source and channel."""
    replace_company_contacts(
        session,
        vendor_id,
        source=source,
        channel=channel,
        values=[value] if value else [],
        source_url=source_url,
        llm_run_id=llm_run_id,
    )


def replace_company_contacts(
    session: Session,
    vendor_id: int,
    *,
    source: str,
    channel: str,
    values: Iterable[str],
    source_url: str | None = None,
    llm_run_id: int | None = None,
) -> None:
    """Replace one source and channel with every value, in the order given."""
    session.query(VendorContact).filter(
        VendorContact.vendor_id == vendor_id,
        VendorContact.source == source,
        VendorContact.channel == channel,
        VendorContact.tender_id.is_(None),
    ).delete(synchronize_session=False)
    for value in values:
        add_contact(
            session,
            vendor_id,
            channel=channel,
            value=value,
            source=source,
            source_url=source_url,
            llm_run_id=llm_run_id,
        )


def record_pdf_contacts(
    session: Session,
    vendor_id: int,
    tender_id: str,
    emails: Iterable[str],
    phones: Iterable[str],
) -> int:
    """Append every PDF address. Earlier ones stay."""
    written = 0
    for email in emails:
        if add_contact(
            session,
            vendor_id,
            channel=CHANNEL_EMAIL,
            value=email,
            source=CONTACT_PDF,
            tender_id=tender_id,
        ):
            written += 1
    for phone in phones:
        if add_contact(
            session,
            vendor_id,
            channel=CHANNEL_PHONE,
            value=phone,
            source=CONTACT_PDF,
            tender_id=tender_id,
        ):
            written += 1
    return written


def replace_pdf_contacts(
    session: Session,
    vendor_id: int,
    tender_id: str,
    emails: Iterable[str],
    phones: Iterable[str],
) -> int:
    """This tender's PDF addresses for this company are exactly these now.

    Re-reading a tender's PDFs replaces what they said, so a clue the files no
    longer hold is not kept or mailed. Other tenders' PDF rows stay.
    """
    session.query(VendorContact).filter(
        VendorContact.vendor_id == vendor_id,
        VendorContact.source == CONTACT_PDF,
        VendorContact.tender_id == tender_id,
    ).delete(synchronize_session=False)
    return record_pdf_contacts(session, vendor_id, tender_id, emails, phones)


def clear_pdf_contacts(session: Session, tender_id: str) -> int:
    """Drop every company's PDF rows for one tender. Several winners share one file."""
    return int(
        session.query(VendorContact)
        .filter(
            VendorContact.source == CONTACT_PDF,
            VendorContact.tender_id == tender_id,
        )
        .delete(synchronize_session=False)
    )


def refresh_vendor_pdf_columns(session: Session, vendor_id: int) -> None:
    """``vendors.pdf_email`` and ``pdf_phone`` from the company's PDF rows.

    The first valid one across all its tenders. This is company-wide evidence
    for stage 2; the award page and the grid read the tender's own rows.
    """
    vendor = session.get(Vendor, vendor_id)
    if vendor is None:
        return
    session.flush()
    for channel, attribute in ((CHANNEL_EMAIL, "pdf_email"), (CHANNEL_PHONE, "pdf_phone")):
        first = session.scalar(
            select(VendorContact.value)
            .where(
                VendorContact.vendor_id == vendor_id,
                VendorContact.channel == channel,
                VendorContact.source == CONTACT_PDF,
                VendorContact.valid == 1,
            )
            .order_by(VendorContact.contact_id)
            .limit(1)
        )
        setattr(vendor, attribute, first)


def ordered_contacts(
    session: Session,
    vendor_id: int,
    tender_id: str,
    channel: str = CHANNEL_EMAIL,
) -> list[VendorContact]:
    """Every address the company has, in the order a letter uses them.

    PDF addresses read for this tender, then PDF addresses read for the
    company's other tenders, then a PDF address with no tender, then
    enrichment, then typed.
    """
    rows = session.scalars(
        select(VendorContact).where(
            VendorContact.vendor_id == vendor_id,
            VendorContact.channel == channel,
        )
    ).all()

    def rank(row: VendorContact) -> tuple[int, int, int]:
        if row.source != CONTACT_PDF:
            return (_SOURCE_ORDER.get(row.source, 9), 0, row.contact_id)
        if row.tender_id == tender_id:
            return (0, 0, row.contact_id)
        return (0, 1 if row.tender_id else 2, row.contact_id)

    rows.sort(key=rank)
    return rows


def _deduped(rows: list[VendorContact]) -> list[VendorContact]:
    seen: set[str] = set()
    kept: list[VendorContact] = []
    for row in rows:
        key = _mail_key(row.value)
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    return kept


def choose_addresses(
    session: Session,
    vendor_id: int,
    tender_id: str,
    *,
    mode: str,
    chosen_ids: set[int],
    fallback: str = "",
) -> list[str]:
    """Valid emails for one tender, in PDF, enrichment, typed order."""
    rows = _deduped(
        [row for row in ordered_contacts(session, vendor_id, tender_id) if row.valid]
    )
    if mode == ADDRESS_ALL:
        picked = rows
    elif mode == ADDRESS_CHOSEN:
        picked = [row for row in rows if row.contact_id in chosen_ids]
    else:
        picked = rows[:1]
    if picked:
        return [row.value for row in picked]
    if mode == ADDRESS_CHOSEN or not (fallback or "").strip():
        return []
    if not contact_is_valid(CHANNEL_EMAIL, clean_email(fallback)):
        return []
    return [clean_email(fallback)]


def _mail_key(value: str | None) -> str:
    """Two spellings of one mailbox, such as ``-a@x.com`` and ``A@x.com``, match."""
    return clean_email(value).casefold()


def clean_stored_contacts(session: Session) -> int:
    """Clean every stored email and re-judge every stored address. Safe to repeat.

    Removes what a scan or a model left at the edges (``-name@x.com``), merges a
    row that cleaning made a twin of another, and recomputes ``valid`` with the
    current rule. Returns how many rows changed.
    """
    changed = 0
    kept: set[tuple[Any, ...]] = set()
    for row in session.scalars(select(VendorContact).order_by(VendorContact.contact_id)).all():
        if row.channel == CHANNEL_EMAIL:
            value = clean_email(row.value)
        else:
            value = (row.value or "").strip()
        key = (row.vendor_id, row.tender_id, row.channel, row.source, value.casefold())
        if not value or key in kept:
            session.delete(row)
            changed += 1
            continue
        kept.add(key)
        valid = 1 if contact_is_valid(row.channel, value) else 0
        if value != row.value or valid != row.valid:
            row.value = value
            row.valid = valid
            changed += 1
    for vendor in session.scalars(
        select(Vendor).where(or_(Vendor.pdf_email.is_not(None), Vendor.email.is_not(None)))
    ).all():
        for attribute in ("pdf_email", "email"):
            raw = getattr(vendor, attribute)
            if raw and clean_email(raw) != raw:
                setattr(vendor, attribute, clean_email(raw) or None)
                changed += 1
    for doc in session.scalars(
        select(TenderDocument).where(
            TenderDocument.emails.is_not(None), TenderDocument.emails != ""
        )
    ).all():
        cleaned = join_clues(clean_email(value) for value in split_clues(doc.emails))
        if cleaned != doc.emails:
            doc.emails = cleaned
            changed += 1
    session.flush()
    return changed


def backfill_vendor_contacts(session: Session) -> int:
    """Copy addresses already stored into ``vendor_contacts``. Safe to repeat."""
    written = 0
    counts = session.execute(
        select(Award.tender_id, func.count(func.distinct(Award.vendor_id))).group_by(
            Award.tender_id
        )
    ).all()
    single = {tender_id for tender_id, count in counts if count == 1}
    winners = {}
    if single:
        winners = {
            tender_id: int(vendor_id)
            for tender_id, vendor_id in session.execute(
                select(Award.tender_id, Award.vendor_id).where(Award.tender_id.in_(single))
            )
        }
    for doc in session.scalars(select(TenderDocument)).all():
        vendor_id = winners.get(doc.tender_id)
        if vendor_id is None:
            continue
        written += record_pdf_contacts(
            session,
            vendor_id,
            doc.tender_id,
            split_clues(doc.emails),
            split_clues(doc.phones),
        )
    for vendor in session.scalars(select(Vendor)).all():
        for channel, raw in ((CHANNEL_EMAIL, vendor.pdf_email), (CHANNEL_PHONE, vendor.pdf_phone)):
            if not raw or _pdf_value_known(session, vendor.vendor_id, channel, raw):
                continue
            if add_contact(
                session,
                vendor.vendor_id,
                channel=channel,
                value=raw,
                source=CONTACT_PDF,
                tender_id=_tender_holding(session, vendor.vendor_id, channel, raw),
            ):
                written += 1
        source = vendor.contact_origin if vendor.contact_origin in {CONTACT_MODEL, CONTACT_TYPED} else CONTACT_MODEL
        if vendor.email and add_contact(
            session,
            vendor.vendor_id,
            channel=CHANNEL_EMAIL,
            value=vendor.email,
            source=source,
        ):
            written += 1
        if vendor.phone and add_contact(
            session,
            vendor.vendor_id,
            channel=CHANNEL_PHONE,
            value=vendor.phone,
            source=source,
        ):
            written += 1
    return written


def backfill_model_contact_sources(session: Session) -> int:
    """Link model addresses saved before ``source_url`` existed to their answer.

    Each takes the source and run id of the newest ``ok`` call for that company
    whose answer holds the same value. Returns how many rows were linked.
    """
    answers: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for run_id, vendor_id, output in session.execute(
        select(LlmRun.run_id, LlmRun.vendor_id, LlmRun.output_json)
        .where(LlmRun.status == "ok", LlmRun.output_json.is_not(None))
        .order_by(LlmRun.run_id.desc())
    ):
        try:
            answer = json.loads(output)
        except ValueError:
            continue
        if isinstance(answer, dict):
            answers.setdefault(int(vendor_id), []).append((int(run_id), answer))
    linked = 0
    rows = session.scalars(
        select(VendorContact).where(
            VendorContact.source == CONTACT_MODEL, VendorContact.llm_run_id.is_(None)
        )
    ).all()
    for row in rows:
        for run_id, answer in answers.get(row.vendor_id, []):
            given = str(answer.get(row.channel) or "").strip()
            if given.casefold() == row.value.strip().casefold():
                row.llm_run_id = run_id
                row.source_url = str(answer.get("source") or "").strip() or None
                linked += 1
                break
    return linked


def _tender_holding(
    session: Session, vendor_id: int, channel: str, value: str
) -> str | None:
    """The company's tender whose stored PDF clues hold this value, if one does."""
    wanted = _mail_key(value) if channel == CHANNEL_EMAIL else value.strip().casefold()
    column = TenderDocument.emails if channel == CHANNEL_EMAIL else TenderDocument.phones
    tenders = select(Award.tender_id).where(Award.vendor_id == vendor_id)
    for tender_id, clues in session.execute(
        select(TenderDocument.tender_id, column)
        .where(TenderDocument.tender_id.in_(tenders))
        .order_by(TenderDocument.tender_id, TenderDocument.filename)
    ):
        for clue in split_clues(clues):
            found = _mail_key(clue) if channel == CHANNEL_EMAIL else clue.casefold()
            if found == wanted:
                return tender_id
    return None


def _pdf_value_known(session: Session, vendor_id: int, channel: str, value: str) -> bool:
    folded = (clean_email(value) if channel == CHANNEL_EMAIL else value.strip()).casefold()
    rows = session.scalars(
        select(VendorContact).where(
            VendorContact.vendor_id == vendor_id,
            VendorContact.channel == channel,
            VendorContact.source == CONTACT_PDF,
        )
    ).all()
    return any(row.value.casefold() == folded for row in rows)


@dataclass(frozen=True)
class PendingVendor:
    """A unit of work for stage 2: who to look up, and everything we already know."""

    vendor_id: int
    name_raw: str
    city: str | None
    state: str | None
    #: The `awards.work_title` of the company's newest tender — fills the
    #: `{title}` slot in the prompt.
    context: str | None
    #: Read from the work order by OCR or its text layer, so characters may
    #: already be wrong. Evidence for the model to confirm or correct, never an
    #: answer in itself.
    pdf_email: str | None = None
    pdf_phone: str | None = None
    #: The tender `context` came from.
    context_tender_id: str | None = None

    @property
    def title(self) -> str:
        return self.context or ""

    @property
    def has_evidence(self) -> bool:
        return bool(self.pdf_email or self.pdf_phone)


def _vendor_rows(
    session: Session,
    where,
    *,
    source: str | None = None,
    vendor_ids: Iterable[int] | None = None,
    limit: int | None = None,
) -> list[PendingVendor]:
    if vendor_ids is not None:
        ids = list(vendor_ids)
        if not ids:
            return []
    else:
        ids = None
    def newest(column):
        """One value from the company's most recently scraped tender."""
        return (
            select(column)
            .select_from(Award)
            .join(Tender, Tender.tender_id == Award.tender_id)
            .where(Award.vendor_id == Vendor.vendor_id)
            .order_by(Tender.scraped_at.desc(), Award.tender_id.desc(), Award.bid_number)
            .limit(1)
            .correlate(Vendor)
            .scalar_subquery()
        )

    statement = (
        select(
            Vendor.vendor_id,
            Vendor.name_raw,
            Vendor.city,
            Vendor.state,
            newest(Award.work_title).label("context"),
            Vendor.pdf_email,
            Vendor.pdf_phone,
            newest(Award.tender_id).label("context_tender_id"),
        )
        .where(where)
        .order_by(Vendor.vendor_id)
    )
    if source is not None:
        statement = statement.where(Vendor.source == source)
    if ids is not None:
        statement = statement.where(Vendor.vendor_id.in_(ids))
    if limit is not None:
        statement = statement.limit(limit)
    return [PendingVendor(*row) for row in session.execute(statement).all()]


def pending_vendors(
    session: Session,
    limit: int | None = None,
    *,
    source: str | None = None,
    vendor_ids: Iterable[int] | None = None,
) -> list[PendingVendor]:
    """Stage 2's work queue, oldest vendor first.

    Only ever returns `pending` rows, so a vendor already answered -- `done` or
    `not_found` -- is never handed out again and never re-paid for.

    `source` narrows it to one origin ('scrape' / 'bideasy'), which is how you
    work through what the scraper just found without touching the imported rows.
    """
    return _vendor_rows(
        session,
        Vendor.enrichment_status == STATUS_PENDING,
        source=source,
        vendor_ids=vendor_ids,
        limit=limit,
    )


def mark_enriched(
    session: Session,
    vendor_id: int,
    *,
    email: str | None,
    phone: str | None,
    source_url: str | None = None,
    llm_run_id: int | None = None,
) -> str:
    """Record what the model found. Returns the status the row landed on.

    Only what `usable_contact` keeps is stored: a government address or a phone
    that cannot be dialled is left out, and the raw answer stays in ``llm_runs``.
    The company columns keep the first value. Every kept email and phone is its
    own ``vendor_contacts`` row. A result with nothing usable is `not_found`,
    not `failed` — the call worked, the answer was "nothing we can use". Only
    `failed` is retried.
    """
    from .emailcheck import usable_contact

    verdict = usable_contact(email, phone)
    clean_email = verdict.email
    clean_phone = verdict.phone
    status = STATUS_DONE if (clean_email or clean_phone) else STATUS_NOT_FOUND
    session.query(Vendor).filter(Vendor.vendor_id == vendor_id).update(
        {
            Vendor.email: clean_email,
            Vendor.phone: clean_phone,
            Vendor.enrichment_status: status,
            Vendor.enriched_at: utcnow(),
            Vendor.contact_origin: CONTACT_MODEL,
        },
        synchronize_session=False,
    )
    for channel, values in (
        (CHANNEL_EMAIL, verdict.emails),
        (CHANNEL_PHONE, verdict.phones),
    ):
        replace_company_contacts(
            session,
            vendor_id,
            source=CONTACT_MODEL,
            channel=channel,
            values=values,
            source_url=source_url,
            llm_run_id=llm_run_id,
        )
    return status


class ContactError(ValueError):
    """A typed email did not match the mailer's format check."""


def set_typed_contact(
    session: Session,
    vendor_id: int,
    *,
    email: str | None = None,
    phone: str | None = None,
) -> None:
    """Record a contact a person typed. A blank field leaves the stored value.

    The company is marked answered, so stage 2 will not pay to look it up again.
    """
    from .emailcheck import is_email

    vendor = session.get(Vendor, vendor_id)
    if vendor is None:
        raise ContactError(f"No company with id {vendor_id}.")
    typed_email = clean_email(email)
    typed_phone = (phone or "").strip()
    if typed_email and not is_email(typed_email):
        raise ContactError("That email is not a valid address.")
    if not typed_email and not typed_phone:
        raise ContactError("Type an email or a phone.")
    new_email = typed_email or vendor.email
    new_phone = typed_phone or vendor.phone
    session.query(Vendor).filter(Vendor.vendor_id == vendor_id).update(
        {
            Vendor.email: new_email,
            Vendor.phone: new_phone,
            Vendor.enrichment_status: STATUS_DONE,
            Vendor.enriched_at: utcnow(),
            Vendor.contact_origin: CONTACT_TYPED,
        },
        synchronize_session=False,
    )
    if typed_email:
        replace_company_contact(
            session,
            vendor_id,
            source=CONTACT_TYPED,
            channel=CHANNEL_EMAIL,
            value=typed_email,
        )
    if typed_phone:
        replace_company_contact(
            session,
            vendor_id,
            source=CONTACT_TYPED,
            channel=CHANNEL_PHONE,
            value=typed_phone,
        )


def vendor_by_name(session: Session, name_raw: str) -> Vendor | None:
    """The company this name already is, after the shared normalisation."""
    from .naming import normalize_name

    return session.scalar(select(Vendor).where(Vendor.name_norm == normalize_name(name_raw)))


def reset_failed_ids(session: Session, vendor_ids: Iterable[int]) -> int:
    """Put the ticked `failed` companies back on the queue."""
    ids = list(vendor_ids)
    if not ids:
        return 0
    return int(
        session.query(Vendor)
        .filter(Vendor.vendor_id.in_(ids), Vendor.enrichment_status == STATUS_FAILED)
        .update({Vendor.enrichment_status: STATUS_PENDING}, synchronize_session=False)
    )


def _blank(column):
    return or_(column.is_(None), column == "")


def _no_contact():
    """No contact email and no phone. PDF evidence is a different column."""
    return and_(_blank(Vendor.email), _blank(Vendor.phone))


def _filled(column):
    return and_(column.is_not(None), column != "")


def _has_contact():
    return or_(_filled(Vendor.email), _filled(Vendor.phone))


def reset_not_found_ids(session: Session, vendor_ids: Iterable[int]) -> int:
    """Put ticked `not_found` companies with no email and no phone back on the queue.

    A row that already has either contact stays `not_found`.
    """
    ids = list(vendor_ids)
    if not ids:
        return 0
    return int(
        session.query(Vendor)
        .filter(
            Vendor.vendor_id.in_(ids),
            Vendor.enrichment_status == STATUS_NOT_FOUND,
            _no_contact(),
        )
        .update({Vendor.enrichment_status: STATUS_PENDING}, synchronize_session=False)
    )


def not_found_with_contact(session: Session, vendor_ids: Iterable[int]) -> int:
    """Ticked `not_found` companies that already have an email or a phone."""
    ids = list(vendor_ids)
    if not ids:
        return 0
    return int(
        session.query(Vendor)
        .filter(
            Vendor.vendor_id.in_(ids),
            Vendor.enrichment_status == STATUS_NOT_FOUND,
            _has_contact(),
        )
        .count()
    )


@dataclass(frozen=True)
class SelectionCounts:
    """How the selected companies split, and which of them Enrich will look up."""

    pending: int = 0
    not_found: int = 0
    failed: int = 0
    done: int = 0
    not_found_ready: int = 0
    waiting_ids: tuple[int, ...] = ()
    names: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


def _contact_empty(email: str | None, phone: str | None) -> bool:
    return not (email or "").strip() and not (phone or "").strip()


def classify_selection(session: Session, vendor_ids: Iterable[int]) -> SelectionCounts:
    """One read of the selected companies.

    The lookup list is pending, failed, and not_found with no email and no phone.
    A not_found company that already has either contact stays out, and so does done.
    """
    ids = list(vendor_ids)
    if not ids:
        return SelectionCounts()
    rows = session.execute(
        select(
            Vendor.vendor_id,
            Vendor.name_raw,
            Vendor.enrichment_status,
            Vendor.email,
            Vendor.phone,
        )
        .where(Vendor.vendor_id.in_(ids))
        .order_by(Vendor.vendor_id)
    ).all()
    pending = not_found = failed = done = ready = 0
    waiting_ids: list[int] = []
    names: list[str] = []
    skipped: list[str] = []
    for vendor_id, name, status, email, phone in rows:
        if status == STATUS_PENDING:
            pending += 1
            waiting_ids.append(int(vendor_id))
            names.append(name)
        elif status == STATUS_NOT_FOUND:
            not_found += 1
            if _contact_empty(email, phone):
                ready += 1
                waiting_ids.append(int(vendor_id))
                names.append(name)
            else:
                skipped.append(name)
        elif status == STATUS_FAILED:
            failed += 1
            waiting_ids.append(int(vendor_id))
            names.append(name)
        elif status == STATUS_DONE:
            done += 1
            skipped.append(name)
    return SelectionCounts(
        pending=pending,
        not_found=not_found,
        failed=failed,
        done=done,
        not_found_ready=ready,
        waiting_ids=tuple(waiting_ids),
        names=tuple(names),
        skipped=tuple(skipped),
    )


def count_not_found_ready(session: Session) -> int:
    """`not_found` companies a requeue would move: no email and no phone."""
    return int(
        session.query(Vendor)
        .filter(Vendor.enrichment_status == STATUS_NOT_FOUND, _no_contact())
        .count()
    )


def count_not_found_ready_ids(session: Session, vendor_ids: Iterable[int]) -> int:
    """How many of these companies are not_found with no email and no phone."""
    ids = list(vendor_ids)
    if not ids:
        return 0
    return int(
        session.query(Vendor)
        .filter(
            Vendor.vendor_id.in_(ids),
            Vendor.enrichment_status == STATUS_NOT_FOUND,
            _no_contact(),
        )
        .count()
    )


def preview_lookup(session: Session, vendor_ids: Iterable[int]) -> list[PendingVendor]:
    """Companies Enrich would look up. Does not change a status."""
    return _vendor_rows(session, _lookup_where(), vendor_ids=vendor_ids)


def prepare_lookup(session: Session, vendor_ids: Iterable[int]) -> list[PendingVendor]:
    """Put failed, and not_found with no contact, onto the queue, then list pending.

    A not_found company that already has an email or a phone stays not_found.
    """
    reset_failed_ids(session, vendor_ids)
    reset_not_found_ids(session, vendor_ids)
    return pending_vendors(session, vendor_ids=vendor_ids)


def _lookup_where():
    return or_(
        Vendor.enrichment_status.in_((STATUS_PENDING, STATUS_FAILED)),
        and_(Vendor.enrichment_status == STATUS_NOT_FOUND, _no_contact()),
    )


@dataclass(frozen=True)
class RecheckRow:
    """One company whose saved model answer the address rule would change."""

    vendor_id: int
    name: str
    dropped: tuple[str, ...]
    status_before: str
    status_after: str


def recheck_model_contacts(session: Session, *, apply: bool = False) -> list[RecheckRow]:
    """Apply `usable_contact` to model answers saved before the rule existed.

    Only companies whose contact came from the model are looked at; a typed
    contact is a person's decision and is left alone. Without ``apply`` nothing
    changes and the list says what would.
    """
    from .emailcheck import usable_contact

    changed: list[RecheckRow] = []
    vendors = session.scalars(
        select(Vendor).where(Vendor.contact_origin == CONTACT_MODEL).order_by(Vendor.vendor_id)
    ).all()
    for vendor in vendors:
        verdict = usable_contact(vendor.email, vendor.phone)
        if not verdict.dropped:
            continue
        after = STATUS_DONE if (verdict.email or verdict.phone) else STATUS_NOT_FOUND
        changed.append(
            RecheckRow(
                vendor_id=int(vendor.vendor_id),
                name=vendor.name_raw,
                dropped=verdict.dropped,
                status_before=vendor.enrichment_status,
                status_after=after,
            )
        )
        if not apply:
            continue
        vendor.email = verdict.email
        vendor.phone = verdict.phone
        vendor.enrichment_status = after
        for channel, kept in ((CHANNEL_EMAIL, verdict.email), (CHANNEL_PHONE, verdict.phone)):
            if kept is None:
                session.query(VendorContact).filter(
                    VendorContact.vendor_id == vendor.vendor_id,
                    VendorContact.source == CONTACT_MODEL,
                    VendorContact.channel == channel,
                    VendorContact.tender_id.is_(None),
                ).delete(synchronize_session=False)
    return changed


def _saved_answer(raw: str | None) -> tuple[str | None, str | None, str | None]:
    """``email``, ``phone``, and ``source`` from one ``llm_runs.output_json``."""
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return None, None, None
    if not isinstance(value, dict):
        return None, None, None

    def text(key: str) -> str | None:
        item = value.get(key)
        if item is None:
            return None
        return str(item)

    return text("email"), text("phone"), text("source")


def _model_contact_values(session: Session, vendor_id: int, channel: str) -> tuple[str, ...]:
    rows = session.scalars(
        select(VendorContact.value)
        .where(
            VendorContact.vendor_id == vendor_id,
            VendorContact.source == CONTACT_MODEL,
            VendorContact.channel == channel,
            VendorContact.tender_id.is_(None),
        )
        .order_by(VendorContact.contact_id)
    ).all()
    return tuple(rows)


def reapply_saved_answers(session: Session) -> list[int]:
    """Store every value the current rule keeps from the latest successful answer.

    Reads ``llm_runs``. Does not call the model. A typed contact is a person's
    decision and is left alone. Returns the companies whose stored contacts changed.
    """
    from .emailcheck import usable_contact

    latest = (
        select(func.max(LlmRun.run_id))
        .where(LlmRun.vendor_id == Vendor.vendor_id, LlmRun.status == "ok")
        .correlate(Vendor)
        .scalar_subquery()
    )
    rows = session.execute(
        select(Vendor, LlmRun)
        .join(LlmRun, LlmRun.run_id == latest)
        .where(Vendor.contact_origin == CONTACT_MODEL)
        .order_by(Vendor.vendor_id)
    ).all()
    changed: list[int] = []
    for vendor, run in rows:
        email, phone, source = _saved_answer(run.output_json)
        verdict = usable_contact(email, phone)
        stored_email = (vendor.email or "").strip() or None
        stored_phone = (vendor.phone or "").strip() or None
        if (
            stored_email == verdict.email
            and stored_phone == verdict.phone
            and _model_contact_values(session, vendor.vendor_id, CHANNEL_EMAIL) == verdict.emails
            and _model_contact_values(session, vendor.vendor_id, CHANNEL_PHONE) == verdict.phones
        ):
            continue
        mark_enriched(
            session,
            int(vendor.vendor_id),
            email=email,
            phone=phone,
            source_url=source,
            llm_run_id=int(run.run_id),
        )
        session.expire(vendor)
        changed.append(int(vendor.vendor_id))
    return changed


def mark_failed(session: Session, vendor_id: int) -> str:
    """The call itself errored. Leaves contact columns alone so a retry can fill them."""
    session.query(Vendor).filter(Vendor.vendor_id == vendor_id).update(
        {Vendor.enrichment_status: STATUS_FAILED, Vendor.enriched_at: utcnow()},
        synchronize_session=False,
    )
    return STATUS_FAILED


def reset_failed(session: Session) -> int:
    """Put every `failed` vendor back on the queue. Returns how many moved."""
    return int(
        session.query(Vendor)
        .filter(Vendor.enrichment_status == STATUS_FAILED)
        .update(
            {Vendor.enrichment_status: STATUS_PENDING}, synchronize_session=False
        )
    )


def reset_not_found(session: Session) -> int:
    """Put every `not_found` vendor back on the queue. Returns how many moved.

    Normally wrong: `not_found` means the lookup worked and the answer was
    "nothing public", so asking again the same way just spends money for the
    same reply.

    It is right when the *method* changed -- enabling web search, switching
    model, adding evidence to the prompt -- because then the old verdict was
    reached without the thing that makes an answer possible. Only ever run it
    deliberately, never on a schedule.
    """
    return int(
        session.query(Vendor)
        .filter(Vendor.enrichment_status == STATUS_NOT_FOUND, _no_contact())
        .update(
            {Vendor.enrichment_status: STATUS_PENDING}, synchronize_session=False
        )
    )


def record_llm_run(
    session: Session,
    *,
    vendor_id: int,
    model: str | None,
    prompt_version: str | None,
    input_json: str | None,
    output_json: str | None,
    status: str,
    error: str | None = None,
    job_id: int | None = None,
) -> int:
    run = LlmRun(
        vendor_id=vendor_id,
        model=model,
        prompt_version=prompt_version,
        input_json=input_json,
        output_json=output_json,
        status=status,
        error=error,
        created_at=utcnow(),
        job_id=job_id,
    )
    session.add(run)
    session.flush()
    return int(run.run_id)


def enrichment_breakdown(
    session: Session, *, source: str | None = None
) -> dict[str, int]:
    """Row counts per `enrichment_status`, optionally for one origin.

    `source` is what makes the queue honest. Stage 2 runs with an `ONLY_SOURCE`
    set, so the total pending count and the count it will actually work through
    are different numbers — and reading the first as the second means watching a
    run do nothing.
    """
    statement = select(Vendor.enrichment_status, func.count()).group_by(
        Vendor.enrichment_status
    )
    if source is not None:
        statement = statement.where(Vendor.source == source)
    rows = session.execute(statement).all()
    return {str(status): int(count) for status, count in rows}


def enrichment_by_source(session: Session) -> dict[str, dict[str, int]]:
    """`{source: {status: count}}` — the whole queue split by where rows came from."""
    rows = session.execute(
        select(Vendor.source, Vendor.enrichment_status, func.count()).group_by(
            Vendor.source, Vendor.enrichment_status
        )
    ).all()
    out: dict[str, dict[str, int]] = {}
    for source, status, count in rows:
        out.setdefault(str(source), {})[str(status)] = int(count)
    return out


def _vendor_has_email():
    """The company column, or any valid email row."""
    contact = (
        select(VendorContact.contact_id)
        .where(
            VendorContact.vendor_id == Vendor.vendor_id,
            VendorContact.channel == CHANNEL_EMAIL,
            VendorContact.valid == 1,
        )
        .exists()
    )
    return or_(and_(Vendor.email.is_not(None), Vendor.email != ""), contact)


def contact_breakdown(session: Session) -> dict[str, int]:
    """Why `done` is bigger than the number of people who can be mailed.

    `mark_enriched` lands on `done` when *either* an email or a phone comes
    back, so a phone-only vendor counts as answered but is unreachable by
    stage 3, which reads `email` alone. Counting `done` as the send queue
    overstates it by exactly `phone_only`.
    """
    has_email = _vendor_has_email()
    has_phone = Vendor.phone.is_not(None) & (Vendor.phone != "")
    mailable = int(
        session.scalar(select(func.count()).select_from(Vendor).where(has_email)) or 0
    )
    phone_only = int(
        session.scalar(
            select(func.count()).select_from(Vendor).where(~has_email, has_phone)
        )
        or 0
    )
    no_contact = int(
        session.scalar(
            select(func.count()).select_from(Vendor).where(~has_email, ~has_phone)
        )
        or 0
    )
    return {
        "mailable": mailable,
        "phone_only": phone_only,
        "no_contact": no_contact,
    }


# --------------------------------------------------------------------------
# Stage 3 — outreach
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OutreachTarget:
    vendor_id: int
    company: str
    email: str
    tender_id: str
    title: str
    contract_date: str


def _has_mail_address():
    """A company email, or any valid email stored for the company.

    Every valid address of the company is used for each of its awards, so a
    PDF address read on another tender counts here too.
    """
    return _vendor_has_email()


#: Mail filter for a company taken off the list. Not an ``outreach.status`` value.
MAIL_STOPPED = "do_not_mail"


def _trimmed(column):
    return func.trim(column)


def _is_email_sql(column):
    """``is_email``, registered on every connection by ``db.engine``."""
    return func.coalesce(func.pc_is_email(column), 0) == 1


def _is_buyer_sql(column):
    """``is_buyer_email``, registered on every connection by ``db.engine``."""
    return func.coalesce(func.pc_is_buyer_email(column), 0) == 1


def _mail_contact_for_award():
    """A valid email row ``address_report`` would consider for this award."""
    return and_(
        VendorContact.vendor_id == Vendor.vendor_id,
        VendorContact.channel == CHANNEL_EMAIL,
        VendorContact.valid == 1,
    )


def _address_blocked(address):
    """``sent`` or ``sending`` for this company, tender, and address."""
    return (
        select(Outreach.outreach_id)
        .where(
            Outreach.vendor_id == Vendor.vendor_id,
            Outreach.tender_id == Award.tender_id,
            func.lower(_trimmed(Outreach.email)) == func.lower(_trimmed(address)),
            Outreach.status.in_((OUTREACH_SENT, OUTREACH_SENDING)),
        )
        .correlate_except(Outreach)
        .exists()
    )


def _legacy_send_covers_award():
    """An old company-level ``sent`` row covers tenders scraped at or before it."""
    return (
        select(Outreach.outreach_id)
        .where(
            Outreach.vendor_id == Vendor.vendor_id,
            Outreach.tender_id.is_(None),
            Outreach.status == OUTREACH_SENT,
            Tender.scraped_at <= Outreach.sent_at,
        )
        .exists()
    )


def _company_can_be_mailed():
    return or_(Vendor.do_not_mail.is_(None), Vendor.do_not_mail == "")


def sendable_award():
    """True when ``mail_messages`` would emit at least one address for this award.

    A valid contact is used when one exists. ``vendors.email`` is only the
    fallback when no valid contact row exists, matching ``choose_addresses``.
    A buyer address and an address already ``sent`` or ``sending`` do not count.
    """
    usable_contact = (
        select(VendorContact.contact_id)
        .where(
            _mail_contact_for_award(),
            ~_is_buyer_sql(VendorContact.value),
            ~_address_blocked(VendorContact.value),
        )
        .exists()
    )
    any_valid_contact = (
        select(VendorContact.contact_id).where(_mail_contact_for_award()).exists()
    )
    fallback = and_(
        ~any_valid_contact,
        _is_email_sql(Vendor.email),
        ~_is_buyer_sql(Vendor.email),
        ~_address_blocked(Vendor.email),
    )
    return and_(
        _company_can_be_mailed(),
        ~_legacy_send_covers_award(),
        or_(usable_contact, fallback),
    )


def sent_award_finished():
    """A send covers this award, and nothing is left for ``mail_messages``."""
    named = (
        select(Outreach.outreach_id)
        .where(
            Outreach.vendor_id == Vendor.vendor_id,
            Outreach.tender_id == Award.tender_id,
            Outreach.status == OUTREACH_SENT,
        )
        .exists()
    )
    return and_(or_(named, _legacy_send_covers_award()), ~sendable_award())


def sendable_with_status(status: str):
    """This award still has a letter, and an attempt with ``status`` for it."""
    attempt = (
        select(Outreach.outreach_id)
        .where(
            Outreach.vendor_id == Vendor.vendor_id,
            Outreach.tender_id == Award.tender_id,
            Outreach.status == status,
        )
        .exists()
    )
    return and_(sendable_award(), attempt)


def company_stopped():
    return and_(Vendor.do_not_mail.is_not(None), Vendor.do_not_mail != "")


def apply_mail_addresses(session: Session, rows: list[dict]) -> None:
    """Show the addresses a send would use. A finished award shows what was sent."""
    if not rows:
        return
    pairs = [(int(row["vendor_id"]), str(row["tender_id"])) for row in rows]
    grouped: dict[tuple[int, str], list[str]] = {}
    for message in mail_messages(session, awards=pairs):
        grouped.setdefault((message.vendor_id, message.tender_id), []).append(message.email)
    missing = [pair for pair in pairs if pair not in grouped]
    sent: dict[tuple[int, str], list[str]] = {}
    if missing:
        vendor_ids = {vendor_id for vendor_id, _tender_id in missing}
        for vendor_id, tender_id, email in session.execute(
            select(Outreach.vendor_id, Outreach.tender_id, Outreach.email)
            .where(
                Outreach.vendor_id.in_(vendor_ids),
                Outreach.status == OUTREACH_SENT,
                Outreach.email.is_not(None),
                Outreach.email != "",
            )
            .order_by(Outreach.outreach_id)
        ):
            if not tender_id:
                continue
            key = (int(vendor_id), str(tender_id))
            if key not in missing:
                continue
            folded = email.casefold()
            bucket = sent.setdefault(key, [])
            if folded not in {item.casefold() for item in bucket}:
                bucket.append(email)
    for row in rows:
        key = (int(row["vendor_id"]), str(row["tender_id"]))
        addresses = grouped.get(key) or sent.get(key)
        if addresses:
            row["email"] = ", ".join(addresses)


def outreach_targets(
    session: Session,
    limit: int | None = None,
    *,
    include_sent: bool = False,
) -> list[OutreachTarget]:
    """One message per company and tender that still needs sending.

    A successful attempt for that pair is skipped. A successful attempt with no
    tender id is the older company-level send: it covers awards whose tender
    was already scraped at that time, and leaves a later win in the queue.

    `include_sent` is the deliberate second send. It returns those pairs too.
    A failed attempt never removes a pair.
    """
    sent_for_tender = (
        select(Outreach.outreach_id)
        .where(
            Outreach.vendor_id == Vendor.vendor_id,
            Outreach.tender_id == Award.tender_id,
            Outreach.status == OUTREACH_SENT,
        )
        .exists()
    )
    covered_by_old_send = (
        select(Outreach.outreach_id)
        .where(
            Outreach.vendor_id == Vendor.vendor_id,
            Outreach.tender_id.is_(None),
            Outreach.status == OUTREACH_SENT,
            Tender.scraped_at <= Outreach.sent_at,
        )
        .exists()
    )
    statement = (
        select(
            Vendor.vendor_id,
            Vendor.name_raw,
            Vendor.email,
            Award.tender_id,
            Tender.title,
            Tender.contract_date,
        )
        .join(Award, Award.vendor_id == Vendor.vendor_id)
        .join(Tender, Tender.tender_id == Award.tender_id)
        .where(_has_mail_address())
        .distinct()
        .order_by(Vendor.vendor_id, Award.tender_id)
    )
    if not include_sent:
        statement = statement.where(~sent_for_tender, ~covered_by_old_send)
    if limit is not None:
        statement = statement.limit(limit)
    return [
        OutreachTarget(
            vendor_id=int(vendor_id),
            company=company,
            email=email,
            tender_id=tender_id,
            title=title or "",
            contract_date=contract_date or "",
        )
        for vendor_id, company, email, tender_id, title, contract_date in session.execute(
            statement
        ).all()
    ]


def expand_targets(
    session: Session,
    people: list[OutreachTarget],
    mode: str = ADDRESS_FIRST,
    chosen_ids: Iterable[int] | None = None,
) -> list[OutreachTarget]:
    """One target per address. First, every valid address, or the ticked ids."""
    picked_mode = mode if mode in {ADDRESS_FIRST, ADDRESS_ALL, ADDRESS_CHOSEN} else ADDRESS_FIRST
    chosen = {int(item) for item in (chosen_ids or [])}
    expanded: list[OutreachTarget] = []
    for person in people:
        for email in choose_addresses(
            session,
            person.vendor_id,
            person.tender_id,
            mode=picked_mode,
            chosen_ids=chosen,
            fallback=person.email,
        ):
            expanded.append(
                OutreachTarget(
                    vendor_id=person.vendor_id,
                    company=person.company,
                    email=email,
                    tender_id=person.tender_id,
                    title=person.title,
                    contract_date=person.contract_date,
                )
            )
    return expanded


def _blocked_addresses(
    session: Session,
) -> tuple[set[tuple[int, str, str]], set[tuple[int, str, str]], list[tuple[int, str]]]:
    """Addresses already ``sent`` or ``sending``, plus old company-level sends.

    A company-level row has no tender id. It covers every tender scraped at or
    before that send, and it covers every address on those tenders.
    """
    sent: set[tuple[int, str, str]] = set()
    sending: set[tuple[int, str, str]] = set()
    legacy: list[tuple[int, str]] = []
    rows = session.execute(
        select(
            Outreach.vendor_id,
            Outreach.tender_id,
            Outreach.email,
            Outreach.status,
            Outreach.sent_at,
        ).where(Outreach.status.in_((OUTREACH_SENT, OUTREACH_SENDING)))
    ).all()
    for vendor_id, tender_id, email, status, sent_at in rows:
        if not tender_id:
            if status == OUTREACH_SENT:
                legacy.append((int(vendor_id), sent_at or ""))
            continue
        key = (int(vendor_id), str(tender_id), _mail_key(email))
        if not key[2]:
            continue
        if status == OUTREACH_SENDING:
            sending.add(key)
        else:
            sent.add(key)
    return sent, sending, legacy


def _covered_by_old_send(
    vendor_id: int, scraped_at: str, legacy: list[tuple[int, str]]
) -> bool:
    return any(
        covered_id == vendor_id and (scraped_at or "") <= sent_at
        for covered_id, sent_at in legacy
    )


#: Why an address is not mailed for an award. The award page and the run log
#: print these next to the address.
SKIP_INVALID = "not a valid address"
SKIP_DUPLICATE = "same mailbox as an address above"
SKIP_BUYER = "government or buyer address"
SKIP_STOPPED = "the company is marked do not mail"
SKIP_OLD_SEND = "an older company-level send covers this tender"
SKIP_SENDING = "a send to this address is in progress"
SKIP_SENT = "already sent for this tender"


@dataclass(frozen=True)
class AddressLine:
    """One email of the company, judged for one award."""

    value: str
    #: ``pdf``, ``model`` or ``typed``.
    source: str
    #: For a PDF address: the tender whose files it was read from.
    tender_id: str | None
    #: For a PDF address: that tender's files that hold it.
    filenames: tuple[str, ...]
    #: For a model address: the page the model named, and its ``llm_runs`` row.
    source_url: str | None
    llm_run_id: int | None
    valid: bool
    will_mail: bool
    #: Empty when the address is mailed. Otherwise one of the ``SKIP_`` texts.
    reason: str = ""


def clue_files(
    session: Session, tender_ids: Iterable[str], channel: str = CHANNEL_EMAIL
) -> dict[tuple[str, str], tuple[str, ...]]:
    """``(tender_id, value)`` to the files of that tender whose clues hold the value."""
    ids = [tender_id for tender_id in dict.fromkeys(tender_ids) if tender_id]
    if not ids:
        return {}
    column = TenderDocument.emails if channel == CHANNEL_EMAIL else TenderDocument.phones
    found: dict[tuple[str, str], list[str]] = {}
    for tender_id, filename, clues in session.execute(
        select(TenderDocument.tender_id, TenderDocument.filename, column)
        .where(TenderDocument.tender_id.in_(ids))
        .order_by(TenderDocument.tender_id, TenderDocument.filename)
    ):
        for clue in split_clues(clues):
            key = _mail_key(clue) if channel == CHANNEL_EMAIL else clue.casefold()
            bucket = found.setdefault((tender_id, key), [])
            if filename not in bucket:
                bucket.append(filename)
    return {key: tuple(names) for key, names in found.items()}


def address_report(
    session: Session,
    vendor_id: int,
    tender_id: str,
    *,
    include_sent: bool = False,
    blocked=None,
) -> list[AddressLine]:
    """Every email of the company for one award, and whether a send mails it.

    Order is the PDF address read for this tender, PDF addresses read for the
    company's other tenders, then model, then typed. ``vendors.email`` is used
    only when no valid row exists. ``mail_messages`` sends exactly the lines
    with ``will_mail``; the award page and the run log show the rest with why.
    """
    from .emailcheck import is_buyer_email

    vendor = session.get(Vendor, vendor_id)
    if vendor is None:
        return []
    tender = session.get(Tender, tender_id)
    sent, sending, legacy = blocked if blocked is not None else _blocked_addresses(session)
    rows = ordered_contacts(session, vendor_id, tender_id)
    files = clue_files(
        session, (row.tender_id for row in rows if row.source == CONTACT_PDF)
    )
    candidates = [
        (row.value, row.source, row.tender_id, row.source_url, row.llm_run_id, bool(row.valid))
        for row in rows
    ]
    if not any(valid for *_rest, valid in candidates) and (vendor.email or "").strip():
        fallback = clean_email(vendor.email)
        candidates.append(
            (
                fallback,
                vendor.contact_origin or CONTACT_MODEL,
                None,
                None,
                None,
                contact_is_valid(CHANNEL_EMAIL, fallback),
            )
        )
    stopped = bool((vendor.do_not_mail or "").strip())
    old_send = (
        not include_sent
        and tender is not None
        and _covered_by_old_send(int(vendor_id), tender.scraped_at or "", legacy)
    )
    seen: set[str] = set()
    lines: list[AddressLine] = []
    for value, source, found_on, source_url, run_id, valid in candidates:
        key = _mail_key(value)
        blocked_key = (int(vendor_id), str(tender_id), key)
        if not valid:
            reason = SKIP_INVALID
        elif key in seen:
            reason = SKIP_DUPLICATE
        else:
            seen.add(key)
            if is_buyer_email(value):
                reason = SKIP_BUYER
            elif stopped:
                reason = SKIP_STOPPED
            elif old_send:
                reason = SKIP_OLD_SEND
            elif blocked_key in sending:
                reason = SKIP_SENDING
            elif blocked_key in sent and not include_sent:
                reason = SKIP_SENT
            else:
                reason = ""
        lines.append(
            AddressLine(
                value=value,
                source=source,
                tender_id=found_on,
                filenames=files.get((found_on, key), ()) if found_on else (),
                source_url=source_url,
                llm_run_id=run_id,
                valid=valid,
                will_mail=not reason,
                reason=reason,
            )
        )
    return lines


def mail_messages(
    session: Session,
    limit: int | None = None,
    *,
    include_sent: bool = False,
    awards: Iterable[tuple[int, str]] | None = None,
    vendor_ids: Iterable[int] | None = None,
) -> list[OutreachTarget]:
    """One message per valid company address that a live send would deliver.

    The Mail page, the terminal, and the export all call this. Each award gets
    every valid address of its company, as ``address_report`` orders and judges
    them. A buyer address, a company marked do-not-mail, and an address that
    already has ``sent`` or ``sending`` for that company and tender are left
    out. ``include_sent`` brings ``sent`` addresses back and still skips
    ``sending``. Do-not-mail is never overridden.
    """
    wanted = {(int(vendor_id), str(tender_id)) for vendor_id, tender_id in awards} if awards is not None else None
    wanted_vendors = {int(vendor_id) for vendor_id in vendor_ids} if vendor_ids is not None else None
    statement = (
        select(
            Vendor.vendor_id,
            Vendor.name_raw,
            Award.tender_id,
            Tender.title,
            Tender.contract_date,
        )
        .join(Award, Award.vendor_id == Vendor.vendor_id)
        .join(Tender, Tender.tender_id == Award.tender_id)
        .where(_has_mail_address())
        .where(or_(Vendor.do_not_mail.is_(None), Vendor.do_not_mail == ""))
        .distinct()
        .order_by(Vendor.vendor_id, Award.tender_id)
    )
    blocked = _blocked_addresses(session)
    messages: list[OutreachTarget] = []
    for vendor_id, company, tender_id, title, contract_date in session.execute(
        statement
    ).all():
        pair = (int(vendor_id), str(tender_id))
        if wanted is not None and pair not in wanted:
            continue
        if wanted_vendors is not None and pair[0] not in wanted_vendors:
            continue
        for line in address_report(
            session, pair[0], pair[1], include_sent=include_sent, blocked=blocked
        ):
            if not line.will_mail:
                continue
            messages.append(
                OutreachTarget(
                    vendor_id=pair[0],
                    company=company,
                    email=line.value,
                    tender_id=pair[1],
                    title=title or "",
                    contract_date=contract_date or "",
                )
            )
            if limit is not None and len(messages) >= limit:
                return messages
    return messages


def set_do_not_mail(session: Session, vendor_id: int, *, stopped: bool) -> None:
    """Take a company off the mail list, or put it back. Stage 3 writes this."""
    vendor = session.get(Vendor, vendor_id)
    if vendor is None:
        raise ContactError("No company with that id.")
    vendor.do_not_mail = utcnow() if stopped else None


def record_outreach(
    session: Session,
    *,
    vendor_id: int,
    email: str,
    subject: str,
    transport: str,
    status: str,
    error: str | None = None,
    tender_id: str | None = None,
    job_id: int | None = None,
) -> int:
    row = Outreach(
        vendor_id=vendor_id,
        tender_id=(tender_id or "").strip() or None,
        email=email,
        subject=subject,
        transport=transport,
        status=status,
        error=error,
        sent_at=utcnow(),
        job_id=job_id,
    )
    session.add(row)
    session.flush()
    return int(row.outreach_id)


def finish_outreach(
    session: Session,
    outreach_id: int,
    *,
    status: str,
    error: str | None = None,
) -> None:
    """Move a ``sending`` row to ``sent``, ``failed``, or ``test``."""
    row = session.get(Outreach, outreach_id)
    if row is None:
        return
    row.status = status
    row.error = (error or "")[:500] or None
    row.sent_at = utcnow()


def outreach_breakdown(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(Outreach.status, func.count()).group_by(Outreach.status)
    ).all()
    return {str(status): int(count) for status, count in rows}


def outreach_queue_counts(session: Session) -> dict[str, int]:
    """What stage 3 would do on its next run.

    `remaining` calls `mail_messages` rather than re-deriving the same
    filter, so the number you decide on and the list that gets mailed cannot
    drift apart. That is the same reason `get_data.py` calls it instead of
    writing a lookalike query.
    """
    has_email = _vendor_has_email()
    mailable = int(
        session.scalar(select(func.count()).select_from(Vendor).where(has_email)) or 0
    )
    already_sent = int(
        session.scalar(
            select(func.count(func.distinct(Outreach.vendor_id))).where(
                Outreach.status == OUTREACH_SENT
            )
        )
        or 0
    )
    failed_attempts = int(
        session.scalar(
            select(func.count()).select_from(Outreach).where(
                Outreach.status == OUTREACH_FAILED
            )
        )
        or 0
    )
    return {
        "mailable": mailable,
        "already_sent": already_sent,
        "failed_attempts": failed_attempts,
        "remaining": len(mail_messages(session)),
    }


# --------------------------------------------------------------------------
# Across the whole pipeline
# --------------------------------------------------------------------------


def pipeline_funnel(session: Session) -> list[tuple[str, int, str]]:
    """One row per stage: `(label, count, note)`, top to bottom.

    Answers "at which stage is everything" in a single read. The drop from
    `enriched` to `mailable` is the phone-only gap; the drop from `mailable` to
    `sent` is stage 3's backlog.
    """
    has_email = _vendor_has_email()

    def count(model, *where) -> int:
        return int(
            session.scalar(select(func.count()).select_from(model).where(*where)) or 0
        )

    return [
        ("tenders", count(Tender), "scraped from the portal"),
        ("awards", count(Award), "vendor-to-tender links"),
        ("vendors", count(Vendor), "distinct companies"),
        (
            "enriched",
            count(Vendor, Vendor.enrichment_status == STATUS_DONE),
            "stage 2 found something",
        ),
        ("mailable", count(Vendor, has_email), "has an email address"),
        (
            "sent",
            int(
                session.scalar(
                    select(func.count(func.distinct(Outreach.vendor_id))).where(
                        Outreach.status == OUTREACH_SENT
                    )
                )
                or 0
            ),
            "stage 3 has mailed",
        ),
    ]


def delete_vendors_by_source(session: Session, source: str) -> int:
    """Delete every vendor from one origin. Returns how many rows went.

    Refuses if any `awards`, `llm_runs` or `outreach` row points at one of
    them. The foreign keys would catch it, but only partway through the
    statement and with an opaque message -- and a caller that meant to drop
    imported rows should hear "these are referenced" rather than an
    IntegrityError.
    """
    doomed = select(Vendor.vendor_id).where(Vendor.source == source)
    for model, label in ((Award, "awards"), (LlmRun, "llm_runs"), (Outreach, "outreach")):
        referencing = int(
            session.scalar(
                select(func.count()).select_from(model).where(model.vendor_id.in_(doomed))
            )
            or 0
        )
        if referencing:
            raise ValueError(
                f"Refusing to delete source={source!r}: {referencing} {label} row(s) "
                "reference these vendors. Remove them first, or keep the vendors."
            )
    return int(
        session.query(Vendor)
        .filter(Vendor.source == source)
        .delete(synchronize_session=False)
    )
