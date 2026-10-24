"""The suite runs as a fresh clone: no network, no real settings, no real files."""

from __future__ import annotations

import os
import smtplib
import socket

import pytest
from pipeline_core import settings

from ops_ui.jobs import pdf_zip_path
from ops_ui.runs import credential_gap, load_campaign, organisation_choices


def test_the_portal_a_model_and_a_mailbox_cannot_be_reached(_no_network):
    with pytest.raises(OSError):
        smtplib.SMTP("smtp.office365.com", 587, timeout=1)
    with socket.socket() as raw, pytest.raises(OSError):
        raw.connect(("192.0.2.1", 443))

    assert _no_network == ["smtp.office365.com", ("192.0.2.1", 443)]
    _no_network.clear()


def test_the_real_settings_and_folders_are_out_of_reach(tmp_path):
    campaign = load_campaign()

    assert os.environ["OPENROUTER_API_KEY"] == ""
    assert credential_gap("outlook") is not None
    assert campaign.ENV_PATH.parent == tmp_path
    assert campaign.PREVIEW_DIR.parent == tmp_path
    assert campaign.ATTACHMENT_DIR.parent == tmp_path
    assert settings.project_root() == (tmp_path / "root").resolve()
    assert tmp_path.resolve() in pdf_zip_path(1).resolve().parents
    assert organisation_choices() == []
