"""Document clues come from the model, then land on the single winner."""

from pathlib import Path

from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.models import TenderDocument, Vendor
from pipeline_core.queries import replace_awards, replace_tender_documents, upsert_tender, upsert_vendor, utcnow

from stage1_scrape.persist.document_extract import (
    DocumentExtract,
    apply_document_extracts,
    clues_from_extract,
)
from stage1_scrape.persist.store import Store


def test_clues_drop_a_buyer_email_and_a_short_gstin():
    parsed = DocumentExtract(
        email="storesoffice@vnit.ac.in",
        phone="9123456780",
        gstin="NOT-A-GSTIN",
    )
    found = clues_from_extract(parsed)
    assert found["emails"] == []
    assert found["phones"] == ["9123456780"]
    assert found["gstins"] == []


def test_a_fake_model_fills_clues_and_the_winner_pdf_contact(tmp_path: Path):
    extracts = [
        {
            "filename": "work-order.pdf",
            "text": "Sunrise Builders sunrise@example.com 9123456780",
            "text_source": "liteparse",
            "emails": [],
            "phones": [],
            "gstins": [],
        }
    ]

    def fake(text, source):
        assert "Sunrise" in text
        assert source == "liteparse"
        return DocumentExtract(
            company_name="Sunrise Builders",
            email="sunrise@example.com",
            phone="9123456780",
            gstin="27ABCDE1234F1Z5",
            evidence_quote="sunrise@example.com",
        )

    apply_document_extracts(extracts, fetch=fake)
    assert extracts[0]["emails"] == ["sunrise@example.com"]

    out = tmp_path / "out"
    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Sunrise Builders")
        upsert_tender(current, tender_id="T-1", title="Works", scraped_at=utcnow())
        replace_awards(
            current,
            "T-1",
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Sunrise Builders"}],
        )
        replace_tender_documents(current, "T-1", extracts)
        Store(out)._save_pdf_contacts(current, "T-1", [{"vendor_id": vendor_id}], extracts)
        vendor = current.get(Vendor, vendor_id)
        document = current.get(TenderDocument, ("T-1", "work-order.pdf"))

    assert vendor.pdf_email == "sunrise@example.com"
    assert vendor.pdf_phone == "9123456780"
    assert document.text_source == "liteparse"
    assert "Sunrise" in document.text
    assert document.gstins == "27ABCDE1234F1Z5"
