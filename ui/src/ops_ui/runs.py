"""The runs the window can start. Each one calls the stage's own function."""

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
from pipeline_core.models import STATUS_FAILED, Vendor
from pipeline_core.queries import (
    count_not_found_ready,
    pending_vendors,
    prepare_lookup,
    preview_lookup,
    reset_failed,
    reset_not_found,
)

from ops_ui.jobs import Log

#: Portal field names, the same pairs 01-scrape/main.py sends.
_DATE_FIELDS = {
    "contract": ("fromDate", "toDate"),
    "published": ("publishedFromDate", "publishedToDate"),
}
_SCRAPE_DELAY_FLOOR = 1.5
_MAIL_DELAY_FLOOR = 5.0


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


def scrape_cost(*, max_tenders: int | None, probe: bool) -> str:
    if probe:
        return "Probe. Walks one listing page and fetches no tenders. One captcha read."
    if max_tenders is None:
        return (
            "Fetches every new tender in the date range. One captcha read. "
            "Tenders already saved are skipped."
        )
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
    vendor_ids: list[int] | None = None,
) -> int:
    from stage2_enrich.runner import enrich_pending

    bind = _engine(database)
    notes: list[str] = []
    with session(bind) as current:
        if retry_failed:
            if dry_run:
                count = _waiting_retry(current, STATUS_FAILED)
                notes.append(_would_requeue(count, "failed"))
            else:
                moved = reset_failed(current)
                notes.append(f"Requeued {moved} failed companies.")
        if retry_not_found:
            notes.append(
                "Requeueing not_found. That only makes sense after the lookup method itself changed."
            )
            if dry_run:
                count = count_not_found_ready(current)
                notes.append(_would_requeue(count, "not_found"))
            else:
                moved = reset_not_found(current)
                notes.append(f"Requeued {moved} not_found companies.")
        if vendor_ids is not None:
            work = (
                preview_lookup(current, vendor_ids)
                if dry_run
                else prepare_lookup(current, vendor_ids)
            )
        else:
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
    max_tenders: int | None,
    max_pages: int | None,
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
    from ops_ui.jobs import CaptchaTimedOut, wait_for_captcha

    log.write(scrape_cost(max_tenders=max_tenders, probe=probe))
    log.write("Captcha is read automatically. If that fails, type it on this page.")

    def ask(image_path: Path) -> str:
        try:
            return wait_for_captcha(log, stop, image_path)
        except CaptchaTimedOut as exc:
            raise CaptchaError(str(exc)) from exc
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
            ask=ask,
        )
    except CaptchaError as exc:
        log.write(str(exc))
        if stop.is_set():
            log.write("Stopped. Tenders already saved are kept.")
            return 0
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


def run_fetch_pdfs(log: Log, stop: threading.Event, *, tender_id: str) -> int:
    """One tender, found by the portal's tender-id box, then its PDFs."""
    from stage1_scrape.app.pipeline import fetch_tender_pdfs
    from stage1_scrape.domain.errors import CaptchaError, ParseError

    from ops_ui.jobs import CaptchaTimedOut, pdf_zip_path, wait_for_captcha

    log.write(f"Fetching PDFs for {tender_id}. One captcha read.")
    log.write("Captcha is read automatically. If that fails, type it on this page.")

    def ask(image_path: Path) -> str:
        try:
            return wait_for_captcha(log, stop, image_path)
        except CaptchaTimedOut as exc:
            raise CaptchaError(str(exc)) from exc

    out = settings.project_root() / "01-scrape" / "data"
    handler = _Logger(log)
    logger = logging.getLogger("stage1_scrape")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        info = fetch_tender_pdfs(
            out,
            tender_id,
            zip_path=pdf_zip_path(log.job_id),
            captcha_solver="openrouter",
            captcha_retries=10,
            delay=_SCRAPE_DELAY_FLOOR,
            should_stop=stop.is_set,
            ask=ask,
        )
    except CaptchaError as exc:
        log.write(str(exc))
        return 0 if stop.is_set() else 1
    except ParseError as exc:
        log.write(str(exc))
        return 1
    finally:
        logger.removeHandler(handler)
    log.write(info["message"])
    if info.get("stopped") or stop.is_set():
        return 0
    return 0 if info.get("ok") else 1


def enrich_cost_line(count: int, dry_run: bool) -> str:
    if dry_run:
        return f"{count} companies would be listed. Nothing will be spent."
    return f"{count} companies. One paid model call each."


def _would_requeue(count: int, status: str) -> str:
    noun = "company" if count == 1 else "companies"
    return f"Dry run. Would put {count} {status} {noun} back on the queue."


def _waiting_retry(current, status: str) -> int:
    """How many rows a requeue would move. Dry run counts them and writes nothing."""
    return int(current.query(Vendor).filter(Vendor.enrichment_status == status).count())


def portal_date(raw: str) -> str:
    """The portal's dd/MM/yyyy. An ISO day from the date picker is converted."""
    text = (raw or "").strip()
    if not text:
        return ""
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return text


def date_error(from_date: str, to_date: str, date_field: str) -> str | None:
    """The same date checks a scrape refuses before it starts."""
    if date_field not in _DATE_FIELDS:
        return "Date field must be contract or published."
    parsed = {}
    for label, raw in (("From", from_date.strip()), ("To", to_date.strip())):
        if not raw:
            continue
        try:
            parsed[label] = datetime.strptime(raw, "%d/%m/%Y")
        except ValueError:
            return f"{label} date {raw!r} is not dd/MM/yyyy."
    if len(parsed) == 2 and parsed["From"] > parsed["To"]:
        return "From date is after to date."
    return None


def _dates(from_date: str, to_date: str, date_field: str, log: Log) -> dict[str, str] | None:
    error = date_error(from_date, to_date, date_field)
    if error:
        log.write(error)
        return None
    start, end = _DATE_FIELDS[date_field]
    fields = {}
    if from_date.strip():
        fields[start] = from_date.strip()
    if to_date.strip():
        fields[end] = to_date.strip()
    return fields


def run_outreach(
    log: Log,
    stop: threading.Event,
    *,
    database: Path,
    awards: list[tuple[int, str]] | None = None,
    include_sent: bool = False,
    send: bool,
    preflight: bool,
    transport: str,
    delay: float,
    redirect_to: str,
    vendor_ids: list[int] | None = None,
    letter=None,
) -> int:
    """Preview, preflight, or send. A live send records each attempt before the next.

    ``vendor_ids`` remains so an older call still narrows by company. Mail from
    the table passes ``awards`` instead, one entry per tender. ``letter`` is a
    one-off for this run. When it is omitted, the saved letter is used. Neither
    path writes the saved letter.
    """
    from pipeline_core.emailcheck import EMAIL_RE
    from pipeline_core.queries import outreach_targets

    campaign = load_campaign()
    bind = _engine(database)
    override = redirect_to.strip() or None
    delay = max(delay, _MAIL_DELAY_FLOOR)
    if preflight:
        gap = credential_gap(transport, preflight=True)
        if gap:
            log.write(gap)
            return 1
        return _captured(log, lambda: campaign.preflight(transport, bind=bind))

    if not awards and not vendor_ids:
        log.write("Choose companies on Awards. Nothing was sent.")
        return 0

    with session(bind) as current:
        queue = outreach_targets(current, include_sent=include_sent)
    people = [person for person in queue if EMAIL_RE.match(person.email or "")]
    if awards is not None:
        wanted = set(awards)
        people = [person for person in people if (person.vendor_id, person.tender_id) in wanted]
    elif vendor_ids is not None:
        wanted_vendors = set(vendor_ids)
        people = [person for person in people if person.vendor_id in wanted_vendors]
    if include_sent:
        log.write("Send again is on. Awards already sent are included.")
    if letter is None:
        log.write("Letter: the saved default.")
    else:
        log.write("Letter: phrases for this send. The saved default is unchanged.")
    log.write(f"{len(people)} message(s) can be mailed.")
    if not people:
        log.write("Nobody with an address is waiting. Nothing was sent.")
        return 0
    if not send:
        return _captured(
            log,
            lambda: campaign.dry_run(people, override, bind=bind, letter=letter),
        )

    refusal = live_send_refusal(transport, bind=bind, letter=letter)
    if refusal:
        log.write(refusal)
        return 1
    if override:
        log.write(
            f"Every message goes to {override}. Each company is still marked sent."
        )

    def deliver() -> None:
        campaign.send_all(
            people,
            delay,
            override,
            transport,
            should_stop=stop.is_set,
            bind=bind,
            letter=letter,
        )

    code = _captured(log, deliver)
    if stop.is_set():
        log.write("Stopped. Messages already sent are saved.")
    return code


def live_send_refusal(transport: str, bind=None, source: str = "page", letter=None) -> str | None:
    """Why a live send must not start. Preview is allowed either way."""
    campaign = load_campaign()
    if letter is None:
        problem = campaign.letter_refusal(bind, source)
    else:
        problem = campaign.refusal_for(letter)
    if problem:
        return problem
    return credential_gap(transport)


def credential_gap(transport: str, *, preflight: bool = False) -> str | None:
    """A missing mailbox setting, after the outreach .env has been read."""
    import os

    load_campaign()
    if transport == "gmail":
        if not os.getenv("GMAIL_USER", "").strip() or not os.getenv(
            "GMAIL_APP_PASSWORD", ""
        ).strip():
            return "Gmail needs GMAIL_USER and GMAIL_APP_PASSWORD in 03-outreach/.env."
        return None
    if transport == "graph":
        if not os.getenv("GRAPH_CLIENT_ID", "").strip() or not os.getenv(
            "GRAPH_TENANT_ID", ""
        ).strip():
            return "Microsoft Graph needs GRAPH_CLIENT_ID and GRAPH_TENANT_ID in 03-outreach/.env."
        if preflight and not os.getenv("OUTLOOK_USER", "").strip():
            return "Preflight needs OUTLOOK_USER in 03-outreach/.env."
        return None
    return "Transport must be gmail or graph."


def load_campaign():
    import sys

    root = str(settings.project_root() / "03-outreach")
    if root not in sys.path:
        sys.path.insert(0, root)
    import campaign

    return campaign


def _captured(log: Log, call) -> int:
    buffer = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buffer):
            result = call()
        if isinstance(result, int):
            code = result
    except SystemExit as exc:
        code = 1
        text = exc.code if isinstance(exc.code, str) else str(exc)
        buffer.write(text + "\n")
    written = buffer.getvalue().strip()
    if written:
        log.write(written)
    return code


def _engine(database: Path):
    from pipeline_core.db import engine

    return engine(database)


class _Logger(logging.Handler):
    def __init__(self, log: Log) -> None:
        super().__init__()
        self._log = log

    def emit(self, record: logging.LogRecord) -> None:
        self._log.write(self.format(record))
