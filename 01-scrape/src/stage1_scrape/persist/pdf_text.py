from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def extract_pdf(path: Path) -> dict[str, Any]:
    """Read every page with LiteParse.

    A digital page comes back as its own text. A scanned page is OCR'd.
    Contact clues are not mined here. The document model fills those after
    this returns. ``error`` is set only when LiteParse cannot read the file.
    """
    path = Path(path)
    payload: dict[str, Any] = {
        "path": str(path),
        "filename": path.name,
        "pages": 0,
        "char_count": 0,
        "text": "",
        "text_source": "",
        "emails": [],
        "phones": [],
        "gstins": [],
        "error": None,
    }
    if not path.is_file():
        payload["error"] = f"Could not open {path.name}"
        return payload

    try:
        text, pages = _read_pdf(path)
    except Exception as exc:
        log.warning("PDF read failed for %s: %s", path, exc)
        payload["error"] = str(exc)
        return payload

    text = (text or "").strip()
    if not text:
        payload["error"] = "No text in the PDF"
        payload["pages"] = pages
        return payload

    payload["pages"] = pages
    payload["char_count"] = len(text)
    payload["text"] = text
    payload["text_source"] = "liteparse"
    return payload


def _read_pdf(path: Path) -> tuple[str, int]:
    """Native text where the page has it, OCR at 300 DPI where it does not."""
    from liteparse import LiteParse

    with LiteParse(
        ocr_enabled=True,
        ocr_language="eng",
        dpi=300,
        quiet=True,
    ) as parser:
        result = parser.parse(str(path))
    text = result.text or ""
    pages = int(result.total_pages or 0)
    return text, pages


def pdf_paths(folder: Path) -> list[Path]:
    """PDF files in a folder, one path each. Case differences do not duplicate a file."""
    if not folder.is_dir():
        return []
    found: dict[Path, Path] = {}
    for path in folder.iterdir():
        if path.is_file() and path.suffix.lower() == ".pdf":
            found.setdefault(path.resolve(), path)
    return [found[key] for key in sorted(found)]


def extract_folder(folder: Path) -> list[dict[str, Any]]:
    return [extract_pdf(path) for path in pdf_paths(folder)]


def merge_pdf_contacts(extracts: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Join clue lists already on the extracts. Does not read the page text."""
    emails: list[str] = []
    phones: list[str] = []
    gstins: list[str] = []
    for item in extracts:
        for email in item.get("emails") or []:
            if email.lower() not in {value.lower() for value in emails}:
                emails.append(email)
        for phone in item.get("phones") or []:
            if phone not in phones:
                phones.append(phone)
        for gstin in item.get("gstins") or []:
            if gstin not in gstins:
                gstins.append(gstin)
    return {"emails": emails, "phones": phones, "gstins": gstins}
