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
#: Digits with grouping commas and one decimal part. A dot after letters, as in
#: ``Rs.``, is not a decimal point.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?|(?<![A-Za-z.])\.\d+")


def number_text(value: object) -> str | None:
    """The one number in mixed text, without its commas. None unless there is exactly one.

    ``Rs. 2,50,000/-`` is ``250000``. ``5,00,000 + GST 18%`` is None: two numbers.
    """
    found = _NUMBER.findall(str(value or ""))
    if len(found) != 1:
        return None
    return found[0].replace(",", "")


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
    digits = number_text(value)
    if digits is None:
        return None
    try:
        return float(digits)
    except ValueError:
        return None
