from __future__ import annotations

import json
import logging
from pathlib import Path

from stage1_scrape.config import APP_URL, DEFAULT_DELAY_SECONDS, TENDER_STATUS_AOC
from stage1_scrape.domain.errors import CaptchaError, ParseError
from stage1_scrape.domain.models import TenderRecord
from stage1_scrape.parse import (
    find_next_page,
    find_stage_summary_url,
    pager_debug_html,
    parse_result_count,
    parse_results_table,
    parse_summary_page,
    search_still_showing_form,
)
from stage1_scrape.persist import Store, download_document
from stage1_scrape.persist.pdf_text import extract_folder
from stage1_scrape.scraping import (
    GePNICClient,
    extract_form_fields,
    override_fields,
    resolve_captcha_code,
    save_captcha,
)

log = logging.getLogger(__name__)


def submit_search(
    client: GePNICClient,
    search_html: str,
    captcha_text: str,
    tender_status: str = TENDER_STATUS_AOC,
    extra: dict[str, str] | None = None,
) -> str:
    """POST every field from Step 0, overriding AOC status, captcha, and Search."""
    fields = extract_form_fields(search_html)
    updates = {
        "tenderStatus": tender_status,
        "captchaText": captcha_text,
        "Search": "Search",
    }
    if extra:
        updates.update({key: value for key, value in extra.items() if value})
    payload = override_fields(fields, updates)
    return client.post(payload, url=APP_URL, referer=client._last_url).text


def scrape_listing_pages(
    client: GePNICClient,
    first_html: str,
    store: Store,
    max_pages: int,
) -> list:
    """Walk listing pages via Next. Captures unmatched pager HTML for Step 2b."""
    html = first_html
    listings = []
    for page_no in range(1, max_pages + 1):
        rows = parse_results_table(html)
        count = parse_result_count(html)
        log.info(
            "Listing page %s: %s rows%s",
            page_no,
            len(rows),
            f" ({count[0]}-{count[1]} of {count[2]})" if count else "",
        )
        if not rows:
            store.write_debug(f"listing_page_{page_no}_empty.html", html)
            break
        store.write_debug(f"listing_page_{page_no}.html", html)
        listings.extend(rows)
        if page_no >= max_pages:
            break
        nxt = find_next_page(html)
        if nxt is None or not nxt.url:
            debug = pager_debug_html(html)
            path = store.write_debug("pager_unconfirmed.html", debug or html)
            log.warning(
                "Next-page link not found (Step 2b still unconfirmed). "
                "Saved pager markup to %s",
                path,
            )
            break
        log.info("Following Next (%s): %s", nxt.note, nxt.url)
        html = client.get(nxt.url).text
    return listings


def scrape_one_tender(
    client: GePNICClient,
    listing,
    store: Store,
    download_pdfs: bool,
) -> TenderRecord:
    status_html = client.get(listing.status_page_url).text
    store.write_debug(f"status_{_slug_id(listing.tender_id)}.html", status_html)
    stage_url = find_stage_summary_url(status_html)
    summary_html = client.get(stage_url).text
    store.write_debug(f"summary_{_slug_id(listing.tender_id)}.html", summary_html)
    parsed = parse_summary_page(summary_html)
    record = TenderRecord(
        listing=listing,
        header=parsed["header"],
        stage_summary_url=stage_url,
        bids=parsed["bids"],
        financial_bids=parsed["financial_bids"],
        awarded_bids=parsed["awarded_bids"],
        bid_opening=parsed["bid_opening"],
        technical_eval=parsed["technical_eval"],
        finance_eval=parsed["finance_eval"],
        aoc=parsed["aoc"],
        documents=parsed["documents"],
    )
    if download_pdfs:
        dest = store.pdf_dir_for(listing.tender_id)
        for doc in record.documents:
            try:
                path = download_document(client, doc, dest)
                record.downloaded_files.append(str(path))
            except Exception as exc:
                log.warning("PDF skipped for %s (%s): %s", listing.tender_id, doc.filename, exc)
    record.pdf_extracts = extract_folder(store.pdf_dir_for(listing.tender_id))
    store.save_tender(record)
    return record


def scrape_aoc(
    out_dir: Path,
    captcha_text: str | None = None,
    max_pages: int = 1,
    max_tenders: int | None = None,
    delay: float = DEFAULT_DELAY_SECONDS,
    download_pdfs: bool = True,
    skip_known: bool = True,
    captcha_retries: int = 5,
    extra_fields: dict[str, str] | None = None,
    from_probe: bool = False,
    captcha_solver: str = "manual",
) -> list[TenderRecord]:
    store = Store(out_dir)
    client = GePNICClient(delay=delay)
    results_html = _search_with_captcha(
        client,
        store,
        captcha_text=captcha_text,
        retries=captcha_retries,
        extra_fields=extra_fields,
        from_probe=from_probe,
        captcha_solver=captcha_solver,
    )
    listings = scrape_listing_pages(client, results_html, store, max_pages=max_pages)
    known = store.known_ids() if skip_known else set()
    records: list[TenderRecord] = []
    for listing in listings:
        if max_tenders is not None and len(records) >= max_tenders:
            break
        if skip_known and listing.tender_id in known:
            log.info("Skipping already-saved %s", listing.tender_id)
            continue
        try:
            record = scrape_one_tender(client, listing, store, download_pdfs=download_pdfs)
        except ParseError as exc:
            log.warning("Tender %s skipped: %s", listing.tender_id, exc)
            continue
        records.append(record)
        known.add(listing.tender_id)
        log.info(
            "Saved %s — %s bid(s), %s awarded, %s pdf(s)",
            listing.tender_id,
            len(record.bids),
            len(record.awarded_bids),
            len(record.downloaded_files),
        )
    return records


def _search_with_captcha(
    client: GePNICClient,
    store: Store,
    captcha_text: str | None,
    retries: int,
    extra_fields: dict[str, str] | None = None,
    from_probe: bool = False,
    captcha_solver: str = "manual",
) -> str:
    last_error: Exception | None = None
    session_path = store.root / "session.pkl"
    form_path = store.debug_dir / "search_form.html"
    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        if from_probe and attempt == 1:
            if not session_path.exists() or not form_path.exists():
                raise CaptchaError(
                    "No probe session found. Run `python -m stage1_scrape probe --out data` first."
                )
            client.load_state(session_path)
            html = form_path.read_text(encoding="utf-8", errors="replace")
            image_path = store.root / "captcha.png"
            if not image_path.exists():
                image_path = save_captcha(html, store.root / "captcha")
        else:
            html = client.fetch_search_form()
            client.save_state(session_path)
            store.write_debug("search_form.html", html)
            image_path = save_captcha(html, store.root / "captcha")
        try:
            code = resolve_captcha_code(
                image_path,
                captcha_text if attempt == 1 else None,
                solver=captcha_solver,
            )
        except CaptchaError as exc:
            last_error = exc
            log.warning("Captcha solve failed (attempt %s/%s): %s", attempt, attempts, exc)
            captcha_text = None
            from_probe = False
            continue
        results = submit_search(client, html, code, extra=extra_fields)
        if search_still_showing_form(results):
            store.write_debug(f"search_rejected_{attempt}.html", results)
            last_error = CaptchaError(
                "Search was rejected (captcha still on the page). Try again."
            )
            log.warning(
                "Captcha/search rejected (attempt %s/%s), submitted=%s",
                attempt,
                attempts,
                code,
            )
            captcha_text = None
            from_probe = False
            continue
        rows = parse_results_table(results)
        if not rows:
            store.write_debug("search_no_rows.html", results)
            raise ParseError(
                "Search succeeded but no 'View Tender Status' rows were found. "
                f"HTML saved to {store.debug_dir / 'search_no_rows.html'}"
            )
        client.save_state(session_path)
        return results
    assert last_error is not None
    raise last_error


def probe_search_form(out_dir: Path, delay: float = 1.0) -> dict:
    """Step 0 only: fetch the form, save captcha, report field names."""
    store = Store(out_dir)
    client = GePNICClient(delay=delay)
    html = client.fetch_search_form()
    client.save_state(store.root / "session.pkl")
    store.write_debug("search_form.html", html)
    fields = extract_form_fields(html)
    captcha_path = save_captcha(html, store.root / "captcha")
    names = [name for name, _ in fields]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    token = next((v for n, v in fields if n == "tokenSecret"), "")
    formids = next((v for n, v in fields if n == "formids"), "")
    return {
        "field_count": len(fields),
        "unique_names": len(set(names)),
        "duplicate_names": duplicates,
        "tokenSecret": token,
        "formids": formids,
        "captcha_path": str(captcha_path),
        "search_html": str(store.debug_dir / "search_form.html"),
        "session": str(store.root / "session.pkl"),
    }


def reindex_saved(out_dir: Path) -> int:
    """Re-parse saved summary HTML into JSON plus vendor/award tables. No HTTP."""
    store = Store(out_dir)
    count = 0
    for path in sorted(store.json_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        record = TenderRecord.from_dict(data)
        summary_path = store.debug_dir / f"summary_{_slug_id(record.listing.tender_id)}.html"
        if summary_path.exists():
            parsed = parse_summary_page(
                summary_path.read_text(encoding="utf-8", errors="replace")
            )
            record.financial_bids = parsed["financial_bids"]
            record.awarded_bids = parsed["awarded_bids"]
            if parsed.get("aoc"):
                record.aoc = {**record.aoc, **parsed["aoc"]}
            if parsed.get("header"):
                record.header = {**record.header, **parsed["header"]}
        record.pdf_extracts = extract_folder(store.pdf_dir_for(record.listing.tender_id))
        store.save_tender(record, scraped_at=data.get("scraped_at"))
        count += 1
        log.info(
            "Indexed %s — %s financial row(s), %s awarded",
            record.listing.tender_id,
            len(record.financial_bids),
            len(record.awarded_bids),
        )
    return count


def _slug_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in value)
    return cleaned.strip("._") or "unknown"
