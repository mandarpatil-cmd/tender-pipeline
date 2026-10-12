"""The address checks every stage uses: format, and whose address it is."""

from __future__ import annotations

import re
from dataclasses import dataclass

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Pieces of a buyer's address: government domains, the portal, and the VNIT
#: letterhead with the OCR misreads seen on its scans. An address holding one is
#: the department that ran the tender, never the company that won it.
BUYER_EMAIL_MARKERS = (
    "vnit",
    "ynit",
    "vnii",
    "nic.in",
    "gov.in",
    # One-character OCR twins of gov.in seen on scanned letterheads.
    "qov.in",
    "goy.in",
    "qoy.in",
    "g0v.in",
    "eprocure",
    "storesoffic",
    "sioresoffic",
    "storesoffice",
)


def is_email(value: str | None) -> bool:
    return bool(EMAIL_RE.match((value or "").strip()))


def is_buyer_email(value: str | None) -> bool:
    lowered = (value or "").strip().lower()
    return any(marker in lowered for marker in BUYER_EMAIL_MARKERS)


def is_phone(value: str | None) -> bool:
    """Digits, with an optional leading +. At least 10 digits."""
    text = (value or "").strip()
    if not text:
        return False
    body = text[1:] if text.startswith("+") else text
    if not body or any(ch not in "0123456789- ()" for ch in body):
        return False
    return sum(ch.isdigit() for ch in text) >= 10


@dataclass(frozen=True)
class ContactVerdict:
    """What of a looked-up contact can be stored, and what was left out."""

    email: str | None
    phone: str | None
    #: Each value not kept, with why: ``rfi@ord.gov.in (government or buyer address)``.
    dropped: tuple[str, ...] = ()


def usable_contact(email: str | None, phone: str | None) -> ContactVerdict:
    """Keep a well-formed company email and a dialable phone. Drop the rest."""
    kept_email = (email or "").strip() or None
    kept_phone = (phone or "").strip() or None
    dropped: list[str] = []
    if kept_email and not is_email(kept_email):
        dropped.append(f"{kept_email} (not an email address)")
        kept_email = None
    elif kept_email and is_buyer_email(kept_email):
        dropped.append(f"{kept_email} (government or buyer address)")
        kept_email = None
    if kept_phone and not is_phone(kept_phone):
        dropped.append(f"{kept_phone} (not a phone number)")
        kept_phone = None
    return ContactVerdict(email=kept_email, phone=kept_phone, dropped=tuple(dropped))
