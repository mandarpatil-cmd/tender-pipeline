"""One tender, from its status page to the database, with the portal and PDF reader faked."""

from __future__ import annotations

from pipeline_core.db import session
from pipeline_core.models import Award, Tender, Vendor
from pipeline_core.queries import documents_for_tenders
from sqlalchemy import select

from stage1_scrape.app import pipeline
from stage1_scrape.app.pipeline import scrape_one_tender
from stage1_scrape.domain.models import ListingRow
from stage1_scrape.persist import Store
from stage1_scrape.persist.pdf_text import pdf_paths

STATUS_URL = "https://eprocure.gov.in/eprocure/app?page=WebTenderStatusLists&sp=Sstatus"
TENDER = "2026_ORG_908061_1"
WORK_ORDER = "007WOLTCABLEFORSTREETLIGHTPOLE.pdf"
LISTING = ListingRow(
    serial="1",
    tender_id=TENDER,
    title_and_ref="Supply of cable / REF-007",
    organisation_chain="Ministry / Department",
    tender_stage="AOC",
    status="",
    status_page_url=STATUS_URL,
)


class FakeResponse:
    def __init__(self, body: bytes, content_type: str):
        self.content = body
        self.text = body.decode("utf-8", errors="replace")
        self.headers = {"Content-Type": content_type}


class FakePortal:
    """The status page, the summary page and its PDFs. An expired token answers in HTML."""

    def __init__(self, html, expired=()):
        self.html = html
        self.expired = set(expired)

    def get(self, url, referer=None):
        if url == STATUS_URL:
            return FakeResponse(self.html("status_page.html").encode(), "text/html")
        if "DirectLink_0" in url:
            return FakeResponse(self.html("summary_page.html").encode(), "text/html")
        token = url.rsplit("sp=", 1)[-1]
        if token in self.expired:
            return FakeResponse(b"<html>Your session has expired</html>", "text/html")
        return FakeResponse(b"%PDF-1.4 " + token.encode(), "application/pdf")


def _read(folder):
    """The work order names the winner's email. The other files carry nothing."""
    return [
        {
            "filename": path.name,
            "text": "work order",
            "emails": ["office@winner.example"] if path.name == WORK_ORDER else [],
            "phones": [],
            "gstins": [],
            "error": None,
        }
        for path in pdf_paths(folder)
    ]


def _fake_reader(monkeypatch):
    monkeypatch.setattr(pipeline, "extract_folder", _read)
    monkeypatch.setattr(pipeline, "_read_documents", lambda extracts: None)


def _stored(store):
    with session(store.engine) as current:
        files = [doc["filename"] for doc in documents_for_tenders(current, [TENDER]).get(TENDER, [])]
        winner = current.scalar(
            select(Vendor).join(Award, Award.vendor_id == Vendor.vendor_id).where(Award.tender_id == TENDER)
        )
        return sorted(files), winner.name_raw, winner.pdf_email


def test_a_tender_is_read_from_its_pages_and_saved(tmp_path, monkeypatch, html):
    _fake_reader(monkeypatch)
    store = Store(tmp_path / "out")

    record = scrape_one_tender(FakePortal(html), LISTING, store, download_pdfs=True)

    assert len(record.downloaded_files) == 3
    files, winner, pdf_email = _stored(store)
    assert files == sorted(["techsummary_cable.pdf", "finsummary_cable.pdf", WORK_ORDER])
    assert winner == "WINNER CO"
    assert pdf_email == "office@winner.example"
    with session(store.engine) as current:
        tender = current.get(Tender, TENDER)
        assert tender.title == "Supply of cable / REF-007"
        assert tender.contract_date == "2026-07-20"
    assert not list((tmp_path / "out" / "pdfs").iterdir()), "the PDFs are not kept"


def test_a_failed_download_keeps_the_clues_already_stored(tmp_path, monkeypatch, html):
    _fake_reader(monkeypatch)
    store = Store(tmp_path / "out")
    scrape_one_tender(FakePortal(html), LISTING, store, download_pdfs=True)
    before = _stored(store)

    record = scrape_one_tender(FakePortal(html, expired={"Saoc"}), LISTING, store, download_pdfs=True)

    assert WORK_ORDER not in record.downloaded_files
    assert _stored(store) == before


def test_every_download_failing_keeps_the_clues_already_stored(tmp_path, monkeypatch, html):
    _fake_reader(monkeypatch)
    store = Store(tmp_path / "out")
    scrape_one_tender(FakePortal(html), LISTING, store, download_pdfs=True)
    before = _stored(store)

    scrape_one_tender(
        FakePortal(html, expired={"Stech", "Sfin", "Saoc"}), LISTING, store, download_pdfs=True
    )

    assert _stored(store) == before


def test_a_new_tender_keeps_the_pdfs_that_did_download(tmp_path, monkeypatch, html):
    _fake_reader(monkeypatch)
    store = Store(tmp_path / "out")

    scrape_one_tender(FakePortal(html, expired={"Stech"}), LISTING, store, download_pdfs=True)

    files, _, pdf_email = _stored(store)
    assert files == sorted(["finsummary_cable.pdf", WORK_ORDER])
    assert pdf_email == "office@winner.example"


def test_a_scrape_without_pdfs_leaves_the_stored_clues(tmp_path, monkeypatch, html):
    _fake_reader(monkeypatch)
    store = Store(tmp_path / "out")
    scrape_one_tender(FakePortal(html), LISTING, store, download_pdfs=True)
    before = _stored(store)

    scrape_one_tender(FakePortal(html), LISTING, store, download_pdfs=False)

    assert _stored(store) == before


def test_scraping_a_tender_again_keeps_one_company_one_award_and_its_lookup(tmp_path, monkeypatch, html):
    from pipeline_core.queries import mark_enriched

    _fake_reader(monkeypatch)
    store = Store(tmp_path / "out")
    scrape_one_tender(FakePortal(html), LISTING, store, download_pdfs=True)
    with session(store.engine) as current:
        winner = current.scalar(select(Vendor))
        mark_enriched(current, winner.vendor_id, email="found@winner.example", phone="9876543210")

    scrape_one_tender(FakePortal(html), LISTING, store, download_pdfs=True)

    with session(store.engine) as current:
        assert current.query(Vendor).count() == 1
        assert current.query(Award).count() == 1
        vendor = current.scalar(select(Vendor))
        assert (vendor.email, vendor.phone, vendor.enrichment_status) == (
            "found@winner.example",
            "9876543210",
            "done",
        )
