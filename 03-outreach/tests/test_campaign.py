"""Stage 3's contract: mail each vendor once, and never twice."""

from __future__ import annotations

import pytest
from pipeline_core.db import session
from pipeline_core.models import (
    OUTREACH_FAILED,
    OUTREACH_SENT,
    OUTREACH_TEST,
    MailLetter,
    Outreach,
)
from pipeline_core.queries import mark_enriched, replace_awards, upsert_tender, upsert_vendor

import campaign
from transport import TransportError


class FakeTransport:
    """Records deliveries instead of making them. Optionally rejects some."""

    def __init__(self, reject: set[str] | None = None):
        self.reject = reject or set()
        self.sent: list[tuple[str, str]] = []
        self.files: list[tuple[str, ...]] = []
        self.closed = False

    def send(self, to: str, subject: str, body: str, attachments=None) -> None:
        if to in self.reject:
            raise TransportError(f"550 mailbox unavailable: {to}")
        self.sent.append((to, subject))
        self.files.append(tuple(name for name, _content in (attachments or [])))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def written_pitch(monkeypatch):
    """Stand in for a pitch someone has actually written, and a sender set in .env.

    These tests are about the queue and the log, not about the copy, so they
    must not hit the placeholder or sender guards in `send_all`. The guards
    have their own tests at the bottom of this file.
    """
    monkeypatch.setattr(campaign, "SENDER_NAME", "Test Sender")
    monkeypatch.setattr(campaign, "SENDER_DESIGNATION", "Director")
    monkeypatch.setattr(campaign, "SENDER_ORG", "Test Org")
    monkeypatch.setattr(campaign, "SENDER_MOBILE", "9000000000")
    monkeypatch.setattr(campaign, "SENDER_EMAIL", "sender@example.com")


@pytest.fixture
def fake_transport(monkeypatch, written_pitch):
    holder = {}

    def build(name: str):
        holder["transport"] = holder.get("transport") or FakeTransport()
        return holder["transport"]

    monkeypatch.setattr(campaign, "build_transport", build)
    return holder


def _vendor(name: str, email: str | None) -> int:
    with session() as current:
        vendor_id = upsert_vendor(current, name_raw=name)
        mark_enriched(current, vendor_id, email=email, phone=None)
        if email:
            tender_id = f"T-{vendor_id}"
            upsert_tender(
                current,
                tender_id=tender_id,
                title="Work",
                contract_date="01-Jan-2026",
                scraped_at="2020-01-01T00:00:00+00:00",
            )
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": name}],
            )
        return vendor_id


def test_recipients_come_from_the_database():
    _vendor("Kanta Enterprises", "hi@kanta.example")
    _vendor("No Address Ltd", None)

    people = campaign.load_recipients()

    assert [p.company for p in people] == ["Kanta Enterprises"]
    assert people[0].email == "hi@kanta.example"


def test_malformed_addresses_are_dropped_before_any_transport_sees_them():
    _vendor("Good Ltd", "hi@good.example")
    _vendor("Bad Ltd", "not-an-address")

    assert [p.company for p in campaign.load_recipients()] == ["Good Ltd"]


def test_limit_bounds_the_batch():
    for i in range(3):
        _vendor(f"Company {i} Ltd", f"c{i}@x.example")

    assert len(campaign.load_recipients(limit=2)) == 2


def test_a_dry_run_writes_previews_and_sends_nothing(fake_transport):
    _vendor("Kanta Enterprises", "hi@kanta.example")

    assert campaign.run(send=False) == 0

    previews = list(campaign.PREVIEW_DIR.glob("*.txt"))
    assert len(previews) == 1
    assert "hi@kanta.example" in previews[0].read_text(encoding="utf-8")
    assert "transport" not in fake_transport  # never even built
    with session() as current:
        assert current.query(Outreach).count() == 0


def test_a_sent_vendor_drops_out_of_the_next_run(fake_transport):
    vendor_id = _vendor("Kanta Enterprises", "hi@kanta.example")

    campaign.run(send=True, delay=0)

    assert fake_transport["transport"].sent == [
        ("hi@kanta.example", "Corporate insurance introduction for Kanta Enterprises")
    ]
    assert fake_transport["transport"].files == [
        (
            "PolicyPact_Corporate_Insurance_Pitch.pdf",
            "Surety_Bond_Corporate_Presentation_PolicyPact.pdf",
        )
    ]
    with session() as current:
        row = current.query(Outreach).one()
        assert row.status == OUTREACH_SENT
        assert row.tender_id == f"T-{vendor_id}"
        assert row.transport == "gmail"
        assert row.sent_at

    # The whole point: re-running mails nobody again.
    assert campaign.load_recipients() == []
    assert campaign.run(send=True, delay=0) == 0
    assert len(fake_transport["transport"].sent) == 1


def test_a_rejected_address_is_logged_and_stays_in_the_queue(monkeypatch, written_pitch):
    _vendor("Good Ltd", "hi@good.example")
    _vendor("Bounces Ltd", "nope@bounces.example")
    transport = FakeTransport(reject={"nope@bounces.example"})
    monkeypatch.setattr(campaign, "build_transport", lambda name: transport)

    campaign.run(send=True, delay=0)

    assert transport.sent[0][0] == "hi@good.example"
    assert transport.sent[0][1] == "Corporate insurance introduction for Good Ltd"
    with session() as current:
        rows = {r.email: r for r in current.query(Outreach).all()}
    assert rows["nope@bounces.example"].status == OUTREACH_FAILED
    assert "550" in rows["nope@bounces.example"].error

    # A failure is not a send, so that vendor is offered again next run.
    assert [p.company for p in campaign.load_recipients()] == ["Bounces Ltd"]


def test_the_transport_is_closed_even_when_a_send_fails(monkeypatch, written_pitch):
    _vendor("Bounces Ltd", "nope@bounces.example")
    transport = FakeTransport(reject={"nope@bounces.example"})
    monkeypatch.setattr(campaign, "build_transport", lambda name: transport)

    campaign.run(send=True, delay=0)

    assert transport.closed


def test_stop_keeps_the_sends_already_made(fake_transport):
    _vendor("One Ltd", "one@x.example")
    _vendor("Two Ltd", "two@x.example")
    people = campaign.load_recipients()

    def stop() -> bool:
        return len(fake_transport["transport"].sent) >= 1

    campaign.send_all(people, 0, None, should_stop=stop)

    assert [address for address, _subject in fake_transport["transport"].sent] == [
        "one@x.example"
    ]
    with session() as current:
        assert current.query(Outreach).count() == 1


def test_redirecting_records_a_test_and_leaves_the_company_unsent(fake_transport):
    """TO= delivers to the test inbox and does not take the company off the list."""
    vendor_id = _vendor("Kanta Enterprises", "hi@kanta.example")

    campaign.run(send=True, delay=0, to="me@mine.example")

    assert fake_transport["transport"].sent[0][0] == "me@mine.example"
    with session() as current:
        row = current.query(Outreach).one()
        assert row.vendor_id == vendor_id
        assert row.email == "hi@kanta.example"
        assert row.status == OUTREACH_TEST
        assert row.error == "redirected to me@mine.example"
    assert [person.email for person in campaign.load_recipients()] == ["hi@kanta.example"]


def test_a_bad_redirect_sends_nothing(fake_transport):
    _vendor("Kanta Enterprises", "hi@kanta.example")

    with pytest.raises(SystemExit, match="redirect"):
        campaign.run(send=True, delay=0, to="not-an-address")

    assert "transport" not in fake_transport
    with session() as current:
        assert current.query(Outreach).count() == 0


def test_a_pdf_only_company_is_on_the_send_list():
    from pipeline_core.queries import add_contact

    with session() as current:
        vendor_id = upsert_vendor(current, name_raw="Pdf Only Ltd")
        upsert_tender(
            current,
            tender_id="T-PDF",
            title="Work",
            contract_date="01-Jan-2026",
            scraped_at="2020-01-01T00:00:00+00:00",
        )
        replace_awards(
            current,
            "T-PDF",
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Pdf Only Ltd"}],
        )
        add_contact(
            current,
            vendor_id,
            channel="email",
            value="clue@pdf.example",
            source="pdf",
            tender_id="T-PDF",
        )

    assert [person.email for person in campaign.load_recipients()] == ["clue@pdf.example"]


def test_a_newline_in_the_company_name_does_not_stop_the_next_letter(fake_transport):
    _vendor("Bad\nLtd", "bad@x.example")
    _vendor("Good Ltd", "good@x.example")

    campaign.run(send=True, delay=0)

    sent = fake_transport["transport"].sent
    assert [address for address, _subject in sent] == ["bad@x.example", "good@x.example"]
    assert all("\n" not in subject and "\r" not in subject for _address, subject in sent)


def test_a_bad_header_fails_one_recipient_and_the_batch_continues(monkeypatch, written_pitch):
    _vendor("Bad Ltd", "bad@x.example")
    _vendor("Good Ltd", "good@x.example")
    transport = FakeTransport()
    deliver = transport.send

    def send(to, subject, body, attachments=None):
        if to == "bad@x.example":
            raise ValueError("bad header")
        deliver(to, subject, body, attachments)

    transport.send = send
    monkeypatch.setattr(campaign, "build_transport", lambda name: transport)

    campaign.run(send=True, delay=0)

    assert [address for address, _subject in transport.sent] == ["good@x.example"]
    with session() as current:
        rows = {row.email: row.status for row in current.query(Outreach).all()}
    assert rows["bad@x.example"] == OUTREACH_FAILED
    assert rows["good@x.example"] == OUTREACH_SENT


def test_a_missing_mailbox_raises_instead_of_exiting(monkeypatch):
    monkeypatch.setenv("GMAIL_USER", "")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "")
    from gmail.send import GmailTransport

    with pytest.raises(TransportError):
        GmailTransport()

    import graph.auth as auth

    monkeypatch.setattr(auth, "CLIENT_ID", "")
    monkeypatch.setattr(auth, "TENANT_ID", "")
    with pytest.raises(TransportError):
        auth.get_token()


def test_nothing_to_do_is_not_an_error(fake_transport):
    assert campaign.run(send=True, delay=0) == 0
    assert "transport" not in fake_transport


def test_a_live_send_refuses_while_the_pitch_is_still_a_placeholder(monkeypatch):
    """A dry run and a live send build the same text, so nothing else catches this."""
    _vendor("Kanta Enterprises", "hi@kanta.example")
    campaign.load_letter()
    with session() as current:
        row = current.get(MailLetter, 1)
        row.body = campaign.PLACEHOLDER

    built = []
    monkeypatch.setattr(campaign, "build_transport", lambda name: built.append(name))

    with pytest.raises(SystemExit, match="placeholder"):
        campaign.run(send=True, delay=0)

    assert built == []  # refused before a transport was even opened
    with session() as current:
        assert current.query(Outreach).count() == 0


def test_a_dry_run_writes_the_letter_and_both_attachment_names():
    """Preview needs no mailbox. It shows the letter and the two PDF names."""
    _vendor("Kanta Enterprises", "hi@kanta.example")

    assert campaign.run(send=False) == 0
    previews = list(campaign.PREVIEW_DIR.glob("*.txt"))
    assert len(previews) == 1
    text = previews[0].read_text(encoding="utf-8")
    assert "Subject: Corporate insurance introduction for Kanta Enterprises" in text
    assert "Dear Kanta Enterprises team," in text
    assert "Surety Insurance:" in text
    assert "Attachment: PolicyPact_Corporate_Insurance_Pitch.pdf" in text
    assert "Attachment: Surety_Bond_Corporate_Presentation_PolicyPact.pdf" in text
    assert "a person will take you off the list" in text
    assert 'Reply with "unsubscribe"' not in text
    assert campaign.PLACEHOLDER not in text
    assert "This note is about tender" not in text


@pytest.mark.parametrize(
    ("blank", "label"),
    [
        ("SENDER_NAME", "Your name"),
        ("SENDER_DESIGNATION", "Job title"),
        ("SENDER_ORG", "Organisation"),
        ("SENDER_MOBILE", "Mobile"),
        ("SENDER_EMAIL", "Email"),
    ],
)
def test_a_live_send_refuses_until_the_sender_is_set(monkeypatch, written_pitch, blank, label):
    """A signature token with a blank field must not reach a real inbox."""
    _vendor("Kanta Enterprises", "hi@kanta.example")
    monkeypatch.setattr(campaign, blank, "")

    built = []
    monkeypatch.setattr(campaign, "build_transport", lambda name: built.append(name))

    with pytest.raises(SystemExit, match=f"fill in {label}"):
        campaign.run(send=True, delay=0)

    assert built == []
    with session() as current:
        assert current.query(Outreach).count() == 0


def test_a_blank_signature_token_is_filled_as_empty_in_a_preview(monkeypatch):
    monkeypatch.setattr(campaign, "SENDER_NAME", "")
    monkeypatch.setattr(campaign, "SENDER_DESIGNATION", "")
    monkeypatch.setattr(campaign, "SENDER_ORG", "Acme Ltd")
    monkeypatch.setattr(campaign, "SENDER_MOBILE", "")
    monkeypatch.setattr(campaign, "SENDER_EMAIL", "")

    letter = campaign.load_letter()
    _subject, body = campaign.render_letter(letter, "Kanta Enterprises")

    assert "{{sender_name}}" not in body
    assert "{{sender_org}}" not in body
    assert "Acme Ltd" in body
    assert "a person will take you off the list" in body
    assert "Your name" in (campaign.letter_refusal() or "")


def test_a_live_send_refuses_when_an_attachment_is_missing(monkeypatch, written_pitch):
    _vendor("Kanta Enterprises", "hi@kanta.example")
    campaign.load_letter()
    with session() as current:
        row = current.get(MailLetter, 1)
        row.attachment_names = "no-such-pitch.pdf"

    built = []
    monkeypatch.setattr(campaign, "build_transport", lambda name: built.append(name))

    with pytest.raises(SystemExit, match="no-such-pitch.pdf"):
        campaign.run(send=True, delay=0)

    assert built == []


def test_every_valid_address_of_an_award_is_mailed_and_stamped_with_the_run(
    monkeypatch, written_pitch, capsys
):
    """PDF, model and typed addresses each get the letter. Each attempt names its run."""
    from pipeline_core.queries import record_pdf_contacts, set_typed_contact

    vendor_id = _vendor("Three Address Ltd", "model@three.example")
    with session() as current:
        record_pdf_contacts(current, vendor_id, f"T-{vendor_id}", ["-pdf@three.example"], [])
        set_typed_contact(current, vendor_id, email="typed@three.example")
    transport = FakeTransport(reject={"typed@three.example"})
    monkeypatch.setattr(campaign, "build_transport", lambda name: transport)

    people = campaign.load_recipients()
    assert [person.email for person in people] == [
        "pdf@three.example",
        "model@three.example",
        "typed@three.example",
    ]
    campaign.send_all(people, 0, None, job_id=7)

    assert [address for address, _subject in transport.sent] == [
        "pdf@three.example",
        "model@three.example",
    ]
    with session() as current:
        rows = {row.email: row for row in current.query(Outreach)}
    assert {email: (row.status, row.job_id) for email, row in rows.items()} == {
        "pdf@three.example": (OUTREACH_SENT, 7),
        "model@three.example": (OUTREACH_SENT, 7),
        "typed@three.example": (OUTREACH_FAILED, 7),
    }
    printed = capsys.readouterr().out
    assert printed.count(f"tender T-{vendor_id}") == 3
    assert "FAILED Three Address Ltd <typed@three.example>" in printed
    # Only the failed address is still waiting.
    assert [person.email for person in campaign.load_recipients()] == ["typed@three.example"]


def test_a_redirected_test_is_stamped_with_the_run_too(fake_transport):
    _vendor("Kanta Enterprises", "hi@kanta.example")

    campaign.send_all(campaign.load_recipients(), 0, "me@mine.example", job_id=11)

    with session() as current:
        row = current.query(Outreach).one()
    assert (row.status, row.job_id) == (OUTREACH_TEST, 11)


def test_a_terminal_send_leaves_the_run_empty(fake_transport):
    _vendor("Kanta Enterprises", "hi@kanta.example")

    campaign.run(send=True, delay=0)

    with session() as current:
        assert current.query(Outreach).one().job_id is None
