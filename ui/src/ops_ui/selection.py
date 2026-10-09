"""Which companies a table action applies to."""

from __future__ import annotations

from pathlib import Path

from pipeline_core.db import engine, session
from pipeline_core.grid import filtered_award_keys, filtered_vendor_ids
from pipeline_core.models import Award
from sqlalchemy import select

from ops_ui.present import query_from, stage_query

_ON = {"1", "on", "true", "yes"}


def selected_vendor_ids(form, path: Path) -> list[int]:
    """Ticked rows, or every company in the filter when that box is checked.

    The filter-wide box ignores the ticks on this page. A company that won
    more than one tender is included once. A tick may be ``vendor:tender``.
    """
    if str(form.get("select_all") or "") in _ON:
        with session(engine(path)) as current:
            return filtered_vendor_ids(current, _table_query(form))
    ids = []
    for value in form.getlist("vendor_id"):
        vendor_id, _tender_id = _split(str(value))
        if vendor_id is not None and vendor_id not in ids:
            ids.append(vendor_id)
    return ids


def selected_awards(form, path: Path) -> list[tuple[int, str]]:
    """Ticked awards, or every award in the filter. One company on two tenders is two."""
    if str(form.get("select_all") or "") in _ON:
        with session(engine(path)) as current:
            return filtered_award_keys(current, _table_query(form))
    chosen: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    bare: list[int] = []
    for value in form.getlist("vendor_id"):
        vendor_id, tender_id = _split(str(value))
        if vendor_id is None:
            continue
        if tender_id:
            key = (vendor_id, tender_id)
            if key not in seen:
                seen.add(key)
                chosen.append(key)
        elif vendor_id not in bare:
            bare.append(vendor_id)
    if bare:
        with session(engine(path)) as current:
            rows = current.execute(
                select(Award.vendor_id, Award.tender_id)
                .where(Award.vendor_id.in_(bare))
                .order_by(Award.tender_id)
            )
            for vendor_id, tender_id in rows:
                key = (int(vendor_id), tender_id)
                if key not in seen:
                    seen.add(key)
                    chosen.append(key)
    return chosen


def _table_query(form):
    stage = str(form.get("stage") or "")
    if stage in {"scrape", "enrich", "mail"}:
        return stage_query(stage, form)
    return query_from(form)


def _split(value: str) -> tuple[int | None, str]:
    text = value.strip()
    if not text:
        return None, ""
    vendor_text, separator, tender_id = text.partition(":")
    if separator and vendor_text.isdigit() and tender_id:
        return int(vendor_text), tender_id
    if text.isdigit():
        return int(text), ""
    return None, ""
