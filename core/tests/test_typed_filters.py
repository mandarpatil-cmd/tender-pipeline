"""Filters and sorts compare typed columns, not portal text."""

from __future__ import annotations

from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.grid import AwardQuery, award_view
from pipeline_core.models import Award, Outreach, Tender, Vendor
from pipeline_core.queries import (
    classify_selection,
    mark_enriched,
    mark_failed,
    prepare_lookup,
    record_outreach,
    replace_awards,
    upsert_tender,
    upsert_vendor,
    utcnow,
)

import pytest


@pytest.fixture
def bind(tmp_path):
    eng = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(eng)
    return eng


def _tender(current, tender_id, **values):
    upsert_tender(
        current,
        tender_id=tender_id,
        title=values.pop("title", tender_id),
        organisation=values.pop("organisation", "Works"),
        status=values.pop("status", "AOC"),
        scraped_at=values.pop("scraped_at", utcnow()),
        **values,
    )


def _award(current, tender_id, vendor_id, name, **values):
    replace_awards(
        current,
        tender_id,
        [
            {
                "bid_number": "1",
                "vendor_id": vendor_id,
                "bidder_name": name,
                **values,
            }
        ],
    )


def test_portal_text_is_stored_as_hundredths_and_iso_dates(bind):
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Kanta Enterprises")
        _tender(
            current,
            "SMALL",
            contract_date="21-Sep-2026",
            contract_value="INR 179,853.24",
        )
        _tender(current, "BARE", contract_date="whenever", contract_value="10000000")
        _tender(current, "USD", contract_value="USD 50,000")
        _tender(current, "ODD", contract_date="see annex", contract_value="see annex")
        _award(
            current,
            "SMALL",
            vendor_id,
            "Kanta Enterprises",
            quoted_value="46,08,100.00",
            awarded_value="179853.24",
            awarded_currency="INR",
            rank="L1",
        )

    with session(bind) as current:
        small = current.get(Tender, "SMALL")
        bare = current.get(Tender, "BARE")
        usd = current.get(Tender, "USD")
        odd = current.get(Tender, "ODD")
        award = current.get(Award, ("SMALL", "1"))

    assert small.contract_date == "2026-09-21"
    assert small.contract_value == 17985324
    assert small.contract_currency == "INR"
    assert bare.contract_date is None
    assert bare.contract_value == 1000000000
    assert bare.contract_currency == "INR"
    assert usd.contract_value == 5000000
    assert usd.contract_currency == "USD"
    assert odd.contract_value is None
    assert odd.contract_currency is None
    assert award.quoted_value == 460810000
    assert award.awarded_value == 17985324
    assert award.rank == "L1"
    assert not hasattr(award, "contract_value")


def _scrape(bind):
    with session(bind) as current:
        low = upsert_vendor(current, name_raw="Low Bid Ltd", city="Nagpur", state="Maharashtra")
        high = upsert_vendor(current, name_raw="High Bid Ltd", city="Durgapur", state="West Bengal")
        big = upsert_vendor(current, name_raw="Big Bid Ltd")
        odd = upsert_vendor(current, name_raw="Odd Annex Works")
        _tender(
            current,
            "LOW",
            title="Fans for the hall",
            organisation="National Project||Institute of Tech",
            contract_date="01-Jul-2026",
            contract_value="INR 0",
            scraped_at="2026-07-01T08:00:00+00:00",
        )
        _tender(
            current,
            "MID",
            title="Rooms",
            contract_date="21-Sep-2026",
            contract_value="INR 179,853.24",
            scraped_at="2026-09-21T08:00:00+00:00",
        )
        _tender(
            current,
            "HIGH",
            title="Bridge",
            status="awarded",
            contract_date="01-Sep-2026",
            contract_value="INR 4,608,100",
            scraped_at="2026-09-01T08:00:00+00:00",
        )
        _tender(
            current,
            "BIG",
            title="Dam",
            contract_value="10000000",
            contract_date="",
            scraped_at="2026-08-01T08:00:00+00:00",
        )
        _tender(
            current,
            "2026_WBNPI_1",
            title="Code only",
            organisation="Port office",
            contract_date="whenever",
            contract_value="see annex",
            scraped_at="2020-01-01T08:00:00+00:00",
        )
        _award(current, "LOW", low, "Low Bid Ltd", quoted_value="10", awarded_value="10")
        _award(
            current,
            "MID",
            high,
            "High Bid Ltd",
            quoted_value="179853.24",
            awarded_value="179853.24",
        )
        _award(
            current,
            "HIGH",
            high,
            "High Bid Ltd",
            quoted_value="46,08,100.00",
            awarded_value="46,08,100.00",
        )
        _award(current, "BIG", big, "Big Bid Ltd", quoted_value="10000000")
        _award(current, "2026_WBNPI_1", odd, "Odd Annex Works")


def _ids(bind, **fields):
    with session(bind) as current:
        view = award_view(current, AwardQuery(**fields), page_size=None)
    return [row["tender_id"] for row in view.rows]


def test_scrape_filters_use_the_typed_columns(bind):
    _scrape(bind)

    assert _ids(bind, text="fans") == ["LOW"]
    assert _ids(bind, tender_status="awarded") == ["HIGH"]
    assert _ids(bind, organisation="Institute") == ["LOW"]
    assert _ids(bind, organisation="WBNPI") == []
    assert _ids(bind, scraped_from="2026-09-01", scraped_to="2026-09-21") == ["HIGH", "MID"]
    assert _ids(bind, value_min="1000000") == ["BIG", "HIGH"]
    assert _ids(bind, value_max="200000") == ["LOW", "MID"]
    assert _ids(bind, value_min="100000", value_max="200000") == ["MID"]

    with session(bind) as current:
        view = award_view(current, AwardQuery(value_min="1000000"), page_size=None)
    shown = {row["tender_id"]: row["contract_value"] for row in view.rows}
    assert shown["HIGH"] == "4,608,100.00"
    assert "MID" not in shown


def test_money_and_dates_sort_by_value_with_blanks_last(bind):
    _scrape(bind)

    assert _ids(bind, sort="contract_value") == [
        "LOW",
        "MID",
        "HIGH",
        "BIG",
        "2026_WBNPI_1",
    ]
    assert _ids(bind, sort="contract_value", direction="desc")[0] == "BIG"
    assert _ids(bind, sort="contract_value", direction="desc")[-1] == "2026_WBNPI_1"
    dated = _ids(bind, sort="contract_date")
    assert dated[:3] == ["LOW", "HIGH", "MID"]
    assert set(dated[3:]) == {"BIG", "2026_WBNPI_1"}
    assert _ids(bind, sort="quoted_value")[:2] == ["LOW", "MID"]
    quoted = _ids(bind, sort="quoted_value")
    assert quoted.index("MID") < quoted.index("HIGH")
    assert quoted[-1] == "2026_WBNPI_1"


def test_enrich_status_and_source_narrow_the_same_join(bind):
    with session(bind) as current:
        pending = upsert_vendor(current, name_raw="Waiting Ltd", source="scrape")
        done = upsert_vendor(current, name_raw="Done Ltd", source="manual")
        missing = upsert_vendor(current, name_raw="Missing Ltd", source="bideasy")
        failed = upsert_vendor(current, name_raw="Failed Ltd", source="scrape")
        for tender_id, vendor_id, name in (
            ("P", pending, "Waiting Ltd"),
            ("D", done, "Done Ltd"),
            ("N", missing, "Missing Ltd"),
            ("F", failed, "Failed Ltd"),
        ):
            _tender(current, tender_id)
            _award(current, tender_id, vendor_id, name)
        mark_enriched(current, done, email="done@example.com", phone=None)
        mark_enriched(current, missing, email=None, phone=None)
        mark_failed(current, failed)
        counts = classify_selection(current, [pending, done, missing, failed])

    assert counts.skipped == ("Done Ltd",)
    assert _ids(bind, enrichment_status=("pending",)) == ["P"]
    assert _ids(bind, enrichment_status=("done",)) == ["D"]
    assert _ids(bind, enrichment_status=("not_found",)) == ["N"]
    assert _ids(bind, enrichment_status=("failed",)) == ["F"]
    assert _ids(bind, source="manual") == ["D"]
    assert _ids(bind, source="bideasy") == ["N"]

    with session(bind) as current:
        looked = prepare_lookup(current, [pending, done, missing, failed])
    assert [row.vendor_id for row in looked] == [pending, missing, failed]
    assert looked[0].name_raw == "Waiting Ltd"


def test_a_company_with_a_contact_is_not_looked_up_again(bind):
    with session(bind) as current:
        known = upsert_vendor(current, name_raw="Known Ltd")
        mark_enriched(current, known, email="known@example.com", phone="9000000000")
        _tender(current, "NEW")
        _award(current, "NEW", known, "Known Ltd")
        looked = prepare_lookup(current, [known])
        counts = classify_selection(current, [known])
        vendor = current.get(Vendor, known)

    assert looked == []
    assert counts.done == 1
    assert counts.skipped == ("Known Ltd",)
    assert vendor.enrichment_status == "done"
    assert vendor.email == "known@example.com"


def test_mail_filters_follow_email_and_attempt_status(bind):
    with session(bind) as current:
        ready = upsert_vendor(current, name_raw="Ready Ltd")
        phone = upsert_vendor(current, name_raw="Phone Ltd")
        sent = upsert_vendor(current, name_raw="Sent Ltd")
        failed = upsert_vendor(current, name_raw="Failed Ltd")
        for tender_id, vendor_id, name in (
            ("READY", ready, "Ready Ltd"),
            ("PHONE", phone, "Phone Ltd"),
            ("SENT", sent, "Sent Ltd"),
            ("FAIL", failed, "Failed Ltd"),
        ):
            _tender(current, tender_id, scraped_at="2026-01-01T00:00:00+00:00")
            _award(current, tender_id, vendor_id, name)
        mark_enriched(current, ready, email="ready@example.com", phone=None)
        mark_enriched(current, phone, email=None, phone="9000000000")
        mark_enriched(current, sent, email="sent@example.com", phone=None)
        mark_enriched(current, failed, email="fail@example.com", phone=None)
        early = record_outreach(
            current,
            vendor_id=sent,
            tender_id="SENT",
            email="sent@example.com",
            subject="Hello",
            transport="gmail",
            status="sent",
        )
        current.get(Outreach, early).sent_at = "2026-01-02T00:00:00+00:00"
        later = record_outreach(
            current,
            vendor_id=failed,
            tender_id="FAIL",
            email="fail@example.com",
            subject="Hello",
            transport="gmail",
            status="failed",
        )
        current.get(Outreach, later).sent_at = "2026-06-01T00:00:00+00:00"

    assert _ids(bind, mailable="yes", outreach_status="none") == ["READY"]
    assert _ids(bind, outreach_status="sent") == ["SENT"]
    assert _ids(bind, outreach_status="failed") == ["FAIL"]
    assert "PHONE" not in _ids(bind, mailable="yes")
    ordered = _ids(bind, sort="last_attempt_at")
    assert ordered.index("SENT") < ordered.index("FAIL")
    assert ordered.index("FAIL") < ordered.index("READY")
