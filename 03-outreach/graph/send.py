"""Send through Microsoft Graph as the signed-in user.

Env:
    GRAPH_CLIENT_ID    # Entra > App registrations > Overview
    GRAPH_TENANT_ID    # same page
    OUTLOOK_USER       # your mailbox, used as the --preflight recipient

No password is stored. graph.auth acquires a Mail.Send token by device
code and caches the refresh token in .msal_token_cache.json.
"""

from __future__ import annotations

import requests

from graph.auth import get_token
from transport import TransportError

SENDMAIL_URL = "https://graph.microsoft.com/v1.0/me/sendMail"


class GraphError(TransportError):
    """Failure for one recipient."""


class GraphTransport:
    name = "graph"

    def __init__(self) -> None:
        self._token = get_token()
        self._session = requests.Session()

    def send(self, to: str, subject: str, body: str) -> None:
        resp = self._session.post(
            SENDMAIL_URL,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            json={
                "message": {
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": [{"emailAddress": {"address": to}}],
                },
                "saveToSentItems": True,
            },
            timeout=30,
        )
        # 202 Accepted = Graph has queued it. Anything else is a failure.
        if resp.status_code == 202:
            return
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 60))
            raise GraphError(f"throttled, Retry-After {wait}s")
        raise GraphError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    def close(self) -> None:
        self._session.close()
