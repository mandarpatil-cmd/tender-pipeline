"""What the Mail page hands to a run, and the letter's files. Nothing is sent."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.models import Outreach, UiJob
from pipeline_core.queries import mark_enriched, replace_awards, upsert_tender, upsert_vendor, utcnow

from ops_ui.app import create_app
from ops_ui.jobs import get_job
from ops_ui.present import job_id_from
from ops_ui.runs import load_campaign


def _seed(path) -> int:
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Has Email Ltd", state="Goa")
        upsert_tender(current, tender_id="T-1", title="Road", scraped_at=utcnow())
        replace_awards(
            current, "T-1", [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Has Email Ltd"}]
        )
        mark_enriched(current, vendor_id, email="a@b.example", phone=None)
    return vendor_id


@pytest.fixture
def mail(tmp_path, monkeypatch):
    """A seeded window whose runs record their arguments instead of mailing."""
    path = tmp_path / "pipeline.sqlite3"
    vendor_id = _seed(path)
    runs: list[dict] = []

    def run_outreach(log, stop, **params):
        runs.append(params)
        return 0

    monkeypatch.setattr("ops_ui.routes.mail.run_outreach", run_outreach)
    return TestClient(create_app(path)), path, vendor_id, runs


def _post(client, data):
    return client.post("/mail", data=data, follow_redirects=False)


def _finished(path, location: str) -> None:
    job_id = job_id_from(location)
    for _ in range(100):
        if get_job(path, job_id).state in {"done", "failed", "stopped"}:
            return
        threading.Event().wait(0.02)


def test_a_preflight_without_a_mailbox_says_what_to_set_and_starts_nothing(mail):
    client, path, _vendor, runs = mail

    page = _post(client, {"mode": "preflight", "transport": "outlook"})

    assert page.status_code == 200
    assert "OUTLOOK_USER" in page.text
    assert runs == []
    with session(engine(path)) as current:
        assert current.query(UiJob).count() == 0


def test_a_preflight_with_a_mailbox_runs_with_no_company(mail, monkeypatch):
    client, path, _vendor, runs = mail
    monkeypatch.setenv("OUTLOOK_USER", "me@company.example")
    monkeypatch.setenv("OUTLOOK_PASSWORD", "secret")

    started = _post(client, {"mode": "preflight", "transport": "outlook"})
    _finished(path, started.headers["location"])

    assert started.status_code == 303
    assert runs[0]["preflight"] is True
    assert runs[0]["send"] is False
    assert runs[0]["awards"] == []
    with session(engine(path)) as current:
        assert current.query(Outreach).count() == 0


@pytest.mark.parametrize(
    ("asked", "used"),
    [("gmail", "gmail"), ("graph", "graph"), ("outlook", "outlook"), ("sendmail", "outlook"), ("", "outlook")],
)
def test_the_transport_reaches_the_run_and_anything_unknown_is_outlook(mail, asked, used):
    client, path, vendor_id, runs = mail

    started = _post(client, {"mode": "preview", "transport": asked, "award": f"{vendor_id}:T-1"})
    _finished(path, started.headers["location"])

    assert runs[0]["transport"] == used


def test_the_delay_between_sends_cannot_be_lowered(mail):
    client, path, vendor_id, runs = mail

    started = _post(client, {"mode": "preview", "delay": "0", "award": f"{vendor_id}:T-1"})
    _finished(path, started.headers["location"])

    assert runs[0]["delay"] == 5.0


def test_redirect_and_send_again_reach_the_run(mail):
    client, path, vendor_id, runs = mail

    started = _post(
        client,
        {
            "mode": "preview",
            "redirect_to": " me@self.example ",
            "send_again": "on",
            "award": f"{vendor_id}:T-1",
        },
    )
    _finished(path, started.headers["location"])

    assert runs[0]["redirect_to"] == "me@self.example"
    assert runs[0]["include_sent"] is True
    assert runs[0]["awards"] == [(vendor_id, "T-1")]


def test_a_second_outreach_run_is_refused_while_one_is_going(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    vendor_id = _seed(path)
    release = threading.Event()
    monkeypatch.setattr("ops_ui.routes.mail.run_outreach", lambda log, stop, **params: release.wait(5) and 0)
    client = TestClient(create_app(path))

    first = _post(client, {"mode": "preview", "award": f"{vendor_id}:T-1"})
    second = _post(client, {"mode": "preview", "award": f"{vendor_id}:T-1"})
    release.set()

    assert job_id_from(first.headers["location"]) == job_id_from(second.headers["location"])
    assert "refused=1" in second.headers["location"]
    with session(engine(path)) as current:
        assert current.query(UiJob).count() == 1


_SIGNED = {
    "sender_name": "Ada Lovelace",
    "sender_designation": "Director",
    "sender_org": "Policy Pact",
    "sender_mobile": "9000000000",
    "sender_email": "ada@policypact.example",
}


def test_restore_brings_back_the_original_letter_and_keeps_the_signature(mail):
    client, _path, _vendor, _runs = mail
    edited = {"subject": "My own subject", "body": "Hello {{company}}", **_SIGNED}
    assert "Saved. Nothing was sent." in client.post("/mail/letter", data=edited).text

    restored = client.post("/mail/letter", data={**edited, "restore": "1"})

    assert "Saved. Nothing was sent." in restored.text
    page = client.get("/mail").text
    assert "Corporate insurance introduction for {{company}}" in page
    assert "My own subject" not in page
    assert "Ada Lovelace" in page


def test_restore_is_refused_while_the_saved_signature_is_incomplete(mail):
    """The original letter signs with every field, and only a sendable letter is saved."""
    client, _path, _vendor, _runs = mail
    edited = {"subject": "My own subject", "body": "Hello {{company}}", **_SIGNED, "sender_mobile": ""}
    client.post("/mail/letter", data=edited)

    restored = client.post("/mail/letter", data={**edited, "restore": "1"})

    assert "fill in Mobile" in restored.text
    assert "My own subject" in client.get("/mail").text


def test_a_pdf_can_be_added_replaced_and_removed(mail):
    client, _path, _vendor, _runs = mail
    folder = load_campaign().ATTACHMENT_DIR
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF"

    added = client.post("/mail/attachment", files={"pdf": ("brochure.pdf", pdf, "application/pdf")})
    again = client.post("/mail/attachment", files={"pdf": ("brochure.pdf", pdf + b"\n", "application/pdf")})

    assert added.status_code == again.status_code == 200
    assert (folder / "brochure.pdf").read_bytes() == pdf + b"\n"
    assert client.get("/mail").text.count("brochure.pdf") >= 1
    names = [p.name for p in folder.iterdir()]
    assert names.count("brochure.pdf") == 1

    removed = client.post("/mail/attachment/remove", data={"filename": "brochure.pdf"})

    assert removed.status_code == 200
    assert not (folder / "brochure.pdf").exists()
    assert "brochure.pdf" not in client.get("/mail").text


def test_adding_with_no_file_chosen_says_so(mail):
    client, _path, _vendor, _runs = mail

    page = client.post("/mail/attachment", data={})

    assert "Choose a PDF to add." in page.text


def test_removing_a_file_that_is_not_attached_changes_nothing(mail):
    client, _path, _vendor, _runs = mail
    folder = load_campaign().ATTACHMENT_DIR
    before = sorted(p.name for p in folder.iterdir())

    page = client.post("/mail/attachment/remove", data={"filename": "../../core/pyproject.toml"})

    assert page.status_code == 200
    assert sorted(p.name for p in folder.iterdir()) == before
