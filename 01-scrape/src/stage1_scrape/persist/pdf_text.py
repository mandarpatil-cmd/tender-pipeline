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


def extract_pdf(path: Path) -> dict[str, Any]:
    """Pull text from a downloaded AOC/work-order PDF. Scanned pages yield empty text."""
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
    try:
        from pypdf import PdfReader
    except ImportError:
        payload["error"] = "pypdf is not installed"
        return payload
    try:
        reader = PdfReader(str(path))
        payload["pages"] = len(reader.pages)
        chunks: list[str] = []
        for page in reader.pages:
            chunks.append(page.extract_text() or "")
        text = "\n".join(chunks).strip()
    except Exception as exc:
        log.warning("PDF extract failed for %s: %s", path, exc)
        payload["error"] = str(exc)
        return payload
    contacts = harvest_contacts(text)
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


def extract_folder(folder: Path) -> list[dict[str, Any]]:
    if not folder.is_dir():
        return []
    files = sorted({*folder.glob("*.pdf"), *folder.glob("*.PDF")})
    return [extract_pdf(path) for path in files]


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
