"""Opening one award shows the tender, the company history, and the PDF clues."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.queries import (
    record_llm_run,
    record_outreach,
    replace_awards,
    record_pdf_contacts,
    replace_tender_documents,
    set_pdf_contacts,
    upsert_tender,
    upsert_vendor,
    utcnow,
)

from ops_ui.app import create_app


def _seed(path, json_path: str):
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        winner = upsert_vendor(current, name_raw="Large Electricals", state="Assam")
        other = upsert_vendor(current, name_raw="Other Bidder Ltd")
        set_pdf_contacts(current, winner, email="clue@example.com", phone="9876543210")
        upsert_tender(
            current,
            tender_id="LARGE",
            title="Large rooms",
            organisation="NIT Durgapur",
            status="AOC",
            contract_date="21-Sep-2026",
            contract_value="INR 100",
            json_path=json_path,
            scraped_at=utcnow(),
        )
        replace_awards(
            current,
            "LARGE",
            [
                {"bid_number": "1", "vendor_id": winner, "bidder_name": "Large Electricals"},
                {"bid_number": "2", "vendor_id": other, "bidder_name": "Other Bidder Ltd"},
            ],
        )
        record_llm_run(
            current,
            vendor_id=winner,
            model="test-model",
            prompt_version="v1",
            input_json="{}",
            output_json='{"email":"found@example.com"}',
            status="error",
            error="provider timeout",
        )
        record_outreach(
            current,
            vendor_id=winner,
            email="found@example.com",
            subject="Hello",
            transport="gmail",
            status="failed",
            error="mailbox full",
        )


def test_do_not_mail_is_saved_from_the_award_page(tmp_path):
    from pipeline_core.models import Vendor

    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path="")
    with session(engine(path)) as current:
        vendor_id = current.query(Vendor).filter_by(name_raw="Large Electricals").one().vendor_id
    client = TestClient(create_app(path))
    page = client.get("/award?tender_id=LARGE&bid=1")
    assert "Do not mail this company" in page.text

    saved = client.post(
        f"/vendor/{vendor_id}/do-not-mail",
        data={"do_not_mail": "1", "back": "/award?tender_id=LARGE&bid=1"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    with session(engine(path)) as current:
        assert current.get(Vendor, vendor_id).do_not_mail

    cleared = client.post(
        f"/vendor/{vendor_id}/do-not-mail",
        data={"back": "/award?tender_id=LARGE&bid=1"},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    with session(engine(path)) as current:
        assert current.get(Vendor, vendor_id).do_not_mail is None


def test_award_page_names_the_portal_search_that_found_it(tmp_path):
    from stage1_scrape.config import SEARCH_PAGE_URL

    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path="")
    with session(engine(path)) as current:
        upsert_tender(
            current,
            tender_id="LARGE",
            scraped_at=utcnow(),
            origin="eprocure.gov.in",
            search_mode="awarded",
            search_fields='{"fromDate": "01/01/2026", "tenderStatus": "6"}',
            listing_page=2,
            listing_serial=14,
            scrape_job_id=7,
        )
    html = TestClient(create_app(path)).get("/award?tender_id=LARGE&bid=1").text

    assert "Where this came from" in html
    assert "<dd>eprocure.gov.in</dd>" in html
    assert SEARCH_PAGE_URL.replace("&", "&amp;") in html
    assert "Awarded (AOC)" in html
    assert "fromDate=01/01/2026, tenderStatus=6 (AOC)" in html
    assert "<dd>2</dd>" in html
    assert "<dd>14</dd>" in html
    assert '<a href="/jobs/7">7</a>' in html


def test_award_page_says_when_the_source_was_not_recorded(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path="")
    client = TestClient(create_app(path))
    with session(engine(path)) as current:
        upsert_tender(current, tender_id="LARGE", scraped_at=utcnow(), origin="eprocure.gov.in")
    older = client.get("/award?tender_id=LARGE&bid=1").text
    with session(engine(path)) as current:
        upsert_tender(current, tender_id="LARGE", scraped_at=utcnow(), origin="manual")
    typed = client.get("/award?tender_id=LARGE&bid=1").text

    assert "<dd>eprocure.gov.in</dd>" in older
    assert "<dd>Not recorded</dd>" in older
    assert "Typed by hand on the Add a tender page." in typed


def test_award_page_shows_where_the_model_found_the_address(tmp_path):
    from pipeline_core.models import Vendor
    from pipeline_core.queries import mark_enriched, record_llm_run
    from sqlalchemy import select

    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path="")
    with session(engine(path)) as current:
        vendor_id = current.scalar(select(Vendor.vendor_id).where(Vendor.name_raw == "Large Electricals"))
        run_id = record_llm_run(
            current,
            vendor_id=vendor_id,
            model="test-model",
            prompt_version="contact-v1",
            input_json="{}",
            output_json='{"email": "found@co.example", "source": "https://co.example/about"}',
            status="ok",
            job_id=9,
        )
        mark_enriched(
            current,
            vendor_id,
            email="found@co.example",
            phone=None,
            source_url="https://co.example/about",
            llm_run_id=run_id,
        )
        record_llm_run(
            current,
            vendor_id=vendor_id,
            model="test-model",
            prompt_version="contact-v1",
            input_json="{}",
            output_json='{"email": "rfi@ord.gov.in", "phone": "+918043867273,6957"}',
            status="ok",
            job_id=9,
        )
    html = TestClient(create_app(path)).get("/award?tender_id=LARGE&bid=1").text

    assert "found at https://co.example/about" in html
    assert 'href="/jobs/9"' in html
    assert "Not kept:" in html
    assert "rfi@ord.gov.in (government or buyer address)" in html
    assert "+918043867273,6957 (not a phone number)" in html


def test_row_links_to_the_award(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path="")
    html = TestClient(create_app(path)).get("/scrape").text
    assert "tender_id=LARGE" in html
    assert "bid=1" in html
    assert ">LARGE</a>" in html


def test_detail_shows_tender_awards_model_call_and_mail(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path="")
    with session(engine(path)) as current:
        replace_tender_documents(
            current,
            "LARGE",
            [
                {
                    "filename": "work-order.pdf",
                    "emails": ["clue@example.com"],
                    "phones": [],
                    "gstins": ["27ABCDE1234F1Z5"],
                }
            ],
        )
    response = TestClient(create_app(path)).get("/award?tender_id=LARGE&bid=1")

    assert response.status_code == 200
    html = response.text
    assert "Large rooms" in html
    assert "Other Bidder Ltd" in html
    assert "provider timeout" in html
    assert "mailbox full" in html
    assert "found@example.com" in html
    assert "work-order.pdf" in html
    assert "27ABCDE1234F1Z5" in html
    assert "clue@example.com" in html
    assert "A mail run can use them, PDF address first." in html
    assert "Download PDFs" in html
    assert "The files were not kept." in html


def test_tender_screen_counts_every_email(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Two Addresses Ltd")
        upsert_tender(current, tender_id="TWO", title="Bridge", scraped_at=utcnow())
        replace_awards(
            current,
            "TWO",
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Two Addresses Ltd"}],
        )
        record_pdf_contacts(
            current,
            vendor_id,
            "TWO",
            ["one@roads.example", "two@roads.example"],
            ["9811111111"],
        )
    html = TestClient(create_app(path)).get("/award?tender_id=TWO&bid=1").text
    assert "2 emails for this tender." in html
    assert "one@roads.example" in html
    assert "two@roads.example" in html
    assert "9811111111" in html


def test_unknown_award_is_not_found(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    response = TestClient(create_app(path)).get("/award?tender_id=MISSING&bid=1")
    assert response.status_code == 404
    assert "No award is stored" in response.text
