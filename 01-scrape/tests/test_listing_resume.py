"""A dropped connection must not throw away tenders already saved."""

from pathlib import Path
from unittest.mock import patch

import requests

from stage1_scrape.app.pipeline import scrape_aoc
from stage1_scrape.domain.errors import ScraperError
from stage1_scrape.domain.models import TenderRecord
from stage1_scrape.scraping.client import GePNICClient

_PAGE = """
<table>
<tr>
  <td>1</td><td>T-ONE</td><td>Cable</td><td>Org</td><td>AOC</td><td>AOC</td>
  <td><a title="View Tender Status" href="/eprocure/app?sp=one">view</a></td>
</tr>
</table>
<a id="loadNext" title="Load Next" href="/eprocure/app?component=loadNext">Next &gt;</a>
"""


class _Store:
    def __init__(self, root, db_path=None) -> None:
        self.root = Path(root)

    def write_debug(self, name, html):
        return self.root / name

    def known_ids(self):
        return set()


def test_saved_tender_survives_a_failed_next_page(tmp_path, monkeypatch):
    order = []

    class Client:
        def __init__(self, delay=0) -> None:
            pass

        def get(self, url):
            order.append("next")
            raise ScraperError("dns down")

    def save_one(client, listing, store, download_pdfs):
        order.append(listing.tender_id)
        return TenderRecord(listing=listing)

    monkeypatch.setattr("stage1_scrape.app.pipeline.GePNICClient", Client)
    monkeypatch.setattr("stage1_scrape.app.pipeline.Store", _Store)
    monkeypatch.setattr("stage1_scrape.app.pipeline.scrape_one_tender", save_one)
    with patch("stage1_scrape.app.pipeline._search_with_captcha", return_value=_PAGE):
        records = scrape_aoc(tmp_path, max_pages=3, max_tenders=5)

    assert order == ["T-ONE", "next"]
    assert [record.listing.tender_id for record in records] == ["T-ONE"]


def test_no_page_cap_walks_until_the_portal_stops(tmp_path, monkeypatch):
    second = """
    <table>
    <tr>
      <td>2</td><td>T-TWO</td><td>Works</td><td>Org</td><td>AOC</td><td></td>
      <td><a title="View Tender Status" href="/eprocure/app?sp=two">view</a></td>
    </tr>
    </table>
    """

    class Client:
        def __init__(self, delay=0) -> None:
            pass

        def get(self, url):
            class Response:
                text = second

            return Response()

    saved: list[str] = []

    def save_one(client, listing, store, download_pdfs):
        saved.append(listing.tender_id)
        return TenderRecord(listing=listing)

    monkeypatch.setattr("stage1_scrape.app.pipeline.GePNICClient", Client)
    monkeypatch.setattr("stage1_scrape.app.pipeline.Store", _Store)
    monkeypatch.setattr("stage1_scrape.app.pipeline.scrape_one_tender", save_one)
    with patch("stage1_scrape.app.pipeline._search_with_captcha", return_value=_PAGE):
        records = scrape_aoc(tmp_path, max_pages=None, max_tenders=None)

    assert saved == ["T-ONE", "T-TWO"]
    assert [record.listing.tender_id for record in records] == ["T-ONE", "T-TWO"]


def test_get_retries_a_dropped_connection(monkeypatch):
    client = GePNICClient(delay=0)
    calls = {"n": 0}

    class Response:
        url = "https://eprocure.gov.in/ok"
        text = "ok"

        def raise_for_status(self) -> None:
            return None

    def get(url, headers, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("getaddrinfo failed")
        return Response()

    client.session.get = get
    monkeypatch.setattr("stage1_scrape.scraping.client.time.sleep", lambda _seconds: None)

    response = client.get("https://eprocure.gov.in/eprocure/app")

    assert calls["n"] == 3
    assert response.text == "ok"


def test_get_retries_a_stalled_read_on_a_fresh_connection(monkeypatch):
    client = GePNICClient(delay=0)
    calls = {"n": 0, "closed": 0}
    seen_timeouts: list[object] = []

    class Response:
        url = "https://eprocure.gov.in/ok"
        text = "ok"

        def raise_for_status(self) -> None:
            return None

    def get(url, headers, timeout):
        calls["n"] += 1
        seen_timeouts.append(timeout)
        if calls["n"] < 3:
            raise requests.ReadTimeout("Read timed out.")
        return Response()

    def close() -> None:
        calls["closed"] += 1

    client.session.get = get
    client.session.close = close
    monkeypatch.setattr("stage1_scrape.scraping.client.time.sleep", lambda _seconds: None)

    response = client.get("https://eprocure.gov.in/eprocure/app")

    assert calls["n"] == 3
    assert calls["closed"] == 2
    assert seen_timeouts == [(10.0, 90.0), (10.0, 90.0), (10.0, 90.0)]
    assert response.text == "ok"


def test_get_does_not_retry_a_non_connection_error(monkeypatch):
    client = GePNICClient(delay=0)
    calls = {"n": 0}

    def get(url, headers, timeout):
        calls["n"] += 1
        raise requests.RequestException("refused by us")

    client.session.get = get
    monkeypatch.setattr("stage1_scrape.scraping.client.time.sleep", lambda _seconds: None)

    try:
        client.get("https://eprocure.gov.in/eprocure/app")
    except ScraperError:
        pass
    else:
        raise AssertionError("expected ScraperError")

    assert calls["n"] == 1
