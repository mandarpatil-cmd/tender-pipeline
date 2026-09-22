"""Download stage 3's input -- who it would mail -- as CSV and Excel.

    python get_data.py

Writes `exports/outreach_queue.csv` and `.xlsx`. Reads only; nothing is sent,
and nothing in the database changes.

The row selection comes from `outreach_targets()`, the exact query `campaign.py`
sends from. That is deliberate: a hand-written "similar" query here could drift
from the real one, and you would be reviewing a list that is not the list.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
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
        "    uv run python get_data.py\n\n"
        "or call the venv interpreter directly:\n"
        "    ..\\.venv\\Scripts\\python.exe get_data.py\n\n"
        "If there is no .venv yet, create it from the repo root:\n"
        "    uv venv --python 3.13\n"
        "    uv pip install -r requirements.txt"
    )


# ============================== CONFIG ==============================
# Edit these, save, then run:  python get_data.py

# Where the two files land, relative to this folder.
OUT_DIR = "exports"

# False -> only who is still waiting: exactly what a live run would mail next.
# True  -> also everyone already mailed, so you can see the whole campaign.
INCLUDE_SENT = False

# Restrict to one origin: "scrape", "bideasy", or None for both.
ONLY_SOURCE = None

# True -> stamp the filenames with the date, keeping past downloads.
#         outreach_queue_2026-09-20.csv
TIMESTAMP = False
# ====================================================================


_require(
    ("sqlalchemy", "SQLAlchemy"),
    ("openpyxl", "openpyxl"),
)

from pipeline_core.db import session  # noqa: E402
from pipeline_core.models import Outreach, Vendor  # noqa: E402
from pipeline_core.queries import outreach_targets  # noqa: E402

COLUMNS = [
    "vendor_id",
    "company",
    "email",
    "phone",
    "city",
    "state",
    "source",
    "enrichment_status",
    "enriched_at",
    "pdf_email",
    "outreach_status",
    "last_attempt_at",
]


def collect_rows(*, include_sent: bool = False, source: str | None = None) -> list[dict]:
    """Stage 3's input, one dict per recipient.

    `outreach_targets()` decides who is in the queue. Everything else here is
    just extra columns looked up for those same vendors.
    """
    with session() as current:
        queued = {t.vendor_id for t in outreach_targets(current)}

        query = current.query(Vendor).filter(
            Vendor.email.is_not(None), Vendor.email != ""
        )
        if source is not None:
            query = query.filter(Vendor.source == source)
        if not include_sent:
            query = query.filter(Vendor.vendor_id.in_(queued or [-1]))
        vendors = query.order_by(Vendor.vendor_id).all()

        # Latest attempt per vendor, for the two status columns.
        attempts: dict[int, Outreach] = {}
        for row in current.query(Outreach).order_by(Outreach.outreach_id).all():
            attempts[row.vendor_id] = row

    rows = []
    for vendor in vendors:
        attempt = attempts.get(vendor.vendor_id)
        rows.append(
            {
                "vendor_id": vendor.vendor_id,
                "company": vendor.name_raw,
                "email": vendor.email,
                "phone": vendor.phone or "",
                "city": vendor.city or "",
                "state": vendor.state or "",
                "source": vendor.source or "",
                "enrichment_status": vendor.enrichment_status,
                "enriched_at": vendor.enriched_at or "",
                "pdf_email": vendor.pdf_email or "",
                "outreach_status": (
                    "queued" if vendor.vendor_id in queued else (attempt.status if attempt else "")
                ),
                "last_attempt_at": attempt.sent_at if attempt else "",
            }
        )
    return rows


def write_csv(rows: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" is required on Windows or every row gets a blank line after it.
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_xlsx(rows: list[dict], path: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.title = "outreach queue"

    sheet.append(COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"

    for row in rows:
        sheet.append([row[column] for column in COLUMNS])

    # Width to the longest value, capped so one long note cannot wreck the sheet.
    for index, column in enumerate(COLUMNS, start=1):
        longest = max([len(column)] + [len(str(r[column])) for r in rows] or [0])
        sheet.column_dimensions[get_column_letter(index)].width = min(longest + 2, 46)

    book.save(path)
    return path


def main() -> int:
    rows = collect_rows(include_sent=INCLUDE_SENT, source=ONLY_SOURCE)

    stem = "outreach_queue"
    if TIMESTAMP:
        stem += f"_{datetime.now():%Y-%m-%d}"
    out_dir = _HERE / OUT_DIR

    print("=" * 62)
    print("  stage3-outreach  --  download the send queue")
    print("=" * 62)
    print(f"  include_sent={INCLUDE_SENT}  source={ONLY_SOURCE or 'all'}")

    if not rows:
        print("\n0 rows. Nobody has an address yet -- run stage 2 (../02-enrich) first.")
        return 0

    csv_path = write_csv(rows, out_dir / f"{stem}.csv")
    xlsx_path = write_xlsx(rows, out_dir / f"{stem}.xlsx")

    queued = sum(1 for r in rows if r["outreach_status"] == "queued")
    print(f"\n  {len(rows)} row(s), {queued} still to send")
    print(f"  CSV   {csv_path}")
    print(f"  Excel {xlsx_path}")
    print("\nNothing was sent and nothing in the database changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
