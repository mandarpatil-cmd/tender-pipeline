"""The shared schema — five tables, one writer each.

```text
                 tenders  vendors  awards  llm_runs  outreach
01-scrape  read     *        .        .        .         .
           write    *        *        *        .         .
02-enrich  read     .        *        *        .         .
           write    .      * (1)      .        *         .
03-outreach read    .        *        .        .         *
           write    .        .        .        .         *
```

(1) `vendors` is the one table with two writers, and they touch disjoint
columns: the scraper owns identity, stage 2 owns the contact block.

Column names match the scraper's original hand-rolled SQLite byte for byte, so
the live database migrates with no transformation.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# --- vendors.enrichment_status: stage 2's work queue -------------------------
STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_NOT_FOUND = "not_found"
STATUS_FAILED = "failed"
ENRICHMENT_STATUSES = (STATUS_PENDING, STATUS_DONE, STATUS_NOT_FOUND, STATUS_FAILED)

# --- vendors.source ----------------------------------------------------------
SOURCE_SCRAPE = "scrape"
SOURCE_BIDEASY = "bideasy"

# --- outreach.status ---------------------------------------------------------
OUTREACH_SENT = "sent"
OUTREACH_FAILED = "failed"


class Base(DeclarativeBase):
    pass


class Tender(Base):
    """One row per tender scraped from eprocure.gov.in."""

    __tablename__ = "tenders"

    tender_id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    organisation: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    contract_date: Mapped[str | None] = mapped_column(Text)
    contract_value: Mapped[str | None] = mapped_column(Text)
    #: Path to the raw scrape, `data/json/<id>.json` relative to the stage.
    json_path: Mapped[str | None] = mapped_column(Text)
    scraped_at: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:
        return f"<Tender {self.tender_id}>"


class Vendor(Base):
    """One row per company. The spine of the pipeline.

    Identity columns are written by the scraper and the importer; the contact
    block (`email`, `phone`, `enrichment_status`, `enriched_at`) is stage 2's
    alone.
    """

    __tablename__ = "vendors"

    vendor_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name_raw: Mapped[str] = mapped_column(Text, nullable=False)
    #: Normalised by `naming.normalize_name` — the dedupe key.
    name_norm: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    legal_form: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    buyer_hint: Mapped[str | None] = mapped_column(Text)
    #: 'scrape' | 'bideasy' — where the row came from.
    source: Mapped[str | None] = mapped_column(Text)

    # --- evidence block: written only by stage 1 ---
    # Lifted verbatim out of the tender's work-order PDF, which is often scanned.
    # This is *evidence*, not truth: OCR mangles characters (a real example read
    # "acmctechworks0l@" for "acmetechworks01@"). Stage 2 passes it to the model
    # to confirm or correct. Never mail one of these directly.
    pdf_email: Mapped[str | None] = mapped_column(Text)
    pdf_phone: Mapped[str | None] = mapped_column(Text)

    # --- contact block: written only by stage 2 ---
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    enrichment_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=STATUS_PENDING, default=STATUS_PENDING
    )
    enriched_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("idx_vendors_status", "enrichment_status"),)

    def __repr__(self) -> str:
        return f"<Vendor {self.vendor_id} {self.name_raw!r} {self.enrichment_status}>"


class Award(Base):
    """Links a vendor to a tender. A vendor may win many, a tender may have many."""

    __tablename__ = "awards"

    tender_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tenders.tender_id"), primary_key=True
    )
    bid_number: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=False, server_default=""
    )
    vendor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("vendors.vendor_id"), nullable=False
    )
    bidder_name: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[str | None] = mapped_column(Text)
    quoted_value: Mapped[str | None] = mapped_column(Text)
    awarded_value: Mapped[str | None] = mapped_column(Text)
    awarded_currency: Mapped[str | None] = mapped_column(Text)
    contract_date: Mapped[str | None] = mapped_column(Text)
    contract_value: Mapped[str | None] = mapped_column(Text)
    #: Stage 2 reads this as the LLM's only context about the company.
    work_title: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("idx_awards_vendor", "vendor_id"),)

    def __repr__(self) -> str:
        return f"<Award {self.tender_id}/{self.bid_number} -> {self.vendor_id}>"


class LlmRun(Base):
    """Audit of every LLM call, so a failure is retryable instead of lost."""

    __tablename__ = "llm_runs"

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vendor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("vendors.vendor_id"), nullable=False
    )
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    input_json: Mapped[str | None] = mapped_column(Text)
    output_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_llm_runs_vendor", "vendor_id"),)

    def __repr__(self) -> str:
        return f"<LlmRun {self.run_id} vendor={self.vendor_id} {self.status}>"


class Outreach(Base):
    """One row per send attempt. Replaces stage 3's `sent_log.csv`."""

    __tablename__ = "outreach"

    outreach_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vendor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("vendors.vendor_id"), nullable=False
    )
    #: The address actually used — not necessarily `vendors.email` (see TO=).
    email: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    transport: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("idx_outreach_vendor", "vendor_id"),
        Index("idx_outreach_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<Outreach {self.outreach_id} vendor={self.vendor_id} {self.status}>"


#: Printed by `pipeline-db status`, in pipeline order.
TABLES = (Tender, Vendor, Award, LlmRun, Outreach)
