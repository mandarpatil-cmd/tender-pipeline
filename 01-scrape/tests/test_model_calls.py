"""OpenRouter and portal answers that go wrong become clear errors, never a crash."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
import requests

from stage1_scrape.domain.errors import CaptchaError, DocumentError
from stage1_scrape.domain.models import DocumentLink
from stage1_scrape.persist import document_extract
from stage1_scrape.persist.documents import download_document
from stage1_scrape.scraping import ocr
from stage1_scrape.scraping.captcha import extract_captcha_bytes
from stage1_scrape.settings import OpenRouterEndpoint

ENDPOINT = OpenRouterEndpoint(api_key="sk-test", base_url="https://openrouter.example/api/v1")


class Answer:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} error")


def _vision(monkeypatch, answer):
    def post(url, headers, json, timeout):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(ocr.requests, "post", post)
    return ocr._vision(ENDPOINT, "model", "data:image/png;base64,AA==", "read it", 5)


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (requests.ConnectionError("dns down"), "request failed"),
        (Answer(401), "rejected the API key"),
        (Answer(500, text="upstream down"), "500: upstream down"),
        (Answer(200, {"choices": []}), "no message content"),
        (Answer(200, ValueError("not json")), "no message content"),
    ],
)
def test_an_ocr_failure_is_a_captcha_error(monkeypatch, answer, message):
    with pytest.raises(CaptchaError, match=message):
        _vision(monkeypatch, answer)


def test_an_ocr_answer_in_parts_is_joined(monkeypatch):
    content = [{"type": "text", "text": "AB"}, "C1", {"type": "text", "text": "23"}]

    assert _vision(monkeypatch, Answer(200, {"choices": [{"message": {"content": content}}]})) == "ABC123"


def test_the_document_model_needs_a_key():
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        document_extract.fetch_document_extract("work order text")


def test_a_document_model_error_leaves_the_clues_empty(monkeypatch, caplog):
    monkeypatch.setattr(document_extract, "openrouter_endpoint", lambda: ENDPOINT)
    monkeypatch.setattr(document_extract.requests, "post", lambda *a, **k: Answer(502))
    extracts = [{"filename": "wo.pdf", "text": "Work order", "emails": [], "phones": [], "gstins": []}]

    document_extract.apply_document_extracts(extracts)

    assert extracts[0]["emails"] == [] and extracts[0]["gstins"] == []
    assert "Document extract failed for wo.pdf" in caplog.text


def test_a_document_answer_in_parts_is_read(monkeypatch):
    monkeypatch.setattr(document_extract, "openrouter_endpoint", lambda: ENDPOINT)
    parts = [{"type": "text", "text": '{"email": "office@winner.example", '}, '"phone": null}']
    monkeypatch.setattr(
        document_extract.requests,
        "post",
        lambda *a, **k: Answer(200, {"choices": [{"message": {"content": parts}}]}),
    )

    found = document_extract.fetch_document_extract("Work order")

    assert found.email == "office@winner.example"


def _page(src: str) -> str:
    return f'<form><img id="captchaImage" src="{src}"></form>'


def test_an_inline_captcha_image_is_decoded_with_its_kind():
    data = base64.b64encode(b"\xff\xd8jpeg").decode()

    assert extract_captcha_bytes(_page(f"data:image/jpeg;base64,{data}")) == (b"\xff\xd8jpeg", "jpg")


@pytest.mark.parametrize(
    ("html", "message"),
    [
        ("<form></form>", "was not found"),
        (_page("/captcha.png"), "not an inline data URI"),
    ],
)
def test_a_changed_captcha_image_is_a_captcha_error(html, message):
    with pytest.raises(CaptchaError, match=message):
        extract_captcha_bytes(html)


def test_a_download_that_is_neither_pdf_nor_html_is_a_document_error(tmp_path):
    class Client:
        def get(self, url):
            return SimpleNamespace(headers={"Content-Type": "application/octet-stream"}, content=b"PK\x03\x04zip")

    doc = DocumentLink(label="Work order", filename="wo.pdf", url="https://eprocure.gov.in/x")

    with pytest.raises(DocumentError, match="did not look like a PDF"):
        download_document(Client(), doc, tmp_path)
    assert not (tmp_path / "wo.pdf").exists()
