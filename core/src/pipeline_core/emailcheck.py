"""The address check the mailer and the window both use."""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_email(value: str | None) -> bool:
    return bool(EMAIL_RE.match((value or "").strip()))
