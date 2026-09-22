"""Run the whole stage1-scrape job in one go.

    portal -> SQLite -> data/exports/vendors.csv / .xlsx

Every vendor this finds is left with enrichment_status = 'pending'. Looking up
their contact details is stage 2's job (`02-enrich`), which reads them straight
out of the shared database.

Usage:
    python main.py

Change behaviour by editing the SETTINGS block below.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Running this file directly does not put these on sys.path: this project's own
# `src/`, and the shared `core` package, which lives in a sibling folder.
_HERE = Path(__file__).resolve().parent
for _path in (_HERE / "src", _HERE.parent / "core" / "src"):
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _importable(name: str) -> bool:
    from importlib.util import find_spec

    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _require(*modules: "tuple[str, str]") -> None:
    """Fail with the command that fixes it, not with an import traceback."""
    missing = [package for module, package in modules if not _importable(module)]
    if not missing:
        return
    raise SystemExit(
        "Missing: " + ", ".join(missing) + "\n\n"
        "The pipeline's shared virtualenv has them; your current interpreter does not.\n"
        "Run it with:\n"
        f"    cd {_HERE}\n"
        "    uv run python main.py\n\n"
        "or call the venv interpreter directly:\n"
        "    ..\\.venv\\Scripts\\python.exe main.py\n\n"
        "If there is no .venv yet, create it from the repo root:\n"
        "    uv venv --python 3.13\n"
        "    uv pip install -r requirements.txt"
    )


# ============================== SETTINGS ==============================
OUT_DIR = "data"          # where json/, pdfs/, debug/, exports/ and the DB live
MAX_TENDERS = 1           # NEW tenders to fetch this run (already-stored ones do not count)
MAX_PAGES = 1             # listing pages to walk (10 rows each) -- the candidate pool
REFRESH = False           # True = re-fetch tenders already saved in SQLite
DOWNLOAD_PDFS = True      # False = skip PDF downloads (faster, loses contacts)
DELAY = 1.5               # seconds between portal requests -- do not lower

# --- date filter -------------------------------------------------------
# Blank both to search every date.
#
# Why bother: the skip-list is applied *after* pagination, so MAX_PAGES only
# sets how many rows are looked at -- rows already in SQLite are then thrown
# away. Paging past what you already hold to reach new records wastes requests.
# A date window you have never scraped sidesteps that: every row is a candidate.
#
# Format is dd/MM/yyyy -- the portal's own, from the title attribute on the
# input. Anything else is rejected below rather than silently ignored by the
# server.
FROM_DATE = ""            # e.g. "01/07/2025"; blank both to search every date
TO_DATE = ""              # e.g. "31/12/2025"

# Which pair of portal date fields FROM_DATE/TO_DATE drive:
#   "contract"  -> fromDate / toDate                    ("From Date" / "To Date")
#   "published" -> publishedFromDate / publishedToDate  ("Published From" / "To")
# Which one AOC actually filters on is not documented; try "contract" first and
# probe (below) to see whether the window took.
DATE_FIELD = "contract"

# True  -> PROBE: walk one listing page, fetch no tenders, download no PDFs.
#          ~10 seconds, and it costs the same single captcha call as a real run,
#          so it is nearly free. Use it to confirm a date window applied before
#          committing to several minutes of fetching.
# False -> the real run, using MAX_TENDERS / MAX_PAGES above.
PROBE = False
# -----------------------------------------------------------------------

OCR_ATTEMPTS = 10          # captcha OCR tries before falling back to typing
MANUAL_ATTEMPTS = 3       # typed-captcha tries after OCR gives up
# ======================================================================


_require(
    ("bs4", "beautifulsoup4"),
    ("lxml", "lxml"),
    ("requests", "requests"),
    ("pypdf", "pypdf"),
    ("dotenv", "python-dotenv"),
    ("pandas", "pandas"),
    ("openpyxl", "openpyxl"),
    ("sqlalchemy", "SQLAlchemy"),
)

from stage1_scrape.app.pipeline import scrape_aoc                    # noqa: E402
from stage1_scrape.domain.errors import CaptchaError                  # noqa: E402
from stage1_scrape.export.vendor_sheet import write_vendor_sheet      # noqa: E402
from stage1_scrape.persist.store import Store                         # noqa: E402
from stage1_scrape.settings import (                                  # noqa: E402
    openrouter_api_key,
    openrouter_ocr_model,
)

TOTAL_STEPS = 4

#: SETTINGS names -> the portal's own field names, per DATE_FIELD.
DATE_FIELDS = {
    "contract": ("fromDate", "toDate"),
    "published": ("publishedFromDate", "publishedToDate"),
}


def date_filter() -> dict[str, str]:
    """FROM_DATE/TO_DATE as portal fields, validated. Empty when both are blank.

    A wrong format is rejected here rather than sent: the portal's date inputs
    are readonly and driven by a calendar widget, so nothing on the other end
    tells you the string was unusable -- you would just get an unfiltered
    listing and think the window had applied.
    """
    if DATE_FIELD not in DATE_FIELDS:
        raise SystemExit(
            f"DATE_FIELD is {DATE_FIELD!r} - it must be one of "
            f"{', '.join(sorted(DATE_FIELDS))}. Fix it in the SETTINGS block."
        )

    from datetime import datetime

    values = {"FROM_DATE": FROM_DATE.strip(), "TO_DATE": TO_DATE.strip()}
    parsed: dict[str, datetime] = {}
    for label, raw in values.items():
        if not raw:
            continue
        try:
            parsed[label] = datetime.strptime(raw, "%d/%m/%Y")
        except ValueError:
            raise SystemExit(
                f"{label} is {raw!r}, which is not dd/MM/yyyy.\n"
                'The portal wants day first, e.g. "01/07/2025" for 1 July 2025.'
            ) from None

    if len(parsed) == 2 and parsed["FROM_DATE"] > parsed["TO_DATE"]:
        raise SystemExit(
            f"FROM_DATE ({values['FROM_DATE']}) is after TO_DATE ({values['TO_DATE']})."
        )

    from_field, to_field = DATE_FIELDS[DATE_FIELD]
    # submit_search drops blank values, so a half-open window is fine.
    return {
        field: value
        for field, value in ((from_field, values["FROM_DATE"]), (to_field, values["TO_DATE"]))
        if value
    }


def step(n: int, title: str) -> None:
    print(f"\n[{n}/{TOTAL_STEPS}] {title}")


def say(message: str) -> None:
    print(f"      {message}")


def banner() -> None:
    print("=" * 62)
    print("  stage1-scrape  --  Award of Contract scraper  (stage 1 of 3)")
    print("=" * 62)
    print(f"  out={OUT_DIR}  max_tenders={MAX_TENDERS}  max_pages={MAX_PAGES}")
    print(f"  refresh={REFRESH}  pdfs={DOWNLOAD_PDFS}  delay={DELAY}s")
    window = date_filter()
    if window:
        shown = "  ".join(f"{field}={value}" for field, value in window.items())
        print(f"  dates   {shown}   ({DATE_FIELD})")
    else:
        print("  dates   (none -- searching every date)")


def preflight() -> None:
    """Stop early with a useful message rather than failing mid-scrape."""
    step(1, "Preflight")
    if not openrouter_api_key():
        say("OPENROUTER_API_KEY is not set.")
        say("Copy .env.example to .env and add your key from")
        say("https://openrouter.ai/keys")
        if OCR_ATTEMPTS > 0:
            say("(needed for captcha OCR; set OCR_ATTEMPTS = 0 to type it instead)")
        raise SystemExit(1)
    say("OPENROUTER_API_KEY  ok")
    say(f"captcha model       {openrouter_ocr_model()}")


#: Roughly what an unfiltered AOC search matches, for comparison in the output.
UNFILTERED_AOC_RECORDS = 159_019


def report_search(out: Path) -> None:
    """Say what the search actually matched, by reading the saved listing page.

    This is the only way to tell a filtered search from an unfiltered one. The
    listing table has no date column -- S.No, Tender ID, Title, Organisation
    Chain, Tender Stage, Status -- so the record count is the signal, and the
    years in the tender IDs corroborate it.
    """
    from stage1_scrape.parse import parse_results_table, parse_total_records

    page = Path(out) / "debug" / "listing_page_1.html"
    if not page.is_file():
        return
    html = page.read_text(encoding="utf-8", errors="replace")

    window = date_filter()
    total = parse_total_records(html)

    if total is None:
        say("portal returned  (no record count on the page)")
    else:
        say(f"portal returned  {total:,} record(s)")
    if not window:
        if total is not None:
            say(f"                 unfiltered AOC is ~{UNFILTERED_AOC_RECORDS:,}")
        return

    # The ID year is the tender's own numbering, which may track publication
    # rather than the contract date, so it only corroborates. The record count
    # is the real signal: a window that changed nothing did not apply.
    years = sorted({row.tender_id.split("_")[0] for row in parse_results_table(html)})
    if years:
        say(f"tender ID years  {', '.join(years)}")

    wanted = {value.split("/")[-1] for value in window.values()}
    unchanged = total is not None and total >= UNFILTERED_AOC_RECORDS * 0.99
    years_disagree = bool(years) and bool(wanted) and not (set(years) & wanted)

    if unchanged or years_disagree:
        why = (
            f"the count is unchanged from an unfiltered search (~{UNFILTERED_AOC_RECORDS:,})"
            if unchanged
            else f"IDs are {'/'.join(years)}, window is {'/'.join(sorted(wanted))}"
        )
        say(f"dates do NOT appear to have applied -- {why}")
        other = "published" if DATE_FIELD == "contract" else "contract"
        say(f"-> try DATE_FIELD = {other!r} and probe again")
    elif total is None:
        say("cannot confirm the window applied -- no count to compare against")
    else:
        say("dates appear to have APPLIED")


def scrape(out: Path) -> list[str]:
    """Walk the portal. Try OCR for the captcha, fall back to typing it."""
    step(2, "Scrape the portal")
    # A probe still solves the captcha and still walks a listing page -- it just
    # fetches nothing off it. `max_tenders=0` makes scrape_aoc's loop break
    # before the first tender, after the page has been walked and saved.
    common = dict(
        max_pages=1 if PROBE else MAX_PAGES,
        max_tenders=0 if PROBE else MAX_TENDERS,
        delay=DELAY,
        download_pdfs=False if PROBE else DOWNLOAD_PDFS,
        skip_known=not REFRESH,
        extra_fields=date_filter(),
    )
    if PROBE:
        say("PROBE -- one listing page, no tenders fetched, no PDFs")

    records = []
    if OCR_ATTEMPTS > 0:
        say(f"solving captcha with OCR (up to {OCR_ATTEMPTS} attempts)")
        try:
            records = scrape_aoc(
                out, captcha_solver="openrouter", captcha_retries=OCR_ATTEMPTS, **common
            )
        except CaptchaError as exc:
            say(f"OCR gave up: {exc}")
            say("falling back to manual entry -- read data/captcha.png")
            records = scrape_aoc(
                out, captcha_solver="manual", captcha_retries=MANUAL_ATTEMPTS, **common
            )
    else:
        say("manual captcha -- read data/captcha.png when prompted")
        records = scrape_aoc(
            out, captcha_solver="manual", captcha_retries=MANUAL_ATTEMPTS, **common
        )

    report_search(out)

    if PROBE:
        step(3, "Probe only -- nothing stored")
        say("set PROBE = False to run it for real")
        return []

    step(3, "Store what was scraped")
    if not records:
        say("0 new tenders -- every row in the pool is already in SQLite")
        say(f"-> the pool was {MAX_PAGES} page(s) = ~{MAX_PAGES * 10} rows, all known")
        say("-> set FROM_DATE/TO_DATE to a window you have not scraped (best),")
        say("   or raise MAX_PAGES, or set REFRESH = True to re-fetch one")
        say("continuing: the export still runs on stored data")
        return []

    for record in records:
        say(
            f"{record.listing.tender_id}  "
            f"{len(record.bids)} bid(s), {len(record.awarded_bids)} awarded, "
            f"{len(record.downloaded_files)} pdf(s)"
        )
    return [record.listing.tender_id for record in records]


def export(out: Path, store: Store) -> None:
    """The scraped facts, as a spreadsheet."""
    step(4, "Export the vendor sheet")
    csv_path, xlsx_path = write_vendor_sheet(store, out)
    say(f"{store.vendor_count()} vendor(s)")
    say(f"CSV   {csv_path}")
    say(f"Excel {xlsx_path}")


def main() -> int:
    started = time.time()
    out = Path(OUT_DIR)
    banner()

    try:
        preflight()
        scrape(out)
        export(out, Store(out))
    except KeyboardInterrupt:
        print("\n\nStopped. Anything already saved is still on disk.")
        return 130
    except CaptchaError as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1

    elapsed = time.time() - started
    print("\n" + "-" * 62)
    print(f"Done in {elapsed:.0f}s")
    print(f"  {out / 'exports' / 'vendors.csv'}")
    print("  Next: stage 2 (02-enrich) looks up contacts for the pending vendors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
