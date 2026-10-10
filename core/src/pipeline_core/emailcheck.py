"""The address check the mailer and the window both use."""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_email(value: str | None) -> bool:
    return bool(EMAIL_RE.match((value or "").strip()))


def is_phone(value: str | None) -> bool:
    """Digits, with an optional leading +. At least 10 digits."""
    text = (value or "").strip()
    if not text:
        return False
    body = text[1:] if text.startswith("+") else text
    if not body or any(ch not in "0123456789- ()" for ch in body):
        return False
    return sum(ch.isdigit() for ch in text) >= 10
