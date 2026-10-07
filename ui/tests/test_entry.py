"""The tender form, a typed contact, and a dry-run lookup of ticked rows."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.models import CONTACT_TYPED, Vendor
from pipeline_core.queries import (
    mark_enriched,
    mark_failed,
    replace_awards,
    upsert_tender,
    upsert_vendor,
    utcnow,
)

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
    assert "<h1>Enrich</h1>" in page.text


def _award(current, tender_id, vendor_id, name, *, state="Goa"):
    upsert_tender(current, tender_id=tender_id, title=name, scraped_at=utcnow())
    replace_awards(
        current,
        tender_id,
        [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": name}],
    )


def test_show_not_found_opens_the_combined_list_and_writes_nothing(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        waiting = upsert_vendor(current, name_raw="Still Waiting Ltd", state="Goa")
        empty = upsert_vendor(current, name_raw="No Contact Ltd", state="Goa")
        known = upsert_vendor(current, name_raw="Known Absence", state="Goa")
        other = upsert_vendor(current, name_raw="Other State Ltd", state="Bihar")
        current.get(Vendor, empty).enrichment_status = "not_found"
        current.get(Vendor, known).enrichment_status = "not_found"
        current.get(Vendor, known).email = "known@example.com"
        current.get(Vendor, other).enrichment_status = "not_found"
        _award(current, "W-1", waiting, "Still Waiting Ltd")
        _award(current, "N-1", empty, "No Contact Ltd")
        _award(current, "K-1", known, "Known Absence")
        _award(current, "B-1", other, "Other State Ltd")
    client = TestClient(create_app(path))
    opened = client.post(
        "/selection",
        data={"action": "show_not_found", "enrichment_status": "pending", "state": "Goa"},
        follow_redirects=False,
    )
    location = opened.headers["location"]
    assert opened.status_code == 303
    assert "enrichment_status=pending" in location
    assert "enrichment_status=not_found" in location
    assert "state=Goa" in location
    assert "show=not_found" in location
    page = client.get(location).text
    assert "Still Waiting Ltd" in page
    assert "No Contact Ltd" in page
    assert "Known Absence" in page
    assert "Other State Ltd" not in page
    assert "pending 1, not_found 2, failed 0" in page
    assert "1 not_found with no email and no phone" in page
    assert "Enrich looks up the pending companies" in page
    with session(engine(path)) as current:
        assert current.get(Vendor, waiting).enrichment_status == "pending"
        assert current.get(Vendor, empty).enrichment_status == "not_found"
        assert current.get(Vendor, known).enrichment_status == "not_found"
        assert current.get(Vendor, known).email == "known@example.com"


def test_show_failed_adds_failed_to_a_pending_filter(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        waiting = upsert_vendor(current, name_raw="Still Waiting Ltd", state="Bihar")
        failed = upsert_vendor(current, name_raw="Failed Roads", state="Bihar")
        mark_failed(current, failed)
        _award(current, "W-1", waiting, "Still Waiting Ltd")
        _award(current, "F-1", failed, "Failed Roads")
    client = TestClient(create_app(path))
    opened = client.post(
        "/selection",
        data={"action": "show_failed", "enrichment_status": "pending", "state": "Bihar"},
        follow_redirects=False,
    )
    location = opened.headers["location"]
    assert "enrichment_status=pending" in location
    assert "enrichment_status=failed" in location
    assert "state=Bihar" in location
    assert "show=failed" in location
    page = client.get(location).text
    assert "pending 1, not_found 0, failed 1" in page
    assert "0 not_found with no email and no phone" in page
    assert 'value="failed" checked' in page
    with session(engine(path)) as current:
        assert current.get(Vendor, failed).enrichment_status == "failed"


def test_show_not_found_leaves_any_as_the_whole_list(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        waiting = upsert_vendor(current, name_raw="Still Waiting Ltd")
        _award(current, "W-1", waiting, "Still Waiting Ltd")
    client = TestClient(create_app(path))
    opened = client.post(
        "/selection",
        data={"action": "show_not_found"},
        follow_redirects=False,
    )
    location = opened.headers["location"]
    assert "enrichment_status=" not in location
    assert location.endswith("?show=not_found") or "show=not_found" in location
    page = client.get(location).text
    assert "Still Waiting Ltd" in page
    assert "pending 1, not_found 0, failed 0" in page


def test_enrich_confirm_names_who_will_be_looked_up(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        waiting = upsert_vendor(current, name_raw="Still Waiting Ltd", state="Goa")
        failed = upsert_vendor(current, name_raw="Failed Roads", state="Goa")
        empty = upsert_vendor(current, name_raw="No Contact Ltd", state="Goa")
        phoned = upsert_vendor(current, name_raw="Phone Only Ltd", state="Goa")
        mark_failed(current, failed)
        for vendor_id, email, phone in (
            (empty, None, None),
            (phoned, None, "9999999999"),
        ):
            vendor = current.get(Vendor, vendor_id)
            vendor.enrichment_status = "not_found"
            vendor.email = email
            vendor.phone = phone
        for tender_id, vendor_id, name in (
            ("W-1", waiting, "Still Waiting Ltd"),
            ("F-1", failed, "Failed Roads"),
            ("N-1", empty, "No Contact Ltd"),
            ("P-1", phoned, "Phone Only Ltd"),
        ):
            upsert_tender(current, tender_id=tender_id, title=name, scraped_at=utcnow())
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": name}],
            )
        done = upsert_vendor(current, name_raw="Already Done Ltd", state="Goa")
        mark_enriched(current, done, email="done@example.com", phone=None)
        _award(current, "D-1", done, "Already Done Ltd")
    ids = [str(waiting), str(failed), str(empty), str(phoned), str(done)]
    client = TestClient(create_app(path))
    page = client.post("/selection", data={"action": "lookup", "vendor_id": ids})
    assert page.status_code == 200
    assert _count(page.text, "pending") == "1"
    assert _count(page.text, "not_found") == "2"
    assert _count(page.text, "failed") == "1"
    assert "pending 1, not_found 2, failed 1" in page.text
    assert "1 not_found with no email and no phone" in page.text
    assert "Failed Roads" in page.text
    assert "No Contact Ltd" in page.text
    assert "Still Waiting Ltd" in page.text
    assert f'value="{phoned}"' not in page.text
    assert f'value="{done}"' not in page.text
    assert "Already Done Ltd" not in page.text
    started = client.post(
        "/selection/lookup",
        data={"vendor_id": [str(waiting), str(failed), str(empty)], "dry_run": "on"},
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
    assert "Failed Roads" in text
    assert "No Contact Ltd" in text
    with session(engine(path)) as current:
        assert current.get(Vendor, waiting).enrichment_status == "pending"
        assert current.get(Vendor, failed).enrichment_status == "failed"
        assert current.get(Vendor, empty).enrichment_status == "not_found"
        assert current.get(Vendor, phoned).enrichment_status == "not_found"
        assert current.get(Vendor, phoned).phone == "9999999999"


def _count(html: str, key: str) -> str:
    marker = f'data-count="{key}">'
    start = html.index(marker) + len(marker)
    return html[start : html.index("<", start)].strip()
