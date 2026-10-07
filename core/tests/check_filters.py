"""Check the Awards filters against the database the page reads.

Read-only. Prints one line per case and exits non-zero when a filter returns
a different set of awards than the independent check.

    uv run python core/tests/check_filters.py
"""

from __future__ import annotations

import sqlite3
import sys
from itertools import combinations
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from pipeline_core.grid import AwardQuery, award_view
from pipeline_core.loose import loose_date, loose_number

sys.path.insert(0, str(Path(__file__).resolve().parent))
from filter_oracle import matching_keys

ROOT = Path(__file__).resolve().parents[2]
DATABASE = ROOT / "data" / "pipeline.sqlite3"
_STATUSES = ("pending", "done", "not_found", "failed")


def main() -> int:
    if not DATABASE.is_file():
        print(f"No database at {DATABASE}")
        return 1
    current = _open()
    try:
        catalogue = award_view(current, AwardQuery(), page_size=None).rows
        cases = _cases(catalogue)
        failed = 0
        for label, query in cases:
            view = award_view(current, query, page_size=None)
            expected = matching_keys(catalogue, query)
            got = {(row["tender_id"], str(row["bid_number"])) for row in view.rows}
            chips = {
                status: sum(1 for row in view.rows if row["enrichment_status"] == status)
                for status in ("pending", "not_found", "failed")
            }
            chips_match = (
                view.counts.pending == chips["pending"]
                and view.counts.not_found == chips["not_found"]
                and view.counts.failed == chips["failed"]
                and view.counts.awards == len(view.rows)
            )
            if got == expected and view.counts.awards == len(expected) and chips_match:
                print(f"ok    {label}  expected {len(expected)}  returned {view.total}")
                continue
            failed += 1
            extra = sorted(tender for tender, _bid in got - expected)
            missing = sorted(tender for tender, _bid in expected - got)
            print(f"MISS  {label}  expected {len(expected)}  returned {view.total}")
            if not chips_match:
                print(
                    "      chips "
                    f"pending {view.counts.pending}/{chips['pending']} "
                    f"not_found {view.counts.not_found}/{chips['not_found']} "
                    f"failed {view.counts.failed}/{chips['failed']}"
                )
            if extra:
                print(f"      extra {extra[:20]}")
            if missing:
                print(f"      missing {missing[:20]}")
        print(f"{len(cases) - failed} passed, {failed} failed, {len(catalogue)} awards in the file")
        return 1 if failed else 0
    finally:
        current.rollback()
        current.close()


def _open():
    uri = f"{DATABASE.resolve().as_uri()}?mode=ro"

    def connect():
        connection = sqlite3.connect(uri, uri=True)
        connection.create_function("loose_date", 1, loose_date)
        connection.create_function("loose_number", 1, loose_number)
        return connection

    engine = create_engine("sqlite://", creator=connect)
    return sessionmaker(bind=engine)()


def _cases(rows: list[dict]) -> list[tuple[str, AwardQuery]]:
    dimensions: list[tuple[str, dict]] = []
    named = next((row for row in rows if row.get("name_raw")), None)
    if named is not None:
        token = str(named["name_raw"]).split()[0]
        dimensions.append(("company", {"text": token, "search_in": ("name",)}))
    city = next((row.get("city") for row in rows if row.get("city")), "")
    if city:
        dimensions.append(("city", {"text": city, "search_in": ("city",)}))
    if named is not None:
        dimensions.append(("vendor_id", {"text": str(named["vendor_id"]), "search_in": ("vendor_id",)}))
    status = next((row.get("status") for row in rows if row.get("status")), "")
    if status:
        dimensions.append(("tender_status", {"tender_status": status}))
    present = {row.get("enrichment_status") for row in rows}
    for enrichment in _STATUSES:
        if enrichment in present:
            dimensions.append((enrichment, {"enrichment_status": (enrichment,)}))
    outreach = next((row.get("outreach_status") for row in rows if row.get("outreach_status")), "")
    if outreach:
        dimensions.append(("outreach", {"outreach_status": outreach}))
    source = next((row.get("source") for row in rows if row.get("source")), "")
    if source:
        dimensions.append(("source", {"source": source}))
    if any(row.get("email") for row in rows):
        dimensions.append(("mailable", {"mailable": "yes"}))
    state = next((row.get("state") for row in rows if row.get("state")), "")
    if state:
        dimensions.append(("state", {"state": state}))
    organisation = next((row.get("organisation") for row in rows if row.get("organisation")), "")
    if organisation:
        word = str(organisation).split()[0]
        dimensions.append(("organisation", {"organisation": word}))
    dated = next((row for row in rows if row.get("contract_date")), None)
    if dated is not None:
        dimensions.append(("contract_date", {"date_from": "2020-01-01"}))
    if any(row.get("scraped_at") for row in rows):
        dimensions.append(("scraped", {"scraped_from": "2020-01-01"}))
    if any(row.get("contract_value") for row in rows):
        dimensions.append(("contract_value", {"value_min": "1"}))

    cases = [(label, AwardQuery(**fields)) for label, fields in dimensions]
    for (left, left_fields), (right, right_fields) in combinations(dimensions, 2):
        if set(left_fields) & set(right_fields):
            continue
        merged = {**left_fields, **right_fields}
        cases.append((f"{left} + {right}", AwardQuery(**merged)))
    for size in range(1, len(_STATUSES) + 1):
        for subset in combinations(_STATUSES, size):
            cases.append(("+".join(subset), AwardQuery(enrichment_status=subset)))
    if named is not None:
        token = str(named["name_raw"]).split()[0]
        for size in range(1, 4):
            for scope in combinations(("name", "city", "vendor_id"), size):
                cases.append(
                    (
                        f"search {'+'.join(scope)} {token}",
                        AwardQuery(text=token, search_in=scope),
                    )
                )
        cases.append((f"search any {token}", AwardQuery(text=token)))
    return cases


if __name__ == "__main__":
    raise SystemExit(main())
