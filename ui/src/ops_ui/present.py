"""Turn a request into the award query, and into links the page can follow."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlencode

from pipeline_core.grid import COLUMNS, FILE_COLUMNS, AwardQuery
from starlette.datastructures import QueryParams

_BACK = re.compile(r"[A-Za-z0-9_%=&.+\-]*")

_FIELDS = (
    ("q", "text"),
    ("tender_status", "tender_status"),
    ("enrichment_status", "enrichment_status"),
    ("outreach_status", "outreach_status"),
    ("source", "source"),
    ("state", "state"),
    ("organisation", "organisation"),
    ("date_from", "date_from"),
    ("date_to", "date_to"),
    ("value_min", "value_min"),
    ("value_max", "value_max"),
)


def back_href(raw: str) -> str:
    """A link back to the table. Anything that is not our own query string is dropped."""
    text = unquote(raw or "").strip()
    if not text or _BACK.fullmatch(text) is None:
        return "/"
    return "/?" + text


def query_from(params: QueryParams) -> AwardQuery:
    values = {field: (params.get(name) or "").strip() for name, field in _FIELDS}
    try:
        page = int(params.get("page") or "1")
    except ValueError:
        page = 1
    return AwardQuery(
        **values,
        sort=(params.get("sort") or "tender_id").strip(),
        direction=(params.get("dir") or "asc").strip(),
        page=page,
    ).normalized()


def href(query: AwardQuery, **overrides: str) -> str:
    data = {
        "q": query.text,
        "tender_status": query.tender_status,
        "enrichment_status": query.enrichment_status,
        "outreach_status": query.outreach_status,
        "source": query.source,
        "state": query.state,
        "organisation": query.organisation,
        "date_from": query.date_from,
        "date_to": query.date_to,
        "value_min": query.value_min,
        "value_max": query.value_max,
        "sort": query.sort,
        "dir": query.direction,
        "page": str(query.page),
    }
    data.update({key: str(value) for key, value in overrides.items()})
    defaults = {"sort": "tender_id", "dir": "asc", "page": "1"}
    pairs = [
        (key, value)
        for key, value in data.items()
        if value and defaults.get(key) != value
    ]
    encoded = urlencode(pairs)
    return f"/?{encoded}" if encoded else "/"


def sort_href(query: AwardQuery, key: str) -> str:
    direction = "desc" if query.sort == key and query.direction == "asc" else "asc"
    return href(query, sort=key, dir=direction, page="1")


def export_href(query: AwardQuery, suffix: str) -> str:
    target = href(query, page="1")
    if target == "/":
        return f"/export.{suffix}"
    return f"/export.{suffix}?{target.split('?', 1)[1]}"


@dataclass(frozen=True)
class Header:
    label: str
    group: str
    evidence: bool
    href: str | None
    mark: str


def headers(query: AwardQuery) -> list[Header]:
    built = []
    for key, label, group, evidence in COLUMNS:
        if key in FILE_COLUMNS:
            built.append(Header(label, group, evidence, None, ""))
            continue
        mark = ""
        if query.sort == key:
            mark = query.direction
        built.append(Header(label, group, evidence, sort_href(query, key), mark))
    return built


def groups(columns: list[Header]) -> list[tuple[str, int, bool]]:
    grouped: list[tuple[str, int, bool]] = []
    for column in columns:
        if grouped and grouped[-1][0] == column.group:
            name, count, evidence = grouped[-1]
            grouped[-1] = (name, count + 1, evidence)
        else:
            grouped.append((column.group, 1, column.evidence))
    return grouped


def strip_cards(snapshot, view, filtered: bool) -> list[tuple[str, int, str]]:
    if filtered and view is not None:
        counts = view.counts
        note = "in this view"
        return [
            ("tenders", counts.tenders, note),
            ("awards", counts.awards, note),
            ("vendors", counts.vendors, note),
            ("enriched", counts.enriched, note),
            ("mailable", counts.mailable, note),
            ("sent", counts.sent, note),
            ("phone-only", counts.phone_only, "answered, but can never be mailed"),
        ]
    cards = [(label, count, note) for label, count, note in snapshot.funnel]
    cards.append(("phone-only", snapshot.phone_only, "answered, but can never be mailed"))
    return cards
