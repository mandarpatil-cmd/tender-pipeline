"""Every address is one row. First, all, and chosen follow PDF, then enrichment, then typed."""

from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.models import Vendor, VendorContact
from pipeline_core.queries import (
    ADDRESS_ALL,
    ADDRESS_CHOSEN,
    ADDRESS_FIRST,
    backfill_vendor_contacts,
    choose_addresses,
    mark_enriched,
    record_pdf_contacts,
    replace_awards,
    set_typed_contact,
    upsert_tender,
    upsert_vendor,
    utcnow,
)


def _company(bind) -> tuple[int, str]:
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Roads Ltd")
        upsert_tender(current, tender_id="T-1", title="Road", scraped_at=utcnow())
        replace_awards(
            current,
            "T-1",
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Roads Ltd"}],
        )
    return vendor_id, "T-1"


def test_pdf_enrichment_and_typed_stay_three_rows(tmp_path):
    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    vendor_id, tender_id = _company(bind)
    with session(bind) as current:
        record_pdf_contacts(
            current,
            vendor_id,
            tender_id,
            ["pdf@roads.example", "not-an-email"],
            ["9811111111", "12"],
        )
        mark_enriched(current, vendor_id, email="model@roads.example", phone="9822222222")
        set_typed_contact(current, vendor_id, email="typed@roads.example")
        rows = (
            current.query(VendorContact)
            .filter(VendorContact.channel == "email")
            .order_by(VendorContact.contact_id)
            .all()
        )

    assert [row.value for row in rows] == [
        "pdf@roads.example",
        "not-an-email",
        "model@roads.example",
        "typed@roads.example",
    ]
    assert [row.source for row in rows] == ["pdf", "pdf", "model", "typed"]
    assert [bool(row.valid) for row in rows] == [True, False, True, True]
    with session(bind) as current:
        assert choose_addresses(current, vendor_id, tender_id, mode=ADDRESS_FIRST, chosen_ids=set()) == [
            "pdf@roads.example"
        ]
        assert choose_addresses(current, vendor_id, tender_id, mode=ADDRESS_ALL, chosen_ids=set()) == [
            "pdf@roads.example",
            "model@roads.example",
            "typed@roads.example",
        ]
        typed = current.query(VendorContact).filter(VendorContact.source == "typed").one()
        assert choose_addresses(
            current,
            vendor_id,
            tender_id,
            mode=ADDRESS_CHOSEN,
            chosen_ids={typed.contact_id},
        ) == ["typed@roads.example"]


def test_a_second_pdf_appends_and_enrichment_replaces(tmp_path):
    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    vendor_id, tender_id = _company(bind)
    with session(bind) as current:
        record_pdf_contacts(current, vendor_id, tender_id, ["first@roads.example"], [])
        record_pdf_contacts(current, vendor_id, tender_id, ["first@roads.example", "second@roads.example"], [])
        mark_enriched(current, vendor_id, email="old@roads.example", phone=None)
        mark_enriched(current, vendor_id, email="new@roads.example", phone=None)
        emails = [
            row.value
            for row in current.query(VendorContact)
            .filter(VendorContact.channel == "email")
            .order_by(VendorContact.contact_id)
        ]

    assert emails == ["first@roads.example", "second@roads.example", "new@roads.example"]


def test_backfill_copies_document_lists_and_the_company_address(tmp_path):
    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    vendor_id, tender_id = _company(bind)
    with session(bind) as current:
        from pipeline_core.queries import replace_tender_documents

        replace_tender_documents(
            current,
            tender_id,
            [{"filename": "wo.pdf", "emails": ["a@roads.example", "b@roads.example"], "phones": ["9811111111"]}],
        )
        vendor = current.get(Vendor, vendor_id)
        vendor.email = "typed-direct@roads.example"
        vendor.contact_origin = "typed"
        written = backfill_vendor_contacts(current)
        again = backfill_vendor_contacts(current)

    assert written == 4
    assert again == 0
    with session(bind) as current:
        values = [row.value for row in current.query(VendorContact).order_by(VendorContact.contact_id)]
    assert values == [
        "a@roads.example",
        "b@roads.example",
        "9811111111",
        "typed-direct@roads.example",
    ]
