"""Vendor-name helpers, plus the rule for which bid actually won.

`normalize_name` and friends moved to `pipeline_core.naming`: `vendors.name_norm`
is a unique key that the scraper, the importer and any future stage all write, so
one definition has to serve them all. They are re-exported here because callers in
this package have always imported them from `domain`.

`winner_rows` stays: it reads a scraped `TenderRecord` and is nobody else's business.
"""

from __future__ import annotations

from pipeline_core.naming import (
    buyer_tail,
    infer_city_state,
    infer_legal_form,
    normalize_name,
)

__all__ = [
    "buyer_tail",
    "infer_city_state",
    "infer_legal_form",
    "normalize_name",
    "winner_rows",
]


def winner_rows(record) -> list:
    """Awarded vendors. Prefer the Awarded Bids table, then L1, then Accepted."""
    if record.awarded_bids:
        return list(record.awarded_bids)
    ranked = [row for row in record.financial_bids if (row.rank or "").upper() == "L1"]
    if ranked:
        return ranked
    accepted = [row for row in record.bids if "accepted" in (row.status or "").lower()]
    if accepted:
        return accepted
    remarked = [row for row in record.bids if "l1" in (row.remarks or "").lower()]
    return remarked[:1]
