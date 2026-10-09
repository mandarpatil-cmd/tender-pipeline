"""Opening one award shows the tender, the company history, and the PDF clues."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.queries import (
    record_llm_run,
    record_outreach,
    replace_awards,
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
    assert "not an address to mail" in html
    assert "Download PDFs" in html
    assert "The files were not kept." in html


def test_unknown_award_is_not_found(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    response = TestClient(create_app(path)).get("/award?tender_id=MISSING&bid=1")
    assert response.status_code == 404
    assert "No award is stored" in response.text
