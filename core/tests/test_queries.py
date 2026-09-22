"""The work queues. These are the contracts stages 2 and 3 run on."""

from __future__ import annotations

import pytest

from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.models import (
    OUTREACH_FAILED,
    OUTREACH_SENT,
    SOURCE_BIDEASY,
    SOURCE_SCRAPE,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NOT_FOUND,
    STATUS_PENDING,
    Award,
    Tender,
    Vendor,
)
from pipeline_core.queries import (
    known_tender_ids,
    mark_enriched,
    mark_failed,
    outreach_targets,
    pending_vendors,
    record_llm_run,
    record_outreach,
    replace_awards,
    reset_failed,
    set_pdf_contacts,
    upsert_tender,
    upsert_vendor,
    utcnow,
    vendor_count,
)


@pytest.fixture
def bind(tmp_path):
    eng = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(eng)
    return eng


# --------------------------------------------------------------------------
# Stage 1
# --------------------------------------------------------------------------


def test_upsert_vendor_dedupes_on_normalised_name(bind):
    with session(bind) as s:
        first = upsert_vendor(s, name_raw="M/S Kanta Enterprises", city="Nagpur")
        second = upsert_vendor(s, name_raw="Kanta  Enterprises.", city="")

    assert first == second
    with session(bind) as s:
        assert vendor_count(s) == 1


def test_upsert_vendor_never_touches_the_contact_block(bind):
    """Stage 1 re-scraping a tender must not wipe what stage 2 found."""
    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Kanta Enterprises", city="Nagpur")
        mark_enriched(s, vendor_id, email="hi@kanta.example", phone="+91 1")

    with session(bind) as s:
        upsert_vendor(s, name_raw="Kanta Enterprises", city="Pune", buyer_hint="VNIT")

    with session(bind) as s:
        vendor = s.get(Vendor, vendor_id)
        assert vendor.email == "hi@kanta.example"
        assert vendor.enrichment_status == STATUS_DONE
        assert vendor.buyer_hint == "VNIT"  # identity columns do refresh
        assert vendor.city == "Nagpur"  # a non-empty city is not overwritten


def test_upsert_vendor_keeps_the_original_source(bind):
    with session(bind) as s:
        upsert_vendor(s, name_raw="Acme Ltd", source=SOURCE_BIDEASY)
    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Acme Ltd", source=SOURCE_SCRAPE)
    with session(bind) as s:
        assert s.get(Vendor, vendor_id).source == SOURCE_BIDEASY


def test_known_tender_ids_is_the_skip_list(bind):
    with session(bind) as s:
        upsert_tender(s, tender_id="2026_A_1", title="A", scraped_at=utcnow())
        upsert_tender(s, tender_id="2026_B_1", title="B", scraped_at=utcnow())

    with session(bind) as s:
        assert known_tender_ids(s) == {"2026_A_1", "2026_B_1"}


def test_upsert_tender_replaces_rather_than_duplicates(bind):
    with session(bind) as s:
        upsert_tender(s, tender_id="2026_A_1", title="first", scraped_at=utcnow())
    with session(bind) as s:
        upsert_tender(s, tender_id="2026_A_1", title="second", scraped_at=utcnow())
    with session(bind) as s:
        assert s.query(Tender).count() == 1
        assert s.query(Tender).one().title == "second"


def test_replace_awards_rewrites_one_tender_only(bind):
    with session(bind) as s:
        upsert_tender(s, tender_id="2026_A_1", scraped_at=utcnow())
        upsert_tender(s, tender_id="2026_B_1", scraped_at=utcnow())
        vendor_id = upsert_vendor(s, name_raw="Kanta Enterprises")
        replace_awards(
            s,
            "2026_A_1",
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Kanta"}],
        )
        replace_awards(
            s,
            "2026_B_1",
            [{"bid_number": "2", "vendor_id": vendor_id, "bidder_name": "Kanta"}],
        )

    with session(bind) as s:
        replace_awards(
            s,
            "2026_A_1",
            [{"bid_number": "9", "vendor_id": vendor_id, "bidder_name": "Kanta"}],
        )

    with session(bind) as s:
        rows = {(a.tender_id, a.bid_number) for a in s.query(Award).all()}
        assert rows == {("2026_A_1", "9"), ("2026_B_1", "2")}


# --------------------------------------------------------------------------
# Stage 2
# --------------------------------------------------------------------------


def test_pending_vendors_carries_the_award_title_as_context(bind):
    with session(bind) as s:
        upsert_tender(s, tender_id="2026_A_1", scraped_at=utcnow())
        vendor_id = upsert_vendor(s, name_raw="Kanta Enterprises", city="Nagpur")
        replace_awards(
            s,
            "2026_A_1",
            [
                {
                    "bid_number": "1",
                    "vendor_id": vendor_id,
                    "bidder_name": "Kanta",
                    "work_title": "Supply of ceiling fans",
                }
            ],
        )

    with session(bind) as s:
        queue = pending_vendors(s)

    assert len(queue) == 1
    assert queue[0].name_raw == "Kanta Enterprises"
    assert queue[0].title == "Supply of ceiling fans"


def test_pending_vendors_without_awards_still_queue(bind):
    """BidEasy imports have no awards — they must not vanish from the queue."""
    with session(bind) as s:
        upsert_vendor(s, name_raw="Acme Ltd", source=SOURCE_BIDEASY)

    with session(bind) as s:
        queue = pending_vendors(s)

    assert [v.name_raw for v in queue] == ["Acme Ltd"]
    assert queue[0].title == ""


def test_pending_vendors_respects_limit_and_order(bind):
    with session(bind) as s:
        for name in ("A Ltd", "B Ltd", "C Ltd"):
            upsert_vendor(s, name_raw=name)

    with session(bind) as s:
        assert [v.name_raw for v in pending_vendors(s, limit=2)] == ["A Ltd", "B Ltd"]


def test_enrichment_lifecycle_and_resumability(bind):
    with session(bind) as s:
        found = upsert_vendor(s, name_raw="Has Contact Ltd")
        empty = upsert_vendor(s, name_raw="No Contact Ltd")
        broken = upsert_vendor(s, name_raw="Errored Ltd")

    with session(bind) as s:
        assert mark_enriched(s, found, email="a@b.example", phone=None) == STATUS_DONE
        assert mark_enriched(s, empty, email=None, phone=None) == STATUS_NOT_FOUND
        assert mark_failed(s, broken) == STATUS_FAILED

    # A second run finds nothing pending — this is what makes stage 2 resumable.
    with session(bind) as s:
        assert pending_vendors(s) == []

    with session(bind) as s:
        assert reset_failed(s) == 1
    with session(bind) as s:
        assert [v.vendor_id for v in pending_vendors(s)] == [broken]


def test_mark_enriched_blank_strings_count_as_not_found(bind):
    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Blank Ltd")
    with session(bind) as s:
        assert mark_enriched(s, vendor_id, email="   ", phone="") == STATUS_NOT_FOUND
    with session(bind) as s:
        assert s.get(Vendor, vendor_id).email is None


def test_record_llm_run_audits_success_and_failure(bind):
    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Kanta Enterprises")

    with session(bind) as s:
        ok = record_llm_run(
            s,
            vendor_id=vendor_id,
            model="google/gemini-2.5-flash",
            prompt_version="contact-v1",
            input_json='{"company": "Kanta"}',
            output_json='{"email": "a@b.example"}',
            status="ok",
        )
        bad = record_llm_run(
            s,
            vendor_id=vendor_id,
            model="google/gemini-2.5-flash",
            prompt_version="contact-v1",
            input_json="{}",
            output_json=None,
            status="error",
            error="429 rate limited",
        )

    assert ok >= 1 and bad > ok


# --------------------------------------------------------------------------
# Stage 3
# --------------------------------------------------------------------------


def test_outreach_targets_needs_an_address(bind):
    with session(bind) as s:
        with_email = upsert_vendor(s, name_raw="Reachable Ltd")
        mark_enriched(s, with_email, email="hi@reachable.example", phone=None)
        blank = upsert_vendor(s, name_raw="Blank Ltd")
        mark_enriched(s, blank, email=None, phone="+91 1")

    with session(bind) as s:
        assert [t.vendor_id for t in outreach_targets(s)] == [with_email]


def test_recording_a_send_removes_that_vendor_from_the_queue(bind):
    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Reachable Ltd")
        mark_enriched(s, vendor_id, email="hi@reachable.example", phone=None)

    with session(bind) as s:
        assert len(outreach_targets(s)) == 1
        record_outreach(
            s,
            vendor_id=vendor_id,
            email="hi@reachable.example",
            subject="Enquiry",
            transport="gmail",
            status=OUTREACH_SENT,
        )

    with session(bind) as s:
        assert outreach_targets(s) == []


def test_a_failed_send_stays_in_the_queue_for_the_next_run(bind):
    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Reachable Ltd")
        mark_enriched(s, vendor_id, email="hi@reachable.example", phone=None)
        record_outreach(
            s,
            vendor_id=vendor_id,
            email="hi@reachable.example",
            subject="Enquiry",
            transport="gmail",
            status=OUTREACH_FAILED,
            error="550 mailbox unavailable",
        )

    with session(bind) as s:
        assert [t.vendor_id for t in outreach_targets(s)] == [vendor_id]


def test_outreach_target_carries_what_the_mail_needs(bind):
    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Kanta Enterprises")
        mark_enriched(s, vendor_id, email="hi@kanta.example", phone=None)

    with session(bind) as s:
        target = outreach_targets(s)[0]

    assert target.company == "Kanta Enterprises"
    assert target.email == "hi@kanta.example"


# --------------------------------------------------------------------------
# PDF evidence (stage 1 writes, stage 2 reads)
# --------------------------------------------------------------------------


def test_pdf_contacts_reach_the_stage_2_queue(bind):
    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Sunrise Builders")
        assert set_pdf_contacts(
            s, vendor_id, email="sunrisebuilders@example.com", phone="9123456780"
        )

    with session(bind) as s:
        queued = pending_vendors(s)[0]

    assert queued.pdf_email == "sunrisebuilders@example.com"
    assert queued.pdf_phone == "9123456780"
    assert queued.has_evidence


def test_pdf_contacts_never_overwrite_what_is_already_there(bind):
    """A re-scrape must not churn the evidence, nor let tender 2 clobber tender 1."""
    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Sunrise Builders")
        set_pdf_contacts(s, vendor_id, email="first@x.example", phone=None)

    with session(bind) as s:
        wrote = set_pdf_contacts(s, vendor_id, email="second@x.example", phone="999")

    assert wrote is True  # the phone was blank, so something was written
    with session(bind) as s:
        vendor = s.get(Vendor, vendor_id)
        assert vendor.pdf_email == "first@x.example"
        assert vendor.pdf_phone == "999"


def test_pdf_contacts_are_separate_from_the_researched_contact(bind):
    """Evidence is not an answer: it must not satisfy stage 3's queue on its own."""
    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Sunrise Builders")
        set_pdf_contacts(s, vendor_id, email="sunrisebuilders@example.com", phone=None)

    with session(bind) as s:
        assert outreach_targets(s) == []  # vendors.email is still null
        assert len(pending_vendors(s)) == 1  # still work for stage 2


def test_set_pdf_contacts_ignores_blanks(bind):
    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Blank Ltd")
        assert set_pdf_contacts(s, vendor_id, email="  ", phone=None) is False
    with session(bind) as s:
        assert s.get(Vendor, vendor_id).pdf_email is None


def test_reset_not_found_requeues_only_unanswered_rows(bind):
    """For when the method changed -- web search, a new model, better evidence."""
    from pipeline_core.queries import reset_not_found

    with session(bind) as s:
        empty = upsert_vendor(s, name_raw="Obscure Traders")
        found = upsert_vendor(s, name_raw="Has Contact Ltd")
        mark_enriched(s, empty, email=None, phone=None)
        mark_enriched(s, found, email="a@b.example", phone=None)

    with session(bind) as s:
        assert reset_not_found(s) == 1

    with session(bind) as s:
        assert [v.vendor_id for v in pending_vendors(s)] == [empty]
        assert s.get(Vendor, found).enrichment_status == STATUS_DONE


# --------------------------------------------------------------------------
# Counting the queues — what `pipeline-db status` reports
# --------------------------------------------------------------------------


def test_enrichment_breakdown_can_narrow_to_one_source(bind):
    """The total queue and the runnable queue are different numbers.

    Stage 2 runs with an ONLY_SOURCE set, so reading the total as the runnable
    count means watching a run do nothing.
    """
    from pipeline_core.queries import enrichment_breakdown

    with session(bind) as s:
        upsert_vendor(s, name_raw="Scraped Traders", source=SOURCE_SCRAPE)
        upsert_vendor(s, name_raw="Imported Works", source=SOURCE_BIDEASY)
        upsert_vendor(s, name_raw="Imported Two", source=SOURCE_BIDEASY)

    with session(bind) as s:
        assert enrichment_breakdown(s) == {STATUS_PENDING: 3}
        assert enrichment_breakdown(s, source=SOURCE_SCRAPE) == {STATUS_PENDING: 1}
        assert enrichment_breakdown(s, source=SOURCE_BIDEASY) == {STATUS_PENDING: 2}


def test_enrichment_by_source_splits_the_whole_queue(bind):
    from pipeline_core.queries import enrichment_by_source

    with session(bind) as s:
        scraped = upsert_vendor(s, name_raw="Scraped Traders", source=SOURCE_SCRAPE)
        upsert_vendor(s, name_raw="Imported Works", source=SOURCE_BIDEASY)
        mark_enriched(s, scraped, email="a@b.example", phone=None)

    with session(bind) as s:
        assert enrichment_by_source(s) == {
            SOURCE_SCRAPE: {STATUS_DONE: 1},
            SOURCE_BIDEASY: {STATUS_PENDING: 1},
        }


def test_contact_breakdown_separates_mailable_from_phone_only(bind):
    """`done` overstates the send queue by exactly the phone-only count."""
    from pipeline_core.queries import contact_breakdown, enrichment_breakdown

    with session(bind) as s:
        emailed = upsert_vendor(s, name_raw="Has Email Ltd")
        phoned = upsert_vendor(s, name_raw="Phone Only Traders")
        upsert_vendor(s, name_raw="Nothing Found Works")
        mark_enriched(s, emailed, email="a@b.example", phone=None)
        mark_enriched(s, phoned, email=None, phone="+919876543210")

    with session(bind) as s:
        # Both count as 'done', but only one can ever be mailed.
        assert enrichment_breakdown(s)[STATUS_DONE] == 2
        assert contact_breakdown(s) == {
            "mailable": 1,
            "phone_only": 1,
            "no_contact": 1,
        }


def test_outreach_queue_counts_agree_with_outreach_targets(bind):
    """`remaining` must be the send list itself, not a lookalike query."""
    from pipeline_core.queries import outreach_queue_counts

    with session(bind) as s:
        first = upsert_vendor(s, name_raw="Alpha Ltd")
        second = upsert_vendor(s, name_raw="Beta Traders")
        mark_enriched(s, first, email="a@b.example", phone=None)
        mark_enriched(s, second, email="c@d.example", phone=None)
        record_outreach(
            s,
            vendor_id=first,
            email="a@b.example",
            subject="hi",
            transport="gmail",
            status=OUTREACH_SENT,
        )

    with session(bind) as s:
        counts = outreach_queue_counts(s)
        assert counts["mailable"] == 2
        assert counts["already_sent"] == 1
        assert counts["remaining"] == 1
        assert counts["remaining"] == len(outreach_targets(s))


def test_outreach_queue_counts_failed_attempts_stay_in_the_queue(bind):
    """A failed send leaves the vendor queued -- today there is no attempt cap."""
    from pipeline_core.queries import outreach_queue_counts

    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Bounces Ltd")
        mark_enriched(s, vendor_id, email="bad@nowhere.example", phone=None)
        record_outreach(
            s,
            vendor_id=vendor_id,
            email="bad@nowhere.example",
            subject="hi",
            transport="gmail",
            status=OUTREACH_FAILED,
            error="550 no such mailbox",
        )

    with session(bind) as s:
        counts = outreach_queue_counts(s)
        assert counts["failed_attempts"] == 1
        assert counts["already_sent"] == 0
        assert counts["remaining"] == 1


def test_pipeline_funnel_reports_every_stage(bind):
    from pipeline_core.queries import pipeline_funnel

    with session(bind) as s:
        upsert_tender(s, tender_id="T-1", scraped_at=utcnow())
        vendor_id = upsert_vendor(s, name_raw="Alpha Ltd", source=SOURCE_SCRAPE)
        replace_awards(
            s, "T-1", [{"vendor_id": vendor_id, "bidder_name": "Alpha Ltd"}]
        )
        phoned = upsert_vendor(s, name_raw="Phone Only Traders")
        mark_enriched(s, vendor_id, email="a@b.example", phone=None)
        mark_enriched(s, phoned, email=None, phone="+919876543210")

    with session(bind) as s:
        funnel = {label: count for label, count, _ in pipeline_funnel(s)}

    assert funnel["tenders"] == 1
    assert funnel["awards"] == 1
    assert funnel["vendors"] == 2
    assert funnel["enriched"] == 2  # both answered
    assert funnel["mailable"] == 1  # but only one has an address
    assert funnel["sent"] == 0


# --------------------------------------------------------------------------
# Pruning one source
# --------------------------------------------------------------------------


def test_delete_vendors_by_source_removes_only_that_source(bind):
    from pipeline_core.queries import delete_vendors_by_source

    with session(bind) as s:
        kept = upsert_vendor(s, name_raw="Scraped Traders", source=SOURCE_SCRAPE)
        upsert_vendor(s, name_raw="Imported Works", source=SOURCE_BIDEASY)
        upsert_vendor(s, name_raw="Imported Two", source=SOURCE_BIDEASY)

    with session(bind) as s:
        assert delete_vendors_by_source(s, SOURCE_BIDEASY) == 2

    with session(bind) as s:
        assert [v.vendor_id for v in s.query(Vendor).all()] == [kept]


def test_delete_vendors_by_source_refuses_when_awards_reference_them(bind):
    """A clear refusal beats a half-applied delete and an IntegrityError."""
    from pipeline_core.queries import delete_vendors_by_source

    with session(bind) as s:
        upsert_tender(s, tender_id="T-1", scraped_at=utcnow())
        vendor_id = upsert_vendor(s, name_raw="Imported Works", source=SOURCE_BIDEASY)
        replace_awards(
            s, "T-1", [{"vendor_id": vendor_id, "bidder_name": "Imported Works"}]
        )

    with session(bind) as s:
        with pytest.raises(ValueError, match="awards"):
            delete_vendors_by_source(s, SOURCE_BIDEASY)

    with session(bind) as s:
        assert vendor_count(s) == 1  # nothing was deleted


def test_delete_vendors_by_source_refuses_when_llm_runs_reference_them(bind):
    """Deleting a vendor would discard the audit of what was already paid for."""
    from pipeline_core.queries import delete_vendors_by_source

    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Imported Works", source=SOURCE_BIDEASY)
        record_llm_run(
            s,
            vendor_id=vendor_id,
            model="test/model",
            prompt_version="contact-v1",
            input_json="{}",
            output_json="{}",
            status="ok",
        )

    with session(bind) as s:
        with pytest.raises(ValueError, match="llm_runs"):
            delete_vendors_by_source(s, SOURCE_BIDEASY)


def test_delete_vendors_by_source_refuses_when_already_mailed(bind):
    from pipeline_core.queries import delete_vendors_by_source

    with session(bind) as s:
        vendor_id = upsert_vendor(s, name_raw="Imported Works", source=SOURCE_BIDEASY)
        mark_enriched(s, vendor_id, email="a@b.example", phone=None)
        record_outreach(
            s,
            vendor_id=vendor_id,
            email="a@b.example",
            subject="hi",
            transport="gmail",
            status=OUTREACH_SENT,
        )

    with session(bind) as s:
        with pytest.raises(ValueError, match="outreach"):
            delete_vendors_by_source(s, SOURCE_BIDEASY)


def test_last_scraped_at_reports_the_newest_tender(bind):
    """Stage 1 is the one stage whose backlog lives outside the database."""
    from pipeline_core.queries import last_scraped_at

    with session(bind) as s:
        assert last_scraped_at(s) is None  # nothing scraped yet

    with session(bind) as s:
        upsert_tender(s, tender_id="T-1", scraped_at="2026-01-01T00:00:00+00:00")
        upsert_tender(s, tender_id="T-2", scraped_at="2026-06-01T00:00:00+00:00")

    with session(bind) as s:
        assert last_scraped_at(s) == "2026-06-01T00:00:00+00:00"
