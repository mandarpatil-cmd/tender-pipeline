from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_STORE_CHARS = 8000

_EMAIL = re.compile(
    r"[A-Za-z0-9._%+\-]+\s*@\s*[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)
_MOBILE = re.compile(r"(?:\+91[\s\-]?)?([6-9]\d{9})")
_GSTIN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b", re.I)
_GSTIN_BODY = re.compile(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]")
_BUYER_EMAIL_MARKERS = (
    "vnit",
    "ynit",
    "vnii",
    "nic.in",
    "gov.in",
    "eprocure",
    "storesoffic",
    "sioresoffic",
    "storesoffice",
)
# Institute GSTIN on VNIT letterhead — never treat as the vendor.
_BUYER_GSTIN = ("27AAATV9885C1Z2", "27AAATV9885C1ZZ")


# First five pages plus the last. Six or fewer means the whole file.
_OCR_HEAD_PAGES = 5
# GSTIN slots that must be digits, and slots that must be letters.
_GSTIN_DIGIT_AT = {0, 1, 7, 8, 9, 10}
_GSTIN_LETTER_AT = {2, 3, 4, 5, 6, 11, 13}
_GSTIN_TOKEN = re.compile(r"\b[A-Za-z0-9]{15}\b")


def extract_pdf(path: Path) -> dict[str, Any]:
    """Read a work order, OCR first.

    These files are usually scans. LiteParse reads the pages that can carry a
    contact. pypdf's text layer is the backup when OCR finds no email, phone,
    or GSTIN, or when OCR cannot run. ``error`` is set only when the file
    itself cannot be read.
    """
    path = Path(path)
    payload: dict[str, Any] = {
        "path": str(path),
        "filename": path.name,
        "pages": 0,
        "char_count": 0,
        "text": "",
        "emails": [],
        "phones": [],
        "gstins": [],
        "error": None,
    }
    if not path.is_file():
        payload["error"] = f"Could not open {path.name}"
        return payload

    ocr_text = ""
    ocr_pages = 0
    try:
        ocr_text, ocr_pages = _ocr_pdf(path)
    except Exception as exc:
        log.warning("PDF OCR failed for %s: %s", path, exc)

    if ocr_text:
        repaired = _repair_gstin_tokens(ocr_text)
        contacts = harvest_contacts(repaired)
        if _has_contact(contacts):
            return _fill(payload, repaired, contacts, ocr_pages)

    try:
        layer_text, layer_pages = _embedded_text(path)
    except Exception as exc:
        log.warning("PDF text layer failed for %s: %s", path, exc)
        if ocr_text:
            repaired = _repair_gstin_tokens(ocr_text)
            return _fill(payload, repaired, harvest_contacts(repaired), ocr_pages)
        payload["error"] = str(exc)
        return payload

    layer_contacts = harvest_contacts(layer_text)
    if _has_contact(layer_contacts):
        return _fill(payload, layer_text, layer_contacts, layer_pages or ocr_pages)
    if ocr_text:
        repaired = _repair_gstin_tokens(ocr_text)
        return _fill(payload, repaired, harvest_contacts(repaired), ocr_pages or layer_pages)
    return _fill(payload, layer_text, layer_contacts, layer_pages)


def _ocr_pdf(path: Path) -> tuple[str, int]:
    """OCR the opening pages and the last page. Raises when LiteParse cannot read it."""
    from liteparse import LiteParse

    page_count = _page_count(path)
    options: dict[str, Any] = {
        "ocr_enabled": True,
        "ocr_language": "eng",
        "dpi": 150,
        "quiet": True,
    }
    targets = _ocr_targets(page_count)
    if targets:
        options["target_pages"] = targets
    with LiteParse(**options) as parser:
        result = parser.parse(str(path))
    text = (result.text or "").strip()
    pages = page_count or int(result.total_pages or 0)
    return text, pages


def _page_count(path: Path) -> int:
    """How many pages the file has. This does not read the contact text."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        log.warning("Could not count pages in %s", path)
        return 0


def _ocr_targets(page_count: int) -> str | None:
    if page_count <= _OCR_HEAD_PAGES + 1:
        return None
    return f"1-{_OCR_HEAD_PAGES},{page_count}"


def _embedded_text(path: Path) -> tuple[str, int]:
    """The PDF's own text layer. Raises when that layer cannot be read."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    chunks = [(page.extract_text() or "") for page in reader.pages]
    return "\n".join(chunks).strip(), len(reader.pages)


def _repair_gstin_tokens(text: str) -> str:
    """Fix O/0 and I/1 inside a 15-character GSTIN-shaped token.

    The rest of the page is left alone, so an email is not rewritten.
    """

    def fix(match: re.Match[str]) -> str:
        chars = list(match.group(0).upper())
        for index in _GSTIN_DIGIT_AT:
            if chars[index] == "O":
                chars[index] = "0"
            elif chars[index] == "I":
                chars[index] = "1"
        for index in _GSTIN_LETTER_AT:
            if chars[index] == "0":
                chars[index] = "O"
            elif chars[index] == "1":
                chars[index] = "I"
        repaired = "".join(chars)
        if _GSTIN_BODY.fullmatch(repaired):
            return repaired
        return match.group(0)

    return _GSTIN_TOKEN.sub(fix, text)


def _has_contact(contacts: dict[str, list[str]]) -> bool:
    return bool(contacts["emails"] or contacts["phones"] or contacts["gstins"])


def _fill(payload: dict[str, Any], text: str, contacts: dict[str, list[str]], pages: int) -> dict[str, Any]:
    payload["pages"] = pages
    payload["char_count"] = len(text)
    payload["text"] = text[:MAX_STORE_CHARS]
    payload["emails"] = contacts["emails"]
    payload["phones"] = contacts["phones"]
    payload["gstins"] = contacts["gstins"]
    return payload


_LOCAL_PREFIXES = ("id-", "email-", "mail-", "e-mail-")


def _normalize_email(raw: str) -> str:
    local, _, domain = raw.replace(" ", "").partition("@")
    lowered = local.lower()
    for prefix in _LOCAL_PREFIXES:
        if lowered.startswith(prefix) and len(local) > len(prefix) + 2:
            local = local[len(prefix) :]
            break
    return f"{local}@{domain.lower()}"


def _is_buyer_email(email: str) -> bool:
    lowered = email.lower()
    return any(marker in lowered for marker in _BUYER_EMAIL_MARKERS)


def harvest_contacts(text: str) -> dict[str, list[str]]:
    """Pull emails / Indian mobiles / GSTINs. Drop obvious buyer (VNIT) contacts."""
    emails = []
    for match in _EMAIL.findall(text or ""):
        email = _normalize_email(match)
        if _is_buyer_email(email):
            continue
        if email.lower() not in {item.lower() for item in emails}:
            emails.append(email)
    phones = []
    for match in _MOBILE.findall(text or ""):
        if match not in phones:
            phones.append(match)
    gstins = []
    for match in _GSTIN.findall(text or ""):
        value = match.upper()
        if value in _BUYER_GSTIN:
            continue
        if value not in gstins:
            gstins.append(value)
    return {"emails": emails, "phones": phones, "gstins": gstins}


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
    emails: list[str] = []
    phones: list[str] = []
    gstins: list[str] = []
    for item in extracts:
        text = item.get("text") or ""
        contacts = harvest_contacts(text) if text.strip() else {
            "emails": item.get("emails") or [],
            "phones": item.get("phones") or [],
            "gstins": item.get("gstins") or [],
        }
        for email in contacts["emails"]:
            if email.lower() not in {x.lower() for x in emails}:
                emails.append(email)
        for phone in contacts["phones"]:
            if phone not in phones:
                phones.append(phone)
        for gstin in contacts["gstins"]:
            if gstin not in gstins:
                gstins.append(gstin)
    return {"emails": emails, "phones": phones, "gstins": gstins}
