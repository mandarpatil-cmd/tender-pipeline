from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import Tag

from stage1_scrape.config import APP_URL

CELL_WS = re.compile(r"\s+")
SIZE_RE = re.compile(r"\(\s*([^)]+?)\s*\)")


def cell_text(node: Tag | None) -> str:
    if node is None:
        return ""
    return CELL_WS.sub(" ", node.get_text(" ", strip=True)).strip()


def abs_url(href: str | None) -> str:
    if not href:
        return ""
    return urljoin(APP_URL, href)
