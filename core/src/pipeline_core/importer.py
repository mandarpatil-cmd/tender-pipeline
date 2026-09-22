"""One-off import of companies that already exist in spreadsheets.

Two shapes of workbook feed this: the raw BidEasy exports, and stage 2's old
`outputs/*.xlsx` which already carry a researched email and phone. Rather than
hard-coding either layout, the header is sniffed — the raw exports differ in
their header row, their company column, and everything else.

Rows that arrive with an email are marked `done`, so stage 2 never pays to look
up a contact that was already known. The rest queue up as `pending`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .models import SOURCE_BIDEASY, STATUS_DONE, STATUS_PENDING, Vendor
from .naming import normalize_name
from .queries import upsert_vendor, utcnow

#: Header names that hold the company, best first. Compared case-insensitively.
COMPANY_COLUMNS = (
    "probable_bidder",
    "probable bidder",
    "corporate name / awardee",
    "awardee",
    "company",
    "name_raw",
    "bidder",
    "vendor",
)
EMAIL_COLUMNS = ("email", "emails", "e-mail")
PHONE_COLUMNS = ("phone", "phones", "mobile", "contact")

#: Header rows seen in the wild — BidEasy's exports carry two banner rows.
HEADER_OFFSETS = (0, 1, 2, 3)


class ImportError_(Exception):
    """The workbook could not be read, or has no recognisable company column."""


@dataclass
class ImportReport:
    path: str
    rows_read: int = 0
    companies: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    with_email: int = 0
    with_phone: int = 0
    skipped_blank: int = 0
    samples: list[str] = field(default_factory=list)

    def render(self) -> str:
        return (
            f"{Path(self.path).name}\n"
            f"  rows read          {self.rows_read}\n"
            f"  unique companies   {self.companies}\n"
            f"  inserted           {self.inserted}\n"
            f"  updated            {self.updated}\n"
            f"  unchanged          {self.unchanged}\n"
            f"  with email         {self.with_email}\n"
            f"  with phone         {self.with_phone}\n"
            f"  blank/skipped      {self.skipped_blank}"
        )


@dataclass
class Company:
    name_raw: str
    email: str | None = None
    phone: str | None = None


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "na", "none", "<na>"}:
        return ""
    return " ".join(text.split())


def _pick(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {str(col).strip().lower(): col for col in columns}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def read_companies(path: Path | str) -> tuple[list[Company], int, int]:
    """Sniff the header row, then pull out company/email/phone.

    Returns (companies, rows_read, skipped_blank). Companies are deduplicated on
    `name_norm`, keeping the first non-empty email and phone seen for each —
    these sheets carry roughly twice as many rows as distinct companies.
    """
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ImportError_(
            "pandas and openpyxl are needed to read spreadsheets. From the repo root:\n"
            "  uv pip install -r requirements.txt"
        ) from exc

    source = Path(path)
    if not source.is_file():
        raise ImportError_(f"No such file: {source}")

    frame = None
    company_col = None
    for offset in HEADER_OFFSETS:
        try:
            candidate = pd.read_excel(source, skiprows=offset, dtype=str)
        except Exception:
            continue
        found = _pick(list(candidate.columns), COMPANY_COLUMNS)
        if found:
            frame, company_col = candidate, found
            break

    if frame is None or company_col is None:
        raise ImportError_(
            f"{source.name}: no company column found. Looked for "
            f"{', '.join(COMPANY_COLUMNS)} in the first {max(HEADER_OFFSETS) + 1} rows."
        )

    email_col = _pick(list(frame.columns), EMAIL_COLUMNS)
    phone_col = _pick(list(frame.columns), PHONE_COLUMNS)

    merged: dict[str, Company] = {}
    rows_read = 0
    skipped_blank = 0
    for _, row in frame.iterrows():
        rows_read += 1
        name = _clean(row.get(company_col))
        if not name:
            skipped_blank += 1
            continue
        key = normalize_name(name)
        if not key:
            skipped_blank += 1
            continue
        entry = merged.get(key)
        if entry is None:
            entry = Company(name_raw=name)
            merged[key] = entry
        if email_col and not entry.email:
            entry.email = _clean(row.get(email_col)) or None
        if phone_col and not entry.phone:
            entry.phone = _clean(row.get(phone_col)) or None

    return list(merged.values()), rows_read, skipped_blank


def import_workbook(
    session: Session,
    path: Path | str,
    *,
    source: str = SOURCE_BIDEASY,
    dry_run: bool = False,
) -> ImportReport:
    """Insert every company in one workbook as a vendor.

    An existing vendor keeps whatever contact details it already has — stage 2's
    results are never overwritten by a spreadsheet.
    """
    companies, rows_read, skipped_blank = read_companies(path)
    report = ImportReport(
        path=str(path),
        rows_read=rows_read,
        companies=len(companies),
        skipped_blank=skipped_blank,
    )

    for company in companies:
        if company.email:
            report.with_email += 1
        if company.phone:
            report.with_phone += 1

        existing = session.query(Vendor).filter(
            Vendor.name_norm == normalize_name(company.name_raw)
        ).one_or_none()

        if dry_run:
            if existing is None:
                report.inserted += 1
                if len(report.samples) < 5:
                    report.samples.append(company.name_raw)
            elif (company.email or company.phone) and not existing.email:
                report.updated += 1
            else:
                report.unchanged += 1
            continue

        vendor_id = upsert_vendor(
            session,
            name_raw=company.name_raw,
            source=source,
        )
        vendor = session.get(Vendor, vendor_id)
        assert vendor is not None

        if existing is None:
            report.inserted += 1
            if len(report.samples) < 5:
                report.samples.append(company.name_raw)
            if company.email or company.phone:
                vendor.email = company.email
                vendor.phone = company.phone
                vendor.enrichment_status = STATUS_DONE
                vendor.enriched_at = utcnow()
            else:
                vendor.enrichment_status = STATUS_PENDING
        elif (company.email or company.phone) and not existing.email:
            vendor.email = company.email or vendor.email
            vendor.phone = company.phone or vendor.phone
            vendor.enrichment_status = STATUS_DONE
            vendor.enriched_at = utcnow()
            report.updated += 1
        else:
            report.unchanged += 1

    return report
