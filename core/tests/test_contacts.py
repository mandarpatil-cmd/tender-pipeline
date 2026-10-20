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


# --------------------------------------------------------------------------
# One company, two tenders: whose PDF said what, and what a letter uses
# --------------------------------------------------------------------------


def _two_tenders(bind) -> int:
    """S.K Sales won tender A, whose PDFs held no email, and tender B, whose LOA did."""
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="S.K Sales Company")
        for tender_id, scraped in (
            ("T-A", "2024-10-01T00:00:00+00:00"),
            ("T-B", "2026-10-10T00:00:00+00:00"),
        ):
            upsert_tender(current, tender_id=tender_id, title=tender_id, scraped_at=scraped)
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "S.K Sales Company"}],
            )
    return vendor_id


def test_a_letter_uses_this_tenders_pdf_then_other_tenders_then_model_then_typed(tmp_path):
    from pipeline_core.queries import ordered_contacts

    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    vendor_id = _two_tenders(bind)
    with session(bind) as current:
        set_typed_contact(current, vendor_id, email="typed@sk.example")
        mark_enriched(current, vendor_id, email="model@sk.example", phone=None)
        record_pdf_contacts(current, vendor_id, "T-B", ["loa@sk.example"], [])
        record_pdf_contacts(current, vendor_id, "T-A", ["wo@sk.example"], [])

        for_a = [row.value for row in ordered_contacts(current, vendor_id, "T-A")]
        for_b = [row.value for row in ordered_contacts(current, vendor_id, "T-B")]

    assert for_a == ["wo@sk.example", "loa@sk.example", "model@sk.example", "typed@sk.example"]
    assert for_b == ["loa@sk.example", "wo@sk.example", "model@sk.example", "typed@sk.example"]


def test_replace_pdf_contacts_touches_only_that_tender(tmp_path):
    from pipeline_core.queries import replace_pdf_contacts

    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    vendor_id = _two_tenders(bind)
    with session(bind) as current:
        record_pdf_contacts(current, vendor_id, "T-A", ["old@sk.example"], ["9811111111"])
        record_pdf_contacts(current, vendor_id, "T-B", ["loa@sk.example"], [])
        mark_enriched(current, vendor_id, email="model@sk.example", phone=None)
        replace_pdf_contacts(current, vendor_id, "T-A", [], ["9822222222"])
        rows = {(row.source, row.tender_id, row.value) for row in current.query(VendorContact)}

    assert rows == {
        ("pdf", "T-A", "9822222222"),
        ("pdf", "T-B", "loa@sk.example"),
        ("model", None, "model@sk.example"),
    }


def test_the_company_evidence_columns_follow_the_pdf_rows(tmp_path):
    from pipeline_core.queries import refresh_vendor_pdf_columns, replace_pdf_contacts

    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    vendor_id = _two_tenders(bind)
    with session(bind) as current:
        record_pdf_contacts(current, vendor_id, "T-A", ["not-an-email", "first@sk.example"], [])
        record_pdf_contacts(current, vendor_id, "T-B", ["second@sk.example"], ["9811111111"])
        refresh_vendor_pdf_columns(current, vendor_id)
        vendor = current.get(Vendor, vendor_id)
        assert (vendor.pdf_email, vendor.pdf_phone) == ("first@sk.example", "9811111111")

        replace_pdf_contacts(current, vendor_id, "T-A", [], [])
        refresh_vendor_pdf_columns(current, vendor_id)
        assert vendor.pdf_email == "second@sk.example"

        replace_pdf_contacts(current, vendor_id, "T-B", [], [])
        refresh_vendor_pdf_columns(current, vendor_id)
        assert (vendor.pdf_email, vendor.pdf_phone) == (None, None)


def test_a_dash_spelling_is_stored_clean_and_matches_the_model_address(tmp_path):
    from pipeline_core.queries import mail_messages

    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    vendor_id = _two_tenders(bind)
    with session(bind) as current:
        record_pdf_contacts(current, vendor_id, "T-B", ["-sksalescompany@gmail.com"], [])
        mark_enriched(current, vendor_id, email="sksalescompany@gmail.com", phone=None)
        pdf = current.query(VendorContact).filter(VendorContact.source == "pdf").one()
        assert (pdf.value, pdf.valid) == ("sksalescompany@gmail.com", 1)
        sent_to = [(m.tender_id, m.email) for m in mail_messages(current)]

    # One letter per award, never one to the dash spelling as well.
    assert sent_to == [
        ("T-A", "sksalescompany@gmail.com"),
        ("T-B", "sksalescompany@gmail.com"),
    ]


def test_clean_stored_contacts_repairs_old_rows_once(tmp_path):
    from pipeline_core.models import TenderDocument
    from pipeline_core.queries import clean_stored_contacts

    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    vendor_id = _two_tenders(bind)
    with session(bind) as current:
        # Rows as an older version stored them: the dash kept and judged valid.
        current.add_all(
            [
                VendorContact(vendor_id=vendor_id, tender_id="T-B", channel="email",
                              value="-sales@sk.example", source="pdf", valid=1),
                VendorContact(vendor_id=vendor_id, tender_id="T-B", channel="email",
                              value="sales@sk.example", source="pdf", valid=1),
                VendorContact(vendor_id=vendor_id, tender_id="T-A", channel="email",
                              value="-other@sk.example", source="pdf", valid=1),
                VendorContact(vendor_id=vendor_id, tender_id=None, channel="email",
                              value="a@b.c", source="model", valid=1),
            ]
        )
        current.add(TenderDocument(tender_id="T-B", filename="LOA.pdf",
                                   emails="-sales@sk.example; x@sk.example"))
        current.get(Vendor, vendor_id).pdf_email = "-sales@sk.example"

    with session(bind) as current:
        changed = clean_stored_contacts(current)
    with session(bind) as current:
        again = clean_stored_contacts(current)
    assert changed > 0
    assert again == 0
    with session(bind) as current:
        rows = sorted(
            (row.tender_id or "", row.value, row.valid) for row in current.query(VendorContact)
        )
        assert rows == [
            ("", "a@b.c", 0),
            ("T-A", "other@sk.example", 1),
            ("T-B", "sales@sk.example", 1),
        ]
        assert current.get(Vendor, vendor_id).pdf_email == "sales@sk.example"
        assert current.query(TenderDocument).one().emails == "sales@sk.example; x@sk.example"


def test_backfill_gives_the_company_pdf_address_to_the_tender_that_held_it(tmp_path):
    from pipeline_core.queries import replace_tender_documents

    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    vendor_id = _two_tenders(bind)
    with session(bind) as current:
        # Two winners on T-B, so its documents were never given to one company.
        other = upsert_vendor(current, name_raw="Second Winner Ltd")
        replace_awards(
            current,
            "T-B",
            [
                {"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "S.K Sales Company"},
                {"bid_number": "2", "vendor_id": other, "bidder_name": "Second Winner Ltd"},
            ],
        )
        replace_tender_documents(
            current, "T-B", [{"filename": "LOA.pdf", "emails": ["loa@sk.example"]}]
        )
        current.get(Vendor, vendor_id).pdf_email = "loa@sk.example"
        current.get(Vendor, vendor_id).pdf_phone = "9811111111"
        backfill_vendor_contacts(current)
        rows = {
            (row.channel, row.tender_id, row.value)
            for row in current.query(VendorContact).filter(VendorContact.vendor_id == vendor_id)
        }

    assert rows == {("email", "T-B", "loa@sk.example"), ("phone", None, "9811111111")}


# --------------------------------------------------------------------------
# address_report: every email, and why one is not mailed
# --------------------------------------------------------------------------


def test_address_report_says_why_each_address_is_or_is_not_mailed(tmp_path):
    from pipeline_core.models import OUTREACH_SENDING, OUTREACH_SENT
    from pipeline_core.queries import (
        SKIP_BUYER,
        SKIP_DUPLICATE,
        SKIP_INVALID,
        SKIP_SENDING,
        SKIP_SENT,
        SKIP_STOPPED,
        address_report,
        mail_messages,
        record_outreach,
        replace_tender_documents,
        set_do_not_mail,
    )

    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    vendor_id = _two_tenders(bind)
    with session(bind) as current:
        replace_tender_documents(
            current, "T-B", [{"filename": "LOA.pdf", "emails": ["loa@sk.example"]}]
        )
        record_pdf_contacts(current, vendor_id, "T-B", ["loa@sk.example"], [])
        for tender_id, value, source, valid in (
            ("T-A", "not-an-email", "pdf", 0),
            (None, "stores@iitb.ac.in", "model", 1),
            (None, "done@sk.example", "model", 1),
            (None, "busy@sk.example", "model", 1),
        ):
            current.add(VendorContact(vendor_id=vendor_id, tender_id=tender_id, channel="email",
                                      value=value, source=source, valid=valid))
        set_typed_contact(current, vendor_id, email="LOA@sk.example")
        record_outreach(current, vendor_id=vendor_id, tender_id="T-A", email="done@sk.example",
                        subject="s", transport="gmail", status=OUTREACH_SENT)
        record_outreach(current, vendor_id=vendor_id, tender_id="T-A", email="busy@sk.example",
                        subject="s", transport="gmail", status=OUTREACH_SENDING)

        report = {line.value: line for line in address_report(current, vendor_id, "T-A")}
        assert report["loa@sk.example"].will_mail
        assert report["loa@sk.example"].tender_id == "T-B"
        assert report["loa@sk.example"].filenames == ("LOA.pdf",)
        assert report["not-an-email"].reason == SKIP_INVALID
        assert report["stores@iitb.ac.in"].reason == SKIP_BUYER
        assert report["done@sk.example"].reason == SKIP_SENT
        assert report["busy@sk.example"].reason == SKIP_SENDING
        assert report["LOA@sk.example"].reason == SKIP_DUPLICATE
        assert [m.email for m in mail_messages(current, awards=[(vendor_id, "T-A")])] == [
            "loa@sk.example"
        ]

        again = {
            line.value: line
            for line in address_report(current, vendor_id, "T-A", include_sent=True)
        }
        assert again["done@sk.example"].will_mail
        assert again["busy@sk.example"].reason == SKIP_SENDING

        # The other award was never sent, so all its valid addresses still wait.
        assert {m.email for m in mail_messages(current, awards=[(vendor_id, "T-B")])} == {
            "loa@sk.example",
            "done@sk.example",
            "busy@sk.example",
        }

        set_do_not_mail(current, vendor_id, stopped=True)
        reasons = {
            line.reason
            for line in address_report(current, vendor_id, "T-B")
            if line.valid and line.reason != SKIP_DUPLICATE
        }
        assert reasons == {SKIP_STOPPED, SKIP_BUYER}
        assert mail_messages(current) == []


def test_the_company_email_is_used_only_when_no_valid_row_exists(tmp_path):
    from pipeline_core.queries import address_report

    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    vendor_id = _two_tenders(bind)
    with session(bind) as current:
        current.get(Vendor, vendor_id).email = "-column@sk.example"
        lines = address_report(current, vendor_id, "T-A")
        assert [(line.value, line.will_mail) for line in lines] == [("column@sk.example", True)]
        record_pdf_contacts(current, vendor_id, "T-A", ["pdf@sk.example"], [])
        assert [line.value for line in address_report(current, vendor_id, "T-A")] == [
            "pdf@sk.example"
        ]
