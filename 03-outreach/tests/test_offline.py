"""The suite runs as a fresh clone: no network, no real mailbox, no real files."""

from __future__ import annotations

import os
import smtplib
import socket

import pytest

import campaign


def test_a_mailbox_cannot_be_reached(_no_network):
    with pytest.raises(OSError):
        smtplib.SMTP("smtp.office365.com", 587, timeout=1)
    with socket.socket() as raw, pytest.raises(OSError):
        raw.connect(("192.0.2.1", 465))

    assert _no_network == ["smtp.office365.com", ("192.0.2.1", 465)]
    _no_network.clear()


def test_the_real_settings_and_folders_are_out_of_reach(tmp_path):
    assert os.environ["OUTLOOK_PASSWORD"] == ""
    assert os.environ["GMAIL_APP_PASSWORD"] == ""
    assert campaign.SENDER_NAME == ""
    assert campaign.ENV_PATH.parent == tmp_path
    assert campaign.PREVIEW_DIR.parent == tmp_path
    assert campaign.ATTACHMENT_DIR.parent == tmp_path
    assert sorted(p.name for p in campaign.ATTACHMENT_DIR.iterdir()) == sorted(
        campaign.DEFAULT_ATTACHMENT_NAMES
    )
