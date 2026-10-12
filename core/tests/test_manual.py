"""A typed tender attaches a known company and does not touch its contact."""

from __future__ import annotations

import pytest

from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.emailcheck import is_email
from pipeline_core.manual import NeedsAttach, WinnerInput, save_manual_tender
from pipeline_core.models import (
    CONTACT_MODEL,
    CONTACT_TYPED,
    ORIGIN_MANUAL,
    SOURCE_MANUAL,
    STATUS_DONE,
    Tender,
    Vendor,
)
from pipeline_core.queries import (
    ContactError,
    mark_enriched,
    set_typed_contact,
    upsert_tender,
    upsert_vendor,
    utcnow,
)


@pytest.fixture
def bind(tmp_path):
    eng = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(eng)
    return eng


def test_a_new_company_is_manual_and_has_no_contact(bind):
    with session(bind) as current:
        tender_id = save_manual_tender(
            current,
            tender_id="",
            title="Hand job",
            organisation="Works dept",
            status="AOC",
            contract_date="21-Sep-2026",
            contract_value="100",
            winners=[WinnerInput(name="Fresh Electricals", bid_number="1")],
            attach=False,
        )
        vendor = current.query(Vendor).one()

    assert tender_id.startswith("MANUAL-")
    assert vendor.source == SOURCE_MANUAL
    assert vendor.email is None
    assert vendor.enrichment_status == "pending"


def test_a_typed_tender_is_manual_with_no_portal_search(bind):
    """Typing over a scraped id makes the row the person's, so the portal search goes."""
    with session(bind) as current:
        upsert_tender(
            current,
            tender_id="2026_X_1",
            scraped_at=utcnow(),
            origin="eprocure.gov.in",
            search_mode="awarded",
            search_fields='{"tenderStatus": "6"}',
            listing_page=2,
            listing_serial=14,
            scrape_job_id=3,
        )
        fresh = save_manual_tender(
            current,
            tender_id="",
            title="New hand job",
            organisation="Works dept",
            status="AOC",
            contract_date="",
            contract_value="",
            winners=[WinnerInput(name="Fresh Electricals", bid_number="1")],
            attach=False,
        )
        save_manual_tender(
            current,
            tender_id="2026_X_1",
            title="Typed over",
            organisation="Works dept",
            status="AOC",
            contract_date="",
            contract_value="",
            winners=[WinnerInput(name="Other Electricals", bid_number="1")],
            attach=False,
        )
        rows = [current.get(Tender, fresh), current.get(Tender, "2026_X_1")]

        for tender in rows:
            assert tender.origin == ORIGIN_MANUAL
            assert (
                tender.search_mode,
                tender.search_fields,
                tender.listing_page,
                tender.listing_serial,
                tender.scrape_job_id,
            ) == (None, None, None, None, None)


def test_a_known_name_asks_before_attaching_and_keeps_the_email(bind):
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Acme Pvt Ltd")
        mark_enriched(current, vendor_id, email="keep@acme.example", phone=None)
        winner = WinnerInput(name="ACME PVT. LTD.", bid_number="1")
        with pytest.raises(NeedsAttach):
            save_manual_tender(
                current,
                tender_id="MANUAL-KEEP",
                title="Second job",
                organisation="",
                status="",
                contract_date="",
                contract_value="",
                winners=[winner],
                attach=False,
            )
        save_manual_tender(
            current,
            tender_id="MANUAL-KEEP",
            title="Second job",
            organisation="",
            status="",
            contract_date="",
            contract_value="",
            winners=[winner],
            attach=True,
        )
        vendor = current.get(Vendor, vendor_id)

    assert vendor.email == "keep@acme.example"
    assert vendor.contact_origin == CONTACT_MODEL
    assert current_vendor_count(bind) == 1


def current_vendor_count(bind) -> int:
    with session(bind) as current:
        return current.query(Vendor).count()


def test_typed_phone_keeps_the_email_and_marks_typed(bind):
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Phone Later Ltd")
        set_typed_contact(current, vendor_id, email="a@b.example", phone=None)
        set_typed_contact(current, vendor_id, email="", phone="9876543210")
        vendor = current.get(Vendor, vendor_id)

    assert vendor.email == "a@b.example"
    assert vendor.phone == "9876543210"
    assert vendor.enrichment_status == STATUS_DONE
    assert vendor.contact_origin == CONTACT_TYPED


def test_bad_email_does_not_save(bind):
    assert is_email("a@b.example")
    assert not is_email("not-an-email")
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Bad Mail Ltd")
        with pytest.raises(ContactError):
            set_typed_contact(current, vendor_id, email="not-an-email", phone=None)
        vendor = current.get(Vendor, vendor_id)
    assert vendor.email is None
    assert vendor.enrichment_status == "pending"
