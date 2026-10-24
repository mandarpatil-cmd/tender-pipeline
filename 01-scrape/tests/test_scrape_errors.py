"""A bad tender is skipped, a portal failure keeps what was saved, and callbacks reach the search."""

from pathlib import Path
from unittest.mock import patch

import pytest

from stage1_scrape.app import pipeline, run
from stage1_scrape.app.pipeline import scrape_aoc
from stage1_scrape.domain.errors import ParseError, ScraperError, StopRequested
from stage1_scrape.domain.models import TenderRecord

_PAGE = "<table>" + "".join(
    f"<tr><td>{n}.</td><td>T-{n}</td><td>Title</td><td>Org</td><td>AOC</td>"
    f'<td><a title="View Tender Status" href="/eprocure/app?sp={n}"></a></td></tr>'
    for n in (1, 2, 3)
) + "</table>"


class _Store:
    def __init__(self, root, db_path=None) -> None:
        self.root = Path(root)

    def write_debug(self, name, html):
        return self.root / name

    def known_ids(self):
        return set()


class _Client:
    def __init__(self, delay=0) -> None:
        pass


@pytest.fixture
def portal(monkeypatch):
    monkeypatch.setattr(pipeline, "GePNICClient", _Client)
    monkeypatch.setattr(pipeline, "Store", _Store)


def _opening(failures: dict[str, Exception], opened: list[str]):
    def open_one(client, listing, store, download_pdfs, **_kwargs):
        opened.append(listing.tender_id)
        if listing.tender_id in failures:
            raise failures[listing.tender_id]
        return TenderRecord(listing=listing)

    return open_one


def test_a_tender_that_cannot_be_read_is_skipped_and_the_next_is_saved(portal, monkeypatch, tmp_path):
    opened: list[str] = []
    monkeypatch.setattr(pipeline, "scrape_one_tender", _opening({"T-2": ParseError("no summary link")}, opened))
    with patch.object(pipeline, "_search_with_captcha", return_value=_PAGE):
        records = scrape_aoc(tmp_path, max_tenders=5)

    assert opened == ["T-1", "T-2", "T-3"]
    assert [r.listing.tender_id for r in records] == ["T-1", "T-3"]


def test_a_portal_failure_mid_run_keeps_the_tenders_already_saved(portal, monkeypatch, tmp_path):
    opened: list[str] = []
    monkeypatch.setattr(pipeline, "scrape_one_tender", _opening({"T-2": ScraperError("503")}, opened))
    with patch.object(pipeline, "_search_with_captcha", return_value=_PAGE):
        records = scrape_aoc(tmp_path, max_tenders=5)

    assert opened == ["T-1", "T-2"]
    assert [r.listing.tender_id for r in records] == ["T-1"]


def test_stop_during_the_search_saves_nothing_and_is_not_an_error(portal, monkeypatch, tmp_path):
    opened: list[str] = []
    monkeypatch.setattr(pipeline, "scrape_one_tender", _opening({}, opened))
    with patch.object(pipeline, "_search_with_captcha", side_effect=StopRequested()):
        assert scrape_aoc(tmp_path, max_tenders=5) == []
    assert opened == []


def test_a_probe_opens_no_tender(portal, monkeypatch, tmp_path):
    opened: list[str] = []
    monkeypatch.setattr(pipeline, "scrape_one_tender", _opening({}, opened))
    with patch.object(pipeline, "_search_with_captcha", return_value=_PAGE):
        assert scrape_aoc(tmp_path, max_tenders=0) == []
    assert opened == []


def test_the_window_callbacks_reach_the_captcha_step(portal, monkeypatch, tmp_path):
    prefer, searched = (lambda: False), (lambda: None)
    with patch.object(pipeline, "_search_with_captcha", return_value="<table></table>") as search:
        scrape_aoc(tmp_path, prefer_typed=prefer, searched=searched)

    assert search.call_args.kwargs["prefer_typed"] is prefer
    assert search.call_args.kwargs["searched"] is searched


def test_run_scrape_hands_the_callbacks_on(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(run, "scrape_aoc", lambda out, **kwargs: seen.update(kwargs) or [])
    monkeypatch.setattr(run, "write_vendor_sheet", lambda *a, **k: (tmp_path / "v.csv", tmp_path / "v.xlsx"))
    prefer, searched = (lambda: True), (lambda: None)

    run.run_scrape(tmp_path, prefer_typed=prefer, searched=searched)

    assert seen["prefer_typed"] is prefer and seen["searched"] is searched


def test_a_pdf_fetch_hands_the_callbacks_on(portal, monkeypatch, tmp_path):
    prefer, searched = (lambda: True), (lambda: None)
    with patch.object(pipeline, "_search_with_captcha", return_value="<table></table>") as search:
        with pytest.raises(ParseError):
            pipeline.fetch_tender_pdfs(
                tmp_path, "T-9", zip_path=tmp_path / "t.zip", prefer_typed=prefer, searched=searched
            )

    assert search.call_args.kwargs["prefer_typed"] is prefer
    assert search.call_args.kwargs["searched"] is searched
