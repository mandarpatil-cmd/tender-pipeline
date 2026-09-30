"""The tender form, a typed contact, and a dry-run lookup of ticked rows."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.models import CONTACT_TYPED, Vendor
from pipeline_core.queries import mark_enriched, replace_awards, upsert_tender, upsert_vendor, utcnow

from ops_ui.app import create_app
from ops_ui.jobs import get_job


def _ready(path):
    ensure_schema(engine(path))
    return TestClient(create_app(path))


def test_blank_tender_id_is_manual_and_shows_in_the_table(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    client = _ready(path)
    response = client.post(
        "/tender/new",
        data={
            "title": "Hand job",
            "organisation": "Works",
            "winner_name": "Fresh Electricals",
            "bid_number": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/").text
    assert "MANUAL-" in page
    assert "Fresh Electricals" in page


def test_a_known_company_asks_before_the_award_is_attached(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        vendor_id = upsert_vendor(current, name_raw="Acme Pvt Ltd")
        mark_enriched(current, vendor_id, email="keep@acme.example", phone=None)
    client = TestClient(create_app(path))
    asked = client.post(
        "/tender/new",
        data={"title": "Second", "winner_name": "ACME PVT. LTD.", "bid_number": "1"},
    )
    assert asked.status_code == 200
    assert "already stored" in asked.text
    saved = client.post(
        "/tender/new/attach",
        data={"title": "Second", "winner_name": "ACME PVT. LTD.", "bid_number": "1"},
        follow_redirects=True,
    )
    assert saved.status_code == 200
    with session(engine(path)) as current:
        vendor = current.get(Vendor, vendor_id)
        assert vendor.email == "keep@acme.example"
        assert current.query(Vendor).count() == 1


def test_typed_email_is_marked_and_a_bad_one_is_refused(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        vendor_id = upsert_vendor(current, name_raw="Contact Me Ltd")
        upsert_tender(current, tender_id="T-9", title="Job", scraped_at=utcnow())
        replace_awards(
            current,
            "T-9",
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Contact Me Ltd"}],
        )
        mark_enriched(current, vendor_id, email=None, phone="9999999999")
    client = TestClient(create_app(path))
    refused = client.post(
        f"/vendor/{vendor_id}/contact",
        data={"email": "not-an-email", "phone": "", "back": "/"},
    )
    assert refused.status_code == 400
    saved = client.post(
        f"/vendor/{vendor_id}/contact",
        data={"email": "person@firm.example", "phone": "", "back": "/"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    with session(engine(path)) as current:
        vendor = current.get(Vendor, vendor_id)
    assert vendor.email == "person@firm.example"
    assert vendor.phone == "9999999999"
    assert vendor.contact_origin == CONTACT_TYPED


def test_ticked_company_dry_run_lists_it_and_spends_nothing(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        vendor_id = upsert_vendor(current, name_raw="Still Waiting Ltd")
        upsert_tender(current, tender_id="T-8", title="Wait", scraped_at=utcnow())
        replace_awards(
            current,
            "T-8",
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Still Waiting Ltd"}],
        )
    client = TestClient(create_app(path))
    preview = client.post("/selection", data={"action": "lookup", "vendor_id": str(vendor_id)})
    assert "Still Waiting Ltd" in preview.text
    assert "spends nothing" in preview.text or "Nothing will be spent" in preview.text
    started = client.post(
        "/selection/lookup",
        data={"vendor_id": str(vendor_id), "dry_run": "on"},
        follow_redirects=False,
    )
    job_id = int(started.headers["location"].rsplit("/", 1)[-1])
    text = ""
    for _ in range(50):
        job = get_job(path, job_id)
        text = job.log or ""
        if "Dry run" in text:
            break
        threading.Event().wait(0.05)
    assert "Still Waiting Ltd" in text
    with session(engine(path)) as current:
        assert current.get(Vendor, vendor_id).enrichment_status == "pending"


def test_select_all_uses_the_filter_and_ignores_the_page(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        bihar = upsert_vendor(current, name_raw="Bihar Roads Ltd", state="Bihar")
        assam = upsert_vendor(current, name_raw="Assam Electricals", state="Assam")
        upsert_tender(current, tender_id="B-1", title="Road", scraped_at=utcnow())
        upsert_tender(current, tender_id="A-1", title="Wire", scraped_at=utcnow())
        replace_awards(
            current,
            "B-1",
            [{"bid_number": "1", "vendor_id": bihar, "bidder_name": "Bihar Roads Ltd"}],
        )
        replace_awards(
            current,
            "A-1",
            [{"bid_number": "1", "vendor_id": assam, "bidder_name": "Assam Electricals"}],
        )
    client = TestClient(create_app(path))
    page = client.post(
        "/selection",
        data={"action": "lookup", "select_all": "1", "state": "Bihar"},
    )
    assert page.status_code == 200
    assert "Bihar Roads Ltd" in page.text
    assert "Assam Electricals" not in page.text
