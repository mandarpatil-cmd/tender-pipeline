"""Which mailbox is used, when a live send must not start, and the preflight."""

from __future__ import annotations

import pytest
from pipeline_core.db import session
from pipeline_core.models import Outreach

import campaign
from transport import build_transport

_SIGNED = dict(
    sender_name="Ada Lovelace",
    sender_designation="Director",
    sender_org="Policy Pact",
    sender_mobile="9000000000",
    sender_email="ada@policypact.example",
)


def _letter(**changes) -> campaign.Letter:
    values = dict(
        subject="Introduction for {{company}}",
        body="Dear {{company}} team,\nRegards, {{sender_name}}",
        attachment_names=campaign.DEFAULT_ATTACHMENT_NAMES,
        **_SIGNED,
    )
    values.update(changes)
    return campaign.Letter(**values)


class FakeTransport:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.sent: list[tuple[str, str, int]] = []
        self.closed = False

    def send(self, to, subject, body, attachments=None):
        if self.fail:
            raise self.fail
        self.sent.append((to, subject, len(attachments or [])))

    def close(self):
        self.closed = True


def test_an_unknown_transport_is_refused_by_name():
    with pytest.raises(ValueError, match="unknown transport 'smtp'"):
        build_transport("smtp")


def test_each_name_builds_its_own_mailbox(monkeypatch):
    class Server:
        def starttls(self, context=None): ...
        def login(self, user, password): ...
        def quit(self): ...

    monkeypatch.setenv("GMAIL_USER", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd")
    monkeypatch.setenv("OUTLOOK_USER", "me@company.example")
    monkeypatch.setenv("OUTLOOK_PASSWORD", "secret")
    monkeypatch.setattr("smtplib.SMTP_SSL", lambda *a, **k: Server())
    monkeypatch.setattr("smtplib.SMTP", lambda *a, **k: Server())
    monkeypatch.setattr("graph.send.get_token", lambda: "token")

    assert {name: build_transport(name).name for name in ("gmail", "outlook", "graph")} == {
        "gmail": "gmail",
        "outlook": "outlook",
        "graph": "graph",
    }


def test_a_complete_letter_with_its_pdfs_may_be_sent():
    assert campaign.refusal_for(_letter()) is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"subject": "  "}, "Write a subject."),
        ({"body": ""}, "Write the letter."),
        ({"body": "Hi {{nickname}}"}, "nickname"),
        ({"body": f"Hi {campaign.PLACEHOLDER}"}, "placeholder"),
        ({"sender_org": "", "body": "Regards, {{sender_name}}, {{sender_org}}"}, "Organisation"),
        ({"attachment_names": ()}, "add at least one PDF"),
        ({"attachment_names": ("gone.pdf",)}, "missing: gone.pdf"),
    ],
)
def test_a_live_send_is_refused_and_says_why(changes, reason):
    problem = campaign.refusal_for(_letter(**changes))

    assert problem is not None and reason in problem


def test_the_program_letter_points_at_the_attachments_folder():
    problem = campaign.refusal_for(_letter(attachment_names=()), edited_on="in 03-outreach/.env")

    assert "03-outreach/attachments/" in problem


def test_a_pdf_over_the_limit_refuses_the_send(monkeypatch):
    monkeypatch.setattr(campaign, "MAX_PDF_BYTES", 10)

    assert "over 2 MB" in campaign.refusal_for(_letter())


def test_pdfs_over_the_total_limit_refuse_the_send(monkeypatch):
    monkeypatch.setattr(campaign, "MAX_TOTAL_BYTES", 1_000_000)

    assert "together are over 2.5 MB" in campaign.refusal_for(_letter())


def test_a_preview_clears_the_last_previews_and_nothing_else():
    campaign.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    (campaign.PREVIEW_DIR / "001_Old_Company.txt").write_text("old", encoding="utf-8")
    (campaign.PREVIEW_DIR / "notes.md").write_text("keep", encoding="utf-8")

    campaign.dry_run([], None, letter=_letter())

    assert sorted(p.name for p in campaign.PREVIEW_DIR.iterdir()) == ["notes.md"]


def test_a_preflight_without_its_own_address_stops_before_connecting(monkeypatch, capsys):
    built = []
    monkeypatch.setattr(campaign, "build_transport", lambda name: built.append(name))

    assert campaign.preflight("gmail") == 1
    assert campaign.preflight("graph") == 1

    assert built == []
    assert "OUTLOOK_USER" in capsys.readouterr().err


def test_a_preflight_mails_yourself_with_the_pdfs_and_records_nothing(monkeypatch):
    monkeypatch.setenv("OUTLOOK_USER", "me@company.example")
    transport = FakeTransport()
    monkeypatch.setattr(campaign, "build_transport", lambda name: transport)

    assert campaign.preflight("outlook") == 0

    assert transport.sent == [("me@company.example", "Outlook preflight - SMTP send test", 2)]
    assert transport.closed
    with session() as current:
        assert current.query(Outreach).count() == 0


def test_a_preflight_that_fails_says_so_and_closes_the_mailbox(monkeypatch, capsys):
    monkeypatch.setenv("GMAIL_USER", "me@gmail.com")
    transport = FakeTransport(fail=RuntimeError("535 not accepted"))
    monkeypatch.setattr(campaign, "build_transport", lambda name: transport)

    assert campaign.preflight("gmail") == 1

    assert transport.closed
    assert "FAILED - 535 not accepted" in capsys.readouterr().err
