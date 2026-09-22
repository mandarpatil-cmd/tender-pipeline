from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from stage1_scrape.config import (
    AWARDED_BIDS_HEADING,
    BIDS_LIST_HEADING,
    FINANCIAL_EVAL_HEADING,
)
from stage1_scrape.domain.models import BidRow, DocumentLink
from stage1_scrape.parse.html import SIZE_RE, abs_url, cell_text
from stage1_scrape.scraping.forms import parse_html


def parse_key_value_blocks(html: str) -> dict[str, str]:
    """Two-cell rows: ``td.td_caption`` + ``td.td_field``."""
    soup = parse_html(html)
    data: dict[str, str] = {}
    for row in soup.find_all("tr"):
        caption = row.find("td", class_="td_caption")
        field = row.find("td", class_="td_field")
        if caption is None or field is None:
            continue
        cells = row.find_all("td", recursive=False)
        if len(cells) != 2:
            continue
        if "td_caption" in cells[1].get("class", []) and cells[0].find("b"):
            continue
        key = cell_text(caption).rstrip(" :")
        if not key or key.lower() in {"s.no", "s. no"}:
            continue
        data[key] = cell_text(field)
    return data


def parse_named_table(html: str, heading: str) -> list[BidRow]:
    """Section heading row → header row → data rows.

    Bids List rows on the portal use ``tr.td_field``. Financial Evaluation and
    Awarded Bids List rows often do not — they are ``tr#informal_*`` / ``tr.even``
    whose first cell is a serial number.
    """
    soup = parse_html(html)
    heading_row = _find_section_head(soup, heading)
    if heading_row is None:
        return []

    header_row = heading_row.find_next_sibling("tr")
    if header_row is None:
        return []
    headers = [cell_text(cell).lower() for cell in header_row.find_all("td")]
    rows: list[BidRow] = []
    sibling = header_row.find_next_sibling("tr")
    while sibling is not None:
        if sibling.find("td", class_="section_head"):
            break
        if not _is_bid_data_row(sibling):
            if sibling.find("td", class_="td_caption") and sibling.find("b"):
                break
            sibling = sibling.find_next_sibling("tr")
            continue
        cells = sibling.find_all("td", recursive=False) or sibling.find_all("td")
        link = sibling.find("a", href=True)
        url = abs_url(link["href"]) if link else None
        rows.append(_bid_from_cells(headers, cells, url))
        sibling = sibling.find_next_sibling("tr")
    return rows


def parse_section_key_values(html: str, heading: str) -> dict[str, str]:
    """Key/value rows that follow a named section heading, until the next heading."""
    soup = parse_html(html)
    heading_row = _find_section_head(soup, heading)
    if heading_row is None:
        return {}
    data: dict[str, str] = {}
    sibling = heading_row.find_next_sibling("tr")
    while sibling is not None:
        if sibling.find("td", class_="section_head"):
            break
        cells = sibling.find_all("td", recursive=False)
        if len(cells) == 2:
            key = cell_text(cells[0]).rstrip(" :")
            if key:
                data[key] = cell_text(cells[1])
        sibling = sibling.find_next_sibling("tr")
    return data


def find_document_links(html: str) -> list[DocumentLink]:
    """PDF links whose label comes from the sibling ``td.td_caption``."""
    soup = parse_html(html)
    docs: list[DocumentLink] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = link.get("href") or ""
        filename = _filename_from_link(link)
        caption = _caption_for_link(link)
        if not filename.lower().endswith(".pdf") and "document" not in caption.lower():
            continue
        if "sp=ZH4sI" in href.replace("&amp;", "&"):
            continue
        if filename.lower() in {"document.pdf", "document"}:
            continue
        url = abs_url(href)
        if url in seen:
            continue
        seen.add(url)
        size = None
        parent = link.find_parent("td")
        if parent:
            match = SIZE_RE.search(cell_text(parent))
            if match:
                size = match.group(1)
        docs.append(
            DocumentLink(
                label=caption or "document",
                filename=filename or "document.pdf",
                url=url,
                size=size,
            )
        )
    return docs


def parse_summary_page(html: str) -> dict:
    all_kv = parse_key_value_blocks(html)
    header_keys = {
        "Organisation Chain",
        "Tender ID",
        "Tender Ref No",
        "Tender Title",
        "Tender Reference Number",
    }
    header = {k: v for k, v in all_kv.items() if k in header_keys}
    if not header:
        header = {
            k: v
            for k, v in all_kv.items()
            if k.lower() in {hk.lower() for hk in header_keys}
        }
    aoc_keys = {
        "Contract Date",
        "Total Contract Value",
        "Work Completion Period in days",
        "Updated By",
        "Updated on",
        "AOC document",
    }
    aoc = {k: v for k, v in all_kv.items() if k in aoc_keys or k.lower().startswith("aoc")}
    return {
        "header": header,
        "all_key_values": all_kv,
        "bids": parse_named_table(html, BIDS_LIST_HEADING),
        "financial_bids": parse_named_table(html, FINANCIAL_EVAL_HEADING),
        "awarded_bids": parse_named_table(html, AWARDED_BIDS_HEADING),
        "bid_opening": parse_section_key_values(html, "Bid Opening Summary"),
        "technical_eval": parse_section_key_values(
            html, "Technical Evaluation Summary Details"
        ),
        "finance_eval": parse_section_key_values(
            html, "Finance Evaluation Summary Details"
        ),
        "aoc": aoc,
        "documents": find_document_links(html),
    }


def _find_section_head(soup: BeautifulSoup, heading: str) -> Tag | None:
    target = heading.strip().lower()
    for cell in soup.find_all("td", class_="section_head"):
        if cell_text(cell).lower() == target:
            return cell.find_parent("tr")
    for cell in soup.find_all("td"):
        if cell_text(cell).lower() == target:
            return cell.find_parent("tr")
    return None


def _is_bid_data_row(row: Tag) -> bool:
    if row.find("td", class_="section_head"):
        return False
    classes = [str(item).lower() for item in (row.get("class") or [])]
    if "td_caption" in classes:
        return False
    cells = row.find_all("td", recursive=False) or row.find_all("td")
    if len(cells) < 3:
        return False
    if "td_field" in classes or row.find("td", class_="td_field"):
        return True
    first = cell_text(cells[0])
    return bool(first) and first[0].isdigit()


def _bid_from_cells(headers: list[str], cells: list[Tag], url: str | None) -> BidRow:
    values = [cell_text(cell) for cell in cells]
    by_name = {headers[i]: values[i] if i < len(values) else "" for i in range(len(headers))}

    def pick(*keys: str) -> str:
        for key in keys:
            for name, value in by_name.items():
                if key in name:
                    return value
        return ""

    serial = pick("s.no", "s no", "sno") or (values[0] if values else "")
    bid_number = pick("bid number", "bid no")
    bidder = pick("bidder")
    submitted = pick("submitted")
    status = ""
    updated = ""
    for name, value in by_name.items():
        if "status updated" in name or name.endswith("updated on"):
            updated = value
        elif "status" in name:
            status = value
    remarks = pick("remark")
    if not updated:
        updated = pick("updated on")
    value = pick("awarded value", "value") or None
    rank = pick("rank") or None
    currency = pick("currency") or None
    if not bid_number and len(values) > 1:
        bid_number = values[1]
    if not bidder and len(values) > 2:
        bidder = values[2]
    return BidRow(
        serial=serial,
        bid_number=bid_number,
        bidder_name=bidder,
        submitted_date=submitted,
        status=status,
        remarks=remarks,
        status_updated_on=updated,
        bid_summary_url=url,
        value=value,
        rank=rank,
        currency=currency,
    )


def _filename_from_link(link: Tag) -> str:
    bold = link.find("b")
    if bold:
        return cell_text(bold)
    return cell_text(link)


def _caption_for_link(link: Tag) -> str:
    row = link.find_parent("tr")
    if row is None:
        return ""
    caption = row.find("td", class_="td_caption")
    return cell_text(caption).rstrip(" :")
