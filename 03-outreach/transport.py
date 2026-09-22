"""The transport contract, and the one place that picks between them.

A transport is any object with:

    __init__(self)                         connect / authenticate
    send(self, to, subject, body) -> None  deliver one message
    close(self)                            tear down

and which raises TransportError when a single recipient fails. Failing that
way lets campaign.py log the address and carry on to the next person.

Imports are deliberately inside build_transport: choosing "gmail" never
loads msal or requests, and choosing "graph" never opens smtplib.
"""

from __future__ import annotations

TRANSPORTS = ("gmail", "graph")


class TransportError(Exception):
    """Delivery failed for one recipient. The run continues."""


def build_transport(name: str):
    if name == "gmail":
        from gmail.send import GmailTransport

        return GmailTransport()
    if name == "graph":
        from graph.send import GraphTransport

        return GraphTransport()
    raise ValueError(f"unknown transport {name!r} — choose one of {TRANSPORTS}")
