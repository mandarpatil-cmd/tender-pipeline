"""The queries each stage runs, in one place.

Keeping them here rather than in the stages is what stops the three from
drifting apart again: the "work queue" and the "who is left" query each have
exactly one definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import (
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
    #: Straight from a scanned work order, so possibly OCR-mangled. Evidence for
    #: the model to confirm or correct, never an answer in itself.
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
        },
        synchronize_session=False,
    )
    return status


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


def outreach_targets(session: Session, limit: int | None = None) -> list[OutreachTarget]:
    """Everyone with an address who has not already been sent to.

    This replaces both `emails.csv` and `sent_log.csv`: the "who is left"
    question is answered by the database, so an interrupted run resumes simply
    by being run again.
    """
    already_sent = select(Outreach.vendor_id).where(Outreach.status == OUTREACH_SENT)
    statement = (
        select(Vendor.vendor_id, Vendor.name_raw, Vendor.email)
        .where(
            Vendor.email.is_not(None),
            Vendor.email != "",
            Vendor.vendor_id.not_in(already_sent),
        )
        .order_by(Vendor.vendor_id)
    )
    if limit is not None:
        statement = statement.limit(limit)
    return [OutreachTarget(*row) for row in session.execute(statement).all()]


def record_outreach(
    session: Session,
    *,
    vendor_id: int,
    email: str,
    subject: str,
    transport: str,
    status: str,
    error: str | None = None,
) -> int:
    row = Outreach(
        vendor_id=vendor_id,
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
