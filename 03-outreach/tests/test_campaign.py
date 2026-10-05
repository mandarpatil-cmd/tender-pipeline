"""Stage 3's contract: mail each vendor once, and never twice."""

from __future__ import annotations

import pytest
from pipeline_core.db import session
from pipeline_core.models import OUTREACH_FAILED, OUTREACH_SENT, MailLetter, Outreach
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


def test_redirecting_still_records_the_real_vendor(fake_transport):
    """TO= sends everything to you, but the vendor is still marked as done."""
    vendor_id = _vendor("Kanta Enterprises", "hi@kanta.example")

    campaign.run(send=True, delay=0, to="me@mine.example")

    assert fake_transport["transport"].sent[0][0] == "me@mine.example"
    with session() as current:
        row = current.query(Outreach).one()
        assert row.vendor_id == vendor_id
        assert row.email == "me@mine.example"
    assert campaign.load_recipients() == []


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
    assert "unsubscribe" in text
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
    assert "unsubscribe" in body
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
