from __future__ import annotations

from collections import defaultdict

from bs4 import BeautifulSoup, Tag

from stage1_scrape.config import SEARCH_FORM_ID
from stage1_scrape.domain.errors import FormError

SKIP_INPUT_TYPES = frozenset({"submit", "button", "image", "reset", "file"})


def parse_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def find_search_form(soup: BeautifulSoup) -> Tag:
    form = soup.find("form", id=SEARCH_FORM_ID)
    if form is None:
        raise FormError(
            f"Could not find form#{SEARCH_FORM_ID}. "
            "The session may have expired or the portal returned a different page."
        )
    return form


def extract_form_fields(html: str, form_id: str = SEARCH_FORM_ID) -> list[tuple[str, str]]:
    """Return every named control as (name, value) pairs.

    Duplicate names are preserved in document order. GePNIC/Tapestry result
    pages emit multiple ``iterRows_0`` hidden inputs — collapsing those into
    one value breaks the next POST.
    """
    soup = parse_html(html)
    form = soup.find("form", id=form_id)
    if form is None:
        raise FormError(f"Could not find form#{form_id}")

    fields: list[tuple[str, str]] = []
    for element in form.find_all(["input", "select", "textarea"]):
        name = element.get("name")
        if not name:
            continue
        if element.name == "input":
            input_type = (element.get("type") or "text").lower()
            if input_type in SKIP_INPUT_TYPES:
                continue
            if input_type in {"checkbox", "radio"} and not element.has_attr("checked"):
                continue
            fields.append((name, element.get("value") or ""))
        elif element.name == "select":
            selected = element.find("option", selected=True) or element.find("option")
            fields.append((name, (selected.get("value") or "") if selected else ""))
        else:
            fields.append((name, element.text or ""))
    return fields


def override_fields(
    fields: list[tuple[str, str]],
    updates: dict[str, str],
) -> list[tuple[str, str]]:
    """Replace existing keys in place; append keys that were not in the form."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for name, value in fields:
        if name in updates:
            out.append((name, updates[name]))
            seen.add(name)
        else:
            out.append((name, value))
    for name, value in updates.items():
        if name not in seen:
            out.append((name, value))
    return out


def field_map(fields: list[tuple[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for name, value in fields:
        grouped[name].append(value)
    return dict(grouped)


def first_value(fields: list[tuple[str, str]], name: str, default: str = "") -> str:
    for key, value in fields:
        if key == name:
            return value
    return default
