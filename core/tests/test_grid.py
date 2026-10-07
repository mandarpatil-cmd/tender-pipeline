"""The award grid: one query for the table and for the filtered counts."""

from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from filter_oracle import matching_keys

from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.grid import AwardQuery, award_view, choices, filtered_vendor_ids
from pipeline_core.loose import loose_date, loose_number
from pipeline_core.models import (
    OUTREACH_FAILED,
    OUTREACH_SENT,
    STATUS_DONE,
    STATUS_NOT_FOUND,
    Tender,
    Vendor,
)
from pipeline_core.naming import normalize_name
from pipeline_core.queries import (
    mark_enriched,
    mark_failed,
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


def test_unreadable_date_stays_and_unreadable_amount_drops(bind):
    _seed(bind)
    with session(bind) as current:
        dated = award_view(current, AwardQuery(date_from="2026-01-01"), page_size=50)
        priced = award_view(current, AwardQuery(value_min="100"), page_size=50)

    assert {row["tender_id"] for row in dated.rows} == {"NEW", "ODD"}
    assert {row["tender_id"] for row in priced.rows} == {"NEW"}


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


def test_scraped_day_range_uses_the_stored_timestamp(bind):
    _seed(bind)
    with session(bind) as current:
        current.get(Tender, "OLD").scraped_at = "2020-01-15T08:00:00+00:00"
        current.get(Tender, "NEW").scraped_at = "2026-09-21T08:00:00+00:00"
        current.get(Tender, "ODD").scraped_at = "2026-09-22T08:00:00+00:00"
    with session(bind) as current:
        view = award_view(
            current,
            AwardQuery(scraped_from="2026-09-22", scraped_to="2026-09-21"),
            page_size=50,
        )
        junk = award_view(current, AwardQuery(scraped_from="not-a-date"), page_size=50)

    assert [row["tender_id"] for row in view.rows] == ["NEW", "ODD"]
    assert junk.total == 3


def _prepare(bind):
    ids = _seed(bind)
    with session(bind) as current:
        current.get(Tender, "OLD").scraped_at = "2020-01-15T08:00:00+00:00"
        current.get(Tender, "NEW").scraped_at = "2026-09-21T08:00:00+00:00"
        current.get(Tender, "ODD").scraped_at = "2026-09-22T08:00:00+00:00"
    return ids


def _matching(current, **fields):
    view = award_view(current, AwardQuery(**fields), page_size=50)
    return {row["tender_id"] for row in view.rows}, view.total


def test_each_filter_returns_only_the_matching_awards(bind):
    _prepare(bind)
    cases = [
        ({"text": "electrical"}, {"NEW"}),
        ({"text": "%"}, set()),
        ({"tender_status": "Retender"}, {"ODD"}),
        ({"tender_status": "AOC"}, {"OLD", "NEW"}),
        ({"enrichment_status": "pending"}, {"OLD", "ODD"}),
        ({"enrichment_status": "done"}, {"NEW"}),
        ({"enrichment_status": "not_found"}, set()),
        ({"enrichment_status": "failed"}, set()),
        ({"outreach_status": "none"}, {"OLD", "NEW", "ODD"}),
        ({"outreach_status": "sent"}, set()),
        ({"source": "scrape"}, {"OLD", "NEW", "ODD"}),
        ({"source": "manual"}, set()),
        ({"source": "bideasy"}, set()),
        ({"mailable": "yes"}, {"NEW"}),
        ({"mailable": "no"}, {"OLD", "ODD"}),
        ({"state": "Bihar"}, {"OLD"}),
        ({"state": "Bih"}, set()),
        ({"organisation": "Durgapur"}, {"NEW"}),
        ({"organisation": "no-such-office"}, set()),
        ({"date_from": "2026-01-01"}, {"NEW", "ODD"}),
        ({"date_to": "2020-12-31"}, {"OLD", "ODD"}),
        ({"date_from": "2026-09-01", "date_to": "2026-09-30"}, {"NEW", "ODD"}),
        ({"value_min": "100"}, {"NEW"}),
        ({"value_max": "50"}, {"OLD"}),
        ({"value_min": "1", "value_max": "50"}, {"OLD"}),
        ({"scraped_from": "2026-09-21", "scraped_to": "2026-09-21"}, {"NEW"}),
        ({"scraped_from": "2026-09-22", "scraped_to": "2026-09-21"}, {"NEW", "ODD"}),
        ({"scraped_from": "not-a-date"}, {"OLD", "NEW", "ODD"}),
    ]
    with session(bind) as current:
        for fields, expected in cases:
            got, total = _matching(current, **fields)
            assert got == expected, fields
            assert total == len(expected)


def test_combined_filters_keep_only_awards_that_match_every_rule(bind):
    old, new, odd = _prepare(bind)
    with session(bind) as current:
        record_outreach(
            current,
            vendor_id=new,
            email="new@example.com",
            subject="sent",
            transport="gmail",
            status=OUTREACH_SENT,
        )
        mark_failed(current, old)
        record_outreach(
            current,
            vendor_id=old,
            email="old@example.com",
            subject="failed",
            transport="gmail",
            status=OUTREACH_FAILED,
        )
        current.get(Vendor, odd).source = "manual"
        current.get(Vendor, odd).enrichment_status = STATUS_DONE
        current.get(Vendor, odd).email = None

        empty, _ = _matching(current, enrichment_status="pending", mailable="yes")
        priced, _ = _matching(current, value_min="100", mailable="yes")
        failed, _ = _matching(
            current, enrichment_status="failed", outreach_status="failed"
        )
        pending_sent, _ = _matching(
            current, enrichment_status="pending", outreach_status="sent"
        )
        excluded, _ = _matching(current, text="electrical", state="Bihar")
        manual, _ = _matching(
            current,
            source="manual",
            scraped_from="2026-09-22",
            scraped_to="2026-09-22",
        )
        phone_gap, _ = _matching(current, enrichment_status="done", mailable="no")
        ids = filtered_vendor_ids(current, AwardQuery(value_min="100", mailable="yes"))

    assert empty == set()
    assert priced == {"NEW"}
    assert failed == {"OLD"}
    assert pending_sent == set()
    assert excluded == set()
    assert manual == {"ODD"}
    assert phone_gap == {"ODD"}
    assert ids == [new]


def test_not_found_filter_returns_that_status_only(bind):
    _old, _new, odd = _prepare(bind)
    with session(bind) as current:
        vendor = current.get(Vendor, odd)
        vendor.enrichment_status = STATUS_NOT_FOUND
        vendor.email = None
        got, total = _matching(current, enrichment_status="not_found")
    assert got == {"ODD"}
    assert total == 1


def test_enrichment_filter_keeps_each_combination(bind):
    old, _new, odd = _prepare(bind)
    with session(bind) as current:
        mark_failed(current, old)
        cases = [
            (("pending",), {"ODD"}),
            (("failed",), {"OLD"}),
            (("done",), {"NEW"}),
            (("pending", "failed"), {"ODD", "OLD"}),
            (("pending", "done"), {"ODD", "NEW"}),
            (("not_found", "failed"), {"OLD"}),
            (("pending", "not_found", "failed"), {"ODD", "OLD"}),
        ]
        for statuses, expected in cases:
            got, total = _matching(current, enrichment_status=statuses)
            assert got == expected, statuses
            assert total == len(expected)
        view = award_view(
            current,
            AwardQuery(enrichment_status=("pending", "failed")),
            page_size=50,
        )
    assert view.counts.pending == 1
    assert view.counts.failed == 1
    assert view.counts.not_found == 0


def test_company_name_search_ignores_a_title_that_only_mentions_the_word(bind):
    with session(bind) as current:
        company = upsert_vendor(
            current, name_raw="APSARA INNOVATIONS PRIVATE LIMITED", city="Pune"
        )
        other = upsert_vendor(current, name_raw="ANZEN PROJECTS PRIVATE LIMITED", city="Mumbai")
        upsert_tender(
            current,
            tender_id="NAME",
            title="Rooms",
            organisation="IIT",
            status="AOC",
            contract_date="01-Jan-2026",
            contract_value="INR 10",
            scraped_at=utcnow(),
        )
        upsert_tender(
            current,
            tender_id="TITLE",
            title="Works in the Apsara building",
            organisation="BARC",
            status="AOC",
            contract_date="01-Jan-2026",
            contract_value="INR 10",
            scraped_at=utcnow(),
        )
        replace_awards(
            current,
            "NAME",
            [{"bid_number": "1", "vendor_id": company, "bidder_name": "APSARA INNOVATIONS PRIVATE LIMITED"}],
        )
        replace_awards(
            current,
            "TITLE",
            [{"bid_number": "1", "vendor_id": other, "bidder_name": "ANZEN PROJECTS PRIVATE LIMITED"}],
        )
    with session(bind) as current:
        any_search, _ = _matching(current, text="APSARA")
        by_name, _ = _matching(current, text="APSARA", search_in=("name",))
        by_city, _ = _matching(current, text="Pune", search_in=("city",))
        by_id, _ = _matching(current, text=str(company), search_in=("vendor_id",))
        longer_id, _ = _matching(current, text=str(company) + "1", search_in=("vendor_id",))
        word_as_id, _ = _matching(current, text="APSARA", search_in=("vendor_id",))
    assert any_search == {"NAME", "TITLE"}
    assert by_name == {"NAME"}
    assert by_city == {"NAME"}
    assert by_id == {"NAME"}
    assert longer_id == set()
    assert word_as_id == set()


_DIMENSIONS = (
    ("text", {"text": "electrical"}),
    ("tender_status", {"tender_status": "Retender"}),
    ("enrichment", {"enrichment_status": ("pending",)}),
    ("outreach", {"outreach_status": "sent"}),
    ("source", {"source": "manual"}),
    ("mailable", {"mailable": "yes"}),
    ("state", {"state": "Bihar"}),
    ("organisation", {"organisation": "Durgapur"}),
    ("contract_date", {"date_from": "2026-01-01", "date_to": "2026-12-31"}),
    ("scraped", {"scraped_from": "2026-09-21", "scraped_to": "2026-09-21"}),
    ("contract_value", {"value_min": "100", "value_max": "200000"}),
)


def _varied(bind):
    with session(bind) as current:
        old = upsert_vendor(current, name_raw="Old Roads Ltd", state="Bihar", city="Patna")
        new = upsert_vendor(current, name_raw="New Electricals", state="West Bengal", city="Durgapur")
        odd = upsert_vendor(
            current, name_raw="Odd Annex Works", state="Assam", city="Guwahati", source="manual"
        )
        failed = upsert_vendor(
            current, name_raw="Fail Bridges", state="Goa", city="Panaji", source="bideasy"
        )
        rows = (
            ("OLD", "Old road", "NHAI Bihar", "AOC", "01-Jan-2020", "INR 10", old, "Old Roads Ltd"),
            ("NEW", "New rooms", "NIT Durgapur", "AOC", "21-Sep-2026", "INR 179,853.24", new, "New Electricals"),
            ("ODD", "Annex job", "Port office", "Retender", "whenever", "see annex", odd, "Odd Annex Works"),
            ("FAIL", "Civic hall", "Rail Corp", "AOC", "01-Jun-2024", "INR 50", failed, "Fail Bridges"),
        )
        for tender_id, title, organisation, status, contract_date, contract_value, vendor_id, name in rows:
            upsert_tender(
                current,
                tender_id=tender_id,
                title=title,
                organisation=organisation,
                status=status,
                contract_date=contract_date,
                contract_value=contract_value,
                scraped_at=utcnow(),
            )
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": name}],
            )
        mark_enriched(current, new, email="new@example.com", phone=None)
        mark_enriched(current, odd, email=None, phone=None)
        mark_failed(current, failed)
        record_outreach(
            current,
            vendor_id=new,
            email="new@example.com",
            subject="sent",
            transport="gmail",
            status=OUTREACH_SENT,
        )
        record_outreach(
            current,
            vendor_id=failed,
            email="fail@example.com",
            subject="failed",
            transport="gmail",
            status=OUTREACH_FAILED,
        )
        current.get(Tender, "OLD").scraped_at = "2020-01-15T08:00:00+00:00"
        current.get(Tender, "NEW").scraped_at = "2026-09-21T08:00:00+00:00"
        current.get(Tender, "ODD").scraped_at = "2026-09-22T08:00:00+00:00"
        current.get(Tender, "FAIL").scraped_at = "2024-06-01T08:00:00+00:00"


def _assert_query(current, catalogue, query):
    view = award_view(current, query, page_size=None)
    expected = matching_keys(catalogue, query)
    got = {(row["tender_id"], str(row["bid_number"])) for row in view.rows}
    assert got == expected, query
    assert view.counts.awards == len(expected)
    assert view.counts.tenders == len({tender_id for tender_id, _bid in expected})
    assert view.counts.vendors == len({row["vendor_id"] for row in view.rows})


def test_every_filter_pair_matches_the_oracle(bind):
    _varied(bind)
    with session(bind) as current:
        catalogue = award_view(current, AwardQuery(), page_size=None).rows
        for (left_name, left), (right_name, right) in combinations(_DIMENSIONS, 2):
            fields = {**left, **right}
            _assert_query(current, catalogue, AwardQuery(**fields))


def test_every_enrichment_subset_matches_the_oracle(bind):
    _varied(bind)
    statuses = ("pending", "done", "not_found", "failed")
    with session(bind) as current:
        catalogue = award_view(current, AwardQuery(), page_size=None).rows
        for size in range(1, len(statuses) + 1):
            for subset in combinations(statuses, size):
                _assert_query(current, catalogue, AwardQuery(enrichment_status=subset))


def test_every_search_scope_matches_the_oracle(bind):
    with session(bind) as current:
        current.add(
            Vendor(
                vendor_id=733,
                name_raw="APSARA INNOVATIONS PRIVATE LIMITED",
                name_norm=normalize_name("APSARA INNOVATIONS PRIVATE LIMITED"),
                city="Pune",
                enrichment_status="pending",
            )
        )
        current.add(
            Vendor(
                vendor_id=7331,
                name_raw="ANZEN PROJECTS PRIVATE LIMITED",
                name_norm=normalize_name("ANZEN PROJECTS PRIVATE LIMITED"),
                city="Pune",
                enrichment_status="pending",
            )
        )
        current.flush()
        upsert_tender(
            current,
            tender_id="NAME",
            title="Rooms",
            organisation="IIT",
            status="AOC",
            contract_date="01-Jan-2026",
            contract_value="INR 10",
            scraped_at=utcnow(),
        )
        upsert_tender(
            current,
            tender_id="TITLE",
            title="Works in the Apsara building",
            organisation="BARC",
            status="AOC",
            contract_date="01-Jan-2026",
            contract_value="INR 10",
            scraped_at=utcnow(),
        )
        replace_awards(
            current,
            "NAME",
            [{"bid_number": "1", "vendor_id": 733, "bidder_name": "APSARA INNOVATIONS PRIVATE LIMITED"}],
        )
        replace_awards(
            current,
            "TITLE",
            [{"bid_number": "1", "vendor_id": 7331, "bidder_name": "ANZEN PROJECTS PRIVATE LIMITED"}],
        )
    scopes = []
    for size in range(1, 4):
        scopes.extend(combinations(("name", "city", "vendor_id"), size))
    texts = ("APSARA", "Pune", "733", "7331")
    with session(bind) as current:
        catalogue = award_view(current, AwardQuery(), page_size=None).rows
        _assert_query(current, catalogue, AwardQuery(text="APSARA"))
        for text in texts:
            for scope in scopes:
                _assert_query(
                    current,
                    catalogue,
                    AwardQuery(text=text, search_in=scope),
                )
