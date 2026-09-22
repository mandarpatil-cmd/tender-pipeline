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
from pipeline_core.models import SOURCE_SCRAPE, Award, Vendor
from pipeline_core.naming import buyer_tail, infer_city_state
from pipeline_core.queries import (
    known_tender_ids,
    replace_awards,
    set_pdf_contacts,
    upsert_tender,
)
from pipeline_core.queries import upsert_vendor as core_upsert_vendor
from pipeline_core.queries import vendor_count as core_vendor_count
from sqlalchemy import select
from sqlalchemy.orm import Session

from stage1_scrape.domain.enrichment import winner_rows
from stage1_scrape.domain.models import TenderRecord
from stage1_scrape.persist.pdf_text import extract_folder, merge_pdf_contacts

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
    Award.contract_date,
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

    def save_tender(self, record: TenderRecord, scraped_at: str | None = None) -> Path:
        """Write the raw JSON, then upsert the tender and its awards."""
        payload = record.to_dict()
        payload["scraped_at"] = scraped_at or datetime.now(timezone.utc).isoformat()
        path = self.json_dir / f"{_slug(record.listing.tender_id)}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        listing = record.listing
        with session(self.engine) as current:
            upsert_tender(
                current,
                tender_id=listing.tender_id,
                title=listing.title_and_ref,
                organisation=listing.organisation_chain,
                status=listing.status,
                contract_date=record.aoc.get("Contract Date", ""),
                contract_value=record.aoc.get("Total Contract Value", ""),
                json_path=str(path),
                scraped_at=payload["scraped_at"],
            )
            self._save_awards(current, record)
        return path

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
                    .where(Award.vendor_id == seed["vendor_id"])
                    .order_by(Award.tender_id)
                ).mappings().all()
                seed["awards"] = [dict(award) for award in awards]
                extracts: list[dict] = []
                for award in seed["awards"]:
                    extracts.extend(self._pdf_extracts_for_tender(award["tender_id"]))
                seed["pdf_extracts"] = extracts
                contacts = merge_pdf_contacts(extracts)
                seed["pdf_emails"] = contacts["emails"]
                seed["pdf_phones"] = contacts["phones"]
                seed["pdf_gstins"] = contacts["gstins"]
        return seeds

    # --------------------------------------------------------------- private

    def _save_awards(self, current: Session, record: TenderRecord) -> None:
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
                    "contract_date": record.aoc.get("Contract Date", ""),
                    "contract_value": record.aoc.get("Total Contract Value", ""),
                    "work_title": listing.title_and_ref,
                }
            )
        replace_awards(current, listing.tender_id, rows)
        self._save_pdf_contacts(current, listing.tender_id, rows)

    def _save_pdf_contacts(
        self, current: Session, tender_id: str, rows: list[dict[str, Any]]
    ) -> None:
        """Hand the winner whatever contact details the work order carried.

        These are free and come straight from the awarding document, so they beat
        anything a model can guess -- but they are OCR'd off a scan, so stage 2
        treats them as evidence to confirm rather than as an answer.

        Only attributed when the tender had exactly one winner. A work order names
        one firm; with several winners on one document there is no way to tell
        whose address it is, and a wrong address is worse than none.
        """
        if len(rows) != 1:
            return
        contacts = merge_pdf_contacts(self._pdf_extracts_for_tender(tender_id))
        set_pdf_contacts(
            current,
            rows[0]["vendor_id"],
            email=next(iter(contacts["emails"]), None),
            phone=next(iter(contacts["phones"]), None),
        )

    def _pdf_extracts_for_tender(self, tender_id: str) -> list[dict]:
        path = self.json_dir / f"{_slug(tender_id)}.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            stored = data.get("pdf_extracts") or []
            if stored:
                return list(stored)
        return extract_folder(self.pdf_dir_for(tender_id))


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in value)
    return cleaned.strip("._") or "unknown"
