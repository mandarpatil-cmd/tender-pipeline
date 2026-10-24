"""After automatic captcha tries are rejected, the window can type the next one."""

from unittest.mock import MagicMock

import pytest

from stage1_scrape.app import pipeline
from stage1_scrape.domain.errors import CaptchaError, StopRequested
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


def test_stop_before_the_second_attempt_does_not_read_again(tmp_path, monkeypatch):
    store = Store(tmp_path)
    client = MagicMock()
    client.fetch_search_form.return_value = "<form></form>"
    calls: list[str] = []

    def resolve(image, text, *, solver, ask=None, should_stop=None):
        calls.append(solver)
        return "OCR123"

    def stop() -> bool:
        return len(calls) >= 1

    monkeypatch.setattr(pipeline, "resolve_captcha_code", resolve)
    monkeypatch.setattr(pipeline, "save_captcha", _save)
    monkeypatch.setattr(
        pipeline,
        "submit_search",
        lambda *args, **kwargs: "<html>captcha still</html>",
    )
    monkeypatch.setattr(pipeline, "search_still_showing_form", lambda html: True)

    with pytest.raises(StopRequested):
        pipeline._search_with_captcha(
            client,
            store,
            None,
            retries=3,
            captcha_solver="openrouter",
            should_stop=stop,
        )

    assert calls == ["openrouter"]
    assert client.fetch_search_form.call_count == 1


def _rejecting_reads(monkeypatch, submitted: list[str], accepted: str) -> None:
    def resolve(image, text, *, solver, ask=None):
        if solver == "manual":
            return ask(image)
        return "OCR123"

    def submit(client, html, code, **kwargs):
        submitted.append(code)
        if code == accepted:
            return "<html>results</html>"
        return "<html>captcha still</html>"

    monkeypatch.setattr(pipeline, "resolve_captcha_code", resolve)
    monkeypatch.setattr(pipeline, "save_captcha", _save)
    monkeypatch.setattr(pipeline, "submit_search", submit)
    monkeypatch.setattr(
        pipeline, "search_still_showing_form", lambda html: "captcha still" in html
    )
    monkeypatch.setattr(pipeline, "parse_results_table", lambda html: [object()])


def test_asking_to_type_skips_the_remaining_reads(tmp_path, monkeypatch):
    store = Store(tmp_path)
    client = MagicMock()
    client.fetch_search_form.return_value = "<form></form>"
    submitted: list[str] = []
    done: list[bool] = []
    _rejecting_reads(monkeypatch, submitted, accepted="TYPED1")

    html = pipeline._search_with_captcha(
        client,
        store,
        None,
        retries=5,
        captcha_solver="openrouter",
        ask=lambda path: "TYPED1",
        prefer_typed=lambda: len(submitted) >= 1,
        searched=lambda: done.append(True),
    )

    assert submitted == ["OCR123", "TYPED1"]
    assert html == "<html>results</html>"
    assert client.fetch_search_form.call_count == 2
    assert done == [True]


def test_asking_to_type_without_a_window_keeps_reading(tmp_path, monkeypatch):
    store = Store(tmp_path)
    client = MagicMock()
    client.fetch_search_form.return_value = "<form></form>"
    submitted: list[str] = []
    _rejecting_reads(monkeypatch, submitted, accepted="never")

    with pytest.raises(CaptchaError, match="rejected"):
        pipeline._search_with_captcha(
            client,
            store,
            None,
            retries=2,
            captcha_solver="openrouter",
            prefer_typed=lambda: True,
        )

    assert submitted == ["OCR123", "OCR123"]


def test_an_accepted_read_reports_the_search(tmp_path, monkeypatch):
    store = Store(tmp_path)
    client = MagicMock()
    client.fetch_search_form.return_value = "<form></form>"
    submitted: list[str] = []
    done: list[bool] = []
    _rejecting_reads(monkeypatch, submitted, accepted="OCR123")

    pipeline._search_with_captcha(
        client,
        store,
        None,
        retries=3,
        captcha_solver="openrouter",
        ask=lambda path: "unused",
        prefer_typed=lambda: False,
        searched=lambda: done.append(True),
    )

    assert submitted == ["OCR123"]
    assert done == [True]
