from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from stage1_scrape.app.pipeline import scrape_aoc
from stage1_scrape.config import DEFAULT_DELAY_SECONDS
from stage1_scrape.export.vendor_sheet import write_vendor_sheet
from stage1_scrape.persist.store import Store

log = logging.getLogger(__name__)


def run_scrape(
    out_dir: Path,
    *,
    captcha_text: str | None = None,
    captcha_solver: str = "openrouter",
    max_pages: int = 1,
    max_tenders: int | None = 1,
    delay: float = DEFAULT_DELAY_SECONDS,
    download_pdfs: bool = True,
    skip_known: bool = True,
    captcha_retries: int = 5,
    extra_fields: dict[str, str] | None = None,
    from_probe: bool = False,
) -> dict[str, Any]:
    """Scrape AOC records, then export every stored vendor to CSV and Excel.

    Finding contact details is stage 2's job (`02-enrich`), which reads the
    vendors this leaves behind with `enrichment_status = 'pending'`.
    """
    records = scrape_aoc(
        out_dir,
        captcha_text=captcha_text,
        max_pages=max_pages,
        max_tenders=max_tenders,
        delay=delay,
        download_pdfs=download_pdfs,
        skip_known=skip_known,
        captcha_retries=captcha_retries,
        extra_fields=extra_fields,
        from_probe=from_probe,
        captcha_solver=captcha_solver,
    )
    store = Store(out_dir)
    csv_path, xlsx_path = write_vendor_sheet(store, out_dir)

    if not records:
        log.info(
            "Every listing row was already saved; nothing new was scraped. "
            "Re-exported %s vendor(s). For newer AOCs try --max-pages 2 (or more), "
            "or --refresh to re-download one.",
            store.vendor_count(),
        )

    return {
        "scraped": len(records),
        "tender_ids": [record.listing.tender_id for record in records],
        "vendors": store.vendor_count(),
        "csv": str(csv_path),
        "xlsx": str(xlsx_path),
    }
