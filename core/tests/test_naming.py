"""`name_norm` is the unique key on vendors, so these rules are load-bearing.

They were lifted from the scraper unchanged; these cases pin the behaviour so a
later tidy-up cannot silently split one company into two rows.
"""

from __future__ import annotations

import pytest

from pipeline_core.naming import (
    buyer_tail,
    infer_city_state,
    infer_legal_form,
    normalize_name,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("M/S Kanta Enterprises", "KANTA ENTERPRISES"),
        ("Kanta  Enterprises.", "KANTA ENTERPRISES"),
        ("Patle & Sons", "PATLE AND SONS"),
        ("Acme Pvt. Ltd.", "ACME PVT LTD"),
        ("Acme Private Limited", "ACME PVT LTD"),
        ("", ""),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_normalization_collapses_the_variants_that_matter():
    forms = ["M/S Kanta Enterprises", "kanta enterprises", "Kanta  Enterprises."]
    assert len({normalize_name(name) for name in forms}) == 1


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Acme Private Limited", "pvt_ltd"),
        ("Acme Pvt Ltd", "pvt_ltd"),
        ("Acme LLP", "llp"),
        ("Acme Limited", "ltd"),
        ("Kanta Enterprises", "trade"),
        ("Patle Contractors", "trade"),
        ("Ramesh Kumar Sharma", "person"),
    ],
)
def test_infer_legal_form(raw, expected):
    assert infer_legal_form(raw) == expected


def test_infer_city_state_reads_the_buyer_tail():
    chain = "NPIU||Visvesvaraya National Institute of Technology Nagpur"
    assert infer_city_state(chain) == ("Nagpur", "Maharashtra")


def test_infer_city_state_is_blank_when_unknown():
    assert infer_city_state("Some Department||Unknown Town") == ("", "")


def test_buyer_tail_takes_the_last_link():
    assert buyer_tail("A||B||C") == "C"
    assert buyer_tail("") == ""
