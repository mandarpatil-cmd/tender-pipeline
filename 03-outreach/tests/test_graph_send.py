"""The Outlook payload, checked without a mailbox or a network call."""

from __future__ import annotations

import base64

import pytest

from graph.send import RETRY_CAP_SECONDS, TIMEOUT_SECONDS, GraphError, GraphTransport


class FakeResponse:
    def __init__(self, status: int, headers: dict | None = None, text: str = ""):
        self.status_code = status
        self.headers = headers or {}
        self.text = text


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.closed = False

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _transport(monkeypatch, responses: list[FakeResponse], tokens: list[str] | None = None):
    session = FakeSession(responses)
    issued = iter(tokens or ["token"])
    monkeypatch.setattr("graph.send.get_token", lambda: next(issued))
    monkeypatch.setattr("graph.send.requests.Session", lambda: session)
    return GraphTransport(), session


def test_send_includes_both_pdfs_and_saves_to_sent_items(monkeypatch):
    transport, session = _transport(monkeypatch, [FakeResponse(202)])

    transport.send(
        "a@b.example",
        "Corporate insurance introduction for Acme",
        "Dear Acme team,",
        attachments=[("one.pdf", b"%PDF-1"), ("two.pdf", b"%PDF-2")],
    )

    call = session.calls[0]
    assert call["timeout"] == TIMEOUT_SECONDS
    payload = call["json"]
    assert payload["saveToSentItems"] is True
    message = payload["message"]
    assert message["subject"] == "Corporate insurance introduction for Acme"
    assert message["body"] == {"contentType": "Text", "content": "Dear Acme team,"}
    files = message["attachments"]
    assert [item["name"] for item in files] == ["one.pdf", "two.pdf"]
    assert files[0]["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert files[0]["contentType"] == "application/pdf"
    assert files[0]["contentBytes"] == base64.b64encode(b"%PDF-1").decode("ascii")
    assert files[1]["contentBytes"] == base64.b64encode(b"%PDF-2").decode("ascii")


def test_a_dead_token_is_refreshed_once(monkeypatch):
    transport, session = _transport(
        monkeypatch,
        [FakeResponse(401, text="expired"), FakeResponse(202)],
        tokens=["old", "new"],
    )

    transport.send("a@b.example", "Subject", "Body", attachments=[("one.pdf", b"x")])

    assert len(session.calls) == 2
    assert session.calls[0]["headers"]["Authorization"] == "Bearer old"
    assert session.calls[1]["headers"]["Authorization"] == "Bearer new"
    assert session.calls[1]["json"]["message"]["attachments"][0]["name"] == "one.pdf"


def test_a_second_401_is_a_failure(monkeypatch):
    transport, session = _transport(
        monkeypatch,
        [FakeResponse(401, text="expired"), FakeResponse(401, text="still dead")],
        tokens=["old", "new"],
    )

    with pytest.raises(GraphError, match="401"):
        transport.send("a@b.example", "Subject", "Body")

    assert len(session.calls) == 2


def test_a_throttle_waits_once_then_sends(monkeypatch):
    slept: list[int] = []
    monkeypatch.setattr("graph.send.time.sleep", lambda seconds: slept.append(seconds))
    transport, session = _transport(
        monkeypatch,
        [FakeResponse(429, headers={"Retry-After": "120"}), FakeResponse(202)],
    )

    transport.send("a@b.example", "Subject", "Body", attachments=[("one.pdf", b"x")])

    assert slept == [RETRY_CAP_SECONDS]
    assert len(session.calls) == 2


def test_a_second_throttle_fails_the_recipient(monkeypatch):
    monkeypatch.setattr("graph.send.time.sleep", lambda seconds: None)
    transport, _session = _transport(
        monkeypatch,
        [
            FakeResponse(429, headers={"Retry-After": "5"}),
            FakeResponse(429, headers={"Retry-After": "9"}),
        ],
    )

    with pytest.raises(GraphError, match="throttled"):
        transport.send("a@b.example", "Subject", "Body")
