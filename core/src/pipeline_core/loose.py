"""Read dates and amounts stored as text.

The portal writes values like ``21-Sep-2026`` and ``INR 179,853.24``. A date
filter that cannot read a contract date leaves that row on screen. A value
filter drops a row whose amount cannot be read.
"""

from __future__ import annotations

import re
from datetime import date, datetime

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_NAMED_MONTH = re.compile(r"(\d{1,2})[- /.]([A-Za-z]{3,9})[- /.](\d{4})")
_NUMERIC_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")


def loose_date(value: object) -> str | None:
    """ISO date ``YYYY-MM-DD``, or None when the text is not a date."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in _NUMERIC_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    named = _NAMED_MONTH.fullmatch(text)
    if named is None:
        return None
    day, month_name, year = named.groups()
    month = _MONTHS.get(month_name[:3].lower())
    if month is None:
        return None
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def loose_number(value: object) -> float | None:
    """A float parsed from mixed text, or None when no single number is there."""
    if value is None:
        return None
    text = str(value).strip()
    if not any(character.isdigit() for character in text):
        return None
    digits = "".join(character for character in text if character.isdigit() or character == ".")
    if digits.count(".") > 1 or digits in {"", "."}:
        return None
    try:
        return float(digits)
    except ValueError:
        return None
