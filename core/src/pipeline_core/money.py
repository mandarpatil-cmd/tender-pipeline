"""Amounts and calendar days, parsed once at the door.

SQLite has no decimal and no date. An amount is an integer count of hundredths,
so ``179853.24`` is ``17985324`` and two amounts compare exactly. A calendar
day is ``YYYY-MM-DD`` text, which compares in calendar order. A real would
make ``179853.24`` inexact.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .loose import loose_date

_CODES = ("INR", "USD", "EUR", "GBP")


def iso_day(value: object) -> str | None:
    """``YYYY-MM-DD``, or None when the text is blank or not a date."""
    return loose_date(value)


def parse_money(value: object) -> tuple[int | None, str | None]:
    """Integer hundredths, and a currency code when the text names one.

    An int is already hundredths. ``INR 179,853.24`` is ``17985324`` and
    ``INR``. ``46,08,100.00`` is ``460810000``. Text with no number is
    ``(None, code)``.
    """
    if isinstance(value, bool) or value is None:
        return None, None
    if isinstance(value, int):
        return value, None
    text = str(value).strip()
    if not text:
        return None, None
    currency = _currency(text)
    if not any(character.isdigit() for character in text):
        return None, currency
    digits = "".join(character for character in text if character.isdigit() or character == ".")
    if digits.count(".") > 1 or digits in {"", "."}:
        return None, currency
    try:
        hundredths = int(
            (Decimal(digits) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    except Exception:
        return None, currency
    return hundredths, currency


def format_amount(value: object) -> str | None:
    """Hundredths back to a grouped number. ``17985324`` is ``179,853.24``."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        hundredths = value
    else:
        hundredths, _currency_code = parse_money(value)
        if hundredths is None:
            return None
    sign = "-" if hundredths < 0 else ""
    whole, frac = divmod(abs(hundredths), 100)
    return f"{sign}{whole:,}.{frac:02d}"


def _currency(text: str) -> str | None:
    upper = text.upper()
    for code in _CODES:
        if upper.startswith(code) or upper.endswith(code):
            return code
    return None
