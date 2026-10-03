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

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import (
    CONTACT_MODEL,
    CONTACT_TYPED,
    OUTREACH_SENT,
    SOURCE_SCRAPE,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NOT_FOUND,
    STATUS_PENDING,
    Award,
    LlmRun,
    Outreach,
    Tender,
    TenderDocument,
    Vendor,
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


def upsert_tender(session: Session, **values: Any) -> None:
    """Insert or replace one tender. `tender_id` and `scraped_at` are required."""
    statement = sqlite_insert(Tender).values(**values)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[Tender.tender_id],
            set_={
                key: getattr(statement.excluded, key)
                for key in values
                if key != "tender_id"
            },
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

    Never touches the contact block — `email`, `phone`, `enrichment_status` and
    `enriched_at` belong to stage 2. City and state are only filled in when
    blank, so a better value already on the row is not overwritten.
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
    """Rewrite every award for one tender. Returns how many were written."""
    session.query(Award).filter(Award.tender_id == tender_id).delete(
        synchronize_session=False
    )
    written = 0
    for row in rows:
        session.execute(sqlite_insert(Award).values(tender_id=tender_id, **row))
        written += 1
    return written


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
            filename, {"emails": [], "phones": [], "gstins": []}
        )
        for key in ("emails", "phones", "gstins"):
            for value in _clue_list(item.get(key)):
                if value not in bucket[key]:
                    bucket[key].append(value)
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
    """Record what stage 1 read out of a work-order PDF. Fills blanks only.

    Returns True if anything was written. Never overwrites a value already
    there: a re-scrape of the same tender must not churn the evidence, and a
    second tender's PDF should not clobber the first one's.
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


@dataclass(frozen=True)
class PendingVendor:
    """A unit of work for stage 2: who to look up, and everything we already know."""

    vendor_id: int
    name_raw: str
    city: str | None
    state: str | None
    #: One `awards.work_title` — fills the `{title}` slot in the prompt.
    context: str | None
    #: Read from the work order's embedded text layer, not by OCR, so characters
    #: may already be wrong. Evidence for the model to confirm or correct, never
    #: an answer in itself.
    pdf_email: str | None = None
    pdf_phone: str | None = None

    @property
    def title(self) -> str:
        return self.context or ""

    @property
    def has_evidence(self) -> bool:
        return bool(self.pdf_email or self.pdf_phone)


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
    context = (
        select(Award.work_title)
        .where(Award.vendor_id == Vendor.vendor_id)
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
            context.label("context"),
            Vendor.pdf_email,
            Vendor.pdf_phone,
        )
        .where(Vendor.enrichment_status == STATUS_PENDING)
        .order_by(Vendor.vendor_id)
    )
    if source is not None:
        statement = statement.where(Vendor.source == source)
    if vendor_ids is not None:
        ids = list(vendor_ids)
        if not ids:
            return []
        statement = statement.where(Vendor.vendor_id.in_(ids))
    if limit is not None:
        statement = statement.limit(limit)
    return [PendingVendor(*row) for row in session.execute(statement).all()]


def mark_enriched(
    session: Session,
    vendor_id: int,
    *,
    email: str | None,
    phone: str | None,
) -> str:
    """Record what the model found. Returns the status the row landed on.

    A result with neither an email nor a phone is `not_found`, not `failed` —
    the call worked, the answer was "nothing public". Only `failed` is retried.
    """
    clean_email = (email or "").strip() or None
    clean_phone = (phone or "").strip() or None
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
    typed_email = (email or "").strip()
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


def reset_not_found_ids(session: Session, vendor_ids: Iterable[int]) -> int:
    """Put the ticked `not_found` companies back on the queue.

    Only makes sense after the lookup method itself has changed.
    """
    ids = list(vendor_ids)
    if not ids:
        return 0
    return int(
        session.query(Vendor)
        .filter(
            Vendor.vendor_id.in_(ids),
            Vendor.enrichment_status == STATUS_NOT_FOUND,
        )
        .update({Vendor.enrichment_status: STATUS_PENDING}, synchronize_session=False)
    )


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
        .filter(Vendor.enrichment_status == STATUS_NOT_FOUND, Vendor.email.is_(None))
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


def contact_breakdown(session: Session) -> dict[str, int]:
    """Why `done` is bigger than the number of people who can be mailed.

    `mark_enriched` lands on `done` when *either* an email or a phone comes
    back, so a phone-only vendor counts as answered but is unreachable by
    stage 3, which reads `email` alone. Counting `done` as the send queue
    overstates it by exactly `phone_only`.
    """
    has_email = Vendor.email.is_not(None) & (Vendor.email != "")
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
        .where(Vendor.email.is_not(None), Vendor.email != "")
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
    )
    session.add(row)
    session.flush()
    return int(row.outreach_id)


def outreach_breakdown(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(Outreach.status, func.count()).group_by(Outreach.status)
    ).all()
    return {str(status): int(count) for status, count in rows}


def outreach_queue_counts(session: Session) -> dict[str, int]:
    """What stage 3 would do on its next run.

    `remaining` calls `outreach_targets` rather than re-deriving the same
    filter, so the number you decide on and the list that gets mailed cannot
    drift apart. That is the same reason `get_data.py` calls it instead of
    writing a lookalike query.
    """
    has_email = Vendor.email.is_not(None) & (Vendor.email != "")
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
                Outreach.status != OUTREACH_SENT
            )
        )
        or 0
    )
    return {
        "mailable": mailable,
        "already_sent": already_sent,
        "failed_attempts": failed_attempts,
        "remaining": len(outreach_targets(session)),
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
    has_email = Vendor.email.is_not(None) & (Vendor.email != "")

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
