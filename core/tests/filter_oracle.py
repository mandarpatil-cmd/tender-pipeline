"""Which award rows a filter should keep, without using the page query.

The page asks `award_view`. This walks rows that query already returned and
applies the same rules in Python, so a matching bug in both places is harder
to share.
"""

from __future__ import annotations

from pipeline_core.grid import AwardQuery
from pipeline_core.loose import loose_date
from pipeline_core.money import parse_money


def matching_keys(rows: list[dict], query: AwardQuery) -> set[tuple[str, str]]:
    """`(tender_id, bid_number)` for the rows this filter keeps."""
    query = query.normalized()
    return {
        (str(row["tender_id"]), str(row["bid_number"]))
        for row in rows
        if _keeps(row, query)
    }


def _keeps(row: dict, query: AwardQuery) -> bool:
    if query.text and not _search(row, query):
        return False
    if query.tender_status and (row.get("status") or "") != query.tender_status:
        return False
    if query.enrichment_status and (row.get("enrichment_status") or "") not in query.enrichment_status:
        return False
    if query.outreach_status and not _outreach(row.get("outreach_status"), query.outreach_status):
        return False
    if query.source and (row.get("source") or "") != query.source:
        return False
    if query.state and (row.get("state") or "").casefold() != query.state.strip().casefold():
        return False
    if query.mailable and not _mailable(row.get("email"), query.mailable):
        return False
    if query.organisation and not _contains(row.get("organisation"), query.organisation):
        return False
    if not _span(
        loose_date(row.get("contract_date")),
        loose_date(query.date_from) if query.date_from else None,
        loose_date(query.date_to) if query.date_to else None,
        keep_unparsed=True,
    ):
        return False
    if not _span(
        _amount(row.get("contract_value")),
        _amount(query.value_min) if query.value_min else None,
        _amount(query.value_max) if query.value_max else None,
        keep_unparsed=False,
    ):
        return False
    scraped = str(row.get("scraped_at") or "")[:10]
    if query.scraped_from and scraped < query.scraped_from:
        return False
    if query.scraped_to and scraped > query.scraped_to:
        return False
    return True


def _search(row: dict, query: AwardQuery) -> bool:
    if not query.search_in:
        columns = (
            "tender_id",
            "title",
            "organisation",
            "work_title",
            "name_raw",
            "city",
            "state",
            "email",
            "phone",
        )
        return any(_contains(row.get(column), query.text) for column in columns)
    matched = False
    if "name" in query.search_in:
        matched = matched or _contains(row.get("name_raw"), query.text)
    if "city" in query.search_in:
        matched = matched or _contains(row.get("city"), query.text)
    if "vendor_id" in query.search_in:
        typed = query.text.strip()
        matched = matched or (typed.isdigit() and int(row.get("vendor_id") or -1) == int(typed))
    return matched


def _amount(value) -> int | None:
    hundredths, _currency = parse_money(value)
    return hundredths


def _contains(value, text: str) -> bool:
    if value is None:
        return False
    return text.lower() in str(value).lower()


def _outreach(stored, wanted: str) -> bool:
    if wanted == "none":
        return stored in (None, "")
    return stored == wanted


def _mailable(email, wanted: str) -> bool:
    has_email = bool(email)
    if wanted == "yes":
        return has_email
    if wanted == "no":
        return not has_email
    return True


def _span(value, low, high, *, keep_unparsed: bool) -> bool:
    if low in (None, "") and high in (None, ""):
        return True
    if value is None:
        return keep_unparsed
    if low not in (None, "") and value < low:
        return False
    if high not in (None, "") and value > high:
        return False
    return True
