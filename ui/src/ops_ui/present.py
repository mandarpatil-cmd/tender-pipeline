"""Turn a request into the award query, and into links the page can follow."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from urllib.parse import unquote, urlencode

from pipeline_core.grid import COLUMNS, FILE_COLUMNS, AwardQuery
from pipeline_core.models import ENRICHMENT_STATUSES
from pipeline_core.loose import loose_date
from starlette.datastructures import QueryParams

_BACK = re.compile(r"[A-Za-z0-9_%=&.+\-]*")
DATE_PRESETS = ("7", "30", "60", "90")

_FIELDS = (
    ("q", "text"),
    ("tender_status", "tender_status"),
    ("outreach_status", "outreach_status"),
    ("source", "source"),
    ("state", "state"),
    ("organisation", "organisation"),
    ("date_from", "date_from"),
    ("date_to", "date_to"),
    ("scraped_from", "scraped_from"),
    ("scraped_to", "scraped_to"),
    ("value_min", "value_min"),
    ("value_max", "value_max"),
    ("mailable", "mailable"),
)


def back_href(raw: str) -> str:
    """A link back to the table. Anything that is not our own query string is dropped."""
    text = unquote(raw or "").strip()
    if not text or _BACK.fullmatch(text) is None:
        return "/"
    return "/?" + text


def resolve_preset(
    date_from: str,
    date_to: str,
    preset: str,
    *,
    to_mode: str = "",
    today: date | None = None,
) -> tuple[str, str]:
    """A from-preset is that many days before the to date. Any leaves that end open.

    A day count with no to date uses today, so the window still has an end.
    """
    preset = (preset or "").strip()
    to_mode = (to_mode or "").strip()
    date_from = (date_from or "").strip()
    date_to = (date_to or "").strip()
    if preset == "any":
        date_from = ""
    if preset in DATE_PRESETS:
        end_iso = "" if to_mode == "any" else (loose_date(date_to) or "")
        if not end_iso:
            end_iso = (today or date.today()).isoformat()
        end = date.fromisoformat(end_iso)
        start = end - timedelta(days=int(preset))
        return start.isoformat(), end.isoformat()
    if to_mode == "any":
        date_to = ""
    return date_from, date_to


def active_preset(date_from: str, date_to: str) -> str:
    """Any when from is empty, a day count when it matches, otherwise a specific date."""
    start = loose_date(date_from)
    end = loose_date(date_to)
    if not start:
        return "any"
    if not end:
        return ""
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    text = str(days)
    return text if text in DATE_PRESETS else ""


def shown_day(value: str) -> str:
    """An ISO day for a date input. Unreadable text stays blank."""
    return loose_date(value) or ""


def query_from(params: QueryParams) -> AwardQuery:
    values = {field: (params.get(name) or "").strip() for name, field in _FIELDS}
    values["date_from"], values["date_to"] = resolve_preset(
        values["date_from"],
        values["date_to"],
        params.get("date_from_preset") or "",
        to_mode=params.get("date_to_mode") or "",
    )
    values["scraped_from"], values["scraped_to"] = resolve_preset(
        values["scraped_from"],
        values["scraped_to"],
        params.get("scraped_from_preset") or "",
        to_mode=params.get("scraped_to_mode") or "",
    )
    try:
        page = int(params.get("page") or "1")
    except ValueError:
        page = 1
    listed = params.getlist("enrichment_status") if hasattr(params, "getlist") else []
    fields = params.getlist("search_in") if hasattr(params, "getlist") else []
    return AwardQuery(
        **values,
        enrichment_status=tuple(listed),
        search_in=tuple(fields),
        sort=(params.get("sort") or "tender_id").strip(),
        direction=(params.get("dir") or "asc").strip(),
        page=page,
    ).normalized()


def awards_href(raw: str) -> str:
    """The Awards link. A cookie that is not our query string goes to the whole table."""
    text = unquote(raw or "").strip()
    if not text or _BACK.fullmatch(text) is None:
        return "/"
    return "/?" + text


def widen_enrichment(query: AwardQuery, extra: tuple[str, ...]) -> AwardQuery:
    """Any stays any. Otherwise add these statuses to the selection."""
    if not query.enrichment_status:
        return replace(query, page=1)
    chosen = set(query.enrichment_status) | set(extra)
    ordered = tuple(status for status in ENRICHMENT_STATUSES if status in chosen)
    return replace(query, enrichment_status=ordered, page=1)


def enrich_plan(pending: int, not_found: int, failed: int, ready: int) -> str:
    """The sentence the combined list and the Enrich confirm page both show."""
    return (
        f"pending {pending}, not_found {not_found}, failed {failed}. "
        f"{ready} not_found with no email and no phone. "
        "Enrich looks up the pending companies, the failed companies, "
        "and the not_found companies that have no email and no phone. "
        "A not_found company that already has either contact stays not_found."
    )


def filter_token(query: AwardQuery) -> str:
    """The filter to restore, without the page. Empty when the table is unfiltered."""
    target = href(query, page="1")
    if target == "/":
        return ""
    return target.split("?", 1)[1]


def href(query: AwardQuery, **overrides: str) -> str:
    data = {
        "q": query.text,
        "tender_status": query.tender_status,
        "outreach_status": query.outreach_status,
        "source": query.source,
        "state": query.state,
        "organisation": query.organisation,
        "date_from": query.date_from,
        "date_to": query.date_to,
        "scraped_from": query.scraped_from,
        "scraped_to": query.scraped_to,
        "value_min": query.value_min,
        "value_max": query.value_max,
        "mailable": query.mailable,
        "sort": query.sort,
        "dir": query.direction,
        "page": str(query.page),
    }
    data.update({key: str(value) for key, value in overrides.items()})
    defaults = {"sort": "tender_id", "dir": "asc", "page": "1"}

    def keep(key: str) -> bool:
        value = data[key]
        return bool(value) and defaults.get(key) != value

    pairs: list[tuple[str, str]] = []
    for key in ("q",):
        if keep(key):
            pairs.append((key, data[key]))
    pairs.extend(("search_in", field) for field in query.search_in)
    for key in ("tender_status",):
        if keep(key):
            pairs.append((key, data[key]))
    pairs.extend(("enrichment_status", status) for status in query.enrichment_status)
    for key in (
        "outreach_status",
        "source",
        "state",
        "organisation",
        "date_from",
        "date_to",
        "scraped_from",
        "scraped_to",
        "value_min",
        "value_max",
        "mailable",
        "sort",
        "dir",
        "page",
    ):
        if keep(key):
            pairs.append((key, data[key]))
    encoded = urlencode(pairs)
    return f"/?{encoded}" if encoded else "/"


def form_fields(query: AwardQuery) -> list[tuple[str, str]]:
    """The filter the table is showing, so a later POST selects that same set."""
    fields = [
        ("q", query.text),
    ]
    fields.extend(("search_in", field) for field in query.search_in)
    fields.append(("tender_status", query.tender_status))
    fields.extend(("enrichment_status", status) for status in query.enrichment_status)
    fields.extend(
        [
            ("outreach_status", query.outreach_status),
            ("source", query.source),
            ("state", query.state),
            ("organisation", query.organisation),
            ("date_from", query.date_from),
            ("date_to", query.date_to),
            ("scraped_from", query.scraped_from),
            ("scraped_to", query.scraped_to),
            ("value_min", query.value_min),
            ("value_max", query.value_max),
            ("mailable", query.mailable),
            ("sort", query.sort),
            ("dir", query.direction),
        ]
    )
    return fields


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
    key: str


def headers(query: AwardQuery) -> list[Header]:
    built = []
    for key, label, group, evidence in COLUMNS:
        if key in FILE_COLUMNS:
            built.append(Header(label, group, evidence, None, "", key))
            continue
        mark = ""
        if query.sort == key:
            mark = query.direction
        built.append(Header(label, group, evidence, sort_href(query, key), mark, key))
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
