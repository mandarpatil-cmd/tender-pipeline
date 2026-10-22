"""Send through Microsoft 365 SMTP with the mailbox address and password.

Env:
    OUTLOOK_USER       # the mailbox, also the From address and the --preflight recipient
    OUTLOOK_PASSWORD   # its password, or an app password if the account has MFA

The tenant must allow SMTP AUTH for this mailbox. Microsoft plans to turn
password SMTP off by default for existing tenants at the end of December 2026
(an admin can turn it back on). If it is blocked, use TRANSPORT = "graph".
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

from transport import TransportError

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SMTP_HOST = "smtp.office365.com"
SMTP_PORT = 587


class OutlookError(TransportError):
    """Failure for one recipient."""


def explain(text: str) -> str:
    """Translate the Exchange Online sign-in refusals you are actually likely to hit."""
    lowered = text.lower()
    if "smtpclientauthentication is disabled" in lowered:
        return (
            "SMTP AUTH IS OFF for this tenant or mailbox.\n"
            "  A Microsoft 365 admin must turn on Authenticated SMTP for the user:\n"
            "  admin centre > Users > Active users > the user > Mail > Manage email apps,\n"
            "  or run: Set-CASMailbox <user> -SmtpClientAuthenticationDisabled $false"
        )
    if "security defaults" in lowered:
        return (
            "BLOCKED BY SECURITY DEFAULTS -- they forbid password sign-in over SMTP.\n"
            "  An admin must turn off Security defaults in Entra, or switch to\n"
            '  TRANSPORT = "graph".'
        )
    if "5.7.30" in lowered or "basic authentication is not supported" in lowered:
        return (
            "PASSWORD SMTP IS RETIRED for this tenant.\n"
            '  Microsoft no longer accepts it here. Use TRANSPORT = "graph".'
        )
    if "basic authentication is disabled" in lowered or "did not meet the criteria" in lowered:
        return (
            "BLOCKED BY POLICY -- the tenant does not allow password sign-in over SMTP.\n"
            "  An admin must allow it (Conditional Access / authentication policies),\n"
            '  or switch to TRANSPORT = "graph".'
        )
    if "authentication unsuccessful" in lowered or "5.7.3" in lowered:
        return (
            "WRONG PASSWORD, or the account has MFA.\n"
            "  Check OUTLOOK_USER and OUTLOOK_PASSWORD in .env. With MFA on,\n"
            "  OUTLOOK_PASSWORD must be an app password, not your normal password."
        )
    return ""


class OutlookTransport:
    name = "outlook"

    def __init__(self) -> None:
        user = os.getenv("OUTLOOK_USER", "").strip()
        password = os.getenv("OUTLOOK_PASSWORD", "")
        if not user or not password:
            raise OutlookError(
                "Missing OUTLOOK_USER or OUTLOOK_PASSWORD in .env\n"
                "Use the Microsoft 365 mailbox address and its password\n"
                "(an app password if the account has MFA)."
            )
        self._user = user
        self._password = password
        self._server = self._connect()

    def _connect(self) -> smtplib.SMTP:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        try:
            server.starttls(context=ssl.create_default_context())
            server.login(self._user, self._password)
        except smtplib.SMTPAuthenticationError as err:
            server.close()
            detail = err.smtp_error
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", "replace")
            hint = explain(detail)
            message = f"Outlook sign-in failed: {err.smtp_code} {detail}"
            raise OutlookError(f"{message}\n{hint}" if hint else message) from err
        return server

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> None:
        msg = EmailMessage()
        # Exchange rejects any From other than the signed-in mailbox.
        msg["From"] = self._user
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        for name, content in attachments or []:
            msg.add_attachment(content, maintype="application", subtype="pdf", filename=name)
        try:
            self._server.send_message(msg)
        except smtplib.SMTPServerDisconnected:
            print("  connection dropped, reconnecting ...")
            self._server = self._connect()
            self._server.send_message(msg)
        except smtplib.SMTPException as err:
            raise OutlookError(str(err)) from err

    def close(self) -> None:
        try:
            self._server.quit()
        except smtplib.SMTPException:
            pass
