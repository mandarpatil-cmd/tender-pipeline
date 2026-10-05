"""The Gmail message, checked without a mailbox or a network call."""

from __future__ import annotations

import smtplib

from gmail.send import GmailTransport


class FakeSMTP:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list = []

    def login(self, user: str, password: str) -> None:
        return None

    def send_message(self, msg) -> None:
        if self.fail:
            raise smtplib.SMTPServerDisconnected("dropped")
        self.sent.append(msg)

    def quit(self) -> None:
        return None


def _transport(monkeypatch, servers: list[FakeSMTP]) -> GmailTransport:
    monkeypatch.setenv("GMAIL_USER", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    pending = list(servers)

    def connect(*_args, **_kwargs):
        return pending.pop(0)

    monkeypatch.setattr("gmail.send.smtplib.SMTP_SSL", connect)
    return GmailTransport()


def test_send_attaches_both_pdfs(monkeypatch):
    server = FakeSMTP()
    transport = _transport(monkeypatch, [server])

    transport.send(
        "a@b.example",
        "Corporate insurance introduction for Acme",
        "Dear Acme team,",
        attachments=[("one.pdf", b"%PDF-1"), ("two.pdf", b"%PDF-2")],
    )

    msg = server.sent[0]
    assert msg["From"] == "me@gmail.com"
    assert msg["To"] == "a@b.example"
    assert msg["Subject"] == "Corporate insurance introduction for Acme"
    attached = list(msg.iter_attachments())
    assert [part.get_filename() for part in attached] == ["one.pdf", "two.pdf"]
    assert attached[0].get_content_type() == "application/pdf"
    assert attached[0].get_payload(decode=True) == b"%PDF-1"


def test_a_dropped_connection_resends_the_same_message(monkeypatch):
    first = FakeSMTP(fail=True)
    second = FakeSMTP()
    transport = _transport(monkeypatch, [first, second])

    transport.send(
        "a@b.example",
        "Subject",
        "Body",
        attachments=[("one.pdf", b"AAA"), ("two.pdf", b"BBB")],
    )

    assert first.sent == []
    msg = second.sent[0]
    assert [part.get_filename() for part in msg.iter_attachments()] == ["one.pdf", "two.pdf"]
    assert list(msg.iter_attachments())[1].get_payload(decode=True) == b"BBB"
