"""The award grid: one query for the table and for the filtered counts."""

from __future__ import annotations

import pytest

from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.grid import AwardQuery, award_view, choices, filtered_vendor_ids
from pipeline_core.loose import loose_date, loose_number
from pipeline_core.models import OUTREACH_SENT, STATUS_DONE
from pipeline_core.queries import (
    mark_enriched,
    record_outreach,
    replace_awards,
    upsert_tender,
    upsert_vendor,
    utcnow,
)


@pytest.fixture
def bind(tmp_path):
    eng = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(eng)
    return eng


def test_loose_readers_keep_real_values_and_reject_junk():
    assert loose_date("21-Sep-2026") == "2026-09-21"
    assert loose_date("21/09/2026") == "2026-09-21"
    assert loose_date("whenever") is None
    assert loose_number("INR 179,853.24") == 179853.24
    assert loose_number("see annex") is None


def _seed(bind):
    with session(bind) as current:
        old = upsert_vendor(current, name_raw="Old Roads Ltd", state="Bihar")
        new = upsert_vendor(current, name_raw="New Electricals", state="West Bengal")
        odd = upsert_vendor(current, name_raw="Odd Annex Works", state="Assam")
        upsert_tender(
            current,
            tender_id="OLD",
            title="Old road",
            organisation="NHAI Bihar",
            status="AOC",
            contract_date="01-Jan-2020",
            contract_value="INR 10",
            scraped_at=utcnow(),
        )
        upsert_tender(
            current,
            tender_id="NEW",
            title="New rooms",
            organisation="NIT Durgapur",
            status="AOC",
            contract_date="21-Sep-2026",
            contract_value="INR 179,853.24",
            scraped_at=utcnow(),
        )
        upsert_tender(
            current,
            tender_id="ODD",
            title="Annex job",
            organisation="Port office",
            status="Retender",
            contract_date="whenever",
            contract_value="see annex",
            scraped_at=utcnow(),
        )
        for tender_id, vendor_id, name in (
            ("OLD", old, "Old Roads Ltd"),
            ("NEW", new, "New Electricals"),
            ("ODD", odd, "Odd Annex Works"),
        ):
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": name}],
            )
        mark_enriched(current, new, email="new@example.com", phone=None)
        return old, new, odd


def test_unreadable_date_and_amount_stay_in_a_range(bind):
    _seed(bind)
    with session(bind) as current:
        dated = award_view(current, AwardQuery(date_from="2026-01-01"), page_size=50)
        priced = award_view(current, AwardQuery(value_min="100"), page_size=50)

    assert {row["tender_id"] for row in dated.rows} == {"NEW", "ODD"}
    assert {row["tender_id"] for row in priced.rows} == {"NEW", "ODD"}


def test_search_is_partial_and_does_not_treat_percent_as_everything(bind):
    _seed(bind)
    with session(bind) as current:
        found = award_view(current, AwardQuery(text="electrical"), page_size=50)
        literal = award_view(current, AwardQuery(text="%"), page_size=50)

    assert [row["tender_id"] for row in found.rows] == ["NEW"]
    assert found.counts.vendors == 1
    assert found.counts.mailable == 1
    assert literal.total == 0


def test_page_two_follows_the_same_order(bind):
    _seed(bind)
    with session(bind) as current:
        first = award_view(current, AwardQuery(page=1), page_size=2)
        second = award_view(current, AwardQuery(page=2), page_size=2)

    assert first.total == 3
    assert first.pages == 2
    assert [row["tender_id"] for row in first.rows] == ["NEW", "ODD"]
    assert [row["tender_id"] for row in second.rows] == ["OLD"]
    assert second.counts.awards == 3


def test_latest_outreach_does_not_duplicate_the_award(bind):
    _, new, _ = _seed(bind)
    with session(bind) as current:
        record_outreach(
            current,
            vendor_id=new,
            email="new@example.com",
            subject="first",
            transport="gmail",
            status="failed",
        )
        record_outreach(
            current,
            vendor_id=new,
            email="new@example.com",
            subject="second",
            transport="gmail",
            status=OUTREACH_SENT,
        )
        view = award_view(current, AwardQuery(text="electrical"), page_size=50)

    assert len(view.rows) == 1
    row = view.rows[0]
    assert row["outreach_status"] == OUTREACH_SENT
    assert row["attempts"] == 2
    assert row["enrichment_status"] == STATUS_DONE
    assert view.counts.sent == 1


def test_mailable_and_state_match_the_stored_values(bind):
    _seed(bind)
    with session(bind) as current:
        mailed = award_view(current, AwardQuery(mailable="yes"), page_size=50)
        unmailed = award_view(current, AwardQuery(mailable="no"), page_size=50)
        state = award_view(current, AwardQuery(state="bihar"), page_size=50)
        partial = award_view(current, AwardQuery(state="Bih"), page_size=50)
        picked = choices(current)
        ids = filtered_vendor_ids(current, AwardQuery(state="Bihar"))

    assert {row["tender_id"] for row in mailed.rows} == {"NEW"}
    assert {row["tender_id"] for row in unmailed.rows} == {"OLD", "ODD"}
    assert [row["tender_id"] for row in state.rows] == ["OLD"]
    assert partial.total == 0
    assert "Maharashtra" in picked["state"]
    assert "Bihar" in picked["state"]
    assert ids and len(ids) == 1


def test_direction_reverses_the_same_column(bind):
    _seed(bind)
    with session(bind) as current:
        view = award_view(
            current, AwardQuery(sort="tender_id", direction="desc"), page_size=50
        )

    assert [row["tender_id"] for row in view.rows] == ["OLD", "ODD", "NEW"]
