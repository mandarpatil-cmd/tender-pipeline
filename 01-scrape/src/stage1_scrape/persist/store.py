"""Files under `root`; rows in the pipeline's shared database.

The schema is not defined here any more — it lives once, in `pipeline_core`, so
all three stages see the same tables. This module is the scraper's view of it.

Stage 1 owns `tenders`, `awards`, and the *identity* columns of `vendors`. It
must never write `email`, `phone`, `enrichment_status` or `enriched_at`: those
belong to stage 2, and `upsert_vendor` is careful to leave them alone.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline_core import settings as core_settings
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.models import (
    ORIGIN_PORTAL,
    SOURCE_SCRAPE,
    Award,
    Tender,
    TenderDocument,
    Vendor,
)
from pipeline_core.money import format_amount
from pipeline_core.naming import buyer_tail, infer_city_state
from pipeline_core.queries import (
    backfill_tender_documents,
    documents_for_tenders,
    known_tender_ids,
    replace_awards,
    clear_pdf_contacts,
    record_job_tender,
    refresh_vendor_pdf_columns,
    replace_pdf_contacts,
    replace_tender_documents,
    split_clues,
    upsert_tender,
)
from pipeline_core.queries import upsert_vendor as core_upsert_vendor
from pipeline_core.queries import vendor_count as core_vendor_count
from sqlalchemy import select
from sqlalchemy.orm import Session

from stage1_scrape.domain.enrichment import winner_rows
from stage1_scrape.domain.models import (
    SEARCH_TENDER_ID,
    ListingRow,
    ScrapeSource,
    TenderRecord,
)
from stage1_scrape.persist.pdf_text import merge_pdf_contacts

VENDOR_SEED_COLUMNS = (
    Vendor.vendor_id,
    Vendor.name_raw,
    Vendor.name_norm,
    Vendor.legal_form,
    Vendor.city,
    Vendor.state,
    Vendor.buyer_hint,
)
AWARD_SEED_COLUMNS = (
    Award.tender_id,
    Award.bid_number,
    Award.rank,
    Award.awarded_value,
    Award.awarded_currency,
    Tender.contract_date,
    Award.work_title,
)


class Store:
    """`root` holds json/, pdfs/ and debug/. Rows go to the shared database."""

    def __init__(self, root: Path, *, db_path: Path | None = None) -> None:
        self.root = Path(root)
        self.json_dir = self.root / "json"
        self.pdf_dir = self.root / "pdfs"
        self.debug_dir = self.root / "debug"
        for folder in (self.json_dir, self.pdf_dir, self.debug_dir):
            folder.mkdir(parents=True, exist_ok=True)

        # Default to the pipeline's database; `db_path` is for tests and scratch copies.
        self.db_path = Path(db_path) if db_path else core_settings.db_path()
        self.engine = engine(self.db_path)
        ensure_schema(self.engine)
        backfill_tender_documents(self.db_path)

    # ----------------------------------------------------------------- files

    def pdf_dir_for(self, tender_id: str) -> Path:
        folder = self.pdf_dir / _slug(tender_id)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def write_debug(self, name: str, html: str) -> Path:
        path = self.debug_dir / name
        path.write_text(html, encoding="utf-8", errors="replace")
        return path

    # -------------------------------------------------------------- database

    def save_tender(
        self,
        record: TenderRecord,
        scraped_at: str | None = None,
        *,
        replace_documents: bool | None = None,
        source: ScrapeSource | None = None,
    ) -> None:
        """Upsert the tender and its awards. Document clues go in the database.

        `replace_documents` defaults to true when this record carries extracts.
        A scrape that downloaded PDFs passes true even when the extracts are
        empty, so a tender whose files yielded nothing does not keep old clues.

        `source` is the portal search of the run. Without it, as in a reindex,
        the stored search, page and run are left as they are. A PDF download
        (a tender-id search) never changes them, nor ``scraped_at``: the scrape
        that brought the tender in stays its source. Every run that saves the
        tender is kept in ``job_tenders``.
        """
        scraped = scraped_at or datetime.now(timezone.utc).isoformat()
        if replace_documents is None:
            replace_documents = bool(record.pdf_extracts)
        listing = record.listing
        download = source is not None and source.search_mode == SEARCH_TENDER_ID
        with session(self.engine) as current:
            upsert_tender(
                current,
                keep=("scraped_at",) if download else (),
                tender_id=listing.tender_id,
                title=listing.title_and_ref,
                organisation=listing.organisation_chain,
                status=listing_status(listing.status, listing.tender_stage),
                contract_date=record.aoc.get("Contract Date", ""),
                contract_value=record.aoc.get("Total Contract Value", ""),
                json_path=None,
                scraped_at=scraped,
                **_where_from(listing, source),
            )
            if source is not None:
                record_job_tender(current, source.job_id, listing.tender_id)
            if replace_documents:
                replace_tender_documents(
                    current, listing.tender_id, record.pdf_extracts
                )
            self._save_awards(current, record, replace_clues=replace_documents)

    def has_documents(self, tender_id: str) -> bool:
        with session(self.engine) as current:
            return bool(documents_for_tenders(current, [tender_id]))

    def known_ids(self) -> set[str]:
        with session(self.engine) as current:
            return known_tender_ids(current)

    def vendor_count(self) -> int:
        with session(self.engine) as current:
            return core_vendor_count(current)

    def vendor_seeds(self) -> list[dict]:
        """Every vendor, with its awards and anything found in the tender PDFs.

        This dict shape is the export's contract — see
        `export/vendor_sheet.py::vendor_row`.
        """
        with session(self.engine) as current:
            rows = current.execute(
                select(*VENDOR_SEED_COLUMNS).order_by(Vendor.vendor_id)
            ).mappings().all()
            seeds = [dict(row) for row in rows]
            for seed in seeds:
                awards = current.execute(
                    select(*AWARD_SEED_COLUMNS)
                    .join(Tender, Tender.tender_id == Award.tender_id)
                    .where(Award.vendor_id == seed["vendor_id"])
                    .order_by(Award.tender_id)
                ).mappings().all()
                seed["awards"] = [_shown_award(award) for award in awards]
            tender_ids = {
                award["tender_id"] for seed in seeds for award in seed["awards"]
            }
            stored = _extracts_by_tender(current, tender_ids)
            for seed in seeds:
                extracts: list[dict] = []
                for award in seed["awards"]:
                    extracts.extend(stored.get(award["tender_id"], []))
                seed["pdf_extracts"] = extracts
                contacts = merge_pdf_contacts(extracts)
                seed["pdf_emails"] = contacts["emails"]
                seed["pdf_phones"] = contacts["phones"]
                seed["pdf_gstins"] = contacts["gstins"]
        return seeds

    # --------------------------------------------------------------- private

    def _save_awards(
        self, current: Session, record: TenderRecord, *, replace_clues: bool
    ) -> None:
        listing = record.listing
        quotes = {row.bid_number: row for row in record.financial_bids if row.bid_number}
        city, state = infer_city_state(listing.organisation_chain)
        hint = buyer_tail(listing.organisation_chain)

        rows: list[dict[str, Any]] = []
        for row in winner_rows(record):
            vendor_id = core_upsert_vendor(
                current,
                name_raw=row.bidder_name,
                city=city,
                state=state,
                buyer_hint=hint,
                source=SOURCE_SCRAPE,
            )
            quote = quotes.get(row.bid_number)
            rows.append(
                {
                    "bid_number": row.bid_number or "",
                    "vendor_id": vendor_id,
                    "bidder_name": row.bidder_name,
                    "rank": row.rank or (quote.rank if quote else ""),
                    "quoted_value": (quote.value if quote else None) or "",
                    "awarded_value": row.value or record.aoc.get("Total Contract Value", ""),
                    "awarded_currency": row.currency or "",
                    "work_title": listing.title_and_ref,
                }
            )
        replace_awards(current, listing.tender_id, rows)
        if replace_clues:
            self._save_pdf_contacts(current, listing.tender_id, rows, record.pdf_extracts)

    def _save_pdf_contacts(
        self,
        current: Session,
        tender_id: str,
        rows: list[dict[str, Any]],
        extracts: list[dict[str, Any]],
    ) -> None:
        """Hand the winner whatever contact details this tender's work order carried.

        These come from LiteParse text of the work order, then the document model.
        Stage 2 treats them as evidence to confirm rather than as an answer.

        The tender's PDF rows are replaced, so a clue the files no longer hold
        goes. Only attributed when the tender had exactly one winner. A work order
        names one firm; with several winners on one document there is no way to
        tell whose address it is, and a wrong address is worse than none.
        """
        if len(rows) != 1:
            clear_pdf_contacts(current, tender_id)
            return
        contacts = merge_pdf_contacts(extracts)
        vendor_id = rows[0]["vendor_id"]
        replace_pdf_contacts(
            current,
            vendor_id,
            tender_id,
            contacts["emails"],
            contacts["phones"],
        )
        refresh_vendor_pdf_columns(current, vendor_id)


def _extracts_by_tender(current: Session, tender_ids: set[str]) -> dict[str, list[dict]]:
    if not tender_ids:
        return {}
    rows = current.scalars(
        select(TenderDocument).where(TenderDocument.tender_id.in_(tender_ids))
    )
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row.tender_id, []).append(
            {
                "filename": row.filename,
                "text": row.text or "",
                "text_source": row.text_source or "",
                "emails": split_clues(row.emails),
                "phones": split_clues(row.phones),
                "gstins": split_clues(row.gstins),
            }
        )
    return grouped


def _shown_award(row) -> dict:
    shown = dict(row)
    shown["awarded_value"] = format_amount(shown.get("awarded_value"))
    return shown


def _where_from(listing: ListingRow, source: ScrapeSource | None) -> dict[str, Any]:
    """The source columns this save can vouch for. The rest keep what is stored.

    A PDF download searched by tender id only to reach the files. It is not
    where the tender came from, so it vouches for nothing but the website.
    """
    values: dict[str, Any] = {"origin": ORIGIN_PORTAL}
    if source is not None and source.search_mode == SEARCH_TENDER_ID:
        return values
    if listing.page is not None:
        values["listing_page"] = listing.page
        values["listing_serial"] = serial_number(listing.serial)
    if source is not None:
        values["search_mode"] = source.search_mode
        values["search_fields"] = json.dumps(source.search_fields, sort_keys=True)
        values["scrape_job_id"] = source.job_id
    return values


def serial_number(text: str | None) -> int | None:
    """The portal's S.No as a number. ``"25."`` is 25. Anything else is None."""
    digits = (text or "").strip().rstrip(".")
    return int(digits) if digits.isdigit() else None


def listing_status(status: str | None, tender_stage: str | None) -> str:
    """The word to store in tenders.status.

    The portal's Status cell is a view icon, so its text is empty. The stage
    word (AOC, and the rest) is the value the table and the filter can use.
    A status that already has text, including a hand-typed one, is kept.
    """
    text = (status or "").strip()
    if text:
        return text
    return (tender_stage or "").strip()


def backfill_blank_status(db_path: Path, scrape_root: Path) -> int:
    """Fill blank tenders.status from the stage stored in each tender's JSON.

    An absolute json_path is read as stored. A relative path is resolved under
    scrape_root, which is the 01-scrape folder. Rows that already have a
    status are left alone. Returns how many rows changed.
    """
    updated = 0
    with session(engine(db_path)) as current:
        rows = current.scalars(
            select(Tender).where((Tender.status.is_(None)) | (Tender.status == ""))
        ).all()
        for tender in rows:
            stage = _stage_from_json(tender.json_path, scrape_root)
            if not stage:
                continue
            tender.status = stage
            updated += 1
    return updated


def _stage_from_json(json_path: str | None, scrape_root: Path) -> str:
    if not json_path:
        return ""
    path = Path(json_path)
    if not path.is_absolute():
        path = scrape_root / path
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    listing = payload.get("listing") or {}
    return listing_status(listing.get("status"), listing.get("tender_stage"))


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in value)
    return cleaned.strip("._") or "unknown"
