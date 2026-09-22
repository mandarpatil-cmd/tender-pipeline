"""Send through Gmail SMTP using an app password.

Env:
    GMAIL_USER
    GMAIL_APP_PASSWORD   # https://myaccount.google.com/apppasswords  (2FA required)
"""

from __future__ import annotations

import os
import smtplib
import sys
from email.message import EmailMessage

from dotenv import load_dotenv

from pathlib import Path

from transport import TransportError

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


class GmailError(TransportError):
    """Failure for one recipient."""


class GmailTransport:
    name = "gmail"

    def __init__(self) -> None:
        user = os.getenv("GMAIL_USER", "").strip()
        password = os.getenv("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
        if not user or not password:
            sys.exit(
                "Missing GMAIL_USER or GMAIL_APP_PASSWORD in .env\n"
                "Use a Gmail app password (Google Account → Security → App passwords),\n"
                "not your normal Google password."
            )
        self._user = user
        self._password = password
        self._server = self._connect()

    def _connect(self) -> smtplib.SMTP_SSL:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
        server.login(self._user, self._password)
        return server

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self._user
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            self._server.send_message(msg)
        except smtplib.SMTPServerDisconnected:
            print("  connection dropped, reconnecting ...")
            self._server = self._connect()
            self._server.send_message(msg)
        except smtplib.SMTPException as err:
            raise GmailError(str(err)) from err

    def close(self) -> None:
        try:
            self._server.quit()
        except smtplib.SMTPException:
            pass
