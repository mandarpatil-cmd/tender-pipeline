"""Turn a request into the award query, and into links the page can follow."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from urllib.parse import unquote, urlencode

from pipeline_core.grid import COLUMNS, FILE_COLUMNS, AwardQuery
from pipeline_core.loose import loose_date
from starlette.datastructures import QueryParams

_BACK = re.compile(r"[A-Za-z0-9_%=&.+\-]*")
_STAGE_BACK = re.compile(r"^/(scrape|enrich|mail)(\?[A-Za-z0-9_%=&.+\-]*)?$")
DATE_PRESETS = ("7", "30", "60", "90")

STAGE_PATHS = {"scrape": "/scrape", "enrich": "/enrich", "mail": "/mail"}

#: Columns each stage table shows, in that order. Email and phone stay on Scrape
#: so a later tender for a known company shows the contact already stored.
STAGE_COLUMNS: dict[str, tuple[str, ...]] = {
    "scrape": (
        "tender_id",
        "title",
        "organisation",
        "status",
        "contract_date",
        "contract_value",
        "contract_currency",
        "bid_number",
        "rank",
        "quoted_value",
        "awarded_value",
        "awarded_currency",
        "work_title",
        "name_raw",
        "city",
        "state",
        "email",
        "phone",
        "pdf_email",
        "pdf_phone",
        "source",
        "scraped_at",
    ),
    "enrich": (
        "name_raw",
        "city",
        "state",
        "tender_id",
        "enrichment_status",
        "email",
        "phone",
        "pdf_email",
        "pdf_phone",
    ),
    "mail": (
        "name_raw",
        "email",
        "tender_id",
        "title",
        "outreach_status",
        "last_attempt_at",
        "attempts",
    ),
}

_STAGE_KEYS = {
    "scrape": frozenset(
        {
            "q",
            "tender_status",
            "scraped_from",
            "scraped_to",
            "scraped_from_preset",
            "scraped_to_mode",
            "organisation",
            "value_min",
            "value_max",
            "pdf_contact",
            "sort",
            "dir",
            "page",
        }
    ),
    "enrich": frozenset(
        {"q", "enrichment_status", "source", "pdf_contact", "sort", "dir", "page"}
    ),
    "mail": frozenset({"q", "outreach_status", "sort", "dir", "page"}),
}

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
    ("pdf_contact", "pdf_contact"),
)


def back_href(raw: str) -> str:
    """A link back to the stage table. Anything else opens Scrape."""
    text = unquote(raw or "").strip()
    if _STAGE_BACK.fullmatch(text):
        return text
    return "/scrape"


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


def _only(params, keys: frozenset[str]) -> QueryParams:
    """The parameters this stage is allowed to read. The rest are dropped."""
    pairs: list[tuple[str, str]] = []
    listed = getattr(params, "getlist", None)
    for key in keys:
        values = listed(key) if listed is not None else []
        if not values:
            single = params.get(key) if hasattr(params, "get") else None
            values = [single] if single else []
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                pairs.append((key, text))
    return QueryParams(pairs)


def stage_query(stage: str, params) -> AwardQuery:
    """The award query for one stage page.

    Enrich opens on companies still waiting. Mail opens on addresses not yet
    sent. A parameter that belongs to another stage does not change the rows.
    """
    query = query_from(_only(params, _STAGE_KEYS[stage]))
    if stage == "enrich":
        chosen = query.enrichment_status if len(query.enrichment_status) == 1 else ("pending",)
        query = replace(query, enrichment_status=chosen)
    elif stage == "mail":
        outreach = query.outreach_status if query.outreach_status in {"sent", "failed", "none"} else "none"
        query = replace(
            query,
            outreach_status=outreach,
            mailable="yes" if outreach == "none" else "",
        )
    allowed = tuple(key for key in STAGE_COLUMNS[stage] if key not in FILE_COLUMNS)
    if query.sort not in allowed:
        query = replace(query, sort="tender_id", direction="asc")
    return query.normalized()


def stage_href(stage: str, raw: str) -> str:
    """The nav link for a stage. A cookie that is not our query string is dropped."""
    path = STAGE_PATHS[stage]
    text = unquote(raw or "").strip()
    if not text or _BACK.fullmatch(text) is None:
        return path
    return f"{path}?{text}"


def enrich_plan(pending: int, not_found: int, failed: int, ready: int) -> str:
    """The sentence the combined list and the Enrich confirm page both show."""
    return (
        f"pending {pending}, not_found {not_found}, failed {failed}. "
        f"{ready} not_found with no email and no phone. "
        "Enrich looks up the pending companies, the failed companies, "
        "and the not_found companies that have no email and no phone. "
        "A not_found company that already has either contact stays not_found."
    )


def filter_token(query: AwardQuery, path: str = "/") -> str:
    """The filter to restore, without the page. Empty when nothing was narrowed."""
    target = href(query, path=path, page="1")
    if "?" not in target:
        return ""
    return target.split("?", 1)[1]


def href(query: AwardQuery, path: str = "/", **overrides: str) -> str:
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
        "pdf_contact": query.pdf_contact,
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
        "pdf_contact",
        "sort",
        "dir",
        "page",
    ):
        if keep(key):
            pairs.append((key, data[key]))
    encoded = urlencode(pairs)
    base = path if path.startswith("/") else "/"
    return f"{base}?{encoded}" if encoded else base


def stage_form_fields(stage: str, query: AwardQuery) -> list[tuple[str, str]]:
    """Hidden fields for a stage table action. Other stages' filters are left out."""
    fields = [("stage", stage), ("q", query.text)]
    if stage == "scrape":
        fields.extend(
            [
                ("tender_status", query.tender_status),
                ("scraped_from", query.scraped_from),
                ("scraped_to", query.scraped_to),
                ("organisation", query.organisation),
                ("value_min", query.value_min),
                ("value_max", query.value_max),
                ("pdf_contact", query.pdf_contact),
            ]
        )
    elif stage == "enrich":
        fields.extend(("enrichment_status", status) for status in query.enrichment_status)
        fields.append(("source", query.source))
        fields.append(("pdf_contact", query.pdf_contact))
    elif stage == "mail":
        fields.extend(
            [
                ("outreach_status", query.outreach_status),
                ("mailable", query.mailable),
            ]
        )
    fields.extend([("sort", query.sort), ("dir", query.direction)])
    return fields


def sort_href(query: AwardQuery, key: str, path: str = "/") -> str:
    direction = "desc" if query.sort == key and query.direction == "asc" else "asc"
    return href(query, path=path, sort=key, dir=direction, page="1")


def export_href(query: AwardQuery, suffix: str, path: str = "/") -> str:
    target = href(query, path=path, page="1")
    base = f"/export.{suffix}" if path in {"", "/"} else f"{path}/export.{suffix}"
    if "?" not in target:
        return base
    return f"{base}?{target.split('?', 1)[1]}"


@dataclass(frozen=True)
class Header:
    label: str
    group: str
    evidence: bool
    href: str | None
    mark: str
    key: str


def headers(
    query: AwardQuery,
    path: str = "/",
    keys: tuple[str, ...] | None = None,
) -> list[Header]:
    by_key = {key: (label, group, evidence) for key, label, group, evidence in COLUMNS}
    chosen = keys or tuple(key for key, _label, _group, _evidence in COLUMNS)
    built = []
    for key in chosen:
        label, group, evidence = by_key[key]
        if key in FILE_COLUMNS:
            built.append(Header(label, group, evidence, None, "", key))
            continue
        mark = query.direction if query.sort == key else ""
        built.append(Header(label, group, evidence, sort_href(query, key, path), mark, key))
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

