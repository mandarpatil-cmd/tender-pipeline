"""Send through Microsoft Graph as the signed-in user.

Env:
    GRAPH_CLIENT_ID    # Entra > App registrations > Overview
    GRAPH_TENANT_ID    # same page
    OUTLOOK_USER       # your mailbox, used as the --preflight recipient

No password is stored. graph.auth acquires a Mail.Send token by device
code and caches the refresh token in .msal_token_cache.json.

Mail.Send is enough to attach files. Each PDF is sent inline on sendMail.
The two campaign files are about 1.2 MB together, under the 3 MB simple
attachment limit, so this does not open a draft or an upload session.
"""

from __future__ import annotations

import base64
import time

import requests

from graph.auth import get_token
from transport import TransportError

SENDMAIL_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
TIMEOUT_SECONDS = 60
RETRY_CAP_SECONDS = 60


class GraphError(TransportError):
    """Failure for one recipient."""


class GraphTransport:
    name = "graph"

    def __init__(self) -> None:
        self._token = get_token()
        self._session = requests.Session()

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> None:
        self._post(to, subject, body, attachments, refreshed=False, throttled=False)

    def _post(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: list[tuple[str, bytes]] | None,
        *,
        refreshed: bool,
        throttled: bool,
    ) -> None:
        message: dict = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        }
        files = _file_attachments(attachments)
        if files:
            message["attachments"] = files
        resp = self._session.post(
            SENDMAIL_URL,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            json={"message": message, "saveToSentItems": True},
            timeout=TIMEOUT_SECONDS,
        )
        # 202 Accepted = Graph has queued it. Anything else is a failure.
        if resp.status_code == 202:
            return
        if resp.status_code == 401 and not refreshed:
            self._token = get_token()
            self._post(to, subject, body, attachments, refreshed=True, throttled=throttled)
            return
        if resp.status_code == 429 and not throttled:
            time.sleep(_retry_after(resp))
            self._post(to, subject, body, attachments, refreshed=refreshed, throttled=True)
            return
        if resp.status_code == 429:
            wait = _retry_after(resp)
            raise GraphError(f"throttled, Retry-After {wait}s")
        raise GraphError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    def close(self) -> None:
        self._session.close()


def _file_attachments(attachments: list[tuple[str, bytes]] | None) -> list[dict]:
    files = []
    for name, content in attachments or []:
        files.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name,
                "contentType": "application/pdf",
                "contentBytes": base64.b64encode(content).decode("ascii"),
            }
        )
    return files


def _retry_after(resp: requests.Response) -> int:
    raw = resp.headers.get("Retry-After", str(RETRY_CAP_SECONDS))
    try:
        wait = int(raw)
    except (TypeError, ValueError):
        wait = RETRY_CAP_SECONDS
    return max(0, min(wait, RETRY_CAP_SECONDS))
