"""Stage 3's contract: mail each vendor once, and never twice."""

from __future__ import annotations

import pytest
from pipeline_core.db import session
from pipeline_core.models import OUTREACH_FAILED, OUTREACH_SENT, Outreach
from pipeline_core.queries import mark_enriched, upsert_vendor

import campaign
from transport import TransportError


class FakeTransport:
    """Records deliveries instead of making them. Optionally rejects some."""

    def __init__(self, reject: set[str] | None = None):
        self.reject = reject or set()
        self.sent: list[tuple[str, str]] = []
        self.closed = False

    def send(self, to: str, subject: str, body: str) -> None:
        if to in self.reject:
            raise TransportError(f"550 mailbox unavailable: {to}")
        self.sent.append((to, subject))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def written_pitch(monkeypatch):
    """Stand in for a pitch someone has actually written, and a sender set in .env.

    These tests are about the queue and the log, not about the copy, so they
    must not hit the placeholder or sender guards in `send_all`. The guards
    have their own tests at the bottom of this file.
    """
    monkeypatch.setattr(
        campaign, "build_body", lambda company: f"Hello {company} team,\n\nA real pitch.\n"
    )
    monkeypatch.setattr(campaign, "SENDER_NAME", "Test Sender")
    monkeypatch.setattr(campaign, "SENDER_ORG", "Test Org")


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
    _vendor("Kanta Enterprises", "hi@kanta.example")

    campaign.run(send=True, delay=0)

    assert fake_transport["transport"].sent == [
        ("hi@kanta.example", "Enquiry for Kanta Enterprises")
    ]
    with session() as current:
        row = current.query(Outreach).one()
        assert row.status == OUTREACH_SENT
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

    assert transport.sent == [("hi@good.example", "Enquiry for Good Ltd")]
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

    built = []
    monkeypatch.setattr(campaign, "build_transport", lambda name: built.append(name))

    with pytest.raises(SystemExit, match="placeholder"):
        campaign.run(send=True, delay=0)

    assert built == []  # refused before a transport was even opened
    with session() as current:
        assert current.query(Outreach).count() == 0


def test_previews_still_work_while_the_pitch_is_a_placeholder():
    """The guard blocks live sends only -- you can still review the copy."""
    _vendor("Kanta Enterprises", "hi@kanta.example")

    assert campaign.run(send=False) == 0
    assert len(list(campaign.PREVIEW_DIR.glob("*.txt"))) == 1


@pytest.mark.parametrize("blank", ["SENDER_NAME", "SENDER_ORG"])
def test_a_live_send_refuses_until_the_sender_is_set(monkeypatch, written_pitch, blank):
    """The signature comes from .env, so a blank one must not reach a real inbox."""
    _vendor("Kanta Enterprises", "hi@kanta.example")
    monkeypatch.setattr(campaign, blank, "")

    built = []
    monkeypatch.setattr(campaign, "build_transport", lambda name: built.append(name))

    with pytest.raises(SystemExit, match="SENDER_NAME and SENDER_ORG"):
        campaign.run(send=True, delay=0)

    assert built == []
    with session() as current:
        assert current.query(Outreach).count() == 0


def test_previews_show_where_an_unset_sender_will_appear(monkeypatch):
    monkeypatch.setattr(campaign, "SENDER_NAME", "")
    monkeypatch.setattr(campaign, "SENDER_ORG", "Acme Ltd")

    body = campaign.build_body("Kanta Enterprises")

    assert "<SENDER_NAME from .env>" in body
    assert "Acme Ltd" in body
