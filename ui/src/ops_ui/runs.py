"""The three runs the window can start. Each one calls the stage's own function."""

from __future__ import annotations

import contextlib
import io
import logging
import threading
from argparse import Namespace
from datetime import datetime
from pathlib import Path

from pipeline_core import settings
from pipeline_core.cli import cmd_status
from pipeline_core.db import session
from pipeline_core.queries import pending_vendors, reset_failed, reset_not_found

from ops_ui.jobs import Log

#: Portal field names, the same pairs 01-scrape/main.py sends.
_DATE_FIELDS = {
    "contract": ("fromDate", "toDate"),
    "published": ("publishedFromDate", "publishedToDate"),
}
_SCRAPE_DELAY_FLOOR = 1.5


def status_cost() -> str:
    return "Reads the database. Spends nothing and mails nobody."


def enrich_cost(database: Path, *, limit: int, source: str | None, dry_run: bool) -> str:
    with session(_engine(database)) as current:
        waiting = len(pending_vendors(current, source=source))
    this_run = min(limit, waiting)
    if dry_run:
        return (
            f"Dry run. Lists {this_run} of {waiting} waiting companies and spends nothing."
        )
    return (
        f"Looks up {this_run} companies. One paid model call each. "
        f"{waiting} are waiting in this source."
    )


def scrape_cost(*, max_tenders: int, probe: bool) -> str:
    if probe:
        return "Probe. Walks one listing page and fetches no tenders. One captcha read."
    return (
        f"Fetches up to {max_tenders} new tenders. One captcha read. "
        "Tenders already saved are skipped."
    )


def run_status(log: Log, _stop: threading.Event, database: Path) -> int:
    buffer = io.StringIO()
    errors = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
        code = cmd_status(Namespace(db=str(database)))
    text = buffer.getvalue()
    err = errors.getvalue()
    if text:
        log.write(text.rstrip("\n"))
    if err:
        log.write(err.rstrip("\n"))
    return code


def run_enrich(
    log: Log,
    stop: threading.Event,
    *,
    database: Path,
    limit: int,
    delay: float,
    dry_run: bool,
    source: str | None,
    retry_failed: bool,
    retry_not_found: bool,
) -> int:
    from stage2_enrich.runner import enrich_pending

    bind = _engine(database)
    notes: list[str] = []
    with session(bind) as current:
        if retry_failed:
            moved = reset_failed(current)
            notes.append(f"Requeued {moved} failed companies.")
        if retry_not_found:
            notes.append(
                "Requeueing not_found. That only makes sense after the lookup method itself changed."
            )
            moved = reset_not_found(current)
            notes.append(f"Requeued {moved} not_found companies.")
        work = pending_vendors(current, limit, source=source)
    for note in notes:
        log.write(note)

    log.write(enrich_cost_line(len(work), dry_run))
    for vendor in work:
        log.write(f"{vendor.vendor_id:>5}  {vendor.name_raw}")
    if dry_run or stop.is_set() or not work:
        if dry_run:
            log.write("Dry run. Nothing was looked up.")
        elif stop.is_set():
            log.write("Stopped before any lookup. Nothing was spent.")
        return 0

    from stage2_enrich import settings as enrich_settings

    try:
        enrich_settings.require_env()
    except RuntimeError as exc:
        log.write(str(exc))
        return 1
    log.write(f"Model {enrich_settings.model()}.")

    def report(vendor, status, detail) -> None:
        log.write(f"{vendor.vendor_id:>5}  {vendor.name_raw}  {status}  {detail}")

    summary = enrich_pending(
        limit=limit,
        delay=delay,
        vendors=work,
        source=source,
        report=report,
        should_stop=stop.is_set,
    )
    log.write(summary.render())
    if stop.is_set():
        log.write("Stopped. Companies already answered are saved.")
    return 0


def run_scrape(
    log: Log,
    stop: threading.Event,
    *,
    max_tenders: int,
    max_pages: int,
    delay: float,
    download_pdfs: bool,
    refresh: bool,
    probe: bool,
    from_date: str,
    to_date: str,
    date_field: str,
) -> int:
    from stage1_scrape.app.run import run_scrape as scrape
    from stage1_scrape.domain.errors import CaptchaError

    delay = max(delay, _SCRAPE_DELAY_FLOOR)
    extra = _dates(from_date, to_date, date_field, log)
    if extra is None:
        return 1
    log.write(scrape_cost(max_tenders=max_tenders, probe=probe))
    log.write(
        "Captcha is read automatically. If that fails, this run stops. "
        "Typing the captcha in the page is not built yet."
    )
    out = settings.project_root() / "01-scrape" / "data"
    handler = _Logger(log)
    logger = logging.getLogger("stage1_scrape")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        info = scrape(
            out,
            captcha_solver="openrouter",
            captcha_retries=10,
            max_pages=1 if probe else max_pages,
            max_tenders=0 if probe else max_tenders,
            delay=delay,
            download_pdfs=False if probe else download_pdfs,
            skip_known=not refresh,
            extra_fields=extra,
            should_stop=stop.is_set,
        )
    except CaptchaError as exc:
        log.write(str(exc))
        log.write("The captcha was not read. Nothing waited for a terminal.")
        return 1
    finally:
        logger.removeHandler(handler)
    if probe:
        log.write("Probe finished. No tenders were fetched.")
        return 0
    if stop.is_set():
        log.write("Stopped. Tenders already saved are kept.")
    log.write(
        f"Scraped {info['scraped']} new tender(s). "
        f"{info['vendors']} companies are stored."
    )
    return 0


def enrich_cost_line(count: int, dry_run: bool) -> str:
    if dry_run:
        return f"{count} companies would be listed. Nothing will be spent."
    return f"{count} companies. One paid model call each."


def _dates(from_date: str, to_date: str, date_field: str, log: Log) -> dict[str, str] | None:
    if date_field not in _DATE_FIELDS:
        log.write("Date field must be contract or published.")
        return None
    parsed = {}
    for label, raw in (("From", from_date.strip()), ("To", to_date.strip())):
        if not raw:
            continue
        try:
            parsed[label] = datetime.strptime(raw, "%d/%m/%Y")
        except ValueError:
            log.write(f"{label} date {raw!r} is not dd/MM/yyyy.")
            return None
    if len(parsed) == 2 and parsed["From"] > parsed["To"]:
        log.write("From date is after to date.")
        return None
    start, end = _DATE_FIELDS[date_field]
    fields = {}
    if from_date.strip():
        fields[start] = from_date.strip()
    if to_date.strip():
        fields[end] = to_date.strip()
    return fields


def _engine(database: Path):
    from pipeline_core.db import engine

    return engine(database)


class _Logger(logging.Handler):
    def __init__(self, log: Log) -> None:
        super().__init__()
        self._log = log

    def emit(self, record: logging.LogRecord) -> None:
        self._log.write(self.format(record))
