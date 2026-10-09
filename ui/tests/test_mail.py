"""The mail page previews by default and refuses a live send until the pitch is real."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.models import MailLetter, Outreach, Tender
from pipeline_core.queries import mark_enriched, replace_awards, upsert_tender, upsert_vendor, utcnow

from ops_ui.app import create_app
from ops_ui.jobs import get_job
from ops_ui.runs import load_campaign


def _picked(html: str) -> str:
    start = html.index('id="picked"')
    return html[start:html.index("</ul>", start)]


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


def test_mail_page_asks_for_companies_before_it_can_send(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    html = TestClient(create_app(path)).get("/mail").text

    assert "Tick rows in the table, then Mail these." in html
    assert "Choose companies on Awards." not in html
    assert "Edit the default" in html
    assert "Save the default" in html
    assert "Corporate insurance introduction for {{company}}" in html
    assert "Dear Has Email Ltd team," not in html
    assert "Send for real" not in html
    assert "Everyone waiting" not in html


def test_preview_writes_a_file_and_sends_nothing(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    vendor_id = _seed(path)
    campaign = load_campaign()
    monkeypatch.setattr(campaign, "PREVIEW_DIR", tmp_path / "previews")
    client = TestClient(create_app(path))

    started = client.post(
        "/mail",
        data={"mode": "preview", "delay": "5", "award": f"{vendor_id}:T-1"},
        follow_redirects=False,
    )

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
    assert "Letter: the saved default." in text
    preview = next((tmp_path / "previews").glob("*.txt")).read_text(encoding="utf-8")
    assert "Dear Has Email Ltd team," in preview
    with session(engine(path)) as current:
        assert current.query(Outreach).count() == 0


def test_live_send_is_refused_while_the_pitch_is_a_placeholder(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    vendor_id = _seed(path)
    campaign = load_campaign()
    bind = engine(path)
    campaign.load_letter(bind)
    with session(bind) as current:
        row = current.get(MailLetter, 1)
        row.body = campaign.PLACEHOLDER
    client = TestClient(create_app(path))

    refused = client.post(
        "/mail",
        data={"mode": "send", "send": "on", "delay": "5", "award": f"{vendor_id}:T-1"},
    )

    assert refused.status_code == 200
    assert "placeholder" in refused.text
    with session(engine(path)) as current:
        assert current.query(Outreach).count() == 0


def test_saving_a_letter_shows_it_filled_in_and_sends_nothing(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    vendor_id = _seed(path)
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
            "award": f"{vendor_id}:T-1",
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
    assert "Has Email Ltd" in page.text
    assert "1 message(s) will be mailed" in page.text
    assert "Send the default" in page.text
    assert "Change some phrases" in page.text
    assert "cannot be removed" in page.text
    assert 'value="graph" selected' in page.text
    assert "Everyone waiting" not in page.text


def _wait(path, job_id) -> tuple[str, str]:
    text = ""
    state = ""
    for _ in range(50):
        job = get_job(path, job_id)
        text = job.log or ""
        state = job.state
        if state in {"done", "failed", "stopped"}:
            break
        threading.Event().wait(0.05)
    return state, text


def _start(client, data) -> tuple[int, str]:
    started = client.post("/mail", data=data, follow_redirects=False)
    assert started.status_code == 303, started.text
    return int(started.headers["location"].rsplit("/", 1)[-1]), started.text


def _other_vendor(path) -> int:
    bind = engine(path)
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Other Ltd", state="Goa")
        upsert_tender(current, tender_id="T-2", title="Bridge", scraped_at=utcnow())
        replace_awards(
            current,
            "T-2",
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Other Ltd"}],
        )
        mark_enriched(current, vendor_id, email="b@c.example", phone=None)
    return vendor_id


def test_a_preview_without_companies_does_not_mail_everyone(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    _other_vendor(path)
    page = TestClient(create_app(path)).post("/mail", data={"mode": "preview", "delay": "5"})

    assert page.status_code == 200
    assert "Tick at least one row in the table." in page.text
    with session(engine(path)) as current:
        assert current.query(Outreach).count() == 0


def test_preview_and_remove_stay_on_the_picked_companies(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    first = _seed(path)
    second = _other_vendor(path)
    campaign = load_campaign()
    monkeypatch.setattr(campaign, "PREVIEW_DIR", tmp_path / "previews")
    client = TestClient(create_app(path))

    job_id, _body = _start(
        client,
        {"mode": "preview", "delay": "5", "award": [f"{first}:T-1", f"{second}:T-2"]},
    )
    state, text = _wait(path, job_id)
    assert state == "done"
    assert "2 message(s) can be mailed." in text

    kept = client.post("/mail", data={"mode": "remove", "award": f"{first}:T-1"})
    assert kept.status_code == 200
    picked = _picked(kept.text)
    assert "Has Email Ltd" in picked
    assert "Other Ltd" not in picked
    assert "1 message(s) will be mailed" in kept.text

    job_id, _body = _start(
        client,
        {"mode": "preview", "delay": "5", "award": f"{first}:T-1"},
    )
    state, text = _wait(path, job_id)
    assert state == "done"
    assert "1 message(s) can be mailed." in text
    preview = next((tmp_path / "previews").glob("*.txt")).read_text(encoding="utf-8")
    assert "Has Email Ltd" in preview
    assert "Other Ltd" not in preview


def test_a_one_off_send_does_not_replace_the_default(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    vendor_id = _seed(path)
    campaign = load_campaign()
    monkeypatch.setattr(campaign, "PREVIEW_DIR", tmp_path / "previews")
    bind = engine(path)
    campaign.load_letter(bind)
    with session(bind) as current:
        current.get(Tender, "T-1").contract_date = "01-Jan-2026"
        current.get(MailLetter, 1).body = "Saved default for {{company}}."
    client = TestClient(create_app(path))

    job_id, _body = _start(
        client,
        {
            "mode": "preview",
            "letter": "custom",
            "delay": "5",
            "award": f"{vendor_id}:T-1",
            "subject": "Thursday for {{company}}",
            "body": "Thursday for {{company}}. Tender {{title}} on {{contract_date}}.",
            "sender_name": "Ada",
            "sender_designation": "Director",
            "sender_org": "Policy Pact",
            "sender_mobile": "9000000000",
            "sender_email": "ada@example.com",
        },
    )
    state, text = _wait(path, job_id)
    assert state == "done", text
    assert "The saved default is unchanged." in text
    preview = next((tmp_path / "previews").glob("*.txt")).read_text(encoding="utf-8")
    assert "Subject: Thursday for Has Email Ltd" in preview
    assert "Tender Road on 01-Jan-2026" in preview
    assert "Saved default" not in preview
    with session(bind) as current:
        assert current.get(MailLetter, 1).body == "Saved default for {{company}}."
        assert current.query(Outreach).count() == 0


