"""The address checks every stage uses: format, and whose address it is."""

from __future__ import annotations

import re
from dataclasses import dataclass

#: local@domain.tld. The local part starts with a letter or digit and does not
#: end with a dot, the domain is dotted labels, the last label two letters or more.
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._%+'-]*(?<!\.)"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,}$"
)

#: What OCR and a model leave around an address: ``-name@x.com``, ``(name@x.com).``
_EDGE_JUNK = "-\u2013\u2014_.,;:()<>[]{}'\"*\u2022|/\\ \t\r\n"

#: A model sometimes returns two addresses in the one field, joined by a slash,
#: a comma, or "and". Spaces and hyphens stay inside a phone: "+91 7020008149".
_VALUE_SPLIT = re.compile(
    r"\s*(?:/|\||;|,|\band\b|\bor\b|\r?\n)+\s*",
    re.IGNORECASE,
)

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
    # Research institutes and academic domains buy; they do not win tenders.
    "res.in",
    "ac.in",
    "ernet.in",
)


def clean_email(value: str | None) -> str:
    """The address without the punctuation a scan or a model left at its edges.

    ``-name@x.com``, ``mailto:name@x.com``, ``E-mail:-name@x.com`` and
    ``(name@x.com).`` all become ``name@x.com``. Anything inside the address is
    left as it is.
    """
    text = (value or "").strip()
    at = text.find("@")
    label = text.rfind(":", 0, at) if at > 0 else -1
    if label >= 0:
        text = text[label + 1 :]
    while True:
        before = text
        text = text.strip(_EDGE_JUNK)
        if text == before:
            return text


def is_email(value: str | None) -> bool:
    text = (value or "").strip()
    return bool(EMAIL_RE.match(text)) and ".." not in text


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

    #: The first kept value. The company columns and the grid show this one.
    email: str | None
    phone: str | None
    #: Each value not kept, with why: ``rfi@ord.gov.in (government or buyer address)``.
    dropped: tuple[str, ...] = ()
    #: Every kept value, first-seen order. ``email`` and ``phone`` repeat the first.
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()


def _pieces(text: str) -> list[str]:
    return [part.strip() for part in _VALUE_SPLIT.split(text) if part.strip()]


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append(value)
    return kept


def _email_values(raw: str | None) -> tuple[list[str], list[str]]:
    """Company addresses from one field, and the pieces that were left out."""
    text = (raw or "").strip()
    if not text:
        return [], []
    whole = clean_email(text)
    if is_email(whole):
        if is_buyer_email(whole):
            return [], [f"{whole} (government or buyer address)"]
        return [whole], []
    pieces: list[str] = []
    for part in _pieces(text):
        pieces.extend(clean_email(bit) for bit in part.split() if clean_email(bit))
    if not pieces:
        return [], [f"{text} (not an email address)"]
    kept: list[str] = []
    dropped: list[str] = []
    for piece in pieces:
        if not is_email(piece):
            dropped.append(f"{piece} (not an email address)")
        elif is_buyer_email(piece):
            dropped.append(f"{piece} (government or buyer address)")
        else:
            kept.append(piece)
    return _unique(kept), dropped


def _phone_values(raw: str | None) -> tuple[list[str], list[str]]:
    """Dialable numbers from one field, and the pieces that were left out."""
    text = (raw or "").strip()
    if not text:
        return [], []
    if is_phone(text):
        return [text], []
    pieces = _pieces(text)
    if not pieces:
        return [], [f"{text} (not a phone number)"]
    kept: list[str] = []
    dropped: list[str] = []
    for piece in pieces:
        if is_phone(piece):
            kept.append(piece)
        else:
            dropped.append(f"{piece} (not a phone number)")
    return _unique(kept), dropped


def usable_contact(email: str | None, phone: str | None) -> ContactVerdict:
    """Keep every well-formed company email and dialable phone. Drop the rest.

    A field that already passes is one value. Otherwise it is split on ``/``,
    ``|``, ``;``, ``,``, newlines, and the words ``and`` and ``or``. Emails also
    split on whitespace. A short leftover such as an extension is still dropped.
    """
    emails, email_dropped = _email_values(email)
    phones, phone_dropped = _phone_values(phone)
    return ContactVerdict(
        email=emails[0] if emails else None,
        phone=phones[0] if phones else None,
        dropped=tuple(email_dropped + phone_dropped),
        emails=tuple(emails),
        phones=tuple(phones),
    )
