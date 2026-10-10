"""After automatic captcha tries are rejected, the window can type the next one."""

from unittest.mock import MagicMock

import pytest

from stage1_scrape.app import pipeline
from stage1_scrape.domain.errors import CaptchaError
from stage1_scrape.persist.store import Store


def _save(html, dest):
    path = dest.with_suffix(".png")
    path.write_bytes(b"png")
    return path


def test_rejected_reads_then_ask_for_a_typed_captcha(tmp_path, monkeypatch):
    store = Store(tmp_path)
    client = MagicMock()
    client.fetch_search_form.return_value = "<form></form>"
    submitted: list[str] = []

    def resolve(image, text, *, solver, ask=None):
        if solver == "manual":
            assert ask is not None
            return ask(image)
        assert ask is None
        assert solver == "openrouter"
        return "OCR123"

    def submit(client, html, code, **kwargs):
        submitted.append(code)
        if code == "TYPED1":
            return "<html>results</html>"
        return "<html>captcha still</html>"

    monkeypatch.setattr(pipeline, "resolve_captcha_code", resolve)
    monkeypatch.setattr(pipeline, "save_captcha", _save)
    monkeypatch.setattr(pipeline, "submit_search", submit)
    monkeypatch.setattr(
        pipeline, "search_still_showing_form", lambda html: "captcha still" in html
    )
    monkeypatch.setattr(pipeline, "parse_results_table", lambda html: [object()])

    html = pipeline._search_with_captcha(
        client,
        store,
        None,
        retries=2,
        captcha_solver="openrouter",
        ask=lambda path: "TYPED1",
    )

    assert submitted == ["OCR123", "OCR123", "TYPED1"]
    assert html == "<html>results</html>"
    assert client.fetch_search_form.call_count == 3


def test_a_rejected_typed_captcha_asks_again(tmp_path, monkeypatch):
    store = Store(tmp_path)
    client = MagicMock()
    client.fetch_search_form.return_value = "<form></form>"
    submitted: list[str] = []
    typed = iter(("BAD123", "GOOD99"))

    def resolve(image, text, *, solver, ask=None):
        if solver == "manual":
            return ask(image)
        return "OCR123"

    def submit(client, html, code, **kwargs):
        submitted.append(code)
        if code == "GOOD99":
            return "<html>results</html>"
        return "<html>captcha still</html>"

    monkeypatch.setattr(pipeline, "resolve_captcha_code", resolve)
    monkeypatch.setattr(pipeline, "save_captcha", _save)
    monkeypatch.setattr(pipeline, "submit_search", submit)
    monkeypatch.setattr(
        pipeline, "search_still_showing_form", lambda html: "captcha still" in html
    )
    monkeypatch.setattr(pipeline, "parse_results_table", lambda html: [object()])

    html = pipeline._search_with_captcha(
        client,
        store,
        None,
        retries=1,
        captcha_solver="openrouter",
        ask=lambda path: next(typed),
    )

    assert submitted == ["OCR123", "BAD123", "GOOD99"]
    assert html == "<html>results</html>"
    assert client.fetch_search_form.call_count == 3


def test_rejections_without_a_window_still_fail(tmp_path, monkeypatch):
    store = Store(tmp_path)
    client = MagicMock()
    client.fetch_search_form.return_value = "<form></form>"
    monkeypatch.setattr(
        pipeline,
        "resolve_captcha_code",
        lambda *args, **kwargs: "OCR123",
    )
    monkeypatch.setattr(pipeline, "save_captcha", _save)
    monkeypatch.setattr(
        pipeline,
        "submit_search",
        lambda *args, **kwargs: "<html>captcha still</html>",
    )
    monkeypatch.setattr(pipeline, "search_still_showing_form", lambda html: True)

    with pytest.raises(CaptchaError, match="rejected"):
        pipeline._search_with_captcha(
            client,
            store,
            None,
            retries=2,
            captcha_solver="openrouter",
        )

    assert client.fetch_search_form.call_count == 2
