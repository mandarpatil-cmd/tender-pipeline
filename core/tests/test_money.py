"""Amounts as people and the portal write them, read to exact hundredths."""

from __future__ import annotations

import pytest

from pipeline_core.loose import loose_number
from pipeline_core.money import format_amount, iso_day, parse_money


@pytest.mark.parametrize(
    ("text", "hundredths", "currency"),
    [
        ("125000.00", 12500000, None),
        ("INR 179,853.24", 17985324, "INR"),
        ("46,08,100.00", 460810000, None),
        ("Rs. 2,50,000", 25000000, None),
        ("Rs.2,50,000/-", 25000000, None),
        ("2,50,000.00 Rs.", 25000000, None),
        ("₹ 1,00,000.50", 10000050, None),
        ("USD 1,000", 100000, "USD"),
        ("0.5", 50, None),
        (1234, 1234, None),
    ],
)
def test_an_amount_is_read_to_hundredths(text, hundredths, currency):
    assert parse_money(text) == (hundredths, currency)


@pytest.mark.parametrize("text", ["", "  ", "NA", "a lot", "12.50.000", "5,00,000 + GST 18%", None, True])
def test_text_without_one_clear_amount_is_none(text):
    assert parse_money(text)[0] is None


@pytest.mark.parametrize(
    ("text", "number"),
    [("Rs. 2,50,000", 250000.0), ("Rs.2,50,000/-", 250000.0), ("INR 179,853.24", 179853.24), ("1.5", 1.5)],
)
def test_the_filter_reads_the_same_number(text, number):
    assert loose_number(text) == number


@pytest.mark.parametrize("text", ["", "none", "12.50.000", "2 x 50,000"])
def test_the_filter_reads_no_number_from_unclear_text(text):
    assert loose_number(text) is None


def test_hundredths_print_grouped_and_signed():
    assert format_amount(17985324) == "179,853.24"
    assert format_amount(-5) == "-0.05"
    assert format_amount("Rs. 2,50,000") == "250,000.00"
    assert format_amount("") is None and format_amount("NA") is None


def test_a_day_is_iso_or_none():
    assert iso_day("21-Sep-2026") == "2026-09-21"
    assert iso_day("someday") is None
