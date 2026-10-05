"""The mail page previews by default and refuses a live send until the pitch is real."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.models import MailLetter, Outreach
from pipeline_core.queries import mark_enriched, replace_awards, upsert_tender, upsert_vendor, utcnow

from ops_ui.app import create_app
from ops_ui.jobs import get_job
from ops_ui.runs import load_campaign


def _seed(path) -> int:
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Has Email Ltd", state="Goa")
        upsert_tender(current, tender_id="T-1", title="Road", scraped_at=utcnow())
        replace_awards(
            current,
            "T-1",
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Has Email Ltd"}],
        )
        mark_enriched(current, vendor_id, email="a@b.example", phone=None)
    return vendor_id


def test_mail_page_shows_the_message_and_the_queue(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    html = TestClient(create_app(path)).get("/mail").text

    assert "Preview" in html
    assert "Send for real" in html
    assert "1 waiting with an address" in html
    assert "Corporate insurance introduction for {{company}}" in html
    assert "Dear Has Email Ltd team," in html
    assert "Insert company name" in html
    assert "Save letter" in html
    assert "PolicyPact_Corporate_Insurance_Pitch.pdf" in html
    assert "Surety_Bond_Corporate_Presentation_PolicyPact.pdf" in html
    assert "cannot be removed" in html
    assert 'value="graph" selected' in html


def test_preview_writes_a_file_and_sends_nothing(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    campaign = load_campaign()
    monkeypatch.setattr(campaign, "PREVIEW_DIR", tmp_path / "previews")
    client = TestClient(create_app(path))

    started = client.post("/mail", data={"mode": "preview", "delay": "5"}, follow_redirects=False)

    assert started.status_code == 303
    job_id = int(started.headers["location"].rsplit("/", 1)[-1])
    text = ""
    state = ""
    for _ in range(50):
        job = get_job(path, job_id)
        text = job.log or ""
        state = job.state
        if state in {"done", "failed", "stopped"}:
            break
        threading.Event().wait(0.05)
    assert state == "done"
    assert "DRY RUN" in text
    assert list((tmp_path / "previews").glob("*.txt"))
    with session(engine(path)) as current:
        assert current.query(Outreach).count() == 0


def test_live_send_is_refused_while_the_pitch_is_a_placeholder(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    campaign = load_campaign()
    bind = engine(path)
    campaign.load_letter(bind)
    with session(bind) as current:
        row = current.get(MailLetter, 1)
        row.body = campaign.PLACEHOLDER
    client = TestClient(create_app(path))

    refused = client.post("/mail", data={"mode": "send", "send": "on", "delay": "5"})

    assert refused.status_code == 200
    assert "placeholder" in refused.text
    with session(engine(path)) as current:
        assert current.query(Outreach).count() == 0


def test_saving_a_letter_shows_it_filled_in_and_sends_nothing(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    client = TestClient(create_app(path))

    page = client.post(
        "/mail/letter",
        data={
            "subject": "Hello {{company}}",
            "body": "Dear {{company}} team,\n\nFrom {{sender_name}}.",
            "sender_name": "Ada",
            "sender_designation": "Director",
            "sender_org": "Policy Pact Insurance Broker",
            "sender_mobile": "9000000000",
            "sender_email": "ada@example.com",
        },
    )

    assert page.status_code == 200
    assert "Saved. Nothing was sent." in page.text
    assert "Hello Has Email Ltd" in page.text
    assert "From Ada." in page.text
    with session(engine(path)) as current:
        assert current.query(Outreach).count() == 0


def test_the_page_refuses_an_unknown_token_and_a_file_that_is_not_a_pdf(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    campaign = load_campaign()
    monkeypatch.setattr(campaign, "ATTACHMENT_DIR", tmp_path / "files")
    monkeypatch.setattr(campaign, "MAX_PDF_BYTES", 8)
    client = TestClient(create_app(path))

    refused = client.post(
        "/mail/letter",
        data={
            "subject": "Hello",
            "body": "Hello {{cxo}}",
            "sender_name": "Ada",
            "sender_designation": "",
            "sender_org": "",
            "sender_mobile": "",
            "sender_email": "",
        },
    )
    assert refused.status_code == 200
    assert "{{cxo}}" in refused.text
    assert "Saved. Nothing was sent." not in refused.text

    not_pdf = client.post(
        "/mail/attachment",
        files={"pdf": ("notes.txt", b"hello", "text/plain")},
    )
    assert "not a PDF" in not_pdf.text

    too_big = client.post(
        "/mail/attachment",
        files={"pdf": ("big.pdf", b"%PDF-1.4\n" + b"x" * 20, "application/pdf")},
    )
    assert "2 MB" in too_big.text
    with session(engine(path)) as current:
        assert current.query(Outreach).count() == 0


def test_mail_these_opens_the_page_for_the_ticked_company(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    vendor_id = _seed(path)
    client = TestClient(create_app(path))

    page = client.post("/mail/from-table", data={"vendor_id": str(vendor_id), "action": "mail"})

    assert page.status_code == 200
    assert "1 award(s) came from the table" in page.text
    assert "1 message(s) will be mailed" in page.text
    assert "Send again" in page.text
