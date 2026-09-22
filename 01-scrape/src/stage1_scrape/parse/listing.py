from __future__ import annotations

import re

from bs4 import Tag

from stage1_scrape.config import VIEW_STATUS_TITLE
from stage1_scrape.domain.models import ListingRow, NextPage
from stage1_scrape.parse.html import abs_url, cell_text
from stage1_scrape.scraping.forms import parse_html

RECORDS_RE = re.compile(
    r"([\d,]+)\s*[-–to]+\s*([\d,]+)\s*(?:Records\s+of|of)\s*([\d,]+)",
    re.I,
)

#: What this portal actually prints under the results table:
#: ``<b>Total records: 159019&nbsp;</b>``. `RECORDS_RE` above matches the
#: "1 - 10 Records of N" phrasing instead, which this skin never emits -- so
#: `parse_result_count` quietly returns None here and the count is lost.
TOTAL_RECORDS_RE = re.compile(r"Total\s+records\s*:\s*([\d,]+)", re.I)


def search_still_showing_form(html: str) -> bool:
    soup = parse_html(html)
    has_captcha = soup.find("img", id="captchaImage") is not None
    has_results = soup.find("a", attrs={"title": VIEW_STATUS_TITLE}) is not None
    return has_captcha and not has_results


def parse_result_count(html: str) -> tuple[int, int, int] | None:
    """Return (start, end, total) when the page shows '1 - 10 Records of 1,502'."""
    match = RECORDS_RE.search(html)
    if not match:
        return None

    def _n(value: str) -> int:
        return int(value.replace(",", ""))

    return _n(match.group(1)), _n(match.group(2)), _n(match.group(3))


def parse_total_records(html: str) -> int | None:
    """How many records the search matched, or None if the banner is absent.

    This is the only number on a listing page that says whether a filter
    applied. The table itself carries no date column -- S.No, Tender ID, Title,
    Organisation Chain, Tender Stage, Status -- so a filtered page and an
    unfiltered one look alike until you compare totals. Unfiltered AOC is
    around 159,000.
    """
    match = TOTAL_RECORDS_RE.search(html or "")
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def parse_results_table(html: str) -> list[ListingRow]:
    """Parse the 10-row AOC listing.

    CONFIRMED: each data row has an image-only
    ``<a title="View Tender Status">`` whose href is a plain GET (no JS postback).
    ``id="view"`` is reused on every row, so matching is by title, not id.
    """
    soup = parse_html(html)
    rows: list[ListingRow] = []
    seen_urls: set[str] = set()
    for link in soup.find_all("a", attrs={"title": VIEW_STATUS_TITLE}):
        href = link.get("href")
        if not href:
            continue
        url = abs_url(href)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        row = link.find_parent("tr")
        if row is None:
            continue
        cells = [cell_text(td) for td in row.find_all("td", recursive=False)]
        if len(cells) < 6:
            cells = [cell_text(td) for td in row.find_all("td")]
        text_cells = [c for c in cells if c]
        while len(text_cells) < 6:
            text_cells.append("")
        rows.append(
            ListingRow(
                serial=text_cells[0],
                tender_id=text_cells[1],
                title_and_ref=text_cells[2],
                organisation_chain=text_cells[3],
                tender_stage=text_cells[4],
                status=text_cells[5],
                status_page_url=url,
            )
        )
    return rows


def find_next_page(html: str) -> NextPage | None:
    """Locate the listing pager.

    GePNIC usually renders Next as a DirectLink GET (image or title), not a
    form post. If none of the known patterns match, return None so the caller
    can dump the pager HTML for confirmation.
    """
    soup = parse_html(html)
    candidates: list[Tag] = []
    for link in soup.find_all("a", href=True):
        href = link.get("href") or ""
        if href.lower().startswith("javascript:"):
            continue
        title = (link.get("title") or "") + " " + (link.get("id") or "")
        img = link.find("img")
        img_hint = ""
        if img:
            img_hint = " ".join(
                filter(
                    None,
                    [img.get("title"), img.get("alt"), img.get("src")],
                )
            )
        blob = f"{title} {cell_text(link)} {img_hint}".lower()
        if any(
            token in blob
            for token in (
                "next set",
                "go to next",
                "next page",
                "linkfwd",
                "fwd.png",
                "next.gif",
                "next.png",
            )
        ) or re.search(r"\bnext\b", blob):
            candidates.append(link)

    for link in candidates:
        href = link.get("href")
        if href:
            return NextPage(kind="get", url=abs_url(href), note=_next_note(link))
    return None


def _next_note(link: Tag) -> str:
    return (
        f"id={link.get('id')!r} title={link.get('title')!r} "
        f"text={cell_text(link)!r}"
    )


def pager_debug_html(html: str) -> str:
    """Slice likely pager markup so an unmatched Next can be confirmed later."""
    soup = parse_html(html)
    chunks: list[str] = []
    for needle in ("Records of", "Records Of", "Next", "linkFwd", "fwd.png"):
        node = soup.find(string=re.compile(re.escape(needle), re.I))
        if node and isinstance(node.parent, Tag):
            target = node.find_parent("table") or node.parent
            chunks.append(str(target)[:4000])
    for img in soup.find_all("img", src=re.compile(r"fwd|next", re.I)):
        parent = img.find_parent("a") or img.find_parent("td") or img
        chunks.append(str(parent)[:2000])
    unique: list[str] = []
    for chunk in chunks:
        if chunk not in unique:
            unique.append(chunk)
    return "\n\n<!-- pager chunk -->\n\n".join(unique)
