"""The Outlook SMTP message, checked without a mailbox or a network call."""

from __future__ import annotations

import smtplib

import pytest

from outlook.send import OutlookError, OutlookTransport, explain


class FakeSMTP:
    def __init__(self, fail: bool = False, refuse_login: bytes | None = None):
        self.fail = fail
        self.refuse_login = refuse_login
        self.calls: list[str] = []
        self.sent: list = []

    def starttls(self, context=None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append("login")
        if self.refuse_login is not None:
            raise smtplib.SMTPAuthenticationError(535, self.refuse_login)

    def send_message(self, msg) -> None:
        if self.fail:
            raise smtplib.SMTPServerDisconnected("dropped")
        self.sent.append(msg)

    def quit(self) -> None:
        return None

    def close(self) -> None:
        return None


def _transport(monkeypatch, servers: list[FakeSMTP]) -> OutlookTransport:
    monkeypatch.setenv("OUTLOOK_USER", "me@company.example")
    monkeypatch.setenv("OUTLOOK_PASSWORD", "secret")
    pending = list(servers)

    def connect(*_args, **_kwargs):
        return pending.pop(0)

    monkeypatch.setattr("outlook.send.smtplib.SMTP", connect)
    return OutlookTransport()


def test_send_attaches_both_pdfs_after_starttls(monkeypatch):
    server = FakeSMTP()
    transport = _transport(monkeypatch, [server])

    transport.send(
        "a@b.example",
        "Corporate insurance introduction for Acme",
        "Dear Acme team,",
        attachments=[("one.pdf", b"%PDF-1"), ("two.pdf", b"%PDF-2")],
    )

    assert server.calls == ["starttls", "login"]
    msg = server.sent[0]
    assert msg["From"] == "me@company.example"
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
    assert second.calls == ["starttls", "login"]
    msg = second.sent[0]
    assert [part.get_filename() for part in msg.iter_attachments()] == ["one.pdf", "two.pdf"]
    assert list(msg.iter_attachments())[1].get_payload(decode=True) == b"BBB"


def test_a_refused_sign_in_says_which_setting_to_change(monkeypatch):
    refused = FakeSMTP(
        refuse_login=b"5.7.139 Authentication unsuccessful, SmtpClientAuthentication "
        b"is disabled for the Tenant. Visit https://aka.ms/smtp_auth_disabled"
    )

    with pytest.raises(OutlookError, match="Authenticated SMTP") as caught:
        _transport(monkeypatch, [refused])

    assert "535" in str(caught.value)


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        ("5.7.139 Authentication unsuccessful, user is locked by your organization's "
         "security defaults policy.", "SECURITY DEFAULTS"),
        ("5.7.30 Basic authentication is not supported for Client Submission.", "RETIRED"),
        ("5.7.139 Authentication unsuccessful, basic authentication is disabled.", "POLICY"),
        ("5.7.3 Authentication unsuccessful", "WRONG PASSWORD"),
        ("something else entirely", ""),
    ],
)
def test_explain_names_the_cause(text, hint):
    result = explain(text)
    assert hint in result
    if not hint:
        assert result == ""
