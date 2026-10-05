"""The saved letter: one row, filled in per company, edited without sending."""

from __future__ import annotations

from pipeline_core.db import session
from pipeline_core.models import MailLetter, Outreach

import campaign


def _save(**overrides: str) -> str | None:
    fields = {
        "subject": "Hello {{company}}",
        "body": "Dear {{company}} team,\n\nFrom {{sender_name}}.",
        "sender_name": "Ada Lovelace",
        "sender_designation": "Director",
        "sender_org": "Policy Pact Insurance Broker",
        "sender_mobile": "9000000000",
        "sender_email": "ada@example.com",
    }
    fields.update(overrides)
    return campaign.save_letter(**fields)


def test_an_empty_database_seeds_todays_letter_and_the_existing_pdfs():
    letter = campaign.load_letter()

    assert letter.subject == "Corporate insurance introduction for {{company}}"
    assert "Dear {{company}} team," in letter.body
    assert "Surety Insurance:" in letter.body
    assert 'Reply with "unsubscribe"' not in letter.body
    assert letter.attachment_names == (
        "PolicyPact_Corporate_Insurance_Pitch.pdf",
        "Surety_Bond_Corporate_Presentation_PolicyPact.pdf",
    )
    again = campaign.load_letter()
    assert again.subject == letter.subject
    with session() as current:
        assert current.query(MailLetter).count() == 1


def test_saving_a_letter_fills_company_and_signature_and_adds_unsubscribe():
    assert _save() is None

    letter = campaign.load_letter()
    subject, body = campaign.render_letter(letter, "Kanta Enterprises")

    assert subject == "Hello Kanta Enterprises"
    assert "Dear Kanta Enterprises team," in body
    assert "Ada Lovelace" in body
    assert "{{" not in subject
    assert "{{" not in body
    assert 'Reply with "unsubscribe"' in body
    with session() as current:
        assert current.query(Outreach).count() == 0


def test_unsubscribe_is_added_even_when_the_saved_letter_omits_it():
    assert _save(body="Just a note for {{company}}.") is None

    _subject, body = campaign.render_letter(campaign.load_letter(), "Acme")

    assert body.endswith(campaign.UNSUBSCRIBE)
    assert body.count('Reply with "unsubscribe"') == 1


def test_an_unknown_token_is_refused_and_not_saved():
    campaign.load_letter()

    error = _save(body="Hello {{cxo}}")

    assert error is not None
    assert "{{cxo}}" in error
    assert "{{cxo}}" not in campaign.load_letter().body
    with session() as current:
        assert current.query(Outreach).count() == 0


def test_a_blank_signature_token_is_refused():
    error = _save(sender_name="", body="Dear {{sender_name}}")

    assert error is not None
    assert "Your name" in error
    with session() as current:
        assert current.query(Outreach).count() == 0


def test_a_non_pdf_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(campaign, "ATTACHMENT_DIR", tmp_path / "files")

    error = campaign.add_attachment("notes.txt", b"not a pdf")

    assert error == "That file is not a PDF."
    assert not (tmp_path / "files").exists()
    with session() as current:
        assert current.query(Outreach).count() == 0


def test_an_oversized_pdf_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(campaign, "ATTACHMENT_DIR", tmp_path / "files")
    monkeypatch.setattr(campaign, "MAX_PDF_BYTES", 8)

    error = campaign.add_attachment("big.pdf", b"%PDF-1.4\n" + b"x" * 20)

    assert error is not None
    assert "2 MB" in error
    assert not (tmp_path / "files").exists() or list((tmp_path / "files").glob("*")) == []
    with session() as current:
        assert current.query(Outreach).count() == 0


def test_adding_and_removing_a_pdf_updates_the_letter(monkeypatch, tmp_path):
    folder = tmp_path / "files"
    monkeypatch.setattr(campaign, "ATTACHMENT_DIR", folder)
    campaign.load_letter()

    assert campaign.add_attachment("pitch.pdf", b"%PDF-1.4\nhello") is None
    assert campaign.load_letter().attachment_names == ("pitch.pdf",)
    assert (folder / "pitch.pdf").read_bytes().startswith(b"%PDF")

    assert campaign.remove_attachment("pitch.pdf") is None
    assert campaign.load_letter().attachment_names == ()
    assert not (folder / "pitch.pdf").exists()
