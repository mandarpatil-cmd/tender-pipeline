"""The scraped vendor facts as CSV and Excel.

This lives in `export/` rather than `persist/` because it only ever reads:
`persist/` owns the database and the PDF bytes, this is a consumer of both.

Every column here comes from the portal or from a downloaded PDF. There are no
model-generated columns — a contact found by stage 2 lives in the database, not
in this sheet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stage1_scrape.domain.errors import ExportError

#: Written into every cell whose value is missing, so a blank always means
#: "not in the source" rather than "the writer dropped it".
MISSING = "NA"

VENDOR_SHEET_COLUMNS = [
    "vendor_id",
    "name_raw",
    "name_norm",
    "legal_form",
    "city",
    "state",
    "buyer_hint",
    "tender_ids",
    "work_titles",
    "awarded_values",
    "contract_dates",
    "pdf_emails",
    "pdf_phones",
    "pdf_gstins",
    "pdf_files",
]


def cell(value: Any) -> str:
    """Render one value. Keep partial data as-is; write NA only when truly absent."""
    if value is None:
        return MISSING
    if isinstance(value, float):
        try:
            import math

            if math.isnan(value):
                return MISSING
        except (TypeError, ValueError):
            pass
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else MISSING
    if isinstance(value, (list, tuple)):
        rendered = [cell(item) for item in value]
        kept = [item for item in rendered if item != MISSING]
        return "; ".join(kept) if kept else MISSING
    text = str(value).strip()
    return text if text else MISSING


def vendor_row(seed: dict[str, Any]) -> dict[str, str]:
    """One row from a `Store.vendor_seeds` entry."""
    awards = seed.get("awards") or []
    return {
        "vendor_id": cell(seed.get("vendor_id")),
        "name_raw": cell(seed.get("name_raw")),
        "name_norm": cell(seed.get("name_norm")),
        "legal_form": cell(seed.get("legal_form")),
        "city": cell(seed.get("city")),
        "state": cell(seed.get("state")),
        "buyer_hint": cell(seed.get("buyer_hint")),
        "tender_ids": cell([row.get("tender_id") for row in awards]),
        "work_titles": cell([row.get("work_title") for row in awards]),
        "awarded_values": cell(
            [
                " ".join(
                    p
                    for p in (
                        str(row.get("awarded_value") or "").strip(),
                        str(row.get("awarded_currency") or "").strip(),
                    )
                    if p
                )
                for row in awards
            ]
        ),
        "contract_dates": cell([row.get("contract_date") for row in awards]),
        "pdf_emails": cell(seed.get("pdf_emails")),
        "pdf_phones": cell(seed.get("pdf_phones")),
        "pdf_gstins": cell(seed.get("pdf_gstins")),
        "pdf_files": cell(
            [item.get("filename") for item in (seed.get("pdf_extracts") or [])]
        ),
    }


def write_sheet(
    rows: list[dict[str, str]],
    out_dir: Path,
    basename: str = "vendors",
) -> tuple[Path, Path]:
    """Write `<out_dir>/exports/<basename>.csv` and `.xlsx`. Returns both paths."""
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ExportError(
            "pandas/openpyxl are not installed. From the repo root, run: "
            "uv pip install -r requirements.txt"
        ) from exc

    export_dir = Path(out_dir) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows, columns=VENDOR_SHEET_COLUMNS)
    frame = frame.fillna(MISSING)
    for column in VENDOR_SHEET_COLUMNS:
        frame[column] = frame[column].replace("", MISSING)
    csv_path = export_dir / f"{basename}.csv"
    xlsx_path = export_dir / f"{basename}.xlsx"
    frame.to_csv(csv_path, index=False)
    frame.to_excel(xlsx_path, index=False)
    return csv_path, xlsx_path


def write_vendor_sheet(store: Any, out_dir: Path) -> tuple[Path, Path]:
    """Every stored vendor, rebuilt from scratch. The whole export in one call."""
    rows = [vendor_row(seed) for seed in store.vendor_seeds()]
    return write_sheet(rows, out_dir)
