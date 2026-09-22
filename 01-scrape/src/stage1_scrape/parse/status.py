from __future__ import annotations

from stage1_scrape.config import STAGE_SUMMARY_TITLE
from stage1_scrape.domain.errors import ParseError
from stage1_scrape.parse.html import abs_url
from stage1_scrape.scraping.forms import parse_html


def find_stage_summary_url(html: str) -> str:
    """CONFIRMED: match ``title="View the all stage summary Details"``.

    The onclick popup handler only changes how the browser displays the page;
    the href is a working GET.
    """
    soup = parse_html(html)
    link = soup.find("a", attrs={"title": STAGE_SUMMARY_TITLE})
    if link is None or not link.get("href"):
        raise ParseError(
            f'Could not find <a title="{STAGE_SUMMARY_TITLE}"> on the tender status page.'
        )
    return abs_url(link["href"])
