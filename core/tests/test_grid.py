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
    record_llm_run,
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


def test_rows_carry_where_the_tender_came_from_and_sort_by_s_no(bind):
    with session(bind) as current:
        for tender_id, serial in (("LATE", 12), ("EARLY", 3), ("TYPED", None)):
            portal = serial is not None
            vendor_id = upsert_vendor(current, name_raw=f"{tender_id} Works")
            upsert_tender(
                current,
                tender_id=tender_id,
                scraped_at=utcnow(),
                origin="eprocure.gov.in" if portal else "manual",
                search_mode="awarded" if portal else None,
                listing_page=1 if portal else None,
                listing_serial=serial,
                scrape_job_id=5 if portal else None,
            )
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": f"{tender_id} Works"}],
            )
        view = award_view(current, AwardQuery(sort="listing_serial"), page_size=50)

    assert [row["tender_id"] for row in view.rows] == ["EARLY", "LATE", "TYPED"]
    early = view.rows[0]
    assert (
        early["origin"],
        early["search_mode"],
        early["listing_page"],
        early["listing_serial"],
        early["scrape_job_id"],
    ) == ("eprocure.gov.in", "awarded", 1, 3, 5)
    assert view.rows[2]["origin"] == "manual"
    assert view.rows[2]["listing_serial"] is None


def test_found_at_and_lookups_come_from_the_model_answer(bind):
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Traced Roads")
        upsert_tender(current, tender_id="T-1", scraped_at=utcnow())
        replace_awards(
            current,
            "T-1",
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Traced Roads"}],
        )
        run_id = record_llm_run(
            current,
            vendor_id=vendor_id,
            model="test",
            prompt_version="contact-v1",
            input_json="{}",
            output_json='{"email": "found@co.example"}',
            status="ok",
            job_id=3,
        )
        record_llm_run(
            current,
            vendor_id=vendor_id,
            model="test",
            prompt_version="contact-v1",
            input_json="{}",
            output_json=None,
            status="error",
            error="402",
        )
        mark_enriched(
            current,
            vendor_id,
            email="found@co.example",
            phone=None,
            source_url="https://co.example/about",
            llm_run_id=run_id,
        )
        view = award_view(current, AwardQuery(), page_size=50)

    assert view.rows[0]["found_at"] == "https://co.example/about"
    assert view.rows[0]["lookups"] == 2


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


def test_to_send_includes_a_latest_test_attempt(bind):
    from pipeline_core.models import OUTREACH_TEST

    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Tested Ltd", state="Goa")
        mark_enriched(current, vendor_id, email="hi@tested.example", phone=None)
        upsert_tender(
            current,
            tender_id="T-TEST",
            title="Work",
            status="AOC",
            scraped_at=utcnow(),
        )
        replace_awards(
            current,
            "T-TEST",
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Tested Ltd"}],
        )
        record_outreach(
            current,
            vendor_id=vendor_id,
            tender_id="T-TEST",
            email="hi@tested.example",
            subject="Hello",
            transport="gmail",
            status=OUTREACH_TEST,
            error="redirected to me@mine.example",
        )
        waiting = award_view(current, AwardQuery(outreach_status="none"), page_size=50)
        tested = award_view(current, AwardQuery(outreach_status="test"), page_size=50)
        sent = award_view(current, AwardQuery(outreach_status="sent"), page_size=50)
        listed = choices(current)["outreach_status"]

    assert [row["tender_id"] for row in waiting.rows] == ["T-TEST"]
    assert waiting.counts.sent == 0
    assert [row["tender_id"] for row in tested.rows] == ["T-TEST"]
    assert sent.rows == []
    assert OUTREACH_TEST in listed
    assert "do_not_mail" in listed


def test_to_send_is_the_same_pairs_mail_messages_would_send(bind):
    from pipeline_core.grid import filtered_award_keys
    from pipeline_core.models import OUTREACH_FAILED, OUTREACH_SENDING, OUTREACH_TEST
    from pipeline_core.queries import (
        add_contact,
        apply_mail_addresses,
        mail_messages,
        set_do_not_mail,
    )

    with session(bind) as current:
        pdf_only = upsert_vendor(current, name_raw="Pdf Only Ltd")
        both = upsert_vendor(current, name_raw="Two Address Ltd")
        buyer = upsert_vendor(current, name_raw="Buyer Mail Ltd")
        stopped = upsert_vendor(current, name_raw="Stopped Ltd")
        finished = upsert_vendor(current, name_raw="Finished Ltd")
        retry = upsert_vendor(current, name_raw="Retry Ltd")
        for vendor_id, name, tender_id in (
            (pdf_only, "Pdf Only Ltd", "T-PDF"),
            (both, "Two Address Ltd", "T-TWO"),
            (buyer, "Buyer Mail Ltd", "T-BUY"),
            (stopped, "Stopped Ltd", "T-STOP"),
            (finished, "Finished Ltd", "T-DONE"),
            (retry, "Retry Ltd", "T-RETRY"),
        ):
            upsert_tender(
                current,
                tender_id=tender_id,
                title="Work",
                status="AOC",
                scraped_at="2020-01-01T00:00:00+00:00",
            )
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": name}],
            )
        add_contact(
            current,
            pdf_only,
            channel="email",
            value="clue@winner.example",
            source="pdf",
            tender_id="T-PDF",
        )
        mark_enriched(current, both, email="model@two.example", phone=None)
        add_contact(
            current,
            both,
            channel="email",
            value="clue@two.example",
            source="pdf",
            tender_id="T-TWO",
        )
        add_contact(
            current,
            buyer,
            channel="email",
            value="rfi@ord.gov.in",
            source="model",
        )
        mark_enriched(current, stopped, email="hi@stopped.example", phone=None)
        set_do_not_mail(current, stopped, stopped=True)
        mark_enriched(current, finished, email="done@finished.example", phone=None)
        mark_enriched(current, retry, email="again@retry.example", phone=None)
        record_outreach(
            current,
            vendor_id=both,
            tender_id="T-TWO",
            email="model@two.example",
            subject="Sent",
            transport="gmail",
            status=OUTREACH_SENT,
        )
        record_outreach(
            current,
            vendor_id=finished,
            tender_id="T-DONE",
            email="done@finished.example",
            subject="Sent",
            transport="gmail",
            status=OUTREACH_SENT,
        )
        record_outreach(
            current,
            vendor_id=retry,
            tender_id="T-RETRY",
            email="again@retry.example",
            subject="Failed",
            transport="gmail",
            status=OUTREACH_FAILED,
        )
        record_outreach(
            current,
            vendor_id=pdf_only,
            tender_id="T-PDF",
            email="clue@winner.example",
            subject="Test",
            transport="gmail",
            status=OUTREACH_TEST,
            error="redirected to me@mine.example",
        )
        waiting = award_view(current, AwardQuery(outreach_status="none"), page_size=50)
        sent = award_view(current, AwardQuery(outreach_status="sent"), page_size=50)
        failed = award_view(current, AwardQuery(outreach_status="failed"), page_size=50)
        tested = award_view(current, AwardQuery(outreach_status="test"), page_size=50)
        stopped_rows = award_view(
            current, AwardQuery(outreach_status="do_not_mail"), page_size=50
        )
        apply_mail_addresses(current, waiting.rows)
        messages = mail_messages(current)
        keys = filtered_award_keys(current, AwardQuery(outreach_status="none"))

    waiting_ids = {row["tender_id"] for row in waiting.rows}
    assert waiting_ids == {"T-PDF", "T-TWO", "T-RETRY"}
    assert {row["tender_id"] for row in sent.rows} == {"T-DONE"}
    assert {row["tender_id"] for row in failed.rows} == {"T-RETRY"}
    assert {row["tender_id"] for row in tested.rows} == {"T-PDF"}
    assert {row["tender_id"] for row in stopped_rows.rows} == {"T-STOP"}
    assert "T-BUY" not in waiting_ids
    pdf_row = next(row for row in waiting.rows if row["tender_id"] == "T-PDF")
    two_row = next(row for row in waiting.rows if row["tender_id"] == "T-TWO")
    assert pdf_row["email"] == "clue@winner.example"
    assert two_row["email"] == "clue@two.example"
    assert {(row["vendor_id"], row["tender_id"]) for row in waiting.rows} == {
        (message.vendor_id, message.tender_id) for message in messages
    }
    assert set(keys) == {(message.vendor_id, message.tender_id) for message in messages}

    with session(bind) as current:
        record_outreach(
            current,
            vendor_id=pdf_only,
            tender_id="T-PDF",
            email="clue@winner.example",
            subject="Sending",
            transport="gmail",
            status=OUTREACH_SENDING,
        )
        after = {
            row["tender_id"]
            for row in award_view(current, AwardQuery(outreach_status="none"), page_size=50).rows
        }
    assert "T-PDF" not in after


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
        ({"outreach_status": "none"}, {"NEW"}),
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
        current.get(Vendor, old).email = "old@example.com"
        record_outreach(
            current,
            vendor_id=old,
            tender_id="OLD",
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
    assert view.counts.awards == len(view.rows) == len(expected)
    assert view.counts.tenders == len({tender_id for tender_id, _bid in expected})
    assert view.counts.vendors == len({row["vendor_id"] for row in view.rows})
    for status in ("pending", "not_found", "failed"):
        rows = sum(1 for row in view.rows if row["enrichment_status"] == status)
        assert getattr(view.counts, status) == rows, (status, query)


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


def test_failed_chip_counts_each_award_of_the_same_company(bind):
    with session(bind) as current:
        twice = upsert_vendor(current, name_raw="Twice Failed Ltd")
        once = upsert_vendor(current, name_raw="Once Failed Ltd")
        for tender_id, vendor_id, name, status in (
            ("A", twice, "Twice Failed Ltd", "AOC"),
            ("B", twice, "Twice Failed Ltd", "Retender"),
            ("C", once, "Once Failed Ltd", "AOC"),
        ):
            upsert_tender(
                current,
                tender_id=tender_id,
                title=name,
                organisation="Office",
                status=status,
                contract_date="01-Jan-2026",
                contract_value="INR 10",
                scraped_at=utcnow(),
            )
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": name}],
            )
        mark_failed(current, twice)
        mark_failed(current, once)
    with session(bind) as current:
        failed = award_view(current, AwardQuery(enrichment_status=("failed",)), page_size=None)
        narrowed = award_view(
            current,
            AwardQuery(enrichment_status=("failed",), tender_status="AOC"),
            page_size=None,
        )
    assert failed.counts.failed == failed.total == 3
    assert failed.counts.vendors == 2
    assert {row["tender_id"] for row in failed.rows} == {"A", "B", "C"}
    assert narrowed.counts.failed == narrowed.total == 2
    assert {row["tender_id"] for row in narrowed.rows} == {"A", "C"}


# --------------------------------------------------------------------------
# PDF evidence is per tender, runs list every tender they saved
# --------------------------------------------------------------------------


def _company_with_two_tenders(current) -> int:
    vendor_id = upsert_vendor(current, name_raw="S.K Sales Company")
    for tender_id in ("T-A", "T-B"):
        upsert_tender(current, tender_id=tender_id, title=tender_id, scraped_at=utcnow())
        replace_awards(
            current,
            tender_id,
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "S.K Sales Company"}],
        )
    return vendor_id


def test_a_pdf_email_shows_only_on_the_tender_whose_files_held_it(bind):
    from pipeline_core.queries import record_pdf_contacts, set_pdf_contacts

    with session(bind) as current:
        vendor_id = _company_with_two_tenders(current)
        record_pdf_contacts(current, vendor_id, "T-A", [], ["6877812012"])
        record_pdf_contacts(
            current, vendor_id, "T-B", ["loa@sk.example", "second@sk.example"], []
        )
        # The company-wide column must never be shown as a tender's evidence.
        set_pdf_contacts(current, vendor_id, email="loa@sk.example", phone=None)
        rows = {row["tender_id"]: row for row in award_view(current, AwardQuery()).rows}
        both = award_view(current, AwardQuery(pdf_contact="yes")).rows

    assert rows["T-A"]["pdf_email"] is None
    assert rows["T-A"]["pdf_phone"] == "6877812012"
    assert rows["T-B"]["pdf_email"] == "loa@sk.example; second@sk.example"
    assert rows["T-B"]["pdf_phone"] is None
    assert both == []


def test_pdf_contact_filter_needs_both_clues_on_the_same_tender(bind):
    from pipeline_core.queries import record_pdf_contacts

    with session(bind) as current:
        vendor_id = _company_with_two_tenders(current)
        record_pdf_contacts(current, vendor_id, "T-A", ["wo@sk.example"], ["6877812012"])
        record_pdf_contacts(current, vendor_id, "T-B", ["loa@sk.example"], [])
        shown = [row["tender_id"] for row in award_view(current, AwardQuery(pdf_contact="yes")).rows]

    assert shown == ["T-A"]


def test_a_run_lists_every_tender_it_saved_not_only_the_first_scrape(bind):
    from pipeline_core.queries import record_job_tender

    with session(bind) as current:
        _company_with_two_tenders(current)
        upsert_tender(current, tender_id="T-A", scraped_at=utcnow(), scrape_job_id=9)
        upsert_tender(current, tender_id="T-B", scraped_at=utcnow(), scrape_job_id=9)
        # Job 12 refreshed T-B, job 40 downloaded T-A's PDFs.
        upsert_tender(current, tender_id="T-B", scraped_at=utcnow(), scrape_job_id=12)
        record_job_tender(current, 12, "T-B")
        record_job_tender(current, 40, "T-A")

        def shown(job_id):
            return [row["tender_id"] for row in award_view(current, AwardQuery(scrape_job_id=job_id)).rows]

        assert shown(9) == ["T-A", "T-B"]
        assert shown(12) == ["T-B"]
        assert shown(40) == ["T-A"]
        assert current.get(Tender, "T-B").scrape_job_id == 9


def test_to_send_and_mail_messages_agree_on_edge_addresses(bind):
    """The SQL filter and the Python mail list must name the same awards."""
    from pipeline_core.grid import filtered_award_keys
    from pipeline_core.models import OUTREACH_SENDING, OUTREACH_SENT, VendorContact
    from pipeline_core.queries import mail_messages, set_do_not_mail

    cases = {
        "Pdf Elsewhere Ltd": [("pdf", "OTHER", "loa@elsewhere.example", 1)],
        "Buyer Only Ltd": [("model", None, "info@thsti.res.in", 1)],
        "Invalid Only Ltd": [("pdf", "SELF", "not-an-email", 0)],
        "Sent Already Ltd": [("model", None, "done@sent.example", 1)],
        "Sending Ltd": [("model", None, "busy@sending.example", 1)],
        "Twin Ltd": [("model", None, "twin@twin.example", 1), ("typed", None, "TWIN@twin.example", 1)],
        "Stopped Ltd": [("model", None, "stop@stopped.example", 1)],
        "Column Only Ltd": [],
        "Nothing Ltd": [],
    }
    with session(bind) as current:
        ids = {}
        for name, rows in cases.items():
            vendor_id = upsert_vendor(current, name_raw=name)
            ids[name] = vendor_id
            for tender_id in (f"{vendor_id}-SELF", f"{vendor_id}-OTHER"):
                upsert_tender(current, tender_id=tender_id, title=name, scraped_at=utcnow())
                replace_awards(
                    current,
                    tender_id,
                    [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": name}],
                )
            for source, tender, value, valid in rows:
                current.add(
                    VendorContact(
                        vendor_id=vendor_id,
                        tender_id=f"{vendor_id}-{tender}" if tender else None,
                        channel="email",
                        value=value,
                        source=source,
                        valid=valid,
                    )
                )
        current.get(Vendor, ids["Column Only Ltd"]).email = "column@only.example"
        set_do_not_mail(current, ids["Stopped Ltd"], stopped=True)
        for name, status, email in (
            ("Sent Already Ltd", OUTREACH_SENT, "done@sent.example"),
            ("Sending Ltd", OUTREACH_SENDING, "busy@sending.example"),
        ):
            record_outreach(
                current,
                vendor_id=ids[name],
                tender_id=f"{ids[name]}-SELF",
                email=email,
                subject="s",
                transport="gmail",
                status=status,
            )

        to_send = set(filtered_award_keys(current, AwardQuery(outreach_status="none")))
        mailed = {(m.vendor_id, m.tender_id) for m in mail_messages(current)}

    assert to_send == mailed
    assert (ids["Pdf Elsewhere Ltd"], f"{ids['Pdf Elsewhere Ltd']}-SELF") in mailed
    assert (ids["Sent Already Ltd"], f"{ids['Sent Already Ltd']}-OTHER") in mailed
    assert (ids["Sent Already Ltd"], f"{ids['Sent Already Ltd']}-SELF") not in mailed
    assert (ids["Column Only Ltd"], f"{ids['Column Only Ltd']}-SELF") in mailed
    assert not any(vendor_id == ids["Buyer Only Ltd"] for vendor_id, _ in mailed)
    assert not any(vendor_id == ids["Invalid Only Ltd"] for vendor_id, _ in mailed)
    assert not any(vendor_id == ids["Stopped Ltd"] for vendor_id, _ in mailed)
    assert not any(vendor_id == ids["Nothing Ltd"] for vendor_id, _ in mailed)


def test_a_buyer_address_stored_as_valid_does_not_make_a_company_mailable(bind):
    """A PDF row saved before a buyer marker existed still says valid=1.

    The mail queue checks the address again and skips it, so the grid's
    mailable filter and count must too, or the header promises mail that
    never goes out.
    """
    from pipeline_core.models import CHANNEL_EMAIL, VendorContact
    from pipeline_core.queries import contact_breakdown, mail_messages

    with session(bind) as current:
        buyer_only = upsert_vendor(current, name_raw="Buyer Only Works")
        reachable = upsert_vendor(current, name_raw="Reachable Works")
        for vendor_id, tender_id in ((buyer_only, "T-BUYER"), (reachable, "T-OK")):
            upsert_tender(current, tender_id=tender_id, title="Work", scraped_at=utcnow())
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "x"}],
            )
        current.add_all(
            [
                VendorContact(vendor_id=buyer_only, tender_id="T-BUYER", channel=CHANNEL_EMAIL,
                              value="stores@thsti.qov.in", source="pdf", valid=1),
                VendorContact(vendor_id=reachable, tender_id="T-OK", channel=CHANNEL_EMAIL,
                              value="office@reachable.example", source="pdf", valid=1),
            ]
        )

    with session(bind) as current:
        mailable = award_view(current, AwardQuery(mailable="yes"), page_size=None)
        unmailable = award_view(current, AwardQuery(mailable="no"), page_size=None)
        assert [row["tender_id"] for row in mailable.rows] == ["T-OK"]
        assert [row["tender_id"] for row in unmailable.rows] == ["T-BUYER"]
        assert award_view(current, AwardQuery(), page_size=1).counts.mailable == 1
        assert contact_breakdown(current)["mailable"] == 1
        assert {m.vendor_id for m in mail_messages(current)} == {reachable}
