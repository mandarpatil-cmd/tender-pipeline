"""Which companies a table action applies to."""

from __future__ import annotations

from pathlib import Path

from pipeline_core.db import engine, session
from pipeline_core.grid import filtered_vendor_ids

from ops_ui.present import query_from

_ON = {"1", "on", "true", "yes"}


def selected_vendor_ids(form, path: Path) -> list[int]:
    """Ticked rows, or every company in the filter when that box is checked.

    The filter-wide box ignores the ticks on this page. A company that won
    more than one tender is included once.
    """
    if str(form.get("select_all") or "") in _ON:
        with session(engine(path)) as current:
            return filtered_vendor_ids(current, query_from(form))
    ids = []
    for value in form.getlist("vendor_id"):
        text = str(value).strip()
        if text.isdigit():
            ids.append(int(text))
    return ids
